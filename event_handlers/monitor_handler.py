"""QQ空间说说监控事件处理器"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.components.base import BaseEventHandler
from src.app.plugin_system.api.log_api import get_logger
from src.kernel.event import EventDecision

if TYPE_CHECKING:
    from ..plugin import QzoneShuoshuoPlugin

logger = get_logger("qzone_shuoshuo")


class QzoneMonitorHandler(BaseEventHandler):
    """QQ空间说说监控事件处理器"""

    handler_name = "qzone_monitor_handler"
    handler_description = "QQ空间说说监控处理器，检测新说说并推送通知。"

    def __init__(self, plugin: "QzoneShuoshuoPlugin") -> None:
        """初始化处理器"""
        super().__init__(plugin)
        self.plugin = plugin

    async def execute(self, event_name: str, params: dict[str, Any]) -> tuple:
        """处理事件（预留）"""
        return EventDecision.SUCCESS, params
