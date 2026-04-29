"""Qzone 说说 CRUD 操作模块。

封装说说发布、删除、列表获取、详情获取、图片上传等核心操作，
包含 Cookie 失效自动刷新重试逻辑。
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any, TYPE_CHECKING

import httpx
import orjson

from src.app.plugin_system.api.log_api import get_logger

from .types import Result
from .http_client import (
    EMOTION_PUBLISH_URL,
    UPLOAD_URL,
    LIST_URL,
    ZONE_LIST_URL,
    normalize_callback_payload,
)
from .feed_parser import (
    extract_text_from_feed_html,
    extract_image_urls_from_feed_html,
    parse_feed_html_item,
)

if TYPE_CHECKING:
    from .http_client import QzoneHttpClient
    from .state_manager import StateManager
    from .ai_prompts import AIPromptBuilder

logger = get_logger("qzone_shuoshuo")


class FeedOperations:
    """说说 CRUD 操作。

    负责：
    - publish_shuoshuo: 发布说说（含图片上传、内容改写、去重）
    - get_shuoshuo_list: 获取说说列表
    - delete_shuoshuo: 删除说说
    - get_shuoshuo_detail: 获取说说详情
    - get_friend_feed_list: 获取好友动态流
    """

    def __init__(
        self,
        http: "QzoneHttpClient",
        state: "StateManager",
        prompts: "AIPromptBuilder",
        get_qq_from_napcat,  # callback to avoid circular import
    ) -> None:
        self._http = http
        self._state = state
        self._prompts = prompts
        self._get_qq_from_napcat = get_qq_from_napcat

    # ---- 发布说说 ----

    async def publish(
        self,
        qq_number: str = "",
        content: str = "",
        images: list[bytes] | None = None,
        visible: str = "all",
    ) -> Result[dict]:
        """发布说说。"""
        logger.info(f"[发布说说] 开始执行, qq={qq_number or '自动获取'}, 内容长度={len(content)}")

        if not content:
            logger.warning("[发布说说] 内容为空")
            return Result.fail("说说内容不能为空")

        # 近时窗去重
        raw_text = str(content).strip()
        if raw_text:
            current_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            if self._state.check_publish_duplicate(current_hash):
                logger.info("[发布说说] 命中近5分钟重复内容去重，跳过重复发布")
                return Result.ok({"message": "近期已发布同内容，已跳过重复发送"})

        content = await self._prompts.rewrite_publish_content(content)
        if not content:
            return Result.fail("说说内容改写后为空，已取消发布")

        if not qq_number:
            logger.debug("[发布说说] 未指定 QQ 号，尝试从 NapCat 自动获取")
            qq_number = await self._get_qq_from_napcat() or ""

        if not qq_number:
            logger.error("[发布说说] 无法获取 QQ 号")
            return Result.fail("无法获取 QQ 号，请确保 NapCat 适配器已正确配置")

        real_images = self._resolve_images(images)

        logger.debug(f"[发布说说] 准备获取客户端, QQ={qq_number}")
        client_info = await self._http.get_client(qq_number)
        if not client_info:
            return Result.fail("获取客户端失败(Cookie不存在或配置错误)")

        client, uin, gtk = client_info
        logger.info(f"[发布说说] 客户端获取成功, uin={uin}")

        try:
            pic_bos, richvals = await self._upload_images(client, real_images, gtk)

            post_data = self._build_publish_data(uin, content, pic_bos, richvals, visible)

            await self._http.random_human_delay(0.8, 2.0, "[发布说说]")
            resp = await self._http.post_with_backoff(
                client=client,
                url=EMOTION_PUBLISH_URL,
                data=post_data,
                params={"g_tk": gtk},
                tag="[发布说说]",
                max_retries=2,
            )

            try:
                result = orjson.loads(resp.text)
            except Exception:
                logger.error("[发布说说] 服务端响应格式错误")
                return Result.fail("服务端响应格式错误")

            if result.get("code") == -3000:
                logger.warning("[发布说说] Cookie 已失效，尝试刷新...")
                retry_result = await self._retry_publish(qq_number, content, real_images, visible)
                if retry_result:
                    return retry_result
                return Result.fail("Cookie 已失效，刷新失败")

            if result.get("tid"):
                tid = result["tid"]
                if raw_text:
                    current_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
                    self._state.record_publish(current_hash)
                    self._state.remember_published_text(raw_text)
                logger.info(f"[发布说说] 发布成功, tid={tid}")
                return Result.ok({"tid": tid, "message": "发布成功", "content": content})
            else:
                error_msg = result.get("message", "未知错误")
                logger.error(f"[发布说说] 发布失败: {error_msg}")
                return Result.fail(f"发布失败: {error_msg}")

        except Exception as e:
            logger.error(f"[发布说说] 内部异常: {e}")
            return Result.fail(f"内部异常: {e}")
        finally:
            await client.aclose()

    async def _retry_publish(
        self, qq_number: str, content: str, real_images: list[bytes], visible: str
    ) -> Result[dict] | None:
        """Cookie 刷新后重试发布。"""
        client_info = await self._http.get_client(qq_number)
        if not client_info:
            return None

        client, uin, gtk = client_info
        try:
            pic_bos, richvals = await self._upload_images(client, real_images, gtk)
            post_data = self._build_publish_data(uin, content, pic_bos, richvals, visible)

            await self._http.random_human_delay(0.8, 2.0, "[重试发布]")
            resp = await self._http.post_with_backoff(
                client=client,
                url=EMOTION_PUBLISH_URL,
                data=post_data,
                params={"g_tk": gtk},
                tag="[重试发布]",
                max_retries=1,
            )
            result = orjson.loads(resp.text)

            if result.get("code") == -3000:
                logger.error("[重试发布] 刷新后的 Cookie 仍然失效")
                return None

            if result.get("tid"):
                logger.info(f"[重试发布] 成功, tid={result['tid']}")
                return Result.ok({
                    "tid": result["tid"],
                    "message": "发布成功（Cookie已刷新）",
                    "content": content,
                })

            logger.error(f"[重试发布] 失败: {result.get('message', '未知错误')}")
            return Result.fail(f"发布失败: {result.get('message', '未知错误')}")
        except Exception as e:
            logger.error(f"[重试发布] 异常: {e}")
            return None
        finally:
            await client.aclose()

    # ---- 获取列表 ----

    async def get_list(
        self, qq_number: str = "", count: int = 20
    ) -> Result[list[dict]]:
        """获取说说列表。"""
        target_qq = str(qq_number or "").strip()
        logger.info(f"[获取列表] 开始执行, target={target_qq or '自动获取'}, count={count}")

        login_qq = await self._get_qq_from_napcat()
        if not login_qq:
            return Result.fail("无法获取登录 QQ 号")

        if not target_qq:
            target_qq = login_qq

        client_info = await self._http.get_client(login_qq)
        if not client_info:
            return Result.fail("获取客户端失败")

        client, _uin, gtk = client_info
        params = {
            "uin": target_qq, "ftype": "0", "sort": "0", "pos": "0",
            "num": str(count), "replynum": "100", "g_tk": gtk,
            "callback": "_preloadCallback", "code_version": "1",
            "format": "json", "need_private_comment": "1",
        }

        try:
            resp = await client.get(LIST_URL, params=params)
            text = resp.text.strip()
            if text.startswith("_preloadCallback("):
                text = text[len("_preloadCallback("):]
            if text.endswith(")"):
                text = text[:-1]

            data = orjson.loads(text)
            if data.get("code") == 0:
                msglist = data.get("msglist") or []
                pending = self._state.count_pending_candidates(msglist)
                logger.info(f"[获取列表] 成功, 候选说说 {pending} 条")
                return Result.ok(msglist)
            elif data.get("code") == -3000:
                logger.warning("[获取列表] Cookie 已失效，尝试刷新...")
                new_info = await self._http.refresh_cookie_and_get_client(login_qq, "获取列表")
                if new_info:
                    return await self._retry_get_list(new_info[0], target_qq, new_info[2], count)
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                return Result.fail(f"获取列表失败: {data.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_get_list(
        self, client: httpx.AsyncClient, target_qq: str, gtk: str, count: int
    ) -> Result[list[dict]]:
        """重试获取说说列表。"""
        params = {
            "uin": target_qq, "ftype": "0", "sort": "0", "pos": "0",
            "num": str(count), "replynum": "100", "g_tk": gtk,
            "callback": "_preloadCallback", "code_version": "1",
            "format": "json", "need_private_comment": "1",
        }
        try:
            resp = await client.get(LIST_URL, params=params)
            text = resp.text.strip()
            if text.startswith("_preloadCallback("):
                text = text[len("_preloadCallback("):]
            if text.endswith(")"):
                text = text[:-1]
            data = orjson.loads(text)
            if data.get("code") == 0:
                msglist = data.get("msglist") or []
                pending = self._state.count_pending_candidates(msglist)
                logger.info(f"[重试获取列表] 成功, 候选说说 {pending} 条")
                return Result.ok(msglist)
            elif data.get("code") == -3000:
                return Result.fail("Cookie 失效")
            return Result.fail(f"获取列表失败: {data.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    # ---- 删除说说 ----

    async def delete(self, shuoshuo_id: str, qq_number: str = "") -> Result[str]:
        """删除说说。"""
        logger.info(f"[删除说说] 开始执行, tid={shuoshuo_id}")

        if not shuoshuo_id:
            return Result.fail("说说ID不能为空")

        if not qq_number:
            qq_number = await self._get_qq_from_napcat() or ""

        if not qq_number:
            return Result.fail("无法获取 QQ 号")

        client_info = await self._http.get_client(qq_number)
        if not client_info:
            return Result.fail("获取客户端失败")

        client, uin, gtk = client_info
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
        data = {
            "hostuin": uin, "tid": shuoshuo_id, "t1_source": "1",
            "code_version": "1", "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
        }

        try:
            resp = await client.post(url, data=data, params={"g_tk": gtk})
            result = orjson.loads(resp.text)

            if result.get("code") == 0:
                logger.info(f"[删除说说] 成功, tid={shuoshuo_id}")
                return Result.ok("删除成功")
            elif result.get("code") == -3000:
                logger.warning("[删除说说] Cookie 已失效，尝试刷新...")
                new_info = await self._http.refresh_cookie_and_get_client(qq_number, "删除说说")
                if new_info:
                    return await self._retry_delete(new_info[0], new_info[1], new_info[2], shuoshuo_id)
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                return Result.fail(f"删除失败: {result.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_delete(
        self, client: httpx.AsyncClient, uin: str, gtk: str, shuoshuo_id: str
    ) -> Result[str]:
        """重试删除说说。"""
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
        data = {
            "hostuin": uin, "tid": shuoshuo_id, "t1_source": "1",
            "code_version": "1", "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
        }
        try:
            resp = await client.post(url, data=data, params={"g_tk": gtk})
            result = orjson.loads(resp.text)
            if result.get("code") == 0:
                logger.info(f"[重试删除] 成功, tid={shuoshuo_id}")
                return Result.ok("删除成功（Cookie已刷新）")
            elif result.get("code") == -3000:
                return Result.fail("Cookie 失效")
            return Result.fail(f"删除失败: {result.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    # ---- 获取详情 ----

    async def get_detail(self, shuoshuo_id: str, qq_number: str = "") -> Result[dict]:
        """获取说说详情。"""
        logger.info(f"[获取详情] 开始执行, tid={shuoshuo_id}")

        if not shuoshuo_id:
            return Result.fail("说说ID不能为空")

        if not qq_number:
            qq_number = await self._get_qq_from_napcat() or ""

        if not qq_number:
            return Result.fail("无法获取 QQ 号")

        client_info = await self._http.get_client(qq_number)
        if not client_info:
            return Result.fail("获取客户端失败")

        client, uin, gtk = client_info
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
        params = {
            "uin": uin, "tid": shuoshuo_id, "num": "1", "g_tk": gtk,
            "format": "json", "inCharset": "utf-8", "outCharset": "utf-8",
        }

        try:
            resp = await client.get(url, params=params)
            text = normalize_callback_payload(resp.text)
            data = orjson.loads(text)

            if data.get("code") == 0:
                msg_list = data.get("msglist") or []
                if msg_list:
                    logger.info("[获取详情] 成功")
                    return Result.ok(msg_list[0])
                return Result.fail("未找到说说或无权查看")
            elif data.get("code") == -3000:
                logger.warning("[获取详情] Cookie 已失效，尝试刷新...")
                new_info = await self._http.refresh_cookie_and_get_client(qq_number, "获取详情")
                if new_info:
                    return await self._retry_get_detail(new_info[0], new_info[1], new_info[2], shuoshuo_id)
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                return Result.fail(f"获取详情失败: {data.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_get_detail(
        self, client: httpx.AsyncClient, uin: str, gtk: str, shuoshuo_id: str
    ) -> Result[dict]:
        """重试获取说说详情。"""
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
        params = {
            "uin": uin, "tid": shuoshuo_id, "num": "1", "g_tk": gtk,
            "format": "json", "inCharset": "utf-8", "outCharset": "utf-8",
        }
        try:
            resp = await client.get(url, params=params)
            text = normalize_callback_payload(resp.text)
            data = orjson.loads(text)
            if data.get("code") == 0:
                msg_list = data.get("msglist") or []
                if msg_list:
                    logger.info("[重试获取详情] 成功")
                    return Result.ok(msg_list[0])
                return Result.fail("未找到说说或无权查看")
            elif data.get("code") == -3000:
                return Result.fail("Cookie 失效")
            return Result.fail(f"获取详情失败: {data.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    # ---- 好友动态流 ----

    async def get_friend_feed_list(self, count: int = 20) -> Result[list[dict]]:
        """获取好友动态流（feeds3_html_more）。"""
        fetch_count = max(5, min(int(count or 20), 50))
        login_qq = await self._get_qq_from_napcat()
        if not login_qq:
            return Result.fail("无法获取登录 QQ 号")

        client_info = await self._http.get_client(login_qq)
        if not client_info:
            return Result.fail("获取客户端失败")

        client, uin, gtk = client_info
        params = {
            "uin": uin, "scope": "0", "view": "1", "filter": "all",
            "flag": "1", "applist": "all", "pagenum": "1",
            "count": str(fetch_count), "format": "json", "g_tk": gtk,
            "useutf8": "1", "outputhtmlfeed": "1",
        }

        try:
            resp = await client.get(ZONE_LIST_URL, params=params)
            raw_text = resp.text

            if raw_text.lower().startswith("<html") or "<title>" in raw_text.lower():
                logger.error(f"[好友动态] 返回 HTML，疑似接口异常，前200字：{raw_text[:200]}")
                return Result.fail("接口返回 HTML，疑似被风控/未登录")

            payload = normalize_callback_payload(raw_text)
            if not payload:
                logger.warning(f"[好友动态] 接口返回为空，原始响应前200字符: {raw_text[:200]}")
                return Result.fail("接口返回为空")

            try:
                data = orjson.loads(payload)
            except orjson.JSONDecodeError:
                try:
                    from json_repair import repair_json
                    repaired = repair_json(payload)
                    data = orjson.loads(repaired)
                    logger.debug("[好友动态] 成功通过 json_repair 修复解析数据")
                except Exception as fix_err:
                    logger.error(
                        f"[好友动态] JSON解析失败: {fix_err}\n"
                        f"原始响应前500字符: {raw_text[:500]}"
                    )
                    return Result.fail(f"好友动态接口返回数据格式异常: {fix_err}")

            code = int(data.get("code", 0) or 0)
            if code == -3000:
                return Result.fail("Cookie 失效")
            if code != 0:
                return Result.fail(
                    f"获取好友动态失败: {data.get('message') or data.get('msg') or code}"
                )

            feed_rows = ((data.get("data") or {}).get("data") or [])
            items: list[dict[str, Any]] = []

            for row in feed_rows:
                if not isinstance(row, dict):
                    continue

                appid = str(row.get("appid", "") or "").strip()
                if appid and appid != "311":
                    continue

                owner_qq = str(row.get("uin", "") or "").strip()
                if not owner_qq or owner_qq == str(uin):
                    continue

                tid = str(row.get("key") or row.get("tid") or "").strip()
                if not tid:
                    continue

                html_content = str(row.get("html", "") or "")

                parsed = parse_feed_html_item(html_content, owner_qq)

                content = extract_text_from_feed_html(html_content)
                if not content:
                    content = str(row.get("summary") or row.get("title") or "").strip()

                image_urls = extract_image_urls_from_feed_html(html_content)
                pic_list = [{"url": url} for url in image_urls]

                items.append({
                    "tid": tid,
                    "uin": owner_qq,
                    "content": content,
                    "pic": pic_list,
                    "createTime": row.get("abstime", ""),
                    "is_liked": parsed["is_liked"],
                    "comments": parsed["comments"],
                })

            pending = self._state.count_pending_candidates(items)
            logger.info(f"[好友动态] 获取成功, 原始{len(feed_rows)}条, 候选{pending}条")
            return Result.ok(items)
        except Exception as e:
            return Result.fail(f"获取好友动态异常: {e}")
        finally:
            await client.aclose()

    # ---- 辅助方法 ----

    @staticmethod
    def _resolve_images(images: list[bytes] | None) -> list[bytes]:
        """解析图片列表（支持 bytes 和文件路径）。"""
        real_images: list[bytes] = []
        if not images:
            return real_images
        for img in images:
            if isinstance(img, bytes):
                real_images.append(img)
            elif isinstance(img, str):
                path = Path(img)
                if path.exists():
                    try:
                        real_images.append(path.read_bytes())
                    except Exception as e:
                        logger.error(f"[发布说说] 读取图片失败 {path}: {e}")
        return real_images

    async def _upload_images(
        self, client: httpx.AsyncClient, images: list[bytes], gtk: str
    ) -> tuple[list[str], list[str]]:
        """批量上传图片，返回 (pic_bos, richvals)。"""
        pic_bos: list[str] = []
        richvals: list[str] = []
        for img_bytes in images:
            res = await self._upload_single_image(client, img_bytes, gtk)
            if res:
                try:
                    bo, rv = self._get_picbo_and_richval(res)
                    pic_bos.append(bo)
                    richvals.append(rv)
                except Exception as e:
                    logger.error(f"[发布说说] 解析上传参数失败: {e}")
        return pic_bos, richvals

    async def _upload_single_image(
        self, client: httpx.AsyncClient, image_bytes: bytes, gtk: str
    ) -> dict[str, Any] | None:
        """上传单张图片。"""
        try:
            pic_base64 = base64.b64encode(image_bytes).decode("utf-8")
            post_data = {
                "filename": "filename.jpg", "filetype": "1", "uploadtype": "1",
                "albumtype": "7", "exttype": "0", "refer": "shuoshuo",
                "output_type": "json", "charset": "utf-8", "output_charset": "utf-8",
                "upload_hd": "1", "hd_width": "2048", "hd_height": "10000",
                "hd_quality": "96",
                "backUrls": (
                    "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                    "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
                ),
                "url": f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={gtk}",
                "base64": "1", "picfile": pic_base64,
            }
            resp = await client.post(UPLOAD_URL, data=post_data, params={"g_tk": gtk})
            resp.raise_for_status()
            text = normalize_callback_payload(resp.text)
            return orjson.loads(text)
        except Exception as e:
            logger.error(f"上传图片异常: {e}")
            return None

    @staticmethod
    def _get_picbo_and_richval(upload_result: dict) -> tuple[str, str]:
        """从上传结果中提取 picbo 和 richval。"""
        if "ret" not in upload_result or upload_result["ret"] != 0:
            raise ValueError(f"上传失败: {upload_result.get('msg')}")
        data = upload_result.get("data", {})
        url = data.get("url", "")
        parts = url.split("&bo=")
        if len(parts) < 2:
            raise ValueError("上传结果 URL 中缺少 bo 参数")
        picbo = parts[1].split("&")[0]
        richval = ",{},{},{},{},{},{},,{},{}".format(
            data.get("albumid"), data.get("lloc"), data.get("sloc"),
            data.get("type"), data.get("height"), data.get("width"),
            data.get("height"), data.get("width"),
        )
        return picbo, richval

    @staticmethod
    def _build_publish_data(
        uin: str, content: str, pic_bos: list[str], richvals: list[str], visible: str
    ) -> dict[str, Any]:
        """构建发布请求数据。"""
        post_data: dict[str, Any] = {
            "syn_tweet_verson": "1", "paramstr": "1", "who": "1", "con": content,
            "feedversion": "1", "ver": "1", "ugc_right": "1", "to_sign": "0",
            "hostuin": uin, "code_version": "1", "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
        }
        visible_map = {"all": "1", "friends": "2", "self": "4"}
        post_data["ugc_right"] = visible_map.get(visible, "1")

        if pic_bos:
            post_data["pic_bo"] = ",".join(pic_bos)
            post_data["richtype"] = "1"
            post_data["richval"] = "\t".join(richvals)

        return post_data