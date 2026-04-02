"""发送QQ空间说说动作"""

from __future__ import annotations

from typing import Annotated, Any

from src.core.components.base import BaseAction
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


class SendShuoshuoAction(BaseAction):
    """发送QQ空间说说动作"""

    action_name = "send_shuoshuo"
    action_description = (
        "发送QQ空间说说。\n"
        "\n"
        "【重要约束】\n"
        "1. 说说是公开发布的，面向所有好友，内容必须适合所有人群。\n"
        "2. 禁止在说说内容中提及具体用户名、昵称或 @ 任何人。\n"
        "3. 说说你只需要提供内容，不要提供昵称、时间等信息。\n"
        "4. 内容要自然、友善，不要过于私人化或针对性。\n"
        "\n"
        "参数说明：\n"
        "- content: 说说内容（必填），支持纯文本。\n"
        "- images: 图片路径列表（可选），最多9张。\n"
        "- visible: 可见范围（可选），默认使用配置值。all=所有人, friends=好友, self=仅自己。\n"
        "使用示例：\n"
        "1. 发送纯文本说说：action=send_shuoshuo, content='今天很开心！'\n"
        "2. 发送带图片说说：action=send_shuoshuo, content='分享图片', images=['/path/to/image.jpg']\n"
        "3. 仅好友可见：action=send_shuoshuo, content='私密说说', visible='friends'"
    )

    def _get_plugin_config(self) -> Any:
        """获取插件配置"""
        try:
            plugin_obj = getattr(self, "plugin", None)
            config_obj = getattr(plugin_obj, "config", None)
            return config_obj
        except Exception:
            return None

    def _get_default_visible(self) -> str:
        """获取默认可见范围"""
        config_obj = self._get_plugin_config()
        if config_obj is None:
            return "all"

        try:
            qzone_config = getattr(config_obj, "qzone", None)
            if qzone_config is None:
                return "all"
            return str(getattr(qzone_config, "default_visible", "all") or "all")
        except Exception:
            return "all"

    def _get_image_policy(self) -> tuple[bool, int]:
        """获取图片发送策略（是否允许、最大张数）。"""
        config_obj = self._get_plugin_config()
        if config_obj is None:
            return True, 9

        try:
            qzone_config = getattr(config_obj, "qzone", None)
            if qzone_config is None:
                return True, 9
            enable_image = bool(getattr(qzone_config, "enable_image", True))
            max_image_count = int(getattr(qzone_config, "max_image_count", 9) or 9)
            return enable_image, max(1, max_image_count)
        except Exception:
            return True, 9

    async def execute(
        self,
        content: Annotated[str, "说说内容（必填）"],
        images: Annotated[list[str] | None, "图片路径列表（可选）"] = None,
        visible: Annotated[str | None, "可见范围: all/friends/self"] = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行发送说说动作

        Args:
            content: 说说内容
            images: 图片路径列表
            visible: 可见范围
            **kwargs: 其他参数
        """
        # 校验内容
        if not content or not str(content).strip():
            logger.warning("发送说说失败：内容为空")
            return False, "发送说说失败：说说内容不能为空"

        content = str(content).strip()

        default_visible = self._get_default_visible()

        # 确定可见范围
        effective_visible = visible if visible else default_visible
        if effective_visible not in ("all", "friends", "self"):
            effective_visible = default_visible

        # 获取服务
        try:
            from src.app.plugin_system.api.service_api import get_service
            from src.core.components.base import BaseService

            service = get_service("qzone_shuoshuo:service:qzone")
            if service is None or not isinstance(service, BaseService):
                return False, "发送说说失败：服务未启动，请检查插件是否正确加载"
        except Exception as e:
            logger.error(f"获取服务失败: {e}")
            return False, f"发送说说失败：获取服务异常 {str(e)}"

        # 直接发布（Kokoro AI 会自主判断是否合适，无需人工确认）
        try:
            # 处理图片
            real_images = None
            if images:
                enable_image, max_image_count = self._get_image_policy()
                if not enable_image:
                    return False, "发送说说失败：当前配置已禁用图片发送（qzone.enable_image=false）"
                if len(images) > max_image_count:
                    return False, f"发送说说失败：图片数量超限，最多允许 {max_image_count} 张"

                real_images = []
                for img_path in images:
                    from pathlib import Path
                    path = Path(str(img_path))
                    if path.exists():
                        try:
                            real_images.append(path.read_bytes())
                        except Exception as e:
                            logger.warning(f"读取图片失败 {path}: {e}")
                    elif isinstance(img_path, bytes):
                        real_images.append(img_path)

            # 调用发布方法（qq_number 留空，由 service 从 NapCat 自动获取）
            logger.info(f"[发送说说] 开始发布, 可见范围={effective_visible}")
            result = await service.publish_shuoshuo(
                qq_number="",  # 从 NapCat 自动获取
                content=content,
                images=real_images,
                visible=effective_visible,
            )

            if result.is_success:
                tid = result.data.get("tid") if isinstance(result.data, dict) else None
                success_msg = "说说发布成功！"
                if tid:
                    success_msg += f"\n说说ID: {tid}"
                success_msg += "\n【执行回执】task=send_shuoshuo,status=done"
                success_msg += "\n【下一轮上下文】我刚刚已经完成“发送说说”，除非用户明确要求再次发送，否则不要重复调用 send_shuoshuo。"
                logger.info(f"说说发布成功: {content[:30]}...")
                return True, success_msg
            else:
                error_msg = result.error_message or "未知错误"
                logger.error(f"说说发布失败: {error_msg}")
                return False, f"说说发布失败: {error_msg}"

        except Exception as e:
            logger.error(f"发送说说时发生异常: {e}", exc_info=True)
            return False, f"发送说说时发生异常: {str(e)}"
