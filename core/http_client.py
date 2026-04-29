"""Qzone HTTP 客户端工具模块。

提供 QQ 空间 API 调用所需的 HTTP 客户端构建、GTK 计算、
退避重试、响应解析、Cookie 刷新等底层能力。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, TYPE_CHECKING

import httpx

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from .cookie_manager import CookieManager
    from .state_manager import StateManager

logger = get_logger("qzone_shuoshuo")

# QQ 空间 API 端点
EMOTION_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
ADAPTER_SIGNATURE = "napcat_adapter:adapter:napcat_adapter"


def compute_gtk(p_skey: str) -> str:
    """根据 p_skey 计算 GTK 值。"""
    if not p_skey:
        return ""
    hash_val = 5381
    for char in p_skey:
        hash_val += (hash_val << 5) + ord(char)
    return str(hash_val & 2147483647)


def normalize_callback_payload(response_text: str) -> str:
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


def classify_failure_reason(message: str | None) -> str:
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


class QzoneHttpClient:
    """QQ 空间 HTTP 客户端封装。

    负责：
    - 构建带 Cookie 的 httpx.AsyncClient
    - Cookie 失效时自动刷新
    - POST 请求退避重试
    - 人类化随机延迟
    """

    def __init__(
        self,
        cookie_manager: "CookieManager",
        state: "StateManager",
    ) -> None:
        self._cookie_manager = cookie_manager
        self._state = state

        # Cookie 二次确认统计
        self._cookie_confirm_total: int = 0
        self._cookie_confirm_recovered: int = 0
        self._cookie_confirm_refresh: int = 0

    # ---- 客户端构建 ----

    async def get_client(self, qq: str) -> tuple[httpx.AsyncClient, str, str] | None:
        """为指定 QQ 号构建带 Cookie 的 HTTP 客户端。

        Returns:
            (client, uin, gtk) 或 None
        """
        cookies = await self._cookie_manager.get_cookies(qq, ADAPTER_SIGNATURE)
        if not cookies:
            logger.error(f"无法获取 QQ:{qq} 的 Cookie")
            return None

        p_skey = cookies.get("p_skey") or cookies.get("P_skey")
        if not p_skey:
            logger.warning(f"QQ:{qq} Cookie 中缺少 p_skey")
            return None

        uin_cookie = cookies.get("uin") or cookies.get("ptui_loginuin") or qq
        uin = uin_cookie.lstrip("o")
        gtk = compute_gtk(p_skey)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://user.qzone.qq.com/{uin}",
            "Origin": "https://user.qzone.qq.com",
        }

        client = httpx.AsyncClient(cookies=cookies, headers=headers, timeout=30.0)
        return client, uin, gtk

    async def refresh_cookie_and_get_client(
        self, qq_number: str, operation: str
    ) -> tuple[httpx.AsyncClient, str, str] | None:
        """Cookie 失效时自动刷新并返回新客户端。"""
        new_cookies = await self._cookie_manager.refresh_cookies(qq_number, ADAPTER_SIGNATURE)
        if new_cookies:
            logger.info(f"[{operation}] Cookie 刷新成功，重新获取客户端...")
            return await self.get_client(qq_number)
        logger.error(f"[{operation}] Cookie 刷新失败")
        return None

    # ---- 请求工具 ----

    @staticmethod
    async def random_human_delay(min_seconds: float, max_seconds: float, tag: str) -> None:
        """人类化随机延迟，降低风控命中概率。"""
        delay = random.uniform(min_seconds, max_seconds)
        logger.debug(f"{tag} 随机等待 {delay:.2f}s")
        await asyncio.sleep(delay)

    @staticmethod
    async def post_with_backoff(
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

    # ---- Cookie 统计 ----

    def bump_cookie_confirm_stats(self, event: str) -> None:
        """更新并输出评论空响应二次确认统计。"""
        total = self._cookie_confirm_total
        recovered = self._cookie_confirm_recovered
        refresh = self._cookie_confirm_refresh

        total += 1
        if event == "recovered":
            recovered += 1
        elif event == "refresh":
            refresh += 1

        self._cookie_confirm_total = total
        self._cookie_confirm_recovered = recovered
        self._cookie_confirm_refresh = refresh

        recovered_rate = (recovered / total * 100.0) if total > 0 else 0.0
        logger.debug(
            f"[Cookie判定] 二次确认统计 total={total}, recovered={recovered}, "
            f"refresh={refresh}, recovered_rate={recovered_rate:.1f}%"
        )