"""QQ空间说说命令处理器。

命令入口：
- /send_feed [内容]  - 发送说说（空内容时自动生成随机主题）
- /read_feed [数量]  - 读取最近说说并按配置概率自动点赞/评论
"""

from __future__ import annotations

import random
from collections import Counter
from typing import TYPE_CHECKING

from src.core.components.base import BaseCommand
from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from src.core.components.base import BaseService

logger = get_logger("qzone_shuoshuo")


class SendFeedCommand(BaseCommand):
    """发送说说命令"""

    command_name = "send_feed"
    command_prefix = "/"

    _RANDOM_KEYWORDS = ("随机", "random", "rand")

    async def execute(self, message_text: str) -> tuple[bool, str]:
        from src.app.plugin_system.api.service_api import get_service

        service = get_service("qzone_shuoshuo:service:qzone")
        if not service or not isinstance(service, BaseService):
            return False, "服务未启动"

        topic = message_text.strip()

        # 空内容或随机关键词时生成随机主题
        if not topic or topic.lower() in self._RANDOM_KEYWORDS:
            generator = getattr(service, "generate_random_publish_topic", None)
            if callable(generator):
                try:
                    maybe_topic = generator()
                    if hasattr(maybe_topic, "__await__"):
                        maybe_topic = await maybe_topic
                    topic = str(maybe_topic or "").strip()
                except Exception as e:
                    logger.warning(f"生成随机主题失败: {e}")
            if not topic:
                topic = "记录一下今天的一点小感受"

        result = await service.publish_shuoshuo(content=topic)
        if result.is_success:
            content = ""
            if isinstance(result.data, dict):
                content = str(result.data.get("content", "") or "").strip()
            if not content:
                content = topic
            return True, f"已经成功发送说说：{content}" if content else "已经成功发送说说。"
        return False, f"发布失败: {result.error_message}"


class ReadFeedCommand(BaseCommand):
    """读取最近说说并按配置概率自动互动"""

    command_name = "read_feed"
    command_prefix = "/"

    async def execute(self, message_text: str) -> tuple[bool, str]:
        from src.app.plugin_system.api.service_api import get_service

        service = get_service("qzone_shuoshuo:service:qzone")
        if not service or not isinstance(service, BaseService):
            return False, "服务未启动"

        # 解析数量参数（空输入默认5）
        count_str = message_text.strip()
        try:
            count = int(count_str) if count_str else 5
        except ValueError:
            count = 5

        count = max(1, min(count, 20))

        # 获取配置的概率
        monitor_cfg = getattr(service.config, "monitor", None) if getattr(service, "config", None) else None
        like_probability = float(getattr(monitor_cfg, "like_probability", 0.8)) if monitor_cfg else 0.8
        comment_probability = float(getattr(monitor_cfg, "comment_probability", 0.3)) if monitor_cfg else 0.3

        result = await service.get_shuoshuo_list(count=count)
        if not result.is_success:
            return False, f"读取失败: {result.error_message}"

        feed_list = result.data or []
        if not feed_list:
            return True, "📭 暂无说说内容"

        # 获取当前QQ
        current_qq: str | None = None
        get_current_uin = getattr(service, "get_current_uin", None)
        if callable(get_current_uin):
            try:
                resolved_qq = await get_current_uin()
                if resolved_qq:
                    current_qq = str(resolved_qq).strip() or None
            except Exception:
                pass

        # 按配置概率自动点赞/评论
        liked_count = 0
        commented_count = 0
        like_failure_reasons: Counter[str] = Counter()
        comment_failure_reasons: Counter[str] = Counter()

        lines: list[str] = []

        for data in feed_list:
            tid = str(data.get("tid", "") or "").strip()
            if not tid:
                continue

            owner_qq = str(data.get("uin", "") or "") or None
            is_self_feed = bool(current_qq and owner_qq and owner_qq == current_qq)

            # 概率点赞
            if random.random() <= like_probability:
                like_result = await service.like_shuoshuo(shuoshuo_id=tid, qq_number="", owner_qq=owner_qq)
                if like_result.is_success:
                    liked_count += 1
                else:
                    reason = self._classify_failure(str(getattr(like_result, "error_message", "")))
                    like_failure_reasons[reason] += 1

            # 概率评论（跳过自己的说说）
            if not is_self_feed and random.random() <= comment_probability:
                comment_text = "路过~"
                try:
                    if hasattr(service, "_generate_comment_text"):
                        generated = await service._generate_comment_text(
                            str(data.get("content", "")),
                            str(data.get("nickname", "") or data.get("uin", "")),
                        )
                        if generated:
                            comment_text = generated
                except Exception:
                    pass

                comment_result = await service.comment_shuoshuo(
                    shuoshuo_id=tid,
                    content=comment_text,
                    qq_number="",
                    owner_qq=owner_qq,
                )
                if comment_result.is_success:
                    commented_count += 1
                else:
                    reason = self._classify_failure(str(getattr(comment_result, "error_message", "")))
                    comment_failure_reasons[reason] += 1

        # 格式化输出
        lines.append(f"📖 已读取并处理 {len(feed_list)} 条说说")
        lines.append(f"互动结果：点赞 {liked_count} 条，评论 {commented_count} 条")

        if like_failure_reasons:
            lines.append(f"点赞失败：{self._format_counter(like_failure_reasons)}")
        if comment_failure_reasons:
            lines.append(f"评论失败：{self._format_counter(comment_failure_reasons)}")

        # 标记已读
        mark_read_batch = getattr(service, "mark_shuoshuo_read_batch", None)
        if callable(mark_read_batch):
            try:
                mark_read_batch(feed_list)
                lines.append("已标记为已读")
            except Exception:
                pass

        return True, "\n".join(lines)

    def _classify_failure(self, error_message: str) -> str:
        """分类失败原因"""
        text = error_message.lower()
        if "429" in text or "限流" in text:
            return "频率限制"
        if "cookie" in text or "登录" in text:
            return "登录异常"
        if "timeout" in text or "超时" in text:
            return "请求超时"
        return "其他"

    def _format_counter(self, counter: Counter[str]) -> str:
        """格式化计数器"""
        return "、".join(f"{k}×{v}" for k, v in counter.items())
