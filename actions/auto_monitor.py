"""自动监控QQ空间说说动作"""

from __future__ import annotations

from typing import Annotated, Any

from src.core.components.base import BaseAction
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


class AutoMonitorAction(BaseAction):
    """自动监控QQ空间说说动作

    启动或停止对 QQ 空间说说的自动监控。
    监控到新说说时自动通知（推送到群/好友），并可配置自动评论/点赞。
    支持概率控制，每次检测到新说说时会按配置的概率决定是否点赞/评论。
    """

    action_name = "auto_monitor"
    action_description = (
        "自动监控 QQ 空间说说。\n"
        "启动后会自动检测新说说并通知，可以配置自动评论和点赞（支持概率控制）。\n"
        "\n"
        "【重要约束】\n"
        "1. 自动评论是说给所有好友看的，内容必须适合所有人群。\n"
        "2. 评论要自然、友善，像真实用户在社交平台的互动。\n"
        "3. 简短自然，控制在25字以内，禁止提及用户名或 @ 任何人。\n"
        "\n"
        "参数说明：\n"
        "- action_type: 操作类型（必填）。start=启动监控, stop=停止监控, status=查看状态\n"
        "- interval: 监控间隔秒数（可选），仅 start 时有效，默认 300 秒\n"
        "- target_group: 通知推送群号（可选）\n"
        "- target_user: 通知推送QQ号（可选）\n"
        "- auto_comment: 是否自动评论（可选），true/false\n"
        "- auto_like: 是否自动点赞（可选），true/false\n"
        "- like_probability: 点赞概率（可选），0.0-1.0，默认 1.0\n"
        "- comment_probability: 评论概率（可选），0.0-1.0，默认 0.3\n"
        "使用示例：\n"
        "1. 启动监控：action=auto_monitor, action_type='start'\n"
        "2. 启动并设置间隔：action=auto_monitor, action_type='start', interval=600\n"
        "3. 启动并开启自动评论：action=auto_monitor, action_type='start', auto_comment=true\n"
        "4. 启动并设置概率：action=auto_monitor, action_type='start', like_probability=0.8, comment_probability=0.3\n"
        "5. 查看监控状态：action=auto_monitor, action_type='status'\n"
        "6. 停止监控：action=auto_monitor, action_type='stop'\n"
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
        action_type: Annotated[str, "操作类型: start/stop/status（必填）"],
        interval: Annotated[int | None, "监控间隔秒数，仅 start 时有效"] = None,
        target_group: Annotated[str | None, "通知推送群号"] = None,
        target_user: Annotated[str | None, "通知推送QQ号"] = None,
        auto_comment: Annotated[bool | None, "是否自动评论"] = None,
        auto_like: Annotated[bool | None, "是否自动点赞"] = None,
        # 概率范围: 0.0 ~ 1.0 (0%=绝不, 100%=必定)
        like_probability: Annotated[float | None, "点赞概率 (0.0-1.0), 0.0=0%, 1.0=100%, 默认1.0"] = None,
        comment_probability: Annotated[float | None, "评论概率 (0.0-1.0), 0.0=0%, 1.0=100%, 默认0.3"] = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行自动监控动作

        Args:
            action_type: 操作类型 (start/stop/status)
            interval: 监控间隔（秒）
            target_group: 推送群号
            target_user: 推送QQ号
            auto_comment: 是否自动评论
            auto_like: 是否自动点赞
            like_probability: 点赞概率 (0.0-1.0, 0%=绝不, 100%=必定)
            comment_probability: 评论概率 (0.0-1.0, 0%=绝不, 100%=必定)
            **kwargs: 其他参数
        """
        logger.info(f"[AutoMonitor] 执行操作: {action_type}")

        # 获取服务
        try:
            from src.app.plugin_system.api.service_api import get_service
            from src.core.components.base import BaseService

            logger.debug("[AutoMonitor] 获取 Qzone 服务")
            service = get_service("qzone_shuoshuo:service:qzone")
            if service is None or not isinstance(service, BaseService):
                logger.error("[AutoMonitor] 服务未启动")
                return False, "自动监控失败：服务未启动，请检查插件是否正确加载"
        except Exception as e:
            logger.error(f"[AutoMonitor] 获取服务异常: {e}", exc_info=True)
            return False, f"自动监控失败：获取服务异常 {str(e)}"

        action_type = action_type.lower().strip()
        if action_type not in ("start", "stop", "status"):
            return False, "操作类型无效，请使用 start/stop/status"

        # 执行对应操作
        if action_type == "status":
            return await self._do_status(service)
        elif action_type == "start":
            return await self._do_start(
                service, interval, target_group, target_user,
                auto_comment, auto_like, like_probability, comment_probability
            )
        elif action_type == "stop":
            return await self._do_stop(service)

        return False, "未知操作"

    async def _do_status(self, service: Any) -> tuple[bool, str]:
        """查看监控状态"""
        try:
            status = await service.get_monitor_status()
            if status.get("is_running"):
                lines = [
                    "📡 自动监控状态：运行中",
                    f"   间隔：{status.get('interval', 300)} 秒",
                ]
                if status.get("target_group"):
                    lines.append(f"   推送群：{status.get('target_group')}")
                if status.get("target_user"):
                    lines.append(f"   推送用户：{status.get('target_user')}")
                if status.get("auto_like"):
                    prob = status.get("like_probability", 1.0)
                    lines.append(f"   自动点赞：✅ (概率 {int(prob * 100)}%)")
                if status.get("auto_comment"):
                    prob = status.get("comment_probability", 0.3)
                    lines.append(f"   自动评论：✅ (概率 {int(prob * 100)}%)")
                return True, "\n".join(lines)
            else:
                return True, "📡 自动监控状态：未运行\n使用 action=auto_monitor, action_type='start' 启动监控"
        except Exception as e:
            logger.error(f"[AutoMonitor] 获取状态异常: {e}")
            return False, f"获取监控状态失败: {e}"

    async def _do_start(
        self,
        service: Any,
        interval: int | None,
        target_group: str | None,
        target_user: str | None,
        auto_comment: bool | None,
        auto_like: bool | None,
        like_probability: float | None,
        comment_probability: float | None,
    ) -> tuple[bool, str]:
        """启动监控"""
        try:
            # 构建配置
            config: dict[str, Any] = {}
            if interval is not None:
                config["interval"] = max(60, interval)  # 最小 60 秒
            if target_group is not None:
                config["target_group"] = str(target_group)
            if target_user is not None:
                config["target_user"] = str(target_user)
            if auto_comment is not None:
                config["auto_comment"] = auto_comment
            if auto_like is not None:
                config["auto_like"] = auto_like
            if like_probability is not None:
                config["like_probability"] = max(0.0, min(1.0, like_probability))
            if comment_probability is not None:
                config["comment_probability"] = max(0.0, min(1.0, comment_probability))

            result = await service.start_monitor(config)

            if result.get("success"):
                lines = ["✅ 自动监控已启动"]
                if config.get("interval"):
                    lines.append(f"   监控间隔：{config['interval']} 秒")
                if config.get("target_group"):
                    lines.append(f"   推送群：{config['target_group']}")
                if config.get("target_user"):
                    lines.append(f"   推送用户：{config['target_user']}")
                if config.get("auto_like"):
                    prob = config.get("like_probability", 1.0)
                    lines.append(f"   自动点赞：✅ (概率 {int(prob * 100)}%)")
                if config.get("auto_comment"):
                    prob = config.get("comment_probability", 0.3)
                    lines.append(f"   自动评论：✅ (概率 {int(prob * 100)}%)")
                logger.info("[AutoMonitor] 启动成功")
                return True, "\n".join(lines)
            else:
                logger.error(f"[AutoMonitor] 启动失败: {result.get('message')}")
                return False, f"启动失败: {result.get('message', '未知错误')}"
        except Exception as e:
            logger.error(f"[AutoMonitor] 启动异常: {e}", exc_info=True)
            return False, f"启动监控失败: {e}"

    async def _do_stop(self, service: Any) -> tuple[bool, str]:
        """停止监控"""
        try:
            result = await service.stop_monitor()
            if result.get("success"):
                logger.info("[AutoMonitor] 已停止")
                return True, "✅ 自动监控已停止"
            else:
                logger.error(f"[AutoMonitor] 停止失败: {result.get('message')}")
                return False, f"停止失败: {result.get('message', '未知错误')}"
        except Exception as e:
            logger.error(f"[AutoMonitor] 停止异常: {e}", exc_info=True)
            return False, f"停止监控失败: {e}"
