"""删除QQ空间说说动作"""

from __future__ import annotations

from typing import Annotated, Any

from src.core.components.base import BaseAction
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


class DeleteShuoshuoAction(BaseAction):
    """删除QQ空间说说动作"""

    action_name = "delete_shuoshuo"
    action_description = (
        "删除指定ID的QQ空间说说。\n"
        "参数说明：\n"
        "- shuoshuo_id: 说说ID（必填），可通过 list_shuoshuo 获取。\n"
        "使用示例：\n"
        "1. 删除说说：action=delete_shuoshuo, shuoshuo_id='abc123def456'\n"
        "注意：删除操作不可逆，请确认后再执行。"
    )

    def _get_plugin_config(self) -> Any:
        """获取插件配置"""
        try:
            plugin_obj = getattr(self, "plugin", None)
            config_obj = getattr(plugin_obj, "config", None)
            return config_obj
        except Exception:
            return None

    def _get_qzone_config(self) -> str:
        """获取Qzone适配器配置

        注意: QQ号现在从 NapCat 自动获取，不再从配置读取
        """
        config_obj = self._get_plugin_config()
        if config_obj is None:
            return "napcat_adapter:adapter:napcat_adapter"

        try:
            qzone_config = getattr(config_obj, "qzone", None)
            if qzone_config is None:
                return "napcat_adapter:adapter:napcat_adapter"

            return str(getattr(qzone_config, "adapter_signature", "napcat_adapter:adapter:napcat_adapter") or "napcat_adapter:adapter:napcat_adapter")
        except Exception:
            return "napcat_adapter:adapter:napcat_adapter"

    async def execute(
        self,
        shuoshuo_id: Annotated[str, "说说ID（必填）"],
        **kwargs,
    ) -> tuple[bool, str]:
        """执行删除说说动作

        Args:
            shuoshuo_id: 说说ID
            **kwargs: 其他参数
        """
        logger.info(f"[DeleteShuoshuo] 开始执行, tid={shuoshuo_id}")

        # 校验说说ID
        if not shuoshuo_id or not str(shuoshuo_id).strip():
            logger.warning("[DeleteShuoshuo] 说说ID为空")
            return False, "删除说说失败：说说ID不能为空"

        shuoshuo_id = str(shuoshuo_id).strip()
        logger.debug(f"[DeleteShuoshuo] 说说ID: {shuoshuo_id}")

        # 获取配置
        adapter_sign = self._get_qzone_config()
        logger.debug(f"[DeleteShuoshuo] 使用适配器: {adapter_sign}")

        # 获取服务
        try:
            from src.app.plugin_system.api.service_api import get_service
            from src.core.components.base import BaseService

            logger.debug("[DeleteShuoshuo] 获取 Qzone 服务")
            service = get_service("qzone_shuoshuo:service:qzone")
            if service is None or not isinstance(service, BaseService):
                logger.error("[DeleteShuoshuo] 服务未启动")
                return False, "删除说说失败：服务未启动，请检查插件是否正确加载"
            logger.debug("[DeleteShuoshuo] 服务获取成功")
        except Exception as e:
            logger.error(f"[DeleteShuoshuo] 获取服务异常: {e}", exc_info=True)
            return False, f"删除说说失败：获取服务异常 {str(e)}"

        # 调用服务删除说说（QQ号由服务自动获取）
        try:
            logger.info("[DeleteShuoshuo] 调用服务删除说说")
            result = await service.delete_shuoshuo(shuoshuo_id, "")

            if result.is_success:
                logger.info(f"[DeleteShuoshuo] 说说删除成功, tid={shuoshuo_id}")
                return True, f"说说删除成功\n说说ID: {shuoshuo_id}"
            else:
                error_msg = result.error_message or "未知错误"
                logger.error(f"[DeleteShuoshuo] 说说删除失败: {error_msg}")
                return False, f"说说删除失败: {error_msg}"

        except Exception as e:
            logger.error(f"[DeleteShuoshuo] 删除说说时发生异常: {e}", exc_info=True)
            return False, f"删除说说时发生异常: {str(e)}"
