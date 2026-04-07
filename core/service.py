"""Qzone 核心服务层。

封装QQ空间说说相关的业务逻辑、HTTP API调用以及 Cookie 状态管理。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
import datetime
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Tuple, TypeVar, Generic, TYPE_CHECKING

import httpx
import orjson

from src.core.components.base import BaseService
from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from ..plugin import QzoneShuoshuoPlugin
    from ..config import QzoneConfig

logger = get_logger("qzone_shuoshuo")

T = TypeVar("T")


class ResultStatus(Enum):
    """操作结果状态"""
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class Result(Generic[T]):
    """操作结果封装"""
    status: ResultStatus
    data: T | None = None
    error_message: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == ResultStatus.SUCCESS

    @classmethod
    def ok(cls, data: T) -> "Result[T]":
        return cls(status=ResultStatus.SUCCESS, data=data)

    @classmethod
    def fail(cls, message: str) -> "Result[T]":
        return cls(status=ResultStatus.ERROR, error_message=message)


class QzoneService(BaseService):
    """QQ空间服务类"""

    service_name = "qzone"
    service_description = "QQ空间说说核心服务"
    version = "1.1.0"

    EMOTION_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    ADAPTER_SIGNATURE = "napcat_adapter:adapter:napcat_adapter"

    DEFAULT_COMMENT_SYSTEM_PROMPT = (
        "你正在QQ空间场景下生成评论。\n"
        "目标：像真人用户一样，给出自然、友善、贴合上下文的短评论。\n"
        "优先级：\n"
        "1) 先贴合语境，再追求文采；\n"
        "2) 输出只给评论正文，不要解释或前缀；\n"
        "3) 不编造输入外事实，不输出攻击性/敏感内容；\n"
        "4) 风格与人设一致，允许口语化；\n"
        "5) 若不适合评论，返回空字符串。"
    )

    DEFAULT_REPLY_SYSTEM_PROMPT = (
        "你正在QQ空间场景下回复他人评论。\n"
        "目标：给出自然、礼貌、有人味的回应，像真实社交互动。\n"
        "优先级：\n"
        "1) 紧贴对方评论语义，先回应再延展；\n"
        "2) 输出只给回复正文，不要解释或前缀；\n"
        "3) 不编造输入外事实，不输出攻击性/敏感内容；\n"
        "4) 风格与人设一致，允许轻松口语；\n"
        "5) 若无合适回复，返回空字符串。"
    )

    DEFAULT_PUBLISH_SYSTEM_PROMPT = (
        "你正在QQ空间场景下准备发布说说。\n"
        "目标：将输入内容改写为一条自然、友善、可公开展示的说说正文。\n"
        "优先级：\n"
        "1) 保留原意，不编造输入外事实；\n"
        "2) 语言自然，符合人设与表达风格；\n"
        "3) 输出只给说说正文，不要解释或前后缀；\n"
        "4) 避免攻击性、敏感或冒犯表达；\n"
        "5) 若输入不适合发布，返回空字符串。"
    )
    DEFAULT_COMMENT_FORBIDDEN = "禁止使用Emoji表情、@符号、敏感话题"
    IMAGE_CONTEXT_MAX_IMAGES = 3
    IMAGE_CONTEXT_MAX_CONCURRENCY = 1

    def __init__(self, plugin: "QzoneShuoshuoPlugin") -> None:
        self.plugin = plugin
        self.config: QzoneConfig = getattr(plugin, "config", None)  # type: ignore
        from ..core.cookie_manager import CookieManager
        self.cookie_manager = CookieManager(self._data_dir())
        self._last_tid: str | None = None
        # 评论追踪：记录已评论的说说ID及其时间
        self._commented_tids: dict[str, float] = {}
        # 评论回复追踪：记录已回复的评论 (fid + comment_id)
        self._replied_comments: set[str] = set()
        # 监控状态
        self._monitor_running: bool = False
        self._monitor_config: dict[str, Any] = {}
        # 手动触发后的监控冷却截止时间戳（用于“主动执行后重新计时”）
        self._monitor_cooldown_until: float = 0.0
        # 监控运行态（用于 status 可观测性）
        self._last_monitor_run_at: float = 0.0
        self._last_monitor_source: str = ""
        self._last_monitor_force: bool = False
        self._last_monitor_result: str = "never"
        self._last_monitor_error: str = ""
        self._last_monitor_skip_reason: str = ""
        # 启动连接就绪重试（首轮拿不到 QQ 时触发）
        self._startup_retry_active: bool = False
        self._startup_retry_attempt: int = 0
        self._startup_retry_max_attempts: int = 0
        self._startup_retry_interval: int = 0
        self._startup_retry_job_name: str = ""
        self._startup_retry_last_reason: str = ""
        # Cookie 误判防护统计（评论空响应二次确认）
        self._cookie_confirm_total: int = 0
        self._cookie_confirm_recovered: int = 0
        self._cookie_confirm_refresh: int = 0
        # 发布去重（避免模型短时间重复发送同内容）
        self._last_published_content_hash: str | None = None
        self._last_published_at: float = 0.0
        # 最近发布文本历史（用于提示词去重与风格多样性）
        self._published_text_history: list[dict[str, Any]] = []
        # 最近一次阅读摘要（用于跨轮复用，减少重复读取 token）
        self._last_read_snapshot: dict[str, Any] | None = None
        # 已读说说追踪（用于“只读一次、执行一次”语义）
        self._read_tids: dict[str, float] = {}
        # 正在处理中的说说（并发防重）
        self._processing_read_tids: set[str] = set()
        self._load_state()
        logger.info("QzoneService 初始化完成")

    def _data_dir(self) -> Path:
        """获取插件数据目录。"""
        storage_cfg = getattr(self.config, "storage", None) if self.config else None
        data_dir_str = getattr(storage_cfg, "data_dir", "data/qzone_shuoshuo") if storage_cfg else "data/qzone_shuoshuo"
        return Path(data_dir_str)

    def _is_debug(self) -> bool:
        """检查是否启用调试模式"""
        debug_cfg = getattr(self.config, "debug", None) if self.config else None
        if debug_cfg:
            enable_debug = getattr(debug_cfg, "enable_debug", False)
            log_level = getattr(debug_cfg, "log_level", "info")
            return enable_debug or log_level.lower() == "debug"
        return False

    def _log(self, level: str, tag: str, msg: str) -> None:
        """根据日志级别输出日志

        Args:
            level: 日志级别 (debug/info/warning/error)
            tag: 日志标签，如 "[说说监控]"
            msg: 日志消息
        """
        if level == "debug":
            logger.debug(f"{tag} {msg}")
        elif level == "info":
            logger.info(f"{tag} {msg}")
        elif level == "warning":
            logger.warning(f"{tag} {msg}")
        else:
            logger.error(f"{tag} {msg}")

    async def _get_qq_from_napcat(self) -> str | None:
        """从 NapCat 适配器自动获取 Bot 的 QQ 号"""
        try:
            from src.core.managers.adapter_manager import get_adapter_manager
            # NapCat 适配器的 platform 是 "qq"
            bot_info = await get_adapter_manager().get_bot_info_by_platform("qq")
            if bot_info:
                qq_id = bot_info.get("bot_id")
                if qq_id:
                    logger.info(f"自动从 NapCat 获取到 QQ 号: {qq_id}")
                    return str(qq_id)
            logger.warning("未能从 NapCat 获取 QQ 号信息")
        except Exception as e:
            logger.error(f"从 NapCat 获取 QQ 号失败: {e}")
        return None

    def _load_state(self) -> None:
        state_file = self._data_dir() / "monitor_state.json"
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_tid = data.get("last_tid")
                    # 加载已评论的说说追踪
                    self._commented_tids = data.get("commented_tids", {})
                    # 加载已回复的评论追踪
                    self._replied_comments = set(data.get("replied_comments", []))
                    self._last_read_snapshot = data.get("last_read_snapshot")
                    self._read_tids = data.get("read_tids", {})
                    self._published_text_history = data.get("published_text_history", [])
            except Exception as e:
                logger.warning(f"加载监控状态失败: {e}")

    def _save_state(self) -> None:
        try:
            state_file = self._data_dir() / "monitor_state.json"
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "last_tid": self._last_tid,
                    "commented_tids": self._commented_tids,
                    "replied_comments": list(self._replied_comments),
                    "last_read_snapshot": self._last_read_snapshot,
                    "read_tids": self._read_tids,
                    "published_text_history": self._published_text_history,
                }, f)
        except Exception as e:
            logger.error(f"保存监控状态失败: {e}")

    def _remember_published_text(self, text: str, keep_max: int = 20) -> None:
        """记录已发布文本历史（用于后续提示词防重复）。"""
        cleaned = str(text or "").strip()
        if not cleaned:
            return

        if not hasattr(self, "_published_text_history"):
            self._published_text_history = []

        self._published_text_history.append({"text": cleaned, "ts": time.time()})
        if len(self._published_text_history) > keep_max:
            self._published_text_history = self._published_text_history[-keep_max:]
        self._save_state()

    def _build_publish_history_block(self, limit: int = 5) -> str:
        """构建最近发布历史块，帮助模型避免语义重复。"""
        history = list(getattr(self, "_published_text_history", []) or [])
        if not history:
            return ""

        recent = history[-max(1, limit):]
        lines: list[str] = []
        for item in reversed(recent):
            text = str(item.get("text", "") or "").replace("\n", " ").strip()
            if not text:
                continue
            lines.append(f"- {text[:120]}")

        if not lines:
            return ""

        return "最近已发布内容（请避免语义重复）：\n" + "\n".join(lines)

    def _trim_read_tids(self, keep_max: int = 2000) -> None:
        """限制已读追踪规模，避免状态无限增长。"""
        if len(self._read_tids) <= keep_max:
            return
        ordered = sorted(self._read_tids.items(), key=lambda kv: float(kv[1] or 0.0), reverse=True)
        self._read_tids = dict(ordered[:keep_max])

    def is_shuoshuo_read(self, tid: str) -> bool:
        """判断说说是否已读。"""
        key = str(tid or "").strip()
        if not key:
            return False
        return key in self._read_tids

    def mark_shuoshuo_read(self, tid: str) -> None:
        """标记说说为已读。"""
        key = str(tid or "").strip()
        if not key:
            return
        self._read_tids[key] = time.time()
        self._trim_read_tids()
        self._save_state()

    def mark_shuoshuo_read_batch(self, items: list[dict[str, Any]]) -> None:
        """批量标记说说为已读。"""
        changed = False
        now_ts = time.time()
        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue
            self._read_tids[tid] = now_ts
            changed = True

        if changed:
            self._trim_read_tids()
            self._save_state()

    def filter_unread_shuoshuo(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按已读追踪过滤出未读说说列表。"""
        unread: list[dict[str, Any]] = []
        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue
            if tid in self._read_tids:
                continue
            unread.append(item)
        return unread

    def claim_unread_shuoshuo(self, items: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
        """领取未读说说用于处理（并发防重）。

        规则：
        - 已读(`_read_tids`)跳过
        - 已在处理中(`_processing_read_tids`)跳过
        - 领取后立刻加入处理中集合，直到 finalize
        """
        claimed: list[dict[str, Any]] = []
        max_count = int(limit) if limit is not None else None

        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue
            if tid in self._read_tids:
                continue
            if tid in self._processing_read_tids:
                continue

            self._processing_read_tids.add(tid)
            claimed.append(item)

            if max_count is not None and len(claimed) >= max_count:
                break

        return claimed

    def finalize_read_claim(self, items: list[dict[str, Any]], processed: bool = True) -> None:
        """结束未读领取。

        Args:
            items: 本轮领取并处理的说说
            processed: 是否处理成功；成功时写入已读，失败仅解锁处理中
        """
        changed = False
        now_ts = time.time()

        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue

            if tid in self._processing_read_tids:
                self._processing_read_tids.remove(tid)

            if processed:
                self._read_tids[tid] = now_ts
                changed = True

        if changed:
            self._trim_read_tids()
            self._save_state()

    def remember_last_read_snapshot(self, snapshot: dict[str, Any]) -> None:
        """记录最近一次阅读摘要，供下一轮复用。"""
        self._last_read_snapshot = snapshot
        self._save_state()

    def get_last_read_snapshot(self) -> dict[str, Any] | None:
        """获取最近一次阅读摘要。"""
        return self._last_read_snapshot

    def _mark_commented(self, tid: str) -> None:
        """标记说说已被评论"""
        import time
        self._commented_tids[tid] = time.time()
        logger.debug(f"[评论追踪] 标记说说 {tid} 已评论")

    def _is_commented(self, tid: str) -> bool:
        """检查说说是否已被评论"""
        return tid in self._commented_tids

    def _mark_comment_replied(self, fid: str, comment_id: str) -> None:
        """标记评论已被回复"""
        key = f"{fid}_{comment_id}"
        self._replied_comments.add(key)
        self._save_state()
        logger.debug(f"[评论回复追踪] 标记 {fid}_{comment_id} 已回复")

    def _has_replied_comment(self, fid: str, comment_id: str) -> bool:
        """检查评论是否已被回复"""
        key = f"{fid}_{comment_id}"
        return key in self._replied_comments

    def _get_gtk(self, p_skey: str) -> str:
        if not p_skey:
            return ""
        hash_val = 5381
        for char in p_skey:
            hash_val += (hash_val << 5) + ord(char)
        return str(hash_val & 2147483647)

    async def _random_human_delay(self, min_seconds: float, max_seconds: float, tag: str) -> None:
        """人类化随机延迟，降低风控命中概率。"""
        delay = random.uniform(min_seconds, max_seconds)
        self._log("debug", tag, f"随机等待 {delay:.2f}s")
        await asyncio.sleep(delay)

    async def _post_with_backoff(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        data: dict[str, Any],
        params: dict[str, Any],
        tag: str,
        max_retries: int = 2,
    ) -> httpx.Response:
        """发送 POST 请求并在限流/服务波动时进行退避重试。"""
        resp = await client.post(url, data=data, params=params)

        if resp.status_code == 429 or resp.status_code >= 500:
            for attempt in range(1, max_retries + 1):
                backoff = min(2 ** attempt, 6) + random.uniform(0.2, 0.8)
                logger.warning(
                    f"{tag} 遇到疑似限流/服务波动 ({resp.status_code})，"
                    f"第 {attempt} 次退避重试，等待 {backoff:.2f}s"
                )
                await asyncio.sleep(backoff)
                resp = await client.post(url, data=data, params=params)
                if resp.status_code < 500 and resp.status_code != 429:
                    break

        return resp

    def _classify_failure_reason(self, message: str | None) -> str:
        """将失败信息归类，便于排障与观察风控行为。"""
        text = (message or "").lower()

        if any(key in text for key in ("cookie", "-3000", "401", "403", "302", "失效", "登录")):
            return "cookie"
        if any(key in text for key in ("429", "限流", "频率", "too many")):
            return "rate_limit"
        if any(key in text for key in ("500", "502", "503", "504", "server", "服务器")):
            return "server"
        if any(key in text for key in ("无权", "权限", "forbidden", "permission")):
            return "permission"
        if any(key in text for key in ("解析", "json", "格式")):
            return "parse"
        return "other"

    def _bump_cookie_confirm_stats(self, event: str) -> None:
        """更新并输出评论空响应二次确认统计。"""
        total = int(getattr(self, "_cookie_confirm_total", 0) or 0)
        recovered = int(getattr(self, "_cookie_confirm_recovered", 0) or 0)
        refresh = int(getattr(self, "_cookie_confirm_refresh", 0) or 0)

        total += 1
        if event == "recovered":
            recovered += 1
        elif event == "refresh":
            refresh += 1

        self._cookie_confirm_total = total
        self._cookie_confirm_recovered = recovered
        self._cookie_confirm_refresh = refresh

        recovered_rate = (recovered / total * 100.0) if total > 0 else 0.0
        self._log(
            "debug",
            "[Cookie判定]",
            f"二次确认统计 total={total}, recovered={recovered}, refresh={refresh}, recovered_rate={recovered_rate:.1f}%",
        )

    def _normalize_callback_payload(self, response_text: str) -> str:
        """将接口返回统一抽取为可解析 JSON 文本。

        兼容场景：
        - `_Callback({...})`
        - `_preloadCallback({...})`
        - HTML 包裹回调脚本
        """
        text = str(response_text or "").strip()
        if not text:
            return ""

        markers = ("_Callback(", "_preloadCallback(", "callback(")
        for marker in markers:
            idx = text.find(marker)
            if idx >= 0:
                candidate = text[idx + len(marker):]
                end_idx = candidate.rfind(")")
                if end_idx >= 0:
                    candidate = candidate[:end_idx]
                return candidate.strip()

        lowered = text.lower()
        if "<html" in lowered and "{" in text and "}" in text:
            left = text.find("{")
            right = text.rfind("}")
            if 0 <= left < right:
                return text[left:right + 1].strip()

        return text

    async def _get_client(self, qq: str) -> Tuple[httpx.AsyncClient, str, str] | None:
        adapter_sign = self.ADAPTER_SIGNATURE
        cookies = await self.cookie_manager.get_cookies(qq, adapter_sign)
        if not cookies:
            logger.error(f"无法获取 QQ:{qq} 的 Cookie")
            return None

        p_skey = cookies.get("p_skey") or cookies.get("P_skey")
        if not p_skey:
            logger.warning(f"QQ:{qq} Cookie 中缺少 p_skey")
            return None

        uin_cookie = cookies.get("uin") or cookies.get("ptui_loginuin") or qq
        uin = uin_cookie.lstrip("o")
        gtk = self._get_gtk(p_skey)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://user.qzone.qq.com/{uin}",
            "Origin": "https://user.qzone.qq.com"
        }

        client = httpx.AsyncClient(cookies=cookies, headers=headers, timeout=30.0)
        return client, uin, gtk

    async def _refresh_cookie_if_expired(self, qq_number: str, operation: str) -> Tuple[httpx.AsyncClient, str, str] | None:
        """Cookie 失效时自动刷新并返回新客户端

        Args:
            qq_number: QQ号
            operation: 操作名称（用于日志）

        Returns:
            新客户端信息或 None
        """
        adapter_sign = self.ADAPTER_SIGNATURE
        new_cookies = await self.cookie_manager.refresh_cookies(qq_number, adapter_sign)
        if new_cookies:
            logger.info(f"[{operation}] Cookie 刷新成功，重新获取客户端...")
            return await self._get_client(qq_number)
        logger.error(f"[{operation}] Cookie 刷新失败")
        return None

    async def _upload_image(self, client: httpx.AsyncClient, image_bytes: bytes, gtk: str) -> dict[str, Any] | None:
        try:
            pic_base64 = base64.b64encode(image_bytes).decode('utf-8')
            post_data = {
                "filename": "filename.jpg", "filetype": "1", "uploadtype": "1",
                "albumtype": "7", "exttype": "0", "refer": "shuoshuo",
                "output_type": "json", "charset": "utf-8", "output_charset": "utf-8",
                "upload_hd": "1", "hd_width": "2048", "hd_height": "10000",
                "hd_quality": "96",
                "backUrls": "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,http://119.147.64.75/cgi-bin/upload/cgi_upload_image",
                "url": f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={gtk}",
                "base64": "1", "picfile": pic_base64,
            }
            resp = await client.post(self.UPLOAD_URL, data=post_data, params={"g_tk": gtk})
            resp.raise_for_status()
            text = self._normalize_callback_payload(resp.text)
            return orjson.loads(text)
        except Exception as e:
            logger.error(f"上传图片异常: {e}")
            return None

    def _get_picbo_and_richval(self, upload_result: dict) -> Tuple[str, str]:
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

    async def _retry_publish(
        self, qq_number: str, content: str, real_images: list[bytes], visible: str
    ) -> "Result[dict] | None":
        """重试发布说说（Cookie 刷新后）

        Args:
            qq_number: QQ号
            content: 内容
            real_images: 图片字节列表
            visible: 可见范围

        Returns:
            发布结果或 None
        """
        client_info = await self._get_client(qq_number)
        if not client_info:
            return None

        client, uin, gtk = client_info
        try:
            pic_bos, richvals = [], []
            if real_images:
                for img_bytes in real_images:
                    res = await self._upload_image(client, img_bytes, gtk)
                    if res:
                        try:
                            bo, rv = self._get_picbo_and_richval(res)
                            pic_bos.append(bo)
                            richvals.append(rv)
                        except Exception:
                            pass

            post_data = {
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

            await self._random_human_delay(0.8, 2.0, "[重试发布]")
            resp = await self._post_with_backoff(
                client=client,
                url=self.EMOTION_PUBLISH_URL,
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

    async def publish_shuoshuo(
        self, qq_number: str = "", content: str = "", images: list[bytes] | None = None, visible: str = "all"
    ) -> "Result[dict]":
        """发布说说"""
        logger.info(f"[发布说说] 开始执行, qq={qq_number or '自动获取'}, 内容长度={len(content)}")

        if not content:
            logger.warning("[发布说说] 内容为空")
            return Result.fail("说说内容不能为空")

        # 近时窗去重：避免模型在短时间内重复发送同一条原始内容
        raw_text = str(content).strip()
        if raw_text:
            current_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            now_ts = time.time()
            if (
                self._last_published_content_hash == current_hash
                and (now_ts - float(getattr(self, "_last_published_at", 0.0) or 0.0)) <= 300
            ):
                logger.info("[发布说说] 命中近5分钟重复内容去重，跳过重复发布")
                return Result.ok({"message": "近期已发布同内容，已跳过重复发送"})

        content = await self._rewrite_publish_content_with_persona(content)
        if not content:
            return Result.fail("说说内容改写后为空，已取消发布")

        # 获取 QQ 号：优先使用参数，其次自动从 NapCat 获取
        if not qq_number:
            logger.debug("[发布说说] 未指定 QQ 号，尝试从 NapCat 自动获取")
            qq_number = await self._get_qq_from_napcat()

        if not qq_number:
            logger.error("[发布说说] 无法获取 QQ 号，请检查 NapCat 适配器配置")
            return Result.fail("无法获取 QQ 号，请确保 NapCat 适配器已正确配置")

        real_images = []
        if images:
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

        logger.debug(f"[发布说说] 准备获取客户端, QQ={qq_number}")
        client_info = await self._get_client(qq_number)
        if not client_info:
            logger.error(f"[发布说说] 获取客户端失败，Cookie 可能不存在或配置错误, QQ={qq_number}")
            return Result.fail("获取客户端失败(Cookie不存在或配置错误)")

        client, uin, gtk = client_info
        logger.info(f"[发布说说] 客户端获取成功, uin={uin}, gtk={gtk}")

        try:
            pic_bos, richvals = [], []
            if real_images:
                logger.debug(f"[发布说说] 开始上传 {len(real_images)} 张图片")
                for i, img_bytes in enumerate(real_images):
                    res = await self._upload_image(client, img_bytes, gtk)
                    if res:
                        try:
                            bo, rv = self._get_picbo_and_richval(res)
                            pic_bos.append(bo)
                            richvals.append(rv)
                            logger.debug(f"[发布说说] 图片 {i+1} 上传成功")
                        except Exception as e:
                            logger.error(f"[发布说说] 解析上传参数失败: {e}")

            logger.debug(f"[发布说说] 发送说说请求, uin={uin}, content长度={len(content)}")
            post_data = {
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

            await self._random_human_delay(0.8, 2.0, "[发布说说]")
            resp = await self._post_with_backoff(
                client=client,
                url=self.EMOTION_PUBLISH_URL,
                data=post_data,
                params={"g_tk": gtk},
                tag="[发布说说]",
                max_retries=2,
            )
            text = resp.text

            try:
                result = orjson.loads(text)
            except Exception:
                logger.error("[发布说说] 服务端响应格式错误")
                return Result.fail("服务端响应格式错误")

            if result.get("code") == -3000:
                logger.warning("[发布说说] Cookie 已失效，尝试刷新...")
                # 刷新 Cookie 并重试
                new_cookies = await self.cookie_manager.refresh_cookies(
                    qq_number,
                    self.ADAPTER_SIGNATURE,
                )
                if new_cookies:
                    logger.info("[发布说说] Cookie 刷新成功，重试发布...")
                    # 重新获取客户端并重试
                    retry_result = await self._retry_publish(qq_number, content, real_images, visible)
                    if retry_result:
                        return retry_result
                return Result.fail("Cookie 已失效，刷新失败，请检查 NapCat 登录状态")

            if result.get("tid"):
                tid = result["tid"]
                # 发布成功后更新去重状态
                if raw_text:
                    self._last_published_content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
                    self._last_published_at = time.time()
                    self._remember_published_text(raw_text)
                logger.info(f"[发布说说] 发布成功, tid={tid}")
                return Result.ok({"tid": tid, "message": "发布成功", "content": content})
            else:
                error_msg = result.get('message', '未知错误')
                logger.error(f"[发布说说] 发布失败: {error_msg}")
                return Result.fail(f"发布失败: {error_msg}")

        except Exception as e:
            logger.error(f"[发布说说] 内部异常: {e}")
            return Result.fail(f"内部异常: {e}")
        finally:
            await client.aclose()

    async def get_shuoshuo_list(self, qq_number: str = "", count: int = 20) -> "Result[list[dict]]":
        """获取说说列表"""
        target_qq = str(qq_number or "").strip()
        logger.info(f"[获取列表] 开始执行, target={target_qq or '自动获取'}, count={count}")

        login_qq = await self._get_qq_from_napcat()
        if not login_qq:
            logger.error("[获取列表] 无法获取登录 QQ 号")
            return Result.fail("无法获取登录 QQ 号")

        if not target_qq:
            target_qq = login_qq

        logger.debug(f"[获取列表] 准备获取客户端, login_qq={login_qq}, target_qq={target_qq}")
        client_info = await self._get_client(login_qq)
        if not client_info:
            logger.error("[获取列表] 获取客户端失败")
            return Result.fail("获取客户端失败")

        client, _uin, gtk = client_info
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
        params = {
            "uin": target_qq, "ftype": "0", "sort": "0", "pos": "0", "num": str(count),
            "replynum": "100", "g_tk": gtk, "callback": "_preloadCallback",
            "code_version": "1", "format": "json", "need_private_comment": "1"
        }

        try:
            logger.debug(f"[获取列表] 发送请求, target_qq={target_qq}")
            resp = await client.get(url, params=params)
            text = resp.text.strip()
            if text.startswith("_preloadCallback("):
                text = text[len("_preloadCallback("):]
            if text.endswith(")"):
                text = text[:-1]

            data = orjson.loads(text)
            if data.get("code") == 0:
                msglist = data.get("msglist") or []
                logger.info(f"[获取列表] 成功, 获取到 {len(msglist)} 条说说")
                return Result.ok(msglist)
            elif data.get("code") == -3000:
                logger.warning("[获取列表] Cookie 已失效，尝试刷新...")
                client_info = await self._refresh_cookie_if_expired(login_qq, "获取列表")
                if client_info:
                    return await self._retry_get_list(client_info[0], target_qq, client_info[2], count)
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                logger.error(f"[获取列表] 失败: {data.get('message')}")
                return Result.fail(f"获取列表失败: {data.get('message')}")

        except Exception as e:
            logger.error(f"[获取列表] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_get_list(self, client: httpx.AsyncClient, target_qq: str, gtk: str, count: int) -> "Result[list[dict]]":
        """重试获取说说列表"""
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
        params = {
            "uin": target_qq, "ftype": "0", "sort": "0", "pos": "0", "num": str(count),
            "replynum": "100", "g_tk": gtk, "callback": "_preloadCallback",
            "code_version": "1", "format": "json", "need_private_comment": "1"
        }
        try:
            resp = await client.get(url, params=params)
            text = resp.text.strip()
            if text.startswith("_preloadCallback("):
                text = text[len("_preloadCallback("):]
            if text.endswith(")"):
                text = text[:-1]
            data = orjson.loads(text)
            if data.get("code") == 0:
                msglist = data.get("msglist") or []
                logger.info(f"[重试获取列表] 成功, 获取到 {len(msglist)} 条说说")
                return Result.ok(msglist)
            elif data.get("code") == -3000:
                logger.error("[重试获取列表] 刷新后的 Cookie 仍然失效")
                return Result.fail("Cookie 失效")
            return Result.fail(f"获取列表失败: {data.get('message')}")
        except Exception as e:
            logger.error(f"[重试获取列表] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def delete_shuoshuo(self, shuoshuo_id: str, qq_number: str = "") -> "Result[str]":
        """删除说说"""
        logger.info(f"[删除说说] 开始执行, tid={shuoshuo_id}, qq={qq_number or '自动获取'}")

        if not shuoshuo_id:
            logger.warning("[删除说说] tid为空")
            return Result.fail("说说ID不能为空")

        if not qq_number:
            qq_number = await self._get_qq_from_napcat()

        if not qq_number:
            logger.error("[删除说说] 无法获取 QQ 号")
            return Result.fail("无法获取 QQ 号")

        logger.debug(f"[删除说说] 准备获取客户端, QQ={qq_number}")
        client_info = await self._get_client(qq_number)
        if not client_info:
            logger.error("[删除说说] 获取客户端失败")
            return Result.fail("获取客户端失败")

        client, uin, gtk = client_info
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
        data = {
            "hostuin": uin, "tid": shuoshuo_id, "t1_source": "1",
            "code_version": "1", "format": "json", "qzreferrer": f"https://user.qzone.qq.com/{uin}"
        }

        try:
            logger.debug(f"[删除说说] 发送请求, uin={uin}, tid={shuoshuo_id}")
            resp = await client.post(url, data=data, params={"g_tk": gtk})
            text = resp.text
            result = orjson.loads(text)

            if result.get("code") == 0:
                logger.info(f"[删除说说] 成功, tid={shuoshuo_id}")
                return Result.ok("删除成功")
            elif result.get("code") == -3000:
                logger.warning("[删除说说] Cookie 已失效，尝试刷新...")
                client_info = await self._refresh_cookie_if_expired(qq_number, "删除说说")
                if client_info:
                    return await self._retry_delete(client_info[0], client_info[1], client_info[2], shuoshuo_id)
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                logger.error(f"[删除说说] 失败: {result.get('message')}")
                return Result.fail(f"删除失败: {result.get('message')}")

        except Exception as e:
            logger.error(f"[删除说说] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_delete(self, client: httpx.AsyncClient, uin: str, gtk: str, shuoshuo_id: str) -> "Result[str]":
        """重试删除说说"""
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
        data = {
            "hostuin": uin, "tid": shuoshuo_id, "t1_source": "1",
            "code_version": "1", "format": "json", "qzreferrer": f"https://user.qzone.qq.com/{uin}"
        }
        try:
            resp = await client.post(url, data=data, params={"g_tk": gtk})
            result = orjson.loads(resp.text)
            if result.get("code") == 0:
                logger.info(f"[重试删除] 成功, tid={shuoshuo_id}")
                return Result.ok("删除成功（Cookie已刷新）")
            elif result.get("code") == -3000:
                logger.error("[重试删除] 刷新后的 Cookie 仍然失效")
                return Result.fail("Cookie 失效")
            return Result.fail(f"删除失败: {result.get('message')}")
        except Exception as e:
            logger.error(f"[重试删除] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def get_shuoshuo_detail(self, shuoshuo_id: str, qq_number: str = "") -> "Result[dict]":
        """获取说说详情"""
        logger.info(f"[获取详情] 开始执行, tid={shuoshuo_id}, qq={qq_number or '自动获取'}")

        if not shuoshuo_id:
            logger.warning("[获取详情] tid为空")
            return Result.fail("说说ID不能为空")

        if not qq_number:
            qq_number = await self._get_qq_from_napcat()

        if not qq_number:
            logger.error("[获取详情] 无法获取 QQ 号")
            return Result.fail("无法获取 QQ 号")

        logger.debug(f"[获取详情] 准备获取客户端, QQ={qq_number}")
        client_info = await self._get_client(qq_number)
        if not client_info:
            logger.error("[获取详情] 获取客户端失败")
            return Result.fail("获取客户端失败")

        client, uin, gtk = client_info
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
        params = {
            "uin": uin, "tid": shuoshuo_id, "num": "1", "g_tk": gtk,
            "format": "json", "inCharset": "utf-8", "outCharset": "utf-8"
        }

        try:
            logger.debug(f"[获取详情] 发送请求, uin={uin}, tid={shuoshuo_id}")
            resp = await client.get(url, params=params)
            text = self._normalize_callback_payload(resp.text)

            data = orjson.loads(text)
            if data.get("code") == 0:
                msg_list = data.get("msglist") or []
                if msg_list:
                    logger.info("[获取详情] 成功")
                    return Result.ok(msg_list[0])
                logger.warning("[获取详情] 未找到说说或无权查看")
                return Result.fail("未找到说说或无权查看")
            elif data.get("code") == -3000:
                logger.warning("[获取详情] Cookie 已失效，尝试刷新...")
                client_info = await self._refresh_cookie_if_expired(qq_number, "获取详情")
                if client_info:
                    return await self._retry_get_detail(client_info[0], client_info[1], client_info[2], shuoshuo_id)
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                logger.error(f"[获取详情] 失败: {data.get('message')}")
                return Result.fail(f"获取详情失败: {data.get('message')}")

        except Exception as e:
            logger.error(f"[获取详情] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_get_detail(self, client: httpx.AsyncClient, uin: str, gtk: str, shuoshuo_id: str) -> "Result[dict]":
        """重试获取说说详情"""
        url = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
        params = {
            "uin": uin, "tid": shuoshuo_id, "num": "1", "g_tk": gtk,
            "format": "json", "inCharset": "utf-8", "outCharset": "utf-8"
        }
        try:
            resp = await client.get(url, params=params)
            text = self._normalize_callback_payload(resp.text)
            data = orjson.loads(text)
            if data.get("code") == 0:
                msg_list = data.get("msglist") or []
                if msg_list:
                    logger.info("[重试获取详情] 成功")
                    return Result.ok(msg_list[0])
                return Result.fail("未找到说说或无权查看")
            elif data.get("code") == -3000:
                logger.error("[重试获取详情] 刷新后的 Cookie 仍然失效")
                return Result.fail("Cookie 失效")
            return Result.fail(f"获取详情失败: {data.get('message')}")
        except Exception as e:
            logger.error(f"[重试获取详情] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def like_shuoshuo(self, shuoshuo_id: str, qq_number: str = "", owner_qq: str | None = None) -> "Result[str]":
        """点赞说说"""
        logger.info(f"[点赞说说] 开始执行, tid={shuoshuo_id}, qq={qq_number or '自动获取'}, owner={owner_qq}")

        if not shuoshuo_id:
            logger.warning("[点赞说说] tid为空")
            return Result.fail("说说ID不能为空")

        if not qq_number:
            qq_number = await self._get_qq_from_napcat()

        if not qq_number:
            logger.error("[点赞说说] 无法获取 QQ 号")
            return Result.fail("无法获取 QQ 号")

        logger.debug(f"[点赞说说] 准备获取客户端, QQ={qq_number}")
        client_info = await self._get_client(qq_number)
        if not client_info:
            logger.error("[点赞说说] 获取客户端失败")
            return Result.fail("获取客户端失败")

        client, uin, gtk = client_info
        target_owner = owner_qq if owner_qq else uin
        logger.debug(f"[点赞说说] 目标说说所有者: {target_owner}")

        url = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
        form_data = {
            "qzreferrer": f"https://user.qzone.qq.com/{target_owner}/infocenter",
            "opuin": uin, "unikey": f"http://user.qzone.qq.com/{target_owner}/mood/{shuoshuo_id}",
            "curkey": f"http://user.qzone.qq.com/{target_owner}/mood/{shuoshuo_id}",
            "from": "1", "appid": "311", "typeid": "0", "abstime": "",
            "fid": shuoshuo_id, "active": "0", "fupdate": "1", "g_tk": gtk,
        }

        try:
            logger.debug("[点赞说说] 发送请求")
            await self._random_human_delay(0.5, 1.8, "[点赞说说]")
            resp = await self._post_with_backoff(
                client=client,
                url=url,
                data=form_data,
                params={"g_tk": gtk},
                tag="[点赞说说]",
                max_retries=2,
            )
            text = self._normalize_callback_payload(resp.text)

            try:
                data = orjson.loads(text)
            except Exception:
                if "succ" in text.lower() or "成功" in text or '"ret":0' in text or '"code":0' in text:
                    logger.info("[点赞说说] 成功 (无法解析JSON)")
                    return Result.ok("点赞成功 (无法解析JSON)")
                logger.error(f"[点赞说说] 响应解析失败, snippet={repr(text[:200])}")
                return Result.fail("点赞响应解析失败")

            code = data.get("ret", data.get("code", -1))
            if code == 0:
                logger.info(f"[点赞说说] 成功, tid={shuoshuo_id}")
                return Result.ok("点赞成功")
            elif code == -3000:
                logger.warning("[点赞说说] Cookie 已失效，尝试刷新...")
                client_info = await self._refresh_cookie_if_expired(qq_number, "点赞说说")
                if client_info:
                    return await self._retry_like(client_info[0], client_info[1], client_info[2], shuoshuo_id, owner_qq)
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                logger.error(f"[点赞说说] 失败: {data.get('msg') or data.get('message')}")
                return Result.fail(f"点赞失败: {data.get('msg') or data.get('message')}")

        except Exception as e:
            logger.error(f"[点赞说说] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_like(
        self, client: httpx.AsyncClient, uin: str, gtk: str, shuoshuo_id: str, owner_qq: str | None
    ) -> "Result[str]":
        """重试点赞说说"""
        target_owner = owner_qq if owner_qq else uin
        url = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
        form_data = {
            "qzreferrer": f"https://user.qzone.qq.com/{target_owner}/infocenter",
            "opuin": uin, "unikey": f"http://user.qzone.qq.com/{target_owner}/mood/{shuoshuo_id}",
            "curkey": f"http://user.qzone.qq.com/{target_owner}/mood/{shuoshuo_id}",
            "from": "1", "appid": "311", "typeid": "0", "abstime": "",
            "fid": shuoshuo_id, "active": "0", "fupdate": "1", "g_tk": gtk,
        }
        try:
            resp = await self._post_with_backoff(
                client=client,
                url=url,
                data=form_data,
                params={"g_tk": gtk},
                tag="[重试点赞]",
                max_retries=1,
            )
            text = self._normalize_callback_payload(resp.text)
            data = orjson.loads(text)
            code = data.get("ret", data.get("code", -1))
            if code == 0:
                logger.info(f"[重试点赞] 成功, tid={shuoshuo_id}")
                return Result.ok("点赞成功（Cookie已刷新）")
            elif code == -3000:
                logger.error("[重试点赞] 刷新后的 Cookie 仍然失效")
                return Result.fail("Cookie 失效")
            return Result.fail(f"点赞失败: {data.get('msg') or data.get('message')}")
        except Exception as e:
            logger.error(f"[重试点赞] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def comment_shuoshuo(
        self,
        shuoshuo_id: str,
        content: str,
        qq_number: str = "",
        owner_qq: str | None = None,
        comment_id: str | None = None,
    ) -> "Result[dict]":
        """评论说说

        Args:
            shuoshuo_id: 说说 ID (tid)
            content: 评论内容
            qq_number: 评论者 QQ 号（留空则自动获取）
            owner_qq: 说说作者 QQ 号
            comment_id: 回复的评论 ID（可选，用于回复他人评论）
        """
        logger.info(f"[评论说说] 开始执行, tid={shuoshuo_id}, qq={qq_number or '自动获取'}, owner={owner_qq}")

        if not shuoshuo_id:
            logger.warning("[评论说说] tid为空")
            return Result.fail("说说ID不能为空")

        if not content:
            logger.warning("[评论说说] 内容为空")
            return Result.fail("评论内容不能为空")

        if not qq_number:
            qq_number = await self._get_qq_from_napcat()

        if not qq_number:
            logger.error("[评论说说] 无法获取 QQ 号")
            return Result.fail("无法获取 QQ 号")

        logger.debug(f"[评论说说] 准备获取客户端, QQ={qq_number}")
        client_info = await self._get_client(qq_number)
        if not client_info:
            logger.error("[评论说说] 获取客户端失败")
            return Result.fail("获取客户端失败")

        client, uin, gtk = client_info
        target_owner = owner_qq if owner_qq else qq_number
        logger.debug(f"[评论说说] 目标说说所有者: {target_owner}")

        # 构建评论 API 请求参数
        # 根据参考插件，使用 emotion_cgi_re_feeds 接口
        # topicId 格式: {owner_qq}_{tid}__1 (主评论)
        topic_id = f"{target_owner}_{shuoshuo_id}__1"

        post_data = {
            "topicId": topic_id,
            "uin": uin,
            "hostUin": target_owner,
            "content": content,
            "format": "fs",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "ref": "feeds",
        }

        # 如果是回复评论，添加相关参数
        if comment_id:
            post_data["commentid"] = comment_id # 注意：参考插件中这个字段可能是 parent_tid 或通过 content 拼接，但我们先保持协议一致
            # 在某些接口下，回复他人需要指定 parent_tid
            post_data["parent_tid"] = comment_id
            post_data["topicId"] = f"{target_owner}_{shuoshuo_id}__1" # 接口要求
        
        try:
            logger.info(f"[评论说说] 发送请求, URL={self.COMMENT_URL}, topicId={post_data['topicId']}")
            # 人类化随机延迟，降低请求节奏过于规则导致的风控概率
            await self._random_human_delay(0.8, 2.2, "[评论说说]")
            resp = await self._post_with_backoff(
                client=client,
                url=self.COMMENT_URL,
                data=post_data,
                params={"g_tk": gtk},
                tag="[评论说说]",
                max_retries=2,
            )

            logger.info(f"[评论说说] 响应状态码: {resp.status_code}")
            text = resp.text.strip()
            logger.info(f"[评论说说] 响应长度: {len(text)}")

            if not text:
                # 根据状态码判断错误类型
                if resp.status_code in (302, 401, 403):
                    # 小改：先进行一次同 Cookie 的二次确认，避免误判为 Cookie 失效
                    logger.warning("[评论说说] 收到空响应+认证状态，先二次确认再判断 Cookie 是否失效...")
                    await asyncio.sleep(random.uniform(0.4, 1.0))

                    confirm_resp = await client.post(
                        self.COMMENT_URL,
                        data=post_data,
                        params={"g_tk": gtk},
                    )
                    confirm_text = confirm_resp.text.strip()

                    if confirm_text:
                        self._bump_cookie_confirm_stats("recovered")
                        logger.info("[评论说说] 二次确认拿到有效响应，继续按正常流程解析。")
                        resp = confirm_resp
                        text = confirm_text
                    elif confirm_resp.status_code in (302, 401, 403):
                        self._bump_cookie_confirm_stats("refresh")
                        logger.warning("[评论说说] 二次确认仍是认证状态空响应，按 Cookie 失效处理并尝试刷新...")
                        client_info = await self._refresh_cookie_if_expired(qq_number, "评论说说")
                        if client_info:
                            return await self._retry_comment(
                                client_info[0], client_info[1], client_info[2],
                                shuoshuo_id, content, owner_qq, comment_id
                            )
                        return Result.fail("Cookie 失效，刷新失败")
                    else:
                        return Result.fail(f"评论响应为空 ({confirm_resp.status_code})")
                elif resp.status_code >= 500:
                    # 服务器错误，不刷新 Cookie
                    return Result.fail(f"QQ空间服务器错误 ({resp.status_code})，请稍后重试")
                else:
                    return Result.fail(f"评论响应为空 ({resp.status_code})")

            text = self._normalize_callback_payload(text)

            try:
                data = orjson.loads(text)
            except Exception:
                if "succ" in text or "成功" in text:
                    logger.info("[评论说说] 成功 (无法解析JSON)")
                    return Result.ok({"message": "评论成功"})
                logger.error(f"[评论说说] 响应解析失败: {repr(text[:200])}")
                return Result.fail("评论响应解析失败")

            code = data.get("ret", data.get("code", -1))
            if code == 0:
                cid = data.get("id", data.get("cid", ""))
                # 追踪评论过的说说（避免重复评论）
                self._mark_commented(shuoshuo_id)
                self._save_state()
                logger.info(f"[评论说说] 成功, tid={shuoshuo_id}, cid={cid}")
                return Result.ok({
                    "tid": shuoshuo_id,
                    "cid": cid,
                    "message": "评论成功"
                })
            elif code == -3000:
                logger.warning("[评论说说] Cookie 已失效，尝试刷新...")
                client_info = await self._refresh_cookie_if_expired(qq_number, "评论说说")
                if client_info:
                    return await self._retry_comment(
                        client_info[0], client_info[1], client_info[2],
                        shuoshuo_id, content, owner_qq, comment_id
                    )
                return Result.fail("Cookie 已失效，刷新失败")
            else:
                error_msg = data.get('msg', data.get('message', '未知错误'))
                logger.error(f"[评论说说] 失败: {error_msg}")
                return Result.fail(f"评论失败: {error_msg}")

        except Exception as e:
            logger.error(f"[评论说说] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def _retry_comment(
        self, client: httpx.AsyncClient, uin: str, gtk: str,
        shuoshuo_id: str, content: str, owner_qq: str | None, comment_id: str | None
    ) -> "Result[dict]":
        """重试评论说说"""
        target_owner = owner_qq if owner_qq else uin
        
        # 使用与主函数一致的逻辑
        post_data = {
            "topicId": f"{target_owner}_{shuoshuo_id}__1",
            "uin": uin,
            "hostUin": target_owner,
            "content": content,
            "format": "fs",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "ref": "feeds",
        }

        if comment_id:
            post_data["commentid"] = comment_id
            post_data["parent_tid"] = comment_id

        try:
            logger.info(f"[重试评论] 发送请求, topicId={post_data['topicId']}")
            resp = await self._post_with_backoff(
                client=client,
                url=self.COMMENT_URL,
                data=post_data,
                params={"g_tk": gtk},
                tag="[重试评论]",
                max_retries=1,
            )
            text = self._normalize_callback_payload(resp.text)
            data = orjson.loads(text)
            code = data.get("ret", data.get("code", -1))
            if code == 0:
                cid = data.get("id", data.get("cid", ""))
                self._mark_commented(shuoshuo_id)
                self._save_state()
                logger.info(f"[重试评论] 成功, tid={shuoshuo_id}, cid={cid}")
                return Result.ok({
                    "tid": shuoshuo_id,
                    "cid": cid,
                    "message": "评论成功（Cookie已刷新）"
                })
            elif code == -3000:
                logger.error("[重试评论] 刷新后的 Cookie 仍然失效")
                return Result.fail("Cookie 失效")
            return Result.fail(f"评论失败: {data.get('msg') or data.get('message')}")
        except Exception as e:
            logger.error(f"[重试评论] 异常: {e}")
            return Result.fail(f"异常: {e}")
        finally:
            await client.aclose()

    async def is_logged_in(self) -> bool:
        """检查登录状态"""
        qq = await self._get_qq_from_napcat()
        if not qq:
            logger.debug("[登录检查] 无法获取 QQ 号")
            return False
        logger.debug(f"[登录检查] 检查 QQ={qq} 的登录状态")
        cookies = await self.cookie_manager.load_cookies(qq)
        result = bool(cookies)
        logger.debug(f"[登录检查] QQ={qq} 登录状态: {result}")
        return result

    async def get_qq_suggestion(self) -> str:
        return "QQ 号已自动从 NapCat 适配器获取，无需手动配置"

    async def get_current_uin(self) -> str | None:
        qq = await self._get_qq_from_napcat()
        if qq:
            logger.debug(f"[获取当前UIN] QQ={qq}")
        else:
            logger.debug("[获取当前UIN] 未能获取 QQ 号")
        return qq

    async def try_update_cookies_from_napcat(self) -> str | None:
        logger.info("[Cookie更新] 开始从 NapCat 获取 Cookie")
        adapter_sign = self.ADAPTER_SIGNATURE
        cookies = await self.cookie_manager.fetch_cookies_from_adapter(adapter_sign)
        if cookies:
            uin = cookies.get("uin") or cookies.get("ptui_loginuin")
            if uin:
                real_uin = uin.lstrip("o")
                await self.cookie_manager.save_cookies(real_uin, cookies)
                logger.info(f"[Cookie更新] 成功获取并保存 Cookie, QQ={real_uin}")
                return real_uin
        logger.warning("[Cookie更新] 未能获取 Cookie")
        return None

    async def check_new_shuoshuo(self, *, force: bool = False, source: str = "scheduled") -> str:
        """检查并广播新说说（供内部/外部调用）。

        Args:
            force: 是否强制执行。为 True 时跳过静默窗口/手动冷却检查。
            source: 触发来源，仅用于日志标记。

        Returns:
            本轮执行结果码（如 skip_no_qq / processed_new_items / no_change）。
        """
        if not self._is_monitor_enabled():
            logger.debug("[说说监控] 监控总开关关闭，跳过")
            return "skip_disabled"

        # 不再检查静态配置，改为检查动态配置
        if not self._monitor_running:
            logger.debug("[说说监控] 监控未启动，跳过")
            return "skip_not_running"

        now_ts = time.time()
        cooldown_until = float(getattr(self, "_monitor_cooldown_until", 0.0) or 0.0)
        if not force and cooldown_until > now_ts:
            remaining = int(cooldown_until - now_ts)
            logger.debug(f"[说说监控] 主动执行后冷却中，剩余约 {remaining}s，跳过本轮")
            return "skip_cooldown"

        if not force and self._is_in_quiet_hours():
            logger.debug("[说说监控] 当前处于静默时间窗口，跳过本轮")
            return "skip_quiet_hours"

        if force:
            logger.info(f"[说说监控] 强制执行本轮（source={source}），已跳过静默/冷却检查")

        current_qq = await self.get_current_uin()
        if not current_qq:
            logger.warning("[说说监控] 无法获取 QQ 号，跳过检查")
            return "skip_no_qq"

        # 检查是否启用回复自己说说的评论
        monitor_cfg = getattr(self.config, "monitor", None)
        enable_reply_comments = getattr(monitor_cfg, "enable_auto_reply_comments", True)
        auto_comment_enabled = bool(self._monitor_config.get("auto_comment", False))
        if enable_reply_comments and auto_comment_enabled:
            await self._check_and_reply_own_feed_comments(current_qq)
        elif enable_reply_comments and not auto_comment_enabled:
            logger.debug("[评论回复] auto_comment 未开启，自动回复评论随互动开关一并关闭")

        logger.debug(f"[说说监控] 检查 QQ={current_qq} 的说说")
        result = await self.get_shuoshuo_list(current_qq, count=5)
        if not result.is_success or not result.data:
            logger.warning("[说说监控] 获取说说列表失败")
            return "skip_list_failed"

        latest_list = result.data
        if not latest_list:
            logger.debug("[说说监控] 说说列表为空")
            return "skip_empty_list"

        latest_item = latest_list[0]
        latest_tid = latest_item.get("tid")

        if self._last_tid is None:
            self._last_tid = latest_tid
            self._save_state()
            logger.info(f"[说说监控] 初始化完成，当前最新说说 TID: {latest_tid}")
            return "baseline_initialized"

        if latest_tid != self._last_tid:
            new_items = []
            for item in latest_list:
                if item.get("tid") == self._last_tid:
                    break
                new_items.append(item)

            if new_items:
                monitor_stats = {
                    "detected": len(new_items),
                    "notified": 0,
                    "like_success": 0,
                    "like_failed": 0,
                    "comment_success": 0,
                    "comment_failed": 0,
                }
                logger.info(f"[说说监控] 检测到 {len(new_items)} 条新说说")
                for item in reversed(new_items):
                    await self._notify_new_shuoshuo(item)
                    monitor_stats["notified"] += 1
                    # 自动评论（如果启用）
                    if self._monitor_config.get("auto_comment"):
                        comment_result = await self._auto_comment_if_enabled(item)
                        if comment_result is True:
                            monitor_stats["comment_success"] += 1
                        elif comment_result is False:
                            monitor_stats["comment_failed"] += 1
                    # 自动点赞（如果启用）
                    if self._monitor_config.get("auto_like"):
                        like_result = await self._auto_like_if_enabled(item)
                        if like_result is True:
                            monitor_stats["like_success"] += 1
                        elif like_result is False:
                            monitor_stats["like_failed"] += 1

                logger.info(
                    "[说说监控] 本轮完成: "
                    f"新动态{monitor_stats['detected']}条, 通知{monitor_stats['notified']}条, "
                    f"点赞成功{monitor_stats['like_success']}/失败{monitor_stats['like_failed']}, "
                    f"评论成功{monitor_stats['comment_success']}/失败{monitor_stats['comment_failed']}"
                )
                self._last_tid = latest_tid
                self._save_state()
                return "processed_new_items"
            else:
                logger.debug("[说说监控] 未发现增量说说")
                return "no_change"
        else:
            logger.debug("[说说监控] 最新说说未变化，无需处理")
            return "no_change"

    def _is_in_quiet_hours(self, now: datetime.datetime | None = None) -> bool:
        """判断当前是否处于静默时间窗口。"""
        monitor_cfg = getattr(self.config, "monitor", None) if getattr(self, "config", None) else None
        if not monitor_cfg:
            return False

        quiet_enabled = bool(getattr(monitor_cfg, "quiet_hours_enabled", True))

        if not quiet_enabled:
            return False

        current_dt = now or datetime.datetime.now()
        current_hour = current_dt.hour

        start_hour = int(getattr(monitor_cfg, "quiet_hours_start", 23) or 23)
        end_hour = int(getattr(monitor_cfg, "quiet_hours_end", 7) or 7)
        start_hour = max(0, min(start_hour, 23))
        end_hour = max(0, min(end_hour, 23))

        if start_hour == end_hour:
            # 相同视为不启用静默窗口
            return False

        if start_hour < end_hour:
            return start_hour <= current_hour < end_hour

        # 跨天窗口，如 23 -> 7
        return current_hour >= start_hour or current_hour < end_hour

    def _is_in_active_hours(self, now: datetime.datetime | None = None) -> bool:
        """兼容方法：返回“是否处于非静默时间窗口”。"""
        return not self._is_in_quiet_hours(now)

    def mark_manual_activity(self, source: str = "manual") -> None:
        """记录手动触发行为，并将监控计时重置为一个完整 interval 周期。"""
        if not self._monitor_running:
            return

        interval = int(self._monitor_config.get("interval", 300) or 300)
        interval = max(60, min(interval, 86400))
        self._monitor_cooldown_until = time.time() + interval
        logger.info(f"[说说监控] 检测到手动触发({source})，监控计时已重置（{interval}s）")

    async def _check_and_reply_own_feed_comments(self, qq_number: str) -> None:
        """检查自己说说的评论并回复"""
        self._log("info", "[评论回复]", f"开始检查 QQ={qq_number} 的说说评论...")

        # 获取自己最近的说说
        result = await self.get_shuoshuo_list(qq_number, count=5)
        if not result.is_success or not result.data:
            self._log("warning", "[评论回复]", f"获取说说列表失败: {result.error_message}")
            return

        for feed in result.data:
            tid = feed.get("tid")
            if not tid:
                continue

            content = feed.get("content", "")
            comments = feed.get("commentlist") or feed.get("comments", []) or []
            rt_con = feed.get("rt_con", {})
            if isinstance(rt_con, dict):
                rt_content = rt_con.get("content", "")
            else:
                rt_content = str(rt_con) if rt_con else ""

            pics = feed.get("pic", [])
            images = [p.get("url", p.get("big_url", "")) for p in pics if isinstance(p, dict)]

            await self._reply_to_comments(
                tid=tid,
                story_content=content or rt_content or "说说内容",
                comments=comments,
                images=images,
                qq_number=qq_number,
            )

    async def _reply_to_comments(
        self, tid: str, story_content: str, comments: list, images: list[str], qq_number: str
    ) -> None:
        """回复说说下的评论

        Args:
            tid: 说说 ID
            story_content: 说说内容
            comments: 评论列表
            images: 说说图片 URL 列表
            qq_number: 当前用户 QQ 号
        """
        if not comments:
            return

        for comment in comments:
            # 跳过自己的回复（如果是回复评论的回复）
            comment_uin = str(comment.get("uin", ""))
            if comment_uin == qq_number:
                continue

            comment_id = str(comment.get("id") or comment.get("cid") or "")
            if not comment_id:
                continue

            # 检查是否已回复过
            if self._has_replied_comment(tid, comment_id):
                continue

            nickname = comment.get("nickname", "网友")
            comment_content = comment.get("content", "")
            commenter_qq = str(comment.get("uin", "") or "").strip() or None
            comment_time = (
                str(comment.get("createTime2", "") or "").strip()
                or str(comment.get("create_time", "") or "").strip()
                or str(comment.get("time", "") or "").strip()
            )
            self._log("info", "[评论回复]", f"发现新评论: {nickname}: {comment_content}")

            # 概率控制：与自己说说相关，默认高概率回复
            monitor_cfg = getattr(self.config, "monitor", None)
            reply_probability = float(getattr(monitor_cfg, "auto_reply_probability", 0.9)) if monitor_cfg else 0.9
            reply_probability = max(0.0, min(1.0, reply_probability))
            if random.random() > reply_probability:
                self._log("debug", "[评论回复]", f"概率未命中，跳过回复 tid={tid}, comment_id={comment_id}, probability={reply_probability}")
                continue

            # 生成回复内容
            reply_text = await self._generate_comment_reply(
                story_content=story_content,
                comment_content=comment_content,
                commenter_name=nickname,
                commenter_qq=commenter_qq,
                images=images,
                story_time="",
                comment_time=comment_time,
            )

            if reply_text:
                # 回复前增加随机延迟，避免连续回复过快
                await self._random_human_delay(2.0, 6.0, "[评论回复]")
                # 回复评论（通过评论 API，传入 comment_id 表示回复这条评论）
                result = await self.comment_shuoshuo(
                    shuoshuo_id=tid,
                    content=reply_text,
                    qq_number=qq_number,
                    owner_qq=qq_number,
                    comment_id=comment_id,
                )

                if result.is_success:
                    self._mark_comment_replied(tid, comment_id)
                    self._log("info", "[评论回复]", f"回复成功: {reply_text}")
                else:
                    category = self._classify_failure_reason(result.error_message)
                    self._log("warning", "[评论回复]", f"回复失败[{category}]: {result.error_message}")

    async def _generate_comment_reply(
        self,
        story_content: str,
        comment_content: str,
        commenter_name: str,
        commenter_qq: str | None,
        images: list[str],
        story_time: str | None = None,
        comment_time: str | None = None,
    ) -> str | None:
        """生成评论回复内容（调用 AI）

        Args:
            story_content: 说说内容
            comment_content: 评论内容
            commenter_name: 评论者名称
            commenter_qq: 评论者QQ
            images: 说说图片 URL 列表
            story_time: 说说时间
            comment_time: 评论时间

        Returns:
            回复内容或 None
        """
        forbidden = self.DEFAULT_COMMENT_FORBIDDEN
        reply_system_prompt = self._get_builtin_system_prompt("reply_system_prompt")

        image_context = await self._build_image_context_block(images)
        prompt = self._build_full_reply_prompt(
            story_content=story_content,
            comment_content=comment_content,
            commenter_name=commenter_name,
            commenter_qq=commenter_qq,
            story_time=story_time,
            comment_time=comment_time,
            image_context=image_context,
            forbidden=forbidden,
        )
        if prompt:
            try:
                text = await self._generate_ai_comment_from_full_prompt(prompt, reply_system_prompt)
                if text:
                    return text
            except Exception as e:
                logger.error(f"[AI回复生成] 完整提示词调用异常: {e}")

        return None

    def _build_full_reply_prompt(
        self,
        story_content: str,
        comment_content: str,
        commenter_name: str,
        commenter_qq: str | None,
        story_time: str | None,
        comment_time: str | None,
        image_context: str,
        forbidden: str,
    ) -> str:
        """构建回复评论的上下文输入提示词（行为规则由系统提示词主导）。"""
        persona_text, style_text = self._get_persona_and_style_for_prompt()

        relation_hint = f"你与{commenter_name}是 QQ 空间好友关系，互动应保持自然、礼貌、不冒犯。"
        if commenter_qq:
            relation_hint += f"（对方QQ: {commenter_qq}）"

        timeline_lines = ["- 当前时间：实时对话阶段"]
        if story_time:
            timeline_lines.append(f"- 说说发布时间：{story_time}")
        if comment_time:
            timeline_lines.append(f"- 评论时间：{comment_time}")
        timeline_block = "\n".join(timeline_lines)

        template = f"""# 平台说明

QQ空间是中文社交平台，用户通过“说说”记录日常，好友会进行点赞、评论与回复互动。

# 人设定义

{persona_text}

# 语言风格

{style_text}

# 当前情景

{timeline_block}

- 关系提示：{relation_hint}

- 你的说说内容：{story_content}
- 评论者：{commenter_name}
- 对方评论：{comment_content}

{image_context if image_context else ""}

# 额外约束

{forbidden}

# 接下来你说

请直接生成一条自然、礼貌、有人味的回复正文，贴合当前说说和评论语义。

# 输出要求（最高优先级）

你的输出必须且只能是一条回复正文本身。

绝对禁止输出：
- 思考过程（如“我应该…/让我想想…”）
- 草稿或修改说明（如“版本1/修改后”）
- 字数统计、多版本备选
- 任何前后缀说明（如“回复内容：”）
- 换行符（回复需单行）

若无合适回复，请返回空字符串。"""

        return template

    async def _describe_image_with_vlm(self, image_url: str) -> str | None:
        """使用 VLM 识别图片语义描述。"""
        url = str(image_url).strip()
        if not url:
            return None

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
            if resp.status_code >= 400 or not resp.content:
                return None

            from src.app.plugin_system.api import media_api

            base64_data = base64.b64encode(resp.content).decode("utf-8")
            description = await media_api.recognize_media(
                base64_data=base64_data,
                media_type="image",
                use_cache=True,
            )
            if not description:
                return None

            text = str(description).strip()
            if not text:
                return None

            return text[:120]
        except Exception as e:
            self._log("debug", "[VLM识图]", f"识别失败: {e}")
            return None

    async def _build_image_context_block(self, images: list[str] | None) -> str:
        """构造图片上下文提示块（VLM 描述版）。"""
        if not images:
            return ""

        max_images = self.IMAGE_CONTEXT_MAX_IMAGES
        max_concurrency = self.IMAGE_CONTEXT_MAX_CONCURRENCY
        max_images = max(1, min(max_images, 9))
        max_concurrency = max(1, min(max_concurrency, 5))

        cleaned: list[str] = []
        for url in images:
            u = str(url).strip()
            if u and u not in cleaned:
                cleaned.append(u)
            if len(cleaned) >= max_images:
                break

        if not cleaned:
            return ""

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _describe_with_limit(image_url: str) -> str | None:
            async with semaphore:
                return await self._describe_image_with_vlm(image_url)

        descriptions = await asyncio.gather(*[_describe_with_limit(u) for u in cleaned], return_exceptions=False)

        lines: list[str] = []
        failed_count = 0
        for i, desc in enumerate(descriptions, start=1):
            if desc:
                lines.append(f"- 图片{i}语义：{desc}")
            else:
                failed_count += 1

        if failed_count > 0:
            self._log(
                "warning",
                "[VLM识图]",
                f"本次识图失败 {failed_count}/{len(cleaned)} 张，已使用可识别结果继续生成评论",
            )

        if not lines:
            return ""

        return "# 图片上下文（VLM识别结果）\n" + "\n".join(lines)

    def _get_personality_for_prompt(self) -> str:
        """从 core.toml 获取人设信息"""
        try:
            from src.core.config.core_config import core_config

            core_personality = getattr(core_config, "personality_core", "") or ""
            reply_style = getattr(core_config, "reply_style", "") or ""

            personality_parts = []
            if core_personality:
                personality_parts.append(f"# 你的核心人设\n{core_personality.strip()}")
            if reply_style:
                personality_parts.append(f"# 你的表达风格\n{reply_style.strip()}")

            return "\n\n".join(personality_parts) if personality_parts else ""
        except Exception:
            return ""

    def _get_persona_and_style_for_prompt(self) -> tuple[str, str]:
        """获取用于完整提示词的人设与风格文本。"""
        persona_text = "保持友善、真诚、自然，有基本同理心。"
        style_text = "口语化、简洁、有温度，像真实好友在聊天。"

        try:
            from src.core.config.core_config import core_config

            personality_core = str(getattr(core_config, "personality_core", "") or "").strip()
            personality_side = str(getattr(core_config, "personality_side", "") or "").strip()
            reply_style = str(getattr(core_config, "reply_style", "") or "").strip()

            if personality_core and personality_side:
                persona_text = f"{personality_core}，{personality_side}"
            elif personality_core:
                persona_text = personality_core
            elif personality_side:
                persona_text = personality_side

            if reply_style:
                style_text = reply_style
        except Exception:
            pass

        return persona_text, style_text

    async def _notify_new_shuoshuo(self, item: dict) -> None:
        """发送新说说通知"""
        # 使用动态监控配置
        target_group = self._monitor_config.get("target_group", "")
        target_user = self._monitor_config.get("target_user", "")

        if not target_group and not target_user:
            logger.debug("[说说通知] 未配置推送目标，跳过通知")
            return

        content = item.get("content", "")
        tid = item.get("tid")
        pic_list = item.get("pic", [])
        pic_count_text = f"\n[包含 {len(pic_list)} 张图片]" if pic_list else ""
        create_time = item.get("created_time") or item.get("createTime", "")

        time_str = str(create_time)
        try:
            import datetime
            if str(create_time).isdigit():
                time_str = datetime.datetime.fromtimestamp(int(create_time)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        msg = f"🔔【Qzone 新动态】\n----------------\n{content}{pic_count_text}\n----------------\nID: {tid}\n时间: {time_str}"

        logger.info(f"正在推送新说说: {tid}")
        try:
            from src.app.plugin_system.api import adapter_api

            adapter_sign = self.ADAPTER_SIGNATURE

            if target_group:
                await adapter_api.send_group_message(
                    adapter_sign=adapter_sign, group_id=target_group, message=msg
                )
            if target_user:
                await adapter_api.send_friend_message(
                    adapter_sign=adapter_sign, user_id=target_user, message=msg
                )
        except Exception as e:
            logger.error(f"推送新说说通知失败: {e}")

    async def _auto_like_if_enabled(self, item: dict) -> bool | None:
        """如果启用自动点赞，则按概率点赞说说

        Args:
            item: 说说数据项
        """
        tid = item.get("tid")
        if not tid:
            return None

        # 获取概率配置
        # 范围: 0.0 ~ 1.0 (0%=绝不, 100%=必定)
        monitor_cfg = getattr(self.config, "monitor", None)
        like_probability = self._monitor_config.get("like_probability")
        if like_probability is None and monitor_cfg:
            like_probability = getattr(monitor_cfg, "like_probability", 1.0)
        if like_probability is None:
            like_probability = 1.0
        # 确保概率在有效范围内
        like_probability = max(0.0, min(1.0, like_probability))

        # 概率判断
        import random
        if random.random() > like_probability:
            self._log("debug", "[自动点赞]", f"概率未命中，跳过点赞 tid={tid}, probability={like_probability}")
            return None

        self._log("info", "[自动点赞]", f"开始点赞说说 {tid}, probability={like_probability}")

        try:
            # 随机延迟，模拟人类操作节奏
            await self._random_human_delay(1.0, 4.0, "[自动点赞]")
            result = await self.like_shuoshuo(
                shuoshuo_id=tid,
                qq_number="",
                owner_qq=str(item.get("uin", "")) or None,
            )

            if result.is_success:
                self._log("info", "[自动点赞]", f"说说 {tid} 点赞成功")
                return True
            else:
                self._log("warning", "[自动点赞]", f"说说 {tid} 点赞失败: {result.error_message}")
                return False
        except Exception as e:
            self._log("error", "[自动点赞]", f"说说 {tid} 点赞异常: {e}")
            return False

    async def _auto_comment_if_enabled(self, item: dict) -> bool | None:
        """如果启用自动评论，则按概率评论说说

        Args:
            item: 说说数据项
        """
        # 使用动态配置
        if not self._monitor_config.get("auto_comment"):
            return None

        tid = item.get("tid")
        if not tid:
            return None

        # 检查是否已经评论过
        if self._is_commented(tid):
            self._log("debug", "[自动评论]", f"说说 {tid} 已评论过，跳过")
            return None

        # 获取概率配置
        # 范围: 0.0 ~ 1.0 (0%=绝不, 100%=必定)
        monitor_cfg = getattr(self.config, "monitor", None)
        comment_probability = self._monitor_config.get("comment_probability")
        if comment_probability is None and monitor_cfg:
            comment_probability = getattr(monitor_cfg, "comment_probability", 0.3)
        if comment_probability is None:
            comment_probability = 0.3
        # 确保概率在有效范围内
        comment_probability = max(0.0, min(1.0, comment_probability))

        # 概率判断
        import random
        if random.random() > comment_probability:
            self._log("debug", "[自动评论]", f"概率未命中，跳过评论 tid={tid}, probability={comment_probability}")
            return None

        # 获取说说内容用于生成评论
        content = item.get("content", "")
        nickname = item.get("nickname", "") or item.get("uin", "")

        # 生成评论内容
        pics = item.get("pic", [])
        images = [p.get("url", p.get("big_url", "")) for p in pics if isinstance(p, dict)]

        comment_text = await self._generate_comment_text(content, nickname, images)
        if not comment_text:
            self._log("warning", "[自动评论]", f"说说 {tid} 评论文本生成失败，按策略跳过（无模板兜底）")
            return None

        self._log("info", "[自动评论]", f"开始评论说说 {tid}, 内容: {comment_text}, probability={comment_probability}")

        # 执行评论
        await self._random_human_delay(1.5, 5.0, "[自动评论]")
        result = await self.comment_shuoshuo(
            shuoshuo_id=tid,
            content=comment_text,
            qq_number="",
            owner_qq=str(item.get("uin", "")) or None,
        )

        if result.is_success:
            self._log("info", "[自动评论]", f"说说 {tid} 评论成功")
            return True
        else:
            self._log("warning", "[自动评论]", f"说说 {tid} 评论失败: {result.error_message}")
            return False

    async def _generate_comment_text(self, content: str, nickname: str, images: list[str] | None = None) -> str:
        """生成评论文本

        优先使用 AI 生成（可配置完整情景提示词），否则使用模板。

        Args:
            content: 说说内容
            nickname: 作者昵称
            images: 说说图片 URL 列表

        Returns:
            生成的评论文本
        """
        system_prompt = self._get_comment_system_prompt()

        try:
            image_context = await self._build_image_context_block(images)
            full_prompt = self._build_full_comment_prompt(content, nickname, image_context)
            if full_prompt:
                ai_comment = await self._generate_ai_comment_from_full_prompt(full_prompt, system_prompt)
                if ai_comment:
                    self._log("debug", "[AI评论]", f"AI生成评论成功: {ai_comment}")
                    return ai_comment
        except Exception as e:
            self._log("warning", "[AI评论]", f"完整提示词生成失败: {e}")

        # 禁止模板兜底：模型不可用时直接返回 None，让调用方判定失败/跳过。
        self._log("warning", "[AI评论]", "模型未生成有效评论，按策略不使用模板兜底，已跳过")
        return None

    def _build_full_comment_prompt(self, content: str, nickname: str, image_context: str = "") -> str:
        """构建评论的上下文输入提示词（行为规则由系统提示词主导）。"""
        forbidden = self.DEFAULT_COMMENT_FORBIDDEN

        persona_text, style_text = self._get_persona_and_style_for_prompt()
        relation_hint = f"你与{nickname}是 QQ 空间好友关系，互动应保持自然、友善、不冒犯。"

        # 获取当前时间
        import datetime
        current_time = datetime.datetime.now().strftime("%m月%d日 %H:%M")
        prompt = f"""# 平台说明

QQ空间是中文社交平台，用户通过“说说”记录生活，好友可以点赞、评论和回复。

# 人设定义

{persona_text}

# 语言风格

{style_text}

# 当前情景

- 时间：{current_time}
- 场景：你正在浏览 QQ 空间好友动态并准备互动
- 目标对象：{nickname}
- 关系提示：{relation_hint}
- 对方说说内容：{content[:500] if content else "[无文字内容]"}

{image_context if image_context else ""}

# 行为规范

1. 优先贴合语境与上下文，像真人自然互动。
2. 不说教、不端着，不编造输入外事实。
3. 允许口语化，但避免攻击性、敏感或冒犯表达。

# 额外约束

{forbidden}

# 接下来你说

请直接说一句自然、得体、有互动感的评论正文。

# 输出要求（最高优先级）

你的输出必须且只能是一条评论正文本身。
单行输出，不超过35字，禁止出现 @。

绝对禁止输出：
- 思考过程（如“我应该…/让我想想…”）
- 草稿或修改说明（如“版本1/修改后”）
- 字数统计、多版本备选
- 任何前后缀说明（如“评论内容：”）
- 换行符（评论需单行）

若不适合评论，请返回空字符串。"""

        return prompt

    def _get_comment_system_prompt(self) -> str:
        """获取评论系统提示词（固定内置策略）。"""
        return self._get_builtin_system_prompt("comment_system_prompt")

    def _get_builtin_system_prompt(self, base_field: str) -> str:
        """获取内置系统提示词（当配置未填写时使用）。"""
        if base_field == "comment_system_prompt":
            return self.DEFAULT_COMMENT_SYSTEM_PROMPT
        if base_field == "reply_system_prompt":
            return self.DEFAULT_REPLY_SYSTEM_PROMPT
        if base_field == "publish_system_prompt":
            return self.DEFAULT_PUBLISH_SYSTEM_PROMPT
        return ""

    async def _rewrite_publish_content_with_persona(self, content: str) -> str:
        """发布说说前按人设/风格进行改写。"""
        raw_text = str(content or "").strip()
        if not raw_text:
            return ""

        persona_text, style_text = self._get_persona_and_style_for_prompt()
        system_prompt = self._get_builtin_system_prompt("publish_system_prompt")
        history_block = self._build_publish_history_block(limit=5)
        history_text = f"\n{history_block}\n" if history_block else ""
        user_prompt = (
            "请基于以下信息将原始内容改写成一条适合发布到 QQ 空间的说说正文。\n\n"
            f"人设：{persona_text}\n"
            f"风格：{style_text}\n"
            f"{history_text}"
            f"原始内容：{raw_text}\n\n"
            "输出要求：\n"
            "- 仅输出最终说说正文\n"
            "- 不要解释、不要前缀\n"
            "- 保留原意，不编造事实\n"
            "- 避免与最近发布内容语义重复"
        )

        try:
            text = await self._generate_ai_comment_from_full_prompt(user_prompt, system_prompt)
            if text:
                self._log("debug", "[发布改写]", "发布前内容改写成功")
                return text
        except Exception as e:
            self._log("warning", "[发布改写]", f"发布前改写失败，回退原文: {e}")

        return raw_text

    async def _generate_ai_comment_from_full_prompt(self, full_prompt: str, system_prompt: str) -> str | None:
        """使用完整提示词生成评论

        Args:
            full_prompt: 完整的提示词
            system_prompt: 系统提示词（规范行为主约束）

        Returns:
            生成的评论文本，失败返回 None
        """
        try:
            from src.app.plugin_system.api.llm_api import get_model_set_by_task
            from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text

            self._log("debug", "[AI评论]", "请求AI生成评论（完整提示词）...")
            model_set = get_model_set_by_task("actor")

            # 调用 LLM
            llm_request = LLMRequest(model_set=model_set)
            if system_prompt:
                llm_request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
            llm_request.add_payload(LLMPayload(ROLE.USER, Text(full_prompt)))

            response = await llm_request.send(stream=False)
            comment = str(getattr(response, "message", "") or "").strip()
            if comment:
                # 清理可能的引号包裹
                if comment.startswith('"') and comment.endswith('"'):
                    comment = comment[1:-1]
                if comment.startswith("'") and comment.endswith("'"):
                    comment = comment[1:-1]
                return comment
        except Exception as e:
            self._log("warning", "[AI评论]", f"LLM调用失败: {e}")
        return None

    async def _generate_ai_comment(
        self,
        system_prompt: str,
        user_template: str,
        content: str,
        nickname: str,
    ) -> str | None:
        """使用 AI 生成评论

        Args:
            system_prompt: 系统提示词
            user_template: 用户提示词模板
            content: 说说内容
            nickname: 作者昵称

        Returns:
            生成的评论文本，失败返回 None
        """
        try:
            from src.app.plugin_system.api.llm_api import get_model_set_by_task
            from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text

            # 构建用户提示词
            user_prompt = user_template.replace("{content}", content[:200])
            user_prompt = user_prompt.replace("{nickname}", nickname)

            self._log("debug", "[AI评论]", f"请求AI生成评论, 内容: {content[:50]}...")
            model_set = get_model_set_by_task("actor")

            # 调用 LLM
            llm_request = LLMRequest(model_set=model_set)
            llm_request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
            llm_request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

            response = await llm_request.send(stream=False)
            text = str(getattr(response, "message", "") or "").strip()
            if text:
                return text
        except Exception as e:
            self._log("warning", "[AI评论]", f"LLM调用失败: {e}")
        return None

    async def get_monitor_status(self) -> dict[str, Any]:
        """获取监控状态"""
        # 获取默认概率配置
        monitor_cfg = getattr(self.config, "monitor", None)
        monitor_enabled = bool(getattr(monitor_cfg, "enabled", True)) if monitor_cfg else True
        default_interval = int(getattr(monitor_cfg, "default_interval", 300)) if monitor_cfg else 300
        default_interval = max(60, min(default_interval, 86400))
        like_prob = self._monitor_config.get("like_probability")
        if like_prob is None and monitor_cfg:
            like_prob = getattr(monitor_cfg, "like_probability", 1.0)
        comment_prob = self._monitor_config.get("comment_probability")
        if comment_prob is None and monitor_cfg:
            comment_prob = getattr(monitor_cfg, "comment_probability", 0.3)

        quiet_enabled = bool(getattr(monitor_cfg, "quiet_hours_enabled", True)) if monitor_cfg else True
        quiet_start = int(getattr(monitor_cfg, "quiet_hours_start", 23) or 23) if monitor_cfg else 23
        quiet_end = int(getattr(monitor_cfg, "quiet_hours_end", 7) or 7) if monitor_cfg else 7
        quiet_start = max(0, min(quiet_start, 23))
        quiet_end = max(0, min(quiet_end, 23))

        now_ts = time.time()
        cooldown_until = float(getattr(self, "_monitor_cooldown_until", 0.0) or 0.0)
        cooldown_remaining = max(0, int(cooldown_until - now_ts))
        baseline_tid = str(getattr(self, "_last_tid", "") or "").strip()
        last_run_at = float(getattr(self, "_last_monitor_run_at", 0.0) or 0.0)

        startup_retry_active = bool(getattr(self, "_startup_retry_active", False))
        startup_retry_attempt = int(getattr(self, "_startup_retry_attempt", 0) or 0)
        startup_retry_max = int(getattr(self, "_startup_retry_max_attempts", 0) or 0)
        startup_retry_interval = int(getattr(self, "_startup_retry_interval", 0) or 0)

        return {
            "is_running": self._monitor_running,
            "enabled": monitor_enabled,
            "default_interval": default_interval,
            "interval": self._monitor_config.get("interval", default_interval),
            "target_group": self._monitor_config.get("target_group", ""),
            "target_user": self._monitor_config.get("target_user", ""),
            "auto_comment": self._monitor_config.get("auto_comment", False),
            "auto_like": self._monitor_config.get("auto_like", False),
            "like_probability": like_prob or 1.0,
            "comment_probability": comment_prob or 0.3,
            "quiet_hours_enabled": quiet_enabled,
            "quiet_window": f"{quiet_start:02d}:00-{quiet_end:02d}:00",
            "in_quiet_hours": self._is_in_quiet_hours(),
            "cooldown_remaining_seconds": cooldown_remaining,
            "baseline_initialized": bool(baseline_tid),
            "last_tid": baseline_tid,
            "last_run_at": int(last_run_at) if last_run_at > 0 else 0,
            "last_run_source": str(getattr(self, "_last_monitor_source", "") or ""),
            "last_run_force": bool(getattr(self, "_last_monitor_force", False)),
            "last_run_result": str(getattr(self, "_last_monitor_result", "never") or "never"),
            "last_run_skip_reason": str(getattr(self, "_last_monitor_skip_reason", "") or ""),
            "last_run_error": str(getattr(self, "_last_monitor_error", "") or ""),
            "startup_retry_active": startup_retry_active,
            "startup_retry_attempt": startup_retry_attempt,
            "startup_retry_max_attempts": startup_retry_max,
            "startup_retry_remaining_attempts": max(startup_retry_max - startup_retry_attempt, 0),
            "startup_retry_interval": startup_retry_interval,
            "startup_retry_last_reason": str(getattr(self, "_startup_retry_last_reason", "") or ""),
        }

    async def _stop_startup_retry(self) -> None:
        """停止启动连接就绪重试任务。"""
        if not bool(getattr(self, "_startup_retry_active", False)) and not str(getattr(self, "_startup_retry_job_name", "") or ""):
            return

        try:
            from src.kernel.scheduler import get_unified_scheduler

            scheduler = get_unified_scheduler()
            job_name = str(getattr(self, "_startup_retry_job_name", "") or "")
            if job_name:
                if hasattr(scheduler, "remove_job"):
                    await scheduler.remove_job(job_name)
                elif hasattr(scheduler, "remove_schedule_by_name"):
                    await scheduler.remove_schedule_by_name(job_name)
        except Exception:
            pass
        finally:
            self._startup_retry_active = False
            self._startup_retry_job_name = ""

    async def _schedule_startup_retry(self, *, max_attempts: int, interval_seconds: int, reason: str) -> None:
        """为启动首轮安排连接就绪重试。"""
        from src.kernel.scheduler import get_unified_scheduler, TriggerType

        await self._stop_startup_retry()

        if max_attempts <= 0:
            return

        scheduler = get_unified_scheduler()
        retry_interval = max(5, min(interval_seconds, 300))
        retry_job_name = f"qzone_startup_retry_{id(self)}"

        if hasattr(scheduler, "add_job"):
            interval_trigger = getattr(TriggerType, "INTERVAL", None) or TriggerType.TIME
            await scheduler.add_job(
                func=self._run_startup_retry_tick,
                trigger=interval_trigger,
                seconds=retry_interval,
                id=retry_job_name,
                replace_existing=True,
            )
        elif hasattr(scheduler, "create_schedule"):
            await scheduler.create_schedule(
                callback=self._run_startup_retry_tick,
                trigger_type=TriggerType.TIME,
                trigger_config={"interval_seconds": retry_interval},
                is_recurring=True,
                task_name=retry_job_name,
                force_overwrite=True,
            )
        else:
            raise RuntimeError("当前调度器不支持 add_job/create_schedule 接口")

        self._startup_retry_active = True
        self._startup_retry_attempt = 0
        self._startup_retry_max_attempts = int(max_attempts)
        self._startup_retry_interval = int(retry_interval)
        self._startup_retry_job_name = retry_job_name
        self._startup_retry_last_reason = str(reason or "")

        logger.info(
            f"[自动监控] 已启用连接就绪重试：每 {retry_interval}s 一次，最多 {max_attempts} 次，原因={reason}"
        )

    async def _run_startup_retry_tick(self) -> None:
        """启动首轮连接就绪重试轮询。"""
        if not self._monitor_running:
            await self._stop_startup_retry()
            return

        if not self._startup_retry_active:
            return

        self._startup_retry_attempt += 1
        current_attempt = self._startup_retry_attempt
        max_attempts = max(int(self._startup_retry_max_attempts or 0), 0)

        result_code = await self._run_auto_monitor(force=True, source="startup_retry")

        # 成功拿到连接并执行过真实检查后，关闭重试
        if result_code != "skip_no_qq":
            logger.info(f"[自动监控] 启动连接重试成功（第 {current_attempt}/{max_attempts} 次）")
            await self._stop_startup_retry()
            return

        if current_attempt >= max_attempts:
            self._startup_retry_last_reason = "max_attempts_reached"
            logger.warning("[自动监控] 启动连接重试已达上限，停止重试")
            await self._stop_startup_retry()

    async def start_monitor(self, config: dict[str, Any]) -> dict[str, Any]:
        """启动自动监控

        Args:
            config: 监控配置
                - interval: 监控间隔（秒）
                - target_group: 推送群号
                - target_user: 推送QQ号
                - auto_comment: 是否自动评论
                - auto_like: 是否自动点赞
                - like_probability: 点赞概率 (0.0-1.0)
                - comment_probability: 评论概率 (0.0-1.0)
                - auto_reply_probability: 回复自己说说评论的概率 (0.0-1.0)
        """
        try:
            from src.kernel.scheduler import get_unified_scheduler, TriggerType

            # 兼容测试中使用 object.__new__ 构造实例导致的属性缺失
            self._startup_retry_active = bool(getattr(self, "_startup_retry_active", False))
            self._startup_retry_attempt = int(getattr(self, "_startup_retry_attempt", 0) or 0)
            self._startup_retry_max_attempts = int(getattr(self, "_startup_retry_max_attempts", 0) or 0)
            self._startup_retry_interval = int(getattr(self, "_startup_retry_interval", 0) or 0)
            self._startup_retry_job_name = str(getattr(self, "_startup_retry_job_name", "") or "")
            self._startup_retry_last_reason = str(getattr(self, "_startup_retry_last_reason", "") or "")

            self._last_monitor_run_at = float(getattr(self, "_last_monitor_run_at", 0.0) or 0.0)
            self._last_monitor_source = str(getattr(self, "_last_monitor_source", "") or "")
            self._last_monitor_force = bool(getattr(self, "_last_monitor_force", False))
            self._last_monitor_result = str(getattr(self, "_last_monitor_result", "never") or "never")
            self._last_monitor_error = str(getattr(self, "_last_monitor_error", "") or "")
            self._last_monitor_skip_reason = str(getattr(self, "_last_monitor_skip_reason", "") or "")

            if not self._is_monitor_enabled():
                logger.warning("[自动监控] 启动被拒绝：监控总开关已关闭")
                return {
                    "success": False,
                    "message": "监控总开关已关闭，请在 config.toml 中将 [monitor].enabled 设为 true",
                }

            # 保存配置
            self._monitor_config = config.copy()

            # 获取间隔
            monitor_cfg = getattr(self.config, "monitor", None)
            default_interval = int(getattr(monitor_cfg, "default_interval", 300)) if monitor_cfg else 300
            interval = config.get("interval", default_interval)
            interval = max(60, min(interval, 86400))  # 限制 60 秒 ~ 24 小时
            self._monitor_config["interval"] = interval

            # 启动监控时清空冷却，避免历史手动触发影响新会话
            self._monitor_cooldown_until = 0.0

            # 启动时重置运行态摘要
            self._last_monitor_run_at = 0.0
            self._last_monitor_source = ""
            self._last_monitor_force = False
            self._last_monitor_result = "never"
            self._last_monitor_error = ""
            self._last_monitor_skip_reason = ""

            scheduler = get_unified_scheduler()
            job_name = f"qzone_auto_monitor_{id(self)}"

            # 如果已经在运行，先移除
            if self._monitor_running:
                try:
                    if hasattr(scheduler, "remove_job"):
                        await scheduler.remove_job(job_name)
                    elif hasattr(scheduler, "remove_schedule_by_name"):
                        await scheduler.remove_schedule_by_name(job_name)
                except Exception:
                    pass

            # 添加监控任务
            if hasattr(scheduler, "add_job"):
                interval_trigger = getattr(TriggerType, "INTERVAL", None) or TriggerType.TIME
                job_id = await scheduler.add_job(
                    func=self._run_auto_monitor,
                    trigger=interval_trigger,
                    seconds=interval,
                    id=job_name,
                    replace_existing=True,
                )
            elif hasattr(scheduler, "create_schedule"):
                job_id = await scheduler.create_schedule(
                    callback=self._run_auto_monitor,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": interval},
                    is_recurring=True,
                    task_name=job_name,
                    force_overwrite=True,
                )
            else:
                raise RuntimeError("当前调度器不支持 add_job/create_schedule 接口")

            self._monitor_running = True
            logger.info(f"[自动监控] 已启动，间隔 {interval} 秒")

            # 启动后立即执行一轮（最高优先级）：
            # - 已在启动流程中重置冷却计时
            # - 首轮强制执行，跳过静默/冷却检查
            # - 已读/基线机制仍保留，避免历史刷屏
            try:
                startup_result = await self._run_auto_monitor(force=True, source="startup_immediate")
                if startup_result == "skip_no_qq":
                    retry_max_attempts = int(config.get("startup_retry_max_attempts", 6) or 6)
                    retry_interval = int(config.get("startup_retry_interval", 10) or 10)
                    retry_max_attempts = max(1, min(retry_max_attempts, 60))
                    retry_interval = max(5, min(retry_interval, 300))
                    await self._schedule_startup_retry(
                        max_attempts=retry_max_attempts,
                        interval_seconds=retry_interval,
                        reason="startup_no_qq",
                    )
            except Exception as immediate_err:
                logger.warning(f"[自动监控] 启动即执行首轮失败（已忽略，不影响后续定时监控）: {immediate_err}")

            return {
                "success": True,
                "message": f"监控已启动，间隔 {interval} 秒",
                "job_id": job_id,
                "startup_retry_active": bool(getattr(self, "_startup_retry_active", False)),
                "startup_retry_attempt": int(getattr(self, "_startup_retry_attempt", 0) or 0),
                "startup_retry_max_attempts": int(getattr(self, "_startup_retry_max_attempts", 0) or 0),
                "startup_retry_interval": int(getattr(self, "_startup_retry_interval", 0) or 0),
            }
        except Exception as e:
            logger.error(f"[自动监控] 启动失败: {e}")
            return {"success": False, "message": str(e)}

    async def stop_monitor(self) -> dict[str, Any]:
        """停止自动监控"""
        try:
            from src.kernel.scheduler import get_unified_scheduler

            # 兼容测试中使用 object.__new__ 构造实例导致的属性缺失
            self._monitor_running = bool(getattr(self, "_monitor_running", False))
            self._monitor_cooldown_until = float(getattr(self, "_monitor_cooldown_until", 0.0) or 0.0)
            self._startup_retry_active = bool(getattr(self, "_startup_retry_active", False))
            self._startup_retry_job_name = str(getattr(self, "_startup_retry_job_name", "") or "")

            scheduler = get_unified_scheduler()
            try:
                job_name = f"qzone_auto_monitor_{id(self)}"
                if hasattr(scheduler, "remove_job"):
                    await scheduler.remove_job(job_name)
                elif hasattr(scheduler, "remove_schedule_by_name"):
                    await scheduler.remove_schedule_by_name(job_name)
            except Exception:
                pass

            self._monitor_running = False
            self._monitor_cooldown_until = 0.0
            await self._stop_startup_retry()
            logger.info("[自动监控] 已停止")
            return {"success": True, "message": "监控已停止"}
        except Exception as e:
            logger.error(f"[自动监控] 停止失败: {e}")
            return {"success": False, "message": str(e)}

    async def _run_auto_monitor(self, *, force: bool = False, source: str = "scheduled") -> str:
        """执行自动监控任务。"""
        logger.debug(f"[自动监控] 开始检查新说说(force={force}, source={source})")
        self._last_monitor_run_at = time.time()
        self._last_monitor_source = source
        self._last_monitor_force = force
        self._last_monitor_error = ""
        self._last_monitor_skip_reason = ""

        try:
            result_code = await self.check_new_shuoshuo(force=force, source=source)
            normalized = str(result_code or "unknown")
            if normalized.startswith("skip_"):
                self._last_monitor_result = "skipped"
                self._last_monitor_skip_reason = normalized
            else:
                self._last_monitor_result = "ok"
            return normalized
        except Exception as e:
            self._last_monitor_result = "error"
            self._last_monitor_error = str(e)
            logger.error(f"[自动监控] 本轮执行异常(source={source}): {e}")
            return "error"

    def _is_monitor_enabled(self) -> bool:
        """监控总开关是否开启。"""
        monitor_cfg = getattr(self.config, "monitor", None) if getattr(self, "config", None) else None
        if not monitor_cfg:
            return True
        return bool(getattr(monitor_cfg, "enabled", True))

    async def close(self) -> None:
        """清理资源"""
        # 停止监控
        if self._monitor_running:
            await self.stop_monitor()
        pass
