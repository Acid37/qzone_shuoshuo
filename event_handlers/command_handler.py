"""QQ空间说说命令事件处理器。

用于在消息事件阶段拦截并执行 qzone 插件命令：
- /send_feed
- /read_feed

这样可以在不改核心分发链路的前提下，让命令在聊天消息中直接生效。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.core.components.base import BaseEventHandler
from src.core.components.types import EventType
from src.core.managers import get_command_manager
from src.core.models.message import Message
from src.kernel.event import EventDecision

logger = get_logger("qzone_shuoshuo")


class QzoneCommandHandler(BaseEventHandler):
    """拦截并执行 qzone 命令。"""

    handler_name = "qzone_command_handler"
    handler_description = "拦截 /send_feed 与 /read_feed 命令并直接执行。"
    weight = 200
    intercept_message = True
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    _SUPPORTED_COMMANDS = {"send_feed", "read_feed"}

    def _extract_command_name(self, text: str) -> str | None:
        """提取命令名（去掉前缀 `/`）。"""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None

        parts = stripped[1:].split(maxsplit=1)
        if not parts:
            return None
        return parts[0].strip().lower() or None

    async def execute(self, event_name: str, params: dict[str, Any]) -> tuple[EventDecision, dict[str, Any]]:
        """处理 ON_MESSAGE_RECEIVED 事件中的 qzone 命令。"""
        _ = event_name

        message = params.get("message")
        if not isinstance(message, Message):
            return EventDecision.PASS, params

        text = str(message.processed_plain_text or message.content or "").strip()
        command_name = self._extract_command_name(text)
        if command_name not in self._SUPPORTED_COMMANDS:
            return EventDecision.PASS, params

        command_manager = get_command_manager()
        command_path, command_cls, _args = command_manager.match_command(text)

        # 仅处理 qzone 插件自身命令，避免误拦截其他插件
        if not command_cls:
            await send_text(
                content=(
                    "命令未识别，请检查格式：\n"
                    "- /send_feed [内容]\n"
                    "- /read_feed [数量] [--read-only] [--no-like] [--no-comment]"
                ),
                stream_id=message.stream_id,
                platform=message.platform,
                reply_to=message.message_id,
            )
            return EventDecision.STOP, params

        signature = str(getattr(command_cls, "_signature_", "") or "")
        if not signature.startswith("qzone_shuoshuo:command:"):
            return EventDecision.PASS, params

        try:
            success, result = await command_manager.execute_command(message=message, text=text)
            output = str(result or "").strip()
            if not output:
                output = "✅ 执行完成" if success else f"❌ 执行失败: {command_path or command_name}"

            await send_text(
                content=output,
                stream_id=message.stream_id,
                platform=message.platform,
                reply_to=message.message_id,
            )
            return EventDecision.STOP, params
        except Exception as exc:
            logger.error(f"[命令处理] 执行失败: {exc}")
            await send_text(
                content=f"❌ 命令执行异常：{exc}",
                stream_id=message.stream_id,
                platform=message.platform,
                reply_to=message.message_id,
            )
            return EventDecision.STOP, params
