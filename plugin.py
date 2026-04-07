"""QzoneShuoshuo 插件实现"""

from __future__ import annotations

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

    async def on_plugin_loaded(self) -> None:
        """插件加载钩子：内置依赖检查与自动安装（开箱即用）。"""
        ok = await self._dependency_manager.ensure_dependencies(
            auto_install=True,
            installer="auto",
            timeout_seconds=180,
        )
        if not ok:
            logger.warning("QzoneShuoshuo 依赖检查未完全通过，相关功能可能不可用")

        logger.info("QzoneShuoshuo 插件加载成功")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载钩子。"""
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
