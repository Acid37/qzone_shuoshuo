"""Qzone Action 公共基类。

提供所有 Action 共享的服务获取、配置访问等能力，
消除各 Action 中的重复样板代码。
"""

from __future__ import annotations

from typing import Any

from src.core.components.base import BaseAction
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


class QzoneBaseAction(BaseAction):
    """QQ 空间 Action 公共基类。

    封装：
    - _get_service(): 获取 QzoneService 实例
    - _get_plugin_config(): 获取插件配置对象
    """

    def _get_plugin_config(self) -> Any:
        """获取插件配置对象。"""
        try:
            plugin_obj = getattr(self, "plugin", None)
            return getattr(plugin_obj, "config", None)
        except Exception:
            return None

    async def _get_service(self) -> Any | None:
        """获取 QzoneService 实例。

        Returns:
            QzoneService 实例，获取失败返回 None
        """
        try:
            from src.app.plugin_system.api.service_api import get_service
            from src.core.components.base import BaseService

            service = get_service("qzone_shuoshuo:service:qzone")
            if service is None or not isinstance(service, BaseService):
                logger.error("[QzoneAction] 服务未启动")
                return None
            return service
        except Exception as e:
            logger.error(f"[QzoneAction] 获取服务异常: {e}", exc_info=True)
            return None