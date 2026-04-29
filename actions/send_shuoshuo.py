"""发送QQ空间说说动作"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger

from ._base import QzoneBaseAction

logger = get_logger("qzone_shuoshuo")


class SendShuoshuoAction(QzoneBaseAction):
    """发送QQ空间说说动作"""

    action_name = "send_shuoshuo"
    action_description = (
        "发表一条QQ空间说说，内容会展示给所有好友。\n"
        "约束：内容需适合公开场合，10~60字，自然口语化，禁止 @ 人或提及具体昵称。"
    )

    def _get_default_visible(self) -> str:
        """获取默认可见范围"""
        config_obj = self._get_plugin_config()
        if config_obj is None:
            return "all"
        try:
            qzone_cfg = getattr(config_obj, "qzone", None)
            if qzone_cfg is None:
                return "all"
            return str(getattr(qzone_cfg, "default_visible", "all") or "all")
        except Exception:
            return "all"

    def _get_image_policy(self) -> tuple[bool, int]:
        """获取图片发送策略（是否允许、最大张数）。"""
        config_obj = self._get_plugin_config()
        if config_obj is None:
            return True, 9
        try:
            qzone_cfg = getattr(config_obj, "qzone", None)
            if qzone_cfg is None:
                return True, 9
            enable = bool(getattr(qzone_cfg, "enable_image", True))
            max_count = int(getattr(qzone_cfg, "max_image_count", 9) or 9)
            return enable, max(1, max_count)
        except Exception:
            return True, 9

    async def execute(
        self,
        content: Annotated[str, "说说内容（必填）"],
        images: Annotated[list[str] | None, "图片路径列表（可选）"] = None,
        visible: Annotated[str | None, "可见范围: all/friends/self"] = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行发送说说动作"""
        if not content or not str(content).strip():
            return False, "发送说说失败：说说内容不能为空"

        default_visible = self._get_default_visible()
        effective_visible = visible if visible else default_visible
        if effective_visible not in ("all", "friends", "self"):
            effective_visible = default_visible

        service = await self._get_service()
        if service is None:
            return False, "发送说说失败：服务未启动，请检查插件是否正确加载"

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
                path = Path(str(img_path))
                if path.exists():
                    try:
                        real_images.append(path.read_bytes())
                    except Exception as e:
                        logger.warning(f"读取图片失败 {path}: {e}")
                elif isinstance(img_path, bytes):
                    real_images.append(img_path)

        result = await service.publish_shuoshuo(
            qq_number="",
            content=str(content).strip(),
            images=real_images,
            visible=effective_visible,
        )

        if result.is_success:
            tid = result.data.get("tid") if isinstance(result.data, dict) else None
            msg = "说说发布成功！"
            if tid:
                msg += f"\n说说ID: {tid}"
            return True, msg
        return False, f"说说发布失败: {result.error_message or '未知错误'}"
