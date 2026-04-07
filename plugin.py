"""QzoneShuoshuo 插件实现"""

from __future__ import annotations

import asyncio

# 导入必要的类
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BasePlugin
from src.core.components.loader import register_plugin

from .config import QzoneConfig
from .core.service import QzoneService
from .actions import (
    SendShuoshuoAction,
    DeleteShuoshuoAction,
    LikeShuoshuoAction,
    CommentShuoshuoAction,
    ReadShuoshuoAction,
    AutoMonitorAction,
)
from .commands import SendFeedCommand, ReadFeedCommand
from .event_handlers import QzoneCommandHandler
from .core.dependency_manager import DependencyManager

logger = get_logger("qzone_shuoshuo")


@register_plugin
class QzoneShuoshuoPlugin(BasePlugin):
    """QQ空间说说插件主类"""

    plugin_name = "qzone_shuoshuo"
    plugin_version = "1.1.0"
    plugin_author = "可可和满月月喵"
    plugin_description = "QQ空间说说发送插件，支持发布、查询说说内容"
    configs = [QzoneConfig]

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._dependency_manager = DependencyManager()
        self._auto_start_retry_task_id: str | None = None

    def _is_monitor_auto_start_enabled(self) -> bool:
        """判断是否启用自动监控自启动。"""
        monitor_cfg = getattr(self.config, "monitor", None) if getattr(self, "config", None) else None
        if monitor_cfg is None:
            return True
        return bool(getattr(monitor_cfg, "auto_start", True))

    async def _try_auto_start_monitor(self, *, log_not_ready: bool = True) -> bool:
        """尝试自动启动监控（使用默认配置）。

        Returns:
            bool: True 表示无需继续重试；False 表示服务暂未就绪，可后续重试。
        """
        if not self._is_monitor_auto_start_enabled():
            logger.info("[自动监控] 已禁用自启动（monitor.auto_start=false）")
            return True

        try:
            from src.app.plugin_system.api.service_api import get_service
            from src.core.components.base import BaseService

            service = get_service("qzone_shuoshuo:service:qzone")
            if service is None or not isinstance(service, BaseService):
                if log_not_ready:
                    logger.warning("[自动监控] 自启动跳过：QzoneService 未就绪")
                return False

            result = await service.start_monitor({})
            if bool(result.get("success", False)):
                logger.info("[自动监控] 已按配置自动启动")
                return True
            else:
                logger.warning(f"[自动监控] 自启动失败: {result.get('message', '未知错误')}")
                return True
        except Exception as e:
            logger.warning(f"[自动监控] 自启动异常: {e}")
            return True

    async def _auto_start_monitor_retry_loop(self) -> None:
        """后台重试自启动，处理插件加载时服务尚未就绪的时序问题。"""
        # 让出一个事件循环周期，等待插件完成注册到 plugin_manager
        await asyncio.sleep(0.5)

        max_attempts = 30
        interval_seconds = 1.0
        for attempt in range(1, max_attempts + 1):
            ready = await self._try_auto_start_monitor(log_not_ready=False)
            if ready:
                if attempt > 1:
                    logger.info(f"[自动监控] 自启动重试成功（第 {attempt}/{max_attempts} 次）")
                return
            await asyncio.sleep(interval_seconds)

        logger.warning("[自动监控] 自启动重试结束：QzoneService 持续未就绪")

    def _schedule_auto_start_retry_task(self) -> None:
        """通过 task_manager 启动后台自启动重试任务。"""
        try:
            from src.kernel.concurrency import get_task_manager

            tm = get_task_manager()
            task_info = tm.create_task(
                self._auto_start_monitor_retry_loop(),
                name="qzone_auto_start_retry",
                daemon=True,
            )
            self._auto_start_retry_task_id = task_info.task_id
            logger.info("[自动监控] 已启动后台自启动重试任务")
        except Exception as e:
            logger.warning(f"[自动监控] 创建自启动重试任务失败: {e}")

    def _cancel_auto_start_retry_task(self) -> None:
        """取消后台自启动重试任务。"""
        task_id = str(getattr(self, "_auto_start_retry_task_id", "") or "")
        if not task_id:
            return
        try:
            from src.kernel.concurrency import get_task_manager

            get_task_manager().cancel_task(task_id)
        except Exception:
            pass
        finally:
            self._auto_start_retry_task_id = None

    async def on_plugin_loaded(self) -> None:
        """插件加载钩子：内置依赖检查与自动安装（开箱即用）。"""
        ok = await self._dependency_manager.ensure_dependencies(
            auto_install=True,
            installer="auto",
            timeout_seconds=180,
        )
        if not ok:
            logger.warning("QzoneShuoshuo 依赖检查未完全通过，相关功能可能不可用")

        auto_started = await self._try_auto_start_monitor()
        if not auto_started:
            self._schedule_auto_start_retry_task()
        logger.info("QzoneShuoshuo 插件加载成功")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载钩子。"""
        self._cancel_auto_start_retry_task()
        logger.info("QzoneShuoshuo 插件卸载成功")

    async def on_load(self) -> bool:
        """兼容旧版钩子（若外部仍调用）。"""
        await self.on_plugin_loaded()
        return True

    async def on_unload(self) -> bool:
        """兼容旧版钩子（若外部仍调用）。"""
        await self.on_plugin_unloaded()
        return True

    def get_components(self) -> list[type]:
        """获取插件内所有组件类"""
        return [
            # Services - 暴露的功能
            QzoneService,
            # Actions - 提供给 AI 调用的操作
            SendShuoshuoAction,
            DeleteShuoshuoAction,
            LikeShuoshuoAction,
            CommentShuoshuoAction,
            ReadShuoshuoAction,
            AutoMonitorAction,
            # Commands - 命令行处理
            SendFeedCommand,
            ReadFeedCommand,
            # EventHandlers - 事件拦截处理
            QzoneCommandHandler,
        ]
