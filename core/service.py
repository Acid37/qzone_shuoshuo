"""Qzone 核心服务层（门面）。

将具体实现委托给功能子模块，本文件仅做组装与对外暴露。
保持所有公开 API 签名不变，确保 Action / Command / EventHandler 无感知迁移。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from src.core.components.base import BaseService
from src.app.plugin_system.api.log_api import get_logger

from .types import Result
from .state_manager import StateManager
from .http_client import QzoneHttpClient, ADAPTER_SIGNATURE
from .ai_prompts import AIPromptBuilder
from .feed_ops import FeedOperations
from .interaction import InteractionOps
from .monitor import MonitorScheduler

if TYPE_CHECKING:
    from ..plugin import QzoneShuoshuoPlugin
    from ..config import QzoneConfig

logger = get_logger("qzone_shuoshuo")


class QzoneService(BaseService):
    """QQ空间服务类（门面）

    所有业务逻辑已拆分至 core/ 子模块：
    - StateManager: 状态持久化
    - QzoneHttpClient: HTTP 客户端
    - FeedOperations: 说说 CRUD
    - InteractionOps: 点赞/评论/回复
    - MonitorScheduler: 监控调度
    - AIPromptBuilder: AI 提示词
    """

    service_name = "qzone"
    service_description = "QQ空间说说核心服务"
    version = "1.5.0"

    # 保留类常量以兼容外部直接引用
    ADAPTER_SIGNATURE = ADAPTER_SIGNATURE

    def __init__(self, plugin: "QzoneShuoshuoPlugin") -> None:
        self.plugin = plugin
        self.config: QzoneConfig = getattr(plugin, "config", None)  # type: ignore
        from ..core.cookie_manager import CookieManager
        self.cookie_manager = CookieManager(self._data_dir())

        # 初始化子模块
        self._state = StateManager(self._data_dir())
        self._http = QzoneHttpClient(self.cookie_manager, self._state)
        self._prompts = AIPromptBuilder(self._state)
        self._feeds = FeedOperations(
            self._http, self._state, self._prompts, self._get_qq_from_napcat,
        )
        self._interact = InteractionOps(
            self._http, self._state, self._prompts, self._feeds,
            self._get_qq_from_napcat, self._get_monitor_config,
        )
        self._monitor = MonitorScheduler(
            self._http, self._state, self._feeds, self._interact,
            self._get_config, self._get_qq_from_napcat,
        )

        logger.info("QzoneService 初始化完成")

    # ---- 配置访问辅助 ----

    def _data_dir(self) -> Path:
        """获取插件数据目录。"""
        storage_cfg = getattr(self.config, "storage", None) if self.config else None
        data_dir_str = (
            getattr(storage_cfg, "data_dir", "data/qzone_shuoshuo")
            if storage_cfg else "data/qzone_shuoshuo"
        )
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
        """根据日志级别输出日志"""
        if level == "debug":
            logger.debug(f"{tag} {msg}")
        elif level == "info":
            logger.info(f"{tag} {msg}")
        elif level == "warning":
            logger.warning(f"{tag} {msg}")
        else:
            logger.error(f"{tag} {msg}")

    def _get_config(self):
        """获取 monitor 配置节。"""
        return getattr(self.config, "monitor", None) if self.config else None

    def _get_monitor_config(self) -> dict[str, Any]:
        """获取监控运行配置字典。"""
        return self._monitor.config

    # ---- NapCat 适配器 ----

    async def _get_qq_from_napcat(self) -> str | None:
        """从 NapCat 适配器自动获取 Bot 的 QQ 号。"""
        try:
            from src.app.plugin_system.api import adapter_api

            adapter_signature = ADAPTER_SIGNATURE

            if not adapter_api.is_adapter_active(adapter_signature):
                self._log("debug", "[NapCat探测]", "适配器未启动")
                return None

            adapter = adapter_api.get_adapter(adapter_signature)
            if not adapter:
                self._log("warning", "[NapCat探测]", "无法获取适配器实例")
                return None

            if hasattr(adapter, "send_napcat_api"):
                try:
                    res = await adapter.send_napcat_api("get_login_info", {})
                    if res and res.get("status") == "ok":
                        qq_id = res.get("data", {}).get("user_id")
                        if qq_id:
                            logger.info(f"[NapCat探测] 连接验证成功，获取到 QQ: {qq_id}")
                            return str(qq_id)
                except Exception as probe_err:
                    self._log("debug", "[NapCat探测]", f"主动探测失败，回退到缓存读取: {probe_err}")
            else:
                self._log("debug", "[NapCat探测]", "当前适配器不支持实时探测，已回退到缓存模式")

            bot_info = await adapter.get_bot_info()
            if bot_info:
                qq_id = bot_info.get("bot_id")
                if qq_id:
                    logger.info(f"[NapCat缓存] 读取到缓存 QQ: {qq_id}")
                    return str(qq_id)

            logger.warning("[NapCat探测] 未能获取 QQ 号信息")
        except Exception as e:
            logger.error(f"[NapCat探测] 异常: {e}")
        return None

    # ====================================================================
    #  公开 API（委托给子模块）
    # ====================================================================

    # ---- 说说 CRUD ----

    async def publish_shuoshuo(
        self, qq_number: str = "", content: str = "",
        images: list[bytes] | None = None, visible: str = "all",
    ) -> "Result[dict]":
        """发布说说"""
        return await self._feeds.publish(
            qq_number=qq_number, content=content,
            images=images, visible=visible,
        )

    async def get_shuoshuo_list(
        self, qq_number: str = "", count: int = 20,
    ) -> "Result[list[dict]]":
        """获取说说列表"""
        return await self._feeds.get_list(qq_number=qq_number, count=count)

    async def delete_shuoshuo(
        self, shuoshuo_id: str, qq_number: str = "",
    ) -> "Result[str]":
        """删除说说"""
        return await self._feeds.delete(shuoshuo_id, qq_number=qq_number)

    async def get_shuoshuo_detail(
        self, shuoshuo_id: str, qq_number: str = "",
    ) -> "Result[dict]":
        """获取说说详情"""
        return await self._feeds.get_detail(shuoshuo_id, qq_number=qq_number)

    # ---- 互动操作 ----

    async def like_shuoshuo(
        self, shuoshuo_id: str, qq_number: str = "",
        owner_qq: str | None = None,
    ) -> "Result[str]":
        """点赞说说"""
        return await self._interact.like(
            shuoshuo_id, qq_number=qq_number, owner_qq=owner_qq,
        )

    async def comment_shuoshuo(
        self, shuoshuo_id: str, content: str, qq_number: str = "",
        owner_qq: str | None = None, comment_id: str | None = None,
        parent_tid: str | None = None,
    ) -> "Result[dict]":
        """评论说说（支持盖楼式多级回复）"""
        return await self._interact.comment(
            shuoshuo_id, content, qq_number=qq_number,
            owner_qq=owner_qq, comment_id=comment_id,
            parent_tid=parent_tid,
        )

    # ---- 监控调度 ----

    async def start_monitor(self, config: dict[str, Any]) -> dict[str, Any]:
        """启动自动监控"""
        return await self._monitor.start(config)

    async def stop_monitor(self) -> dict[str, Any]:
        """停止自动监控"""
        return await self._monitor.stop()

    async def check_new_shuoshuo(
        self, *, force: bool = False, source: str = "scheduled",
    ) -> str:
        """检查并广播新说说"""
        return await self._monitor.check_new_shuoshuo(
            force=force, source=source,
        )

    async def get_monitor_status(self) -> dict[str, Any]:
        """获取监控状态"""
        return await self._monitor.get_status()

    def mark_manual_activity(self, source: str = "manual") -> None:
        """记录手动触发行为"""
        self._monitor.mark_manual_activity(source)

    # ---- 状态管理委托 ----

    def is_shuoshuo_read(self, tid: str) -> bool:
        """判断说说是否已读。"""
        return self._state.is_shuoshuo_read(tid)

    def mark_shuoshuo_read(self, tid: str) -> None:
        """标记说说为已读。"""
        self._state.mark_shuoshuo_read(tid)

    def mark_shuoshuo_read_batch(self, items: list[dict[str, Any]]) -> None:
        """批量标记说说为已读。"""
        self._state.mark_shuoshuo_read_batch(items)

    def filter_unread_shuoshuo(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按已读追踪过滤出未读说说列表。"""
        return self._state.filter_unread_shuoshuo(items)

    def claim_unread_shuoshuo(
        self, items: list[dict[str, Any]], limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """领取未读说说用于处理（并发防重）。"""
        return self._state.claim_unread_shuoshuo(items, limit=limit)

    def finalize_read_claim(
        self, items: list[dict[str, Any]], processed: bool = True,
    ) -> None:
        """结束未读领取。"""
        self._state.finalize_read_claim(items, processed=processed)

    def remember_last_read_snapshot(self, snapshot: dict[str, Any]) -> None:
        """记录最近一次阅读摘要。"""
        self._state.remember_last_read_snapshot(snapshot)

    def get_last_read_snapshot(self) -> dict[str, Any] | None:
        """获取最近一次阅读摘要。"""
        return self._state.get_last_read_snapshot()

    # ---- AI 生成委托 ----

    async def generate_random_publish_topic(self) -> str | None:
        """生成随机发布主题（LLM驱动）。"""
        return await self._prompts.generate_random_publish_topic()

    # ---- 登录/状态查询 ----

    async def is_logged_in(self) -> bool:
        """检查登录状态"""
        qq = await self._get_qq_from_napcat()
        if not qq:
            return False
        cookies = await self.cookie_manager.load_cookies(qq)
        return bool(cookies)

    async def get_qq_suggestion(self) -> str:
        """获取 QQ 号配置建议"""
        return "QQ 号已自动从 NapCat 适配器获取，无需手动配置"

    async def get_current_uin(self) -> str | None:
        """获取当前登录 UIN"""
        qq = await self._get_qq_from_napcat()
        if qq:
            logger.debug(f"[获取当前UIN] QQ={qq}")
        else:
            logger.debug("[获取当前UIN] 未能获取 QQ 号")
        return qq

    async def try_update_cookies_from_napcat(self) -> str | None:
        """从 NapCat 获取并保存 Cookie"""
        logger.info("[Cookie更新] 开始从 NapCat 获取 Cookie")
        cookies = await self.cookie_manager.fetch_cookies_from_adapter(ADAPTER_SIGNATURE)
        if cookies:
            uin = cookies.get("uin") or cookies.get("ptui_loginuin")
            if uin:
                real_uin = uin.lstrip("o")
                await self.cookie_manager.save_cookies(real_uin, cookies)
                logger.info(f"[Cookie更新] 成功获取并保存 Cookie, QQ={real_uin}")
                return real_uin
        logger.warning("[Cookie更新] 未能获取 Cookie")
        return None

    # ---- 资源清理 ----

    async def close(self) -> None:
        """清理资源"""
        if self._monitor.is_running:
            await self.stop_monitor()
