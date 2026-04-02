"""评论QQ空间说说动作"""

from __future__ import annotations

from typing import Annotated, Any

from src.core.components.base import BaseAction
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


class CommentShuoshuoAction(BaseAction):
    """评论QQ空间说说动作"""

    action_name = "comment_shuoshuo"
    action_description = (
        "评论指定的QQ空间说说。\n"
        "\n"
        "【重要约束】\n"
        "1. 评论是公开发布的，所有好友都能看到，必须适合所有人群。\n"
        "2. 评论内容应该自然、友善，像真实用户在社交平台的互动。\n"
        "3. 简短自然，控制在25字以内。\n"
        "4. 不要提及自己的真实身份或说说的具体内容细节。\n"
        "\n"
        "参数说明：\n"
        "- shuoshuo_id: 说说ID（必填），可通过 list_shuoshuo 获取。\n"
        "- content: 评论内容（必填），要发送的评论文本。\n"
        "- owner_qq: 说说主人的QQ号（可选），默认使用 Bot 的 QQ 号。\n"
        "- comment_id: 回复的评论ID（可选），用于回复他人的评论。\n"
        "使用示例：\n"
        "1. 评论说说：action=comment_shuoshuo, shuoshuo_id='abc123def456', content='说得太对了！'\n"
        "2. 评论他人说说：action=comment_shuoshuo, shuoshuo_id='abc123', content='赞', owner_qq='123456789'\n"
        "3. 回复他人评论：action=comment_shuoshuo, shuoshuo_id='abc123', content='我也这么觉得', comment_id='999888777'\n"
    )

    def _get_plugin_config(self) -> Any:
        """获取插件配置"""
        try:
            plugin_obj = getattr(self, "plugin", None)
            config_obj = getattr(plugin_obj, "config", None)
            return config_obj
        except Exception:
            return None

    async def execute(
        self,
        shuoshuo_id: Annotated[str, "说说ID（必填）"],
        content: Annotated[str, "评论内容（必填）"],
        owner_qq: Annotated[str | None, "说说主人的QQ号（可选）"] = None,
        comment_id: Annotated[str | None, "回复的评论ID（可选，用于回复他人评论）"] = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行评论说说动作

        Args:
            shuoshuo_id: 说说ID
            content: 评论内容
            owner_qq: 说说主人的QQ号
            comment_id: 回复的评论ID（用于回复他人评论）
            **kwargs: 其他参数
        """
        logger.info(f"[CommentShuoshuo] 开始执行, tid={shuoshuo_id}, content={content[:20] if content else ''}...")

        # 校验说说ID
        if not shuoshuo_id or not str(shuoshuo_id).strip():
            logger.warning("[CommentShuoshuo] 说说ID为空")
            return False, "评论说说失败：说说ID不能为空"

        shuoshuo_id = str(shuoshuo_id).strip()

        # 校验评论内容
        if not content or not str(content).strip():
            logger.warning("[CommentShuoshuo] 评论内容为空")
            return False, "评论说说失败：评论内容不能为空"

        content = str(content).strip()

        logger.debug(f"[CommentShuoshuo] 说说ID: {shuoshuo_id}, 内容: {content}")

        # 确定说说主人QQ
        target_owner = owner_qq if owner_qq else ""
        logger.debug(f"[CommentShuoshuo] 目标主人: {target_owner or '自动获取'}")

        # 获取服务
        try:
            from src.app.plugin_system.api.service_api import get_service
            from src.core.components.base import BaseService

            logger.debug("[CommentShuoshuo] 获取 Qzone 服务")
            service = get_service("qzone_shuoshuo:service:qzone")
            if service is None or not isinstance(service, BaseService):
                logger.error("[CommentShuoshuo] 服务未启动")
                return False, "评论说说失败：服务未启动，请检查插件是否正确加载"
            logger.debug("[CommentShuoshuo] 服务获取成功")
        except Exception as e:
            logger.error(f"[CommentShuoshuo] 获取服务异常: {e}", exc_info=True)
            return False, f"评论说说失败：获取服务异常 {str(e)}"

        # 调用服务评论说说
        try:
            logger.info("[CommentShuoshuo] 调用服务评论说说")
            result = await service.comment_shuoshuo(
                shuoshuo_id=shuoshuo_id,
                content=content,
                qq_number="",  # 空字符串让服务自动获取
                owner_qq=target_owner or None,
                comment_id=comment_id,
            )

            if result.is_success:
                cid = result.data.get("cid") if isinstance(result.data, dict) else ""
                success_msg = "评论发送成功！"
                if cid:
                    success_msg += f"\n评论ID: {cid}"
                success_msg += f"\n说说ID: {shuoshuo_id}"
                success_msg += "\n【执行回执】task=comment_shuoshuo,status=done"
                success_msg += "\n【下一轮上下文】我刚刚已经完成“评论说说”，下一步应优先汇报评论结果，而不是重复评论。"
                logger.info(f"[CommentShuoshuo] 评论成功, tid={shuoshuo_id}, cid={cid}")
                return True, success_msg
            else:
                error_msg = result.error_message or "未知错误"
                logger.error(f"[CommentShuoshuo] 评论失败: {error_msg}")
                return False, f"评论说说失败: {error_msg}"

        except Exception as e:
            logger.error(f"[CommentShuoshuo] 评论说说时发生异常: {e}", exc_info=True)
            return False, f"评论说说时发生异常: {str(e)}"
