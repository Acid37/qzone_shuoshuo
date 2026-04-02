"""点赞QQ空间说说动作"""

from __future__ import annotations

from typing import Annotated, Any

from src.core.components.base import BaseAction
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


class LikeShuoshuoAction(BaseAction):
    """点赞QQ空间说说动作"""

    action_name = "like_shuoshuo"
    action_description = (
        "点赞指定的QQ空间说说。\n"
        "参数说明：\n"
        "- shuoshuo_id: 说说ID（必填），可通过 list_shuoshuo 获取。\n"
        "- owner_qq: 说说主人的QQ号（可选），默认使用配置中的QQ号。\n"
        "使用示例：\n"
        "1. 点赞自己的说说：action=like_shuoshuo, shuoshuo_id='abc123def456'\n"
        "2. 点赞他人说说：action=like_shuoshuo, shuoshuo_id='abc123def456', owner_qq='123456789'\n"
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
        shuoshuo_id: Annotated[str | None, "说说ID（必填）"] = None,
        owner_qq: Annotated[str | None, "说说主人的QQ号（可选）"] = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行点赞说说动作

        Args:
            shuoshuo_id: 说说ID
            owner_qq: 说说主人的QQ号
            **kwargs: 其他参数
        """
        resolved_tid = shuoshuo_id
        if not resolved_tid:
            for key in ("tid", "id", "shuoshuo_tid"):
                value = kwargs.get(key)
                if value is not None and str(value).strip():
                    resolved_tid = str(value).strip()
                    break

        logger.info(f"[LikeShuoshuo] 开始执行, tid={resolved_tid}, owner={owner_qq}")

        # 校验说说ID
        if not resolved_tid or not str(resolved_tid).strip():
            logger.warning("[LikeShuoshuo] 说说ID为空")
            return False, "点赞说说失败：说说ID不能为空（可使用 shuoshuo_id 或 tid）"

        shuoshuo_id = str(resolved_tid).strip()
        logger.debug(f"[LikeShuoshuo] 说说ID: {shuoshuo_id}")

        # 获取配置
        adapter_sign = self._get_qzone_config()
        logger.debug(f"[LikeShuoshuo] 使用适配器: {adapter_sign}")

        # 确定说说主人QQ（用于构造 unikey）
        target_owner = owner_qq if owner_qq else ""
        logger.debug(f"[LikeShuoshuo] 目标主人: {target_owner or '自动获取'}")

        # 获取服务
        try:
            from src.app.plugin_system.api.service_api import get_service
            from src.core.components.base import BaseService

            logger.debug("[LikeShuoshuo] 获取 Qzone 服务")
            service = get_service("qzone_shuoshuo:service:qzone")
            if service is None or not isinstance(service, BaseService):
                logger.error("[LikeShuoshuo] 服务未启动")
                return False, "点赞说说失败：服务未启动，请检查插件是否正确加载"
            logger.debug("[LikeShuoshuo] 服务获取成功")
        except Exception as e:
            logger.error(f"[LikeShuoshuo] 获取服务异常: {e}", exc_info=True)
            return False, f"点赞说说失败：获取服务异常 {str(e)}"

        # 调用服务点赞说说（QQ号由服务自动获取）
        try:
            logger.info("[LikeShuoshuo] 调用服务点赞说说")
            result = await service.like_shuoshuo(
                shuoshuo_id=shuoshuo_id,
                qq_number="",  # 空字符串让服务自动获取
                owner_qq=target_owner or None,
            )

            if result.is_success:
                logger.info(f"[LikeShuoshuo] 说说点赞成功, tid={shuoshuo_id}")
                return True, f"说说点赞成功\n说说ID: {shuoshuo_id}"
            else:
                error_msg = result.error_message or "未知错误"
                logger.error(f"[LikeShuoshuo] 说说点赞失败: {error_msg}")
                return False, f"说说点赞失败: {error_msg}"

        except Exception as e:
            logger.error(f"[LikeShuoshuo] 点赞说说时发生异常: {e}", exc_info=True)
            return False, f"点赞说说时发生异常: {str(e)}"
