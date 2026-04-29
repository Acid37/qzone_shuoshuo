"""Qzone 互动操作模块。

封装点赞、评论、回复等社交互动操作，
包含自动互动（概率控制、并发防重）和盖楼式多级回复。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, TYPE_CHECKING

import httpx
import orjson

from src.app.plugin_system.api.log_api import get_logger

from .types import Result
from .http_client import (
    COMMENT_URL,
    normalize_callback_payload,
    classify_failure_reason,
)

if TYPE_CHECKING:
    from .http_client import QzoneHttpClient
    from .state_manager import StateManager
    from .ai_prompts import AIPromptBuilder
    from .feed_ops import FeedOperations

logger = get_logger("qzone_shuoshuo")


class InteractionOps:
    """社交互动操作。

    负责：
    - like_shuoshuo: 点赞说说
    - comment_shuoshuo: 评论说说（支持盖楼式多级回复）
    - auto_like_if_enabled: 自动点赞（概率控制）
    - auto_comment_if_enabled: 自动评论（概率控制）
    - reply_to_comments: 回复自己说说的评论
    - process_feed_comments: 处理好友动态下的评论
    """

    def __init__(
        self,
        http: "QzoneHttpClient",
        state: "StateManager",
        prompts: "AIPromptBuilder",
        feeds: "FeedOperations",
        get_qq_from_napcat,
        get_monitor_config,
    ) -> None:
        self._http = http
        self._state = state
        self._prompts = prompts
        self._feeds = feeds
        self._get_qq_from_napcat = get_qq_from_napcat
        self._get_monitor_config = get_monitor_config

    # ---- 点赞 ----

    async def like(
        self, shuoshuo_id: str, qq_number: str = "", owner_qq: str | None = None
    ) -> Result[str]:
        """点赞说说。"""
        logger.info(f"[点赞说说] 开始执行, tid={shuoshuo_id}")

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
        target_owner = owner_qq if owner_qq else uin

        url = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
        form_data = {
            "qzreferrer": f"https://user.qzone.qq.com/{target_owner}/infocenter",
            "opuin": uin,
            "unikey": f"http://user.qzone.qq.com/{target_owner}/mood/{shuoshuo_id}",
            "curkey": f"http://user.qzone.qq.com/{target_owner}/mood/{shuoshuo_id}",
            "from": "1", "appid": "311", "typeid": "0", "abstime": "",
            "fid": shuoshuo_id, "active": "0", "fupdate": "1", "g_tk": gtk,
        }

        try:
            await self._http.random_human_delay(0.5, 1.8, "[点赞说说]")
            resp = await self._http.post_with_backoff(
                client=client, url=url, data=form_data,
                params={"g_tk": gtk}, tag="[点赞说说]", max_retries=2,
            )
            text = normalize_callback_payload(resp.text)

            try:
                data = orjson.loads(text)
            except Exception:
                if any(kw in text.lower() for kw in ("succ", "成功", '"ret":0', '"code":0')):
                    logger.info("[点赞说说] 成功 (无法解析JSON)")
                    return Result.ok("点赞成功 (无法解析JSON)")
                return Result.fail("点赞响应解析失败")

            code = data.get("ret", data.get("code", -1))
            if code == 0:
                logger.info(f"[点赞说说] 成功, tid={shuoshuo_id}")
                return Result.ok("点赞成功")
            elif code == -3000:
                logger.warning("[点赞说说] Cookie 已失效，尝试刷新...")
                new_info = await self._http.refresh_cookie_and_get_client(qq_number, "点赞说说")
                if new_info:
                    return await self._retry_like(
                        new_info[0], new_info[1], new_info[2], shuoshuo_id, owner_qq
                    )
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                return Result.fail(f"点赞失败: {data.get('msg') or data.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_like(
        self, client: httpx.AsyncClient, uin: str, gtk: str,
        shuoshuo_id: str, owner_qq: str | None,
    ) -> Result[str]:
        """重试点赞。"""
        target_owner = owner_qq if owner_qq else uin
        url = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
        form_data = {
            "qzreferrer": f"https://user.qzone.qq.com/{target_owner}/infocenter",
            "opuin": uin,
            "unikey": f"http://user.qzone.qq.com/{target_owner}/mood/{shuoshuo_id}",
            "curkey": f"http://user.qzone.qq.com/{target_owner}/mood/{shuoshuo_id}",
            "from": "1", "appid": "311", "typeid": "0", "abstime": "",
            "fid": shuoshuo_id, "active": "0", "fupdate": "1", "g_tk": gtk,
        }
        try:
            resp = await self._http.post_with_backoff(
                client=client, url=url, data=form_data,
                params={"g_tk": gtk}, tag="[重试点赞]", max_retries=1,
            )
            text = normalize_callback_payload(resp.text)
            data = orjson.loads(text)
            code = data.get("ret", data.get("code", -1))
            if code == 0:
                logger.info(f"[重试点赞] 成功, tid={shuoshuo_id}")
                return Result.ok("点赞成功（Cookie已刷新）")
            elif code == -3000:
                return Result.fail("Cookie 失效")
            return Result.fail(f"点赞失败: {data.get('msg') or data.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    # ---- 评论 ----

    async def comment(
        self,
        shuoshuo_id: str,
        content: str,
        qq_number: str = "",
        owner_qq: str | None = None,
        comment_id: str | None = None,
        parent_tid: str | None = None,
    ) -> Result[dict]:
        """评论说说（支持盖楼式多级回复）。"""
        logger.info(f"[评论说说] 开始执行, tid={shuoshuo_id}")

        if not shuoshuo_id:
            return Result.fail("说说ID不能为空")
        if not content:
            return Result.fail("评论内容不能为空")

        if not qq_number:
            qq_number = await self._get_qq_from_napcat() or ""

        if not qq_number:
            return Result.fail("无法获取 QQ 号")

        client_info = await self._http.get_client(qq_number)
        if not client_info:
            return Result.fail("获取客户端失败")

        client, uin, gtk = client_info
        target_owner = owner_qq if owner_qq else qq_number

        topic_id = f"{target_owner}_{shuoshuo_id}__1"
        post_data: dict[str, Any] = {
            "topicId": topic_id, "uin": uin, "hostUin": target_owner,
            "content": content, "format": "fs", "plat": "qzone",
            "source": "ic", "platformid": 52, "ref": "feeds",
        }

        if comment_id:
            post_data["commentid"] = comment_id
            post_data["parent_tid"] = parent_tid if parent_tid else comment_id

        try:
            await self._http.random_human_delay(0.8, 2.2, "[评论说说]")
            resp = await self._http.post_with_backoff(
                client=client, url=COMMENT_URL, data=post_data,
                params={"g_tk": gtk}, tag="[评论说说]", max_retries=2,
            )

            text = resp.text.strip()

            if not text:
                if resp.status_code in (302, 401, 403):
                    logger.warning("[评论说说] 收到空响应+认证状态，先二次确认...")
                    await asyncio.sleep(random.uniform(0.4, 1.0))

                    confirm_resp = await client.post(
                        COMMENT_URL, data=post_data, params={"g_tk": gtk},
                    )
                    confirm_text = confirm_resp.text.strip()

                    if confirm_text:
                        self._http.bump_cookie_confirm_stats("recovered")
                        logger.info("[评论说说] 二次确认拿到有效响应")
                        resp = confirm_resp
                        text = confirm_text
                    elif confirm_resp.status_code in (302, 401, 403):
                        self._http.bump_cookie_confirm_stats("refresh")
                        logger.warning("[评论说说] 二次确认仍是认证状态，按 Cookie 失效处理")
                        new_info = await self._http.refresh_cookie_and_get_client(qq_number, "评论说说")
                        if new_info:
                            return await self._retry_comment(
                                new_info[0], new_info[1], new_info[2],
                                shuoshuo_id, content, owner_qq, comment_id, parent_tid,
                            )
                        return Result.fail("Cookie 失效，刷新失败")
                    else:
                        return Result.fail(f"评论响应为空 ({confirm_resp.status_code})")
                elif resp.status_code >= 500:
                    return Result.fail(f"QQ空间服务器错误 ({resp.status_code})，请稍后重试")
                else:
                    return Result.fail(f"评论响应为空 ({resp.status_code})")

            text = normalize_callback_payload(text)

            try:
                data = orjson.loads(text)
            except Exception:
                if "succ" in text or "成功" in text:
                    logger.info("[评论说说] 成功 (无法解析JSON)")
                    return Result.ok({"message": "评论成功"})
                return Result.fail("评论响应解析失败")

            code = data.get("ret", data.get("code", -1))
            if code == 0:
                cid = data.get("id", data.get("cid", ""))
                self._state.mark_commented(shuoshuo_id)
                self._state.save_state()
                logger.info(f"[评论说说] 成功, tid={shuoshuo_id}")
                return Result.ok({"tid": shuoshuo_id, "cid": cid, "message": "评论成功"})
            elif code == -3000:
                logger.warning("[评论说说] Cookie 已失效，尝试刷新...")
                new_info = await self._http.refresh_cookie_and_get_client(qq_number, "评论说说")
                if new_info:
                    return await self._retry_comment(
                        new_info[0], new_info[1], new_info[2],
                        shuoshuo_id, content, owner_qq, comment_id, parent_tid,
                    )
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                error_msg = data.get("msg", data.get("message", "未知错误"))
                return Result.fail(f"评论失败: {error_msg}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_comment(
        self, client: httpx.AsyncClient, uin: str, gtk: str,
        shuoshuo_id: str, content: str, owner_qq: str | None,
        comment_id: str | None, parent_tid: str | None = None,
    ) -> Result[dict]:
        """重试评论。"""
        target_owner = owner_qq if owner_qq else uin
        post_data: dict[str, Any] = {
            "topicId": f"{target_owner}_{shuoshuo_id}__1",
            "uin": uin, "hostUin": target_owner, "content": content,
            "format": "fs", "plat": "qzone", "source": "ic",
            "platformid": 52, "ref": "feeds",
        }
        if comment_id:
            post_data["commentid"] = comment_id
            post_data["parent_tid"] = parent_tid if parent_tid else comment_id

        try:
            resp = await self._http.post_with_backoff(
                client=client, url=COMMENT_URL, data=post_data,
                params={"g_tk": gtk}, tag="[重试评论]", max_retries=1,
            )
            text = normalize_callback_payload(resp.text)
            data = orjson.loads(text)
            code = data.get("ret", data.get("code", -1))
            if code == 0:
                cid = data.get("id", data.get("cid", ""))
                self._state.mark_commented(shuoshuo_id)
                self._state.save_state()
                logger.info(f"[重试评论] 成功, tid={shuoshuo_id}, cid={cid}")
                return Result.ok({
                    "tid": shuoshuo_id, "cid": cid,
                    "message": "评论成功（Cookie已刷新）",
                })
            elif code == -3000:
                return Result.fail("Cookie 失效")
            return Result.fail(f"评论失败: {data.get('msg') or data.get('message')}")
        except Exception as e:
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    # ---- 自动点赞 ----

    async def auto_like_if_enabled(
        self, item: dict, *, current_qq: str | None = None
    ) -> bool | None:
        """如果启用自动点赞，则按概率点赞说说。"""
        tid = item.get("tid")
        if not tid:
            return None

        owner_qq = str(item.get("uin", "") or "").strip() or None
        normalized_current_qq = str(current_qq or "").strip() or None
        if normalized_current_qq and owner_qq and owner_qq == normalized_current_qq:
            logger.debug(f"[自动点赞] 检测到本人动态，跳过点赞 tid={tid}")
            return None

        monitor_config = self._get_monitor_config()
        like_probability = float(monitor_config.get("like_probability", 1.0))
        like_probability = max(0.0, min(1.0, like_probability))

        if random.random() > like_probability:
            logger.debug(f"[自动点赞] 概率未命中，跳过点赞 tid={tid}")
            return None

        logger.info(f"[自动点赞] 开始点赞说说 {tid}")
        try:
            await self._http.random_human_delay(1.0, 4.0, "[自动点赞]")
            result = await self.like(shuoshuo_id=tid, qq_number="", owner_qq=owner_qq)
            if result.is_success:
                logger.info(f"[自动点赞] 说说 {tid} 点赞成功")
                return True
            else:
                logger.warning(f"[自动点赞] 说说 {tid} 点赞失败: {result.error_message}")
                return False
        except Exception as e:
            logger.error(f"[自动点赞] 说说 {tid} 点赞异常: {e}")
            return False

    # ---- 自动评论 ----

    async def auto_comment_if_enabled(
        self, item: dict, *, current_qq: str | None = None
    ) -> bool | None:
        """如果启用自动评论，则按概率评论说说。"""
        monitor_config = self._get_monitor_config()
        if not monitor_config.get("auto_comment"):
            return None

        tid = item.get("tid")
        if not tid:
            return None

        owner_qq = str(item.get("uin", "") or "").strip() or None
        normalized_current_qq = str(current_qq or "").strip() or None
        if normalized_current_qq and owner_qq and owner_qq == normalized_current_qq:
            logger.debug(f"[自动评论] 检测到本人动态，跳过评论 tid={tid}")
            return None

        if self._state.is_commented(tid):
            logger.debug(f"[自动评论] 说说 {tid} 已评论过，跳过")
            return None

        comment_key = f"{tid}_auto_comment"
        if self._state.is_comment_processing(comment_key):
            logger.debug(f"[自动评论] 说说 {tid} 正在评论中，跳过")
            return None

        self._state.lock_comment(comment_key)
        try:
            comment_probability = float(monitor_config.get("comment_probability", 0.3))
            comment_probability = max(0.0, min(1.0, comment_probability))

            if random.random() > comment_probability:
                logger.debug(f"[自动评论] 概率未命中，跳过评论 tid={tid}")
                return None

            content = item.get("content", "")
            nickname = item.get("nickname", "") or item.get("uin", "")
            pics = item.get("pic", [])
            images = [p.get("url", p.get("big_url", "")) for p in pics if isinstance(p, dict)]

            comment_text = await self._prompts.generate_comment_text(content, nickname, images)
            if not comment_text:
                logger.warning(f"[自动评论] 说说 {tid} 评论文本生成失败，按策略跳过")
                return None

            logger.info(f"[自动评论] 开始评论说说 {tid}")
            await self._http.random_human_delay(1.5, 5.0, "[自动评论]")
            result = await self.comment(
                shuoshuo_id=tid, content=comment_text, qq_number="", owner_qq=owner_qq,
            )

            if result.is_success:
                logger.info(f"[自动评论] 说说 {tid} 评论成功")
                return True
            else:
                logger.warning(f"[自动评论] 说说 {tid} 评论失败: {result.error_message}")
                return False
        finally:
            self._state.unlock_comment(comment_key)

    # ---- 回复自己说说的评论 ----

    async def check_and_reply_own_feed_comments(self, qq_number: str) -> None:
        """检查自己说说的评论并回复（含二级盖楼回复）。"""
        logger.info(f"[评论回复] 开始检查 QQ={qq_number} 的说说评论...")

        result = await self._feeds.get_list(qq_number, count=5)
        if not result.is_success or not result.data:
            logger.warning(f"[评论回复] 获取说说列表失败: {result.error_message}")
            return

        for feed in result.data:
            tid = feed.get("tid")
            if not tid:
                continue

            content = feed.get("content", "")
            comments = feed.get("commentlist") or feed.get("comments", []) or []
            rt_con = feed.get("rt_con", {})
            rt_content = (
                rt_con.get("content", "")
                if isinstance(rt_con, dict)
                else str(rt_con) if rt_con else ""
            )

            pics = feed.get("pic", [])
            images = [
                p.get("url", p.get("big_url", ""))
                for p in pics if isinstance(p, dict)
            ]

            story_time = (
                str(feed.get("createTime2", "") or "").strip()
                or str(feed.get("create_time", "") or "").strip()
                or str(feed.get("time", "") or "").strip()
            )

            flattened_comments = self._flatten_comments(comments)
            await self._reply_to_comments(
                tid=tid,
                story_content=content or rt_content or "说说内容",
                comments=flattened_comments,
                images=images,
                qq_number=qq_number,
                story_time=story_time,
            )

    @staticmethod
    def _flatten_comments(comments: list) -> list[dict]:
        """扁平化评论列表：包含主评论 + 二级盖楼回复。"""
        flattened: list[dict] = []
        for c in comments:
            c_copy = dict(c)
            c_copy["is_nested"] = False
            flattened.append(c_copy)

            nested_list = c.get("list_3") or c.get("replylist") or []
            for nc in nested_list:
                nc_copy = dict(nc)
                nc_copy["is_nested"] = True
                nc_copy["parent_id"] = c.get("id") or c.get("cid")
                flattened.append(nc_copy)
        return flattened

    async def _reply_to_comments(
        self,
        tid: str,
        story_content: str,
        comments: list,
        images: list[str],
        qq_number: str,
        story_time: str = "",
    ) -> None:
        """回复说说下的评论。"""
        if not comments:
            return

        for comment in comments:
            comment_uin = str(comment.get("uin", ""))
            if comment_uin == qq_number:
                continue

            comment_id = str(comment.get("id") or comment.get("cid") or "")
            if not comment_id:
                continue

            if self._state.has_replied_comment(tid, comment_id):
                continue

            comment_key = f"{tid}_{comment_id}"
            if self._state.is_comment_processing(comment_key):
                logger.debug(f"[评论回复] 评论 {comment_key} 正在处理中，跳过")
                continue

            self._state.lock_comment(comment_key)
            try:
                nickname = comment.get("nickname", "网友")
                comment_content = comment.get("content", "")
                commenter_qq = str(comment.get("uin", "") or "").strip() or None
                comment_time = (
                    str(comment.get("createTime2", "") or "").strip()
                    or str(comment.get("create_time", "") or "").strip()
                    or str(comment.get("time", "") or "").strip()
                )
                logger.info(f"[评论回复] 发现新评论: {nickname}: {comment_content}")

                monitor_config = self._get_monitor_config()
                reply_probability = float(monitor_config.get("auto_reply_probability", 0.9))
                reply_probability = max(0.0, min(1.0, reply_probability))
                if random.random() > reply_probability:
                    logger.debug("[评论回复] 概率未命中，跳过回复")
                    continue

                reply_text = await self._prompts.generate_comment_reply(
                    story_content=story_content,
                    comment_content=comment_content,
                    commenter_name=nickname,
                    commenter_qq=commenter_qq,
                    images=images,
                    story_time=story_time,
                    comment_time=comment_time,
                )

                if reply_text:
                    await self._http.random_human_delay(2.0, 6.0, "[评论回复]")
                    result = await self.comment(
                        shuoshuo_id=tid, content=reply_text,
                        qq_number=qq_number, owner_qq=qq_number,
                        comment_id=comment_id,
                    )

                    if result.is_success:
                        self._state.mark_comment_replied(tid, comment_id)
                        logger.info(f"[评论回复] 回复成功: {reply_text}")
                    else:
                        category = classify_failure_reason(result.error_message)
                        logger.warning(f"[评论回复] 回复失败[{category}]: {result.error_message}")
            finally:
                self._state.unlock_comment(comment_key)

    # ---- 处理好友动态下的评论 ----

    async def process_feed_comments(
        self, item: dict, comments: list, *, current_qq: str | None = None
    ) -> None:
        """处理好友动态下的现有评论（支持多级盖楼互动）。"""
        tid = item.get("tid")
        if not tid:
            return

        owner_qq = str(item.get("uin", "") or "").strip() or None
        normalized_current_qq = str(current_qq or "").strip() or None

        for comment in comments:
            comment_uin = str(comment.get("qq_account") or comment.get("uin", ""))
            if normalized_current_qq and comment_uin == normalized_current_qq:
                continue

            comment_id = str(comment.get("comment_tid") or comment.get("id", ""))
            if not comment_id:
                continue

            if self._state.has_replied_comment(tid, comment_id):
                continue

            comment_key = f"{tid}_feed_{comment_id}"
            if self._state.is_comment_processing(comment_key):
                logger.debug(f"[盖楼互动] 评论 {comment_key} 正在处理中，跳过")
                continue

            self._state.lock_comment(comment_key)
            try:
                monitor_config = self._get_monitor_config()
                reply_probability = float(monitor_config.get("auto_reply_probability", 0.9))
                if random.random() > reply_probability:
                    logger.debug("[盖楼互动] 概率未命中，跳过回复")
                    continue

                nickname = comment.get("nickname", "网友")
                content = comment.get("content", "")
                parent_tid = comment.get("parent_tid")

                if parent_tid:
                    logger.info(f"[盖楼互动] 发现二级回复: {nickname}: {content}")
                else:
                    logger.info(f"[盖楼互动] 发现一级评论: {nickname}: {content}")

                reply_text = await self._prompts.generate_comment_reply(
                    story_content=item.get("content", ""),
                    comment_content=content,
                    commenter_name=nickname,
                    commenter_qq=comment_uin,
                    images=[p.get("url") for p in item.get("pic", [])],
                    story_time=str(item.get("createTime", "")),
                    comment_time="",
                )

                if reply_text:
                    await self._http.random_human_delay(2.0, 6.0, "[盖楼互动]")
                    res = await self.comment(
                        shuoshuo_id=tid, content=reply_text,
                        qq_number="", owner_qq=owner_qq,
                        comment_id=comment_id, parent_tid=parent_tid,
                    )
                    if res.is_success:
                        self._state.mark_comment_replied(tid, comment_id)
                        level_text = "二级回复" if parent_tid else "一级评论"
                        logger.info(f"[盖楼互动] {level_text}回复成功: {reply_text}")
                    else:
                        logger.warning(f"[盖楼互动] 回复失败: {res.error_message}")
            finally:
                self._state.unlock_comment(comment_key)