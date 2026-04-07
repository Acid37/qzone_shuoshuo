"""QQ空间说说命令处理器。

当前仅保留对齐后的命令入口：
- /send_feed
- /read_feed
"""

from __future__ import annotations

import datetime
import inspect
import random
import time
from collections import Counter
from typing import TYPE_CHECKING

from src.core.components.base import BaseCommand
from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from src.core.components.base import BaseService

logger = get_logger("qzone_shuoshuo")


class SendFeedCommand(BaseCommand):
    """(兼容旧版) 发送说说命令"""

    command_name = "send_feed"
    command_prefix = "/"

    _RANDOM_SEEDS: tuple[str, ...] = (
        "今天的一个小确幸",
        "最近在想的一件小事",
        "今天的天气和心情",
        "一个想记录下来的瞬间",
        "今天想和朋友分享的感受",
        "最近生活里的微小进展",
        "此刻的一点灵感",
    )
    _RANDOM_KEYWORDS: tuple[str, ...] = ("随机", "random", "rand")
    _IDEMPOTENT_WINDOW_SECONDS: float = 2.5

    def _get_idempotent_cache(self) -> dict[str, float]:
        """获取插件级短窗幂等缓存。

        说明：
        - 缓存挂载在插件实例上，避免跨插件/跨测试污染。
        - 仅做内存态短窗去重，不做持久化。
        """
        cache = getattr(self.plugin, "_send_feed_recent_requests", None)
        if isinstance(cache, dict):
            return cache

        cache = {}
        setattr(self.plugin, "_send_feed_recent_requests", cache)
        return cache

    def _build_idempotent_key(self, *, raw_topic: str, topic: str, used_random_seed: bool) -> str:
        """构造幂等键。"""
        seed = "random" if used_random_seed else (raw_topic or topic)
        normalized_seed = " ".join(str(seed).strip().lower().split())
        return f"{self.stream_id}|{normalized_seed}"

    def _is_duplicate_request(self, *, key: str) -> bool:
        """判断是否为短时间重复请求（非阻塞）。"""
        now = time.monotonic()
        cache = self._get_idempotent_cache()

        expire_before = now - self._IDEMPOTENT_WINDOW_SECONDS
        expired_keys = [k for k, ts in cache.items() if ts < expire_before]
        for expired_key in expired_keys:
            cache.pop(expired_key, None)

        last_time = cache.get(key)
        if last_time is not None and (now - last_time) <= self._IDEMPOTENT_WINDOW_SECONDS:
            return True

        cache[key] = now
        return False

    def _build_random_seed(self) -> str:
        """构造随机发布灵感。"""
        return random.choice(self._RANDOM_SEEDS)

    async def execute(self, message_text: str) -> tuple[bool, str]:
        raw_topic = message_text.strip()
        topic = raw_topic
        used_random_seed = False
        normalized = topic.lower()
        if (not topic) or (normalized in self._RANDOM_KEYWORDS):
            topic = self._build_random_seed()
            used_random_seed = True

        idem_key = self._build_idempotent_key(
            raw_topic=raw_topic,
            topic=topic,
            used_random_seed=used_random_seed,
        )
        if self._is_duplicate_request(key=idem_key):
            return True, "检测到短时间内重复发送请求，已忽略本次触发。"

        from src.app.plugin_system.api.send_api import send_text
        preview_topic = "随机" if used_random_seed else raw_topic
        try:
            await send_text(
                content=f"收到！正在为你生成关于“{preview_topic}”的说说，请稍候...",
                stream_id=self.stream_id,
            )
        except Exception as e:
            logger.debug(f"[send_feed] 预提示发送失败（已忽略）: {e}")

        from src.app.plugin_system.api.service_api import get_service
        from src.core.components.base import BaseService

        service = get_service("qzone_shuoshuo:service:qzone")
        if not service or not isinstance(service, BaseService):
            return False, "服务未启动"

        result = await service.publish_shuoshuo(content=topic)
        if result.is_success:
            published_content = ""
            if isinstance(result.data, dict):
                published_content = str(result.data.get("content", "") or "").strip()

            if not published_content:
                published_content = str(topic or "").strip()

            if published_content:
                return True, f"已经成功发送说说：{published_content}"

            return True, "已经成功发送说说。"
        return False, f"❌ 发布失败: {result.error_message}"


class ReadFeedCommand(BaseCommand):
    """读取最近说说并在阅读流程内进行互动（点赞/评论）。"""

    command_name = "read_feed"
    command_prefix = "/"

    def _parse_options(self, message_text: str) -> tuple[bool, bool, bool, bool, bool, int, float | None, float | None]:
        """解析 /read_feed 命令参数。

        支持参数：
        - [count]
        - --read-only
        - --no-like
        - --no-comment
        - --allow-self-interact
        - --show-list
        - --like-prob=<0~1>
        - --comment-prob=<0~1>
        """
        raw = message_text.strip()
        tokens = raw.split() if raw else []

        read_only = "--read-only" in tokens
        no_like = "--no-like" in tokens
        no_comment = "--no-comment" in tokens
        allow_self_interact = "--allow-self-interact" in tokens
        show_list = "--show-list" in tokens

        like_prob_override: float | None = None
        comment_prob_override: float | None = None

        normalized_tokens: list[str] = []
        for token in tokens:
            if token.startswith("--like-prob="):
                value = token.split("=", 1)[1].strip()
                like_prob_override = float(value)
                continue
            if token.startswith("--comment-prob="):
                value = token.split("=", 1)[1].strip()
                comment_prob_override = float(value)
                continue
            if token in {"--read-only", "--no-like", "--no-comment", "--allow-self-interact", "--show-list"}:
                continue
            normalized_tokens.append(token)

        count = 5
        if normalized_tokens:
            count = int(normalized_tokens[0])

        return read_only, no_like, no_comment, allow_self_interact, show_list, count, like_prob_override, comment_prob_override

    def _classify_interaction_failure(self, service: object, error_message: object) -> str:
        """将互动失败原因分类，便于对用户展示统计摘要。"""
        text = str(error_message or "未知错误").strip()

        classifier = getattr(service, "_classify_failure_reason", None)
        if callable(classifier):
            try:
                classified = classifier(text)
                if isinstance(classified, str) and classified.strip():
                    return classified.strip()
            except Exception:
                pass

        lowered = text.lower()
        if "429" in lowered or "限流" in lowered:
            return "触发频率限制"
        if "cookie" in lowered or "登录" in lowered:
            return "登录态异常"
        if "5xx" in lowered or "500" in lowered or "502" in lowered or "503" in lowered:
            return "服务暂时不稳定"
        if "timeout" in lowered or "超时" in lowered:
            return "请求超时"
        return "其他错误"

    def _resolve_probability(
        self,
        *,
        name: str,
        override: float | None,
        default_value: float,
    ) -> tuple[float, str | None]:
        """解析概率覆盖值，超出范围时回退默认值。

        约定：
        - override 为 None：使用默认值
        - override 在 [0, 1]：使用 override
        - override 超出 [0, 1]：使用默认值，并返回提示信息
        """
        if override is None:
            return default_value, None

        if 0.0 <= override <= 1.0:
            return override, None

        note = f"参数 --{name} 超出范围 [0,1]，已回退为默认值 {default_value:.2f}。"
        return default_value, note

    async def _force_like_once_if_needed(
        self,
        service: BaseService,
        feed_list: list[dict],
        *,
        current_qq: str | None,
        allow_self_interact: bool,
    ) -> tuple[bool | None, str | None]:
        """当点赞在本轮未触发时，补执行一次点赞以满足流程要求。"""
        has_candidate = False
        for data in feed_list:
            tid = str(data.get("tid", "") or "").strip()
            if not tid:
                continue

            owner_qq = str(data.get("uin", "") or "") or None
            has_candidate = True
            result = await service.like_shuoshuo(shuoshuo_id=tid, qq_number="", owner_qq=owner_qq)
            if result.is_success:
                return True, None

            reason = self._classify_interaction_failure(service, getattr(result, "error_message", ""))
            return False, reason

        if not has_candidate:
            return None, None
        return False, "其他错误"

    async def _force_comment_once_if_needed(
        self,
        service: BaseService,
        feed_list: list[dict],
        *,
        current_qq: str | None,
        allow_self_interact: bool,
    ) -> tuple[bool | None, str | None]:
        """当评论在本轮未触发时，补执行一次评论以满足流程要求。"""
        has_candidate = False
        for data in feed_list:
            tid = str(data.get("tid", "") or "").strip()
            if not tid:
                continue

            owner_qq = str(data.get("uin", "") or "") or None
            if (not allow_self_interact) and current_qq and owner_qq and owner_qq == current_qq:
                continue
            has_candidate = True
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

            result = await service.comment_shuoshuo(
                shuoshuo_id=tid,
                content=comment_text,
                qq_number="",
                owner_qq=owner_qq,
                comment_id=None,
            )
            if result.is_success:
                return True, None

            reason = self._classify_interaction_failure(service, getattr(result, "error_message", ""))
            return False, reason

        if not has_candidate:
            return None, None
        return False, "其他错误"

    async def execute(self, message_text: str) -> tuple[bool, str]:
        try:
            read_only, no_like, no_comment, allow_self_interact, show_list, count, like_prob_override, comment_prob_override = self._parse_options(message_text)
        except ValueError:
            return False, (
                "参数格式错误，格式: /read_feed [数量] [--read-only] [--no-like] [--no-comment] [--allow-self-interact] [--show-list] "
                "[--like-prob=0.8] [--comment-prob=0.3]"
            )

        count = max(1, min(count, 20))

        from src.app.plugin_system.api.service_api import get_service
        from src.core.components.base import BaseService

        service = get_service("qzone_shuoshuo:service:qzone")
        if not service or not isinstance(service, BaseService):
            return False, "服务未启动"

        result = await service.get_shuoshuo_list(count=count)
        if not result.is_success:
            return False, f"❌ 读取失败: {result.error_message}"

        raw_feed_list = result.data or []
        claim_unread = getattr(service, "claim_unread_shuoshuo", None)
        if callable(claim_unread):
            try:
                feed_list = claim_unread(raw_feed_list, count)
            except Exception:
                feed_list = []
        else:
            feed_list = list(raw_feed_list)
            filter_unread = getattr(service, "filter_unread_shuoshuo", None)
            if callable(filter_unread):
                try:
                    feed_list = filter_unread(feed_list)
                except Exception:
                    pass
            feed_list = feed_list[:count]

        if not feed_list:
            return True, "📭 当前没有可阅读的说说（未读为空）"

        has_claim_lock = callable(claim_unread)

        try:
            current_qq: str | None = None
            get_current_uin = getattr(service, "get_current_uin", None)
            if callable(get_current_uin):
                try:
                    resolved_qq = await get_current_uin()
                    if resolved_qq:
                        current_qq = str(resolved_qq).strip() or None
                except Exception:
                    current_qq = None

            monitor_cfg = getattr(service.config, "monitor", None) if getattr(service, "config", None) else None
            like_probability = float(getattr(monitor_cfg, "like_probability", 0.8)) if monitor_cfg else 0.8
            comment_probability = float(getattr(monitor_cfg, "comment_probability", 0.3)) if monitor_cfg else 0.3

            like_probability, like_prob_note = self._resolve_probability(
                name="like-prob",
                override=like_prob_override,
                default_value=like_probability,
            )
            comment_probability, comment_prob_note = self._resolve_probability(
                name="comment-prob",
                override=comment_prob_override,
                default_value=comment_probability,
            )

            if read_only:
                no_like = True
                no_comment = True

            require_like = not no_like
            require_comment = not no_comment

            liked_count = 0
            commented_count = 0
            like_attempt_count = 0
            comment_attempt_count = 0
            self_comment_skipped_count = 0
            like_failure_reasons: Counter[str] = Counter()
            comment_failure_reasons: Counter[str] = Counter()

            if show_list:
                lines: list[str] = [f"📖 最近 {len(feed_list)} 条说说："]
            else:
                lines = [f"📖 已读取最近 {len(feed_list)} 条说说（已省略列表内容）。"]
            for idx, data in enumerate(feed_list, 1):
                tid = data.get("tid", "")
                content = data.get("content", "") or "[无文字内容]"
                content_preview = content[:50].replace("\n", " ") + ("..." if len(content) > 50 else "")
                created_time = data.get("created_time") or data.get("createTime", "")
                if str(created_time).isdigit():
                    try:
                        time_str = datetime.datetime.fromtimestamp(int(created_time)).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        time_str = str(created_time)
                else:
                    time_str = str(created_time) if created_time else "未知时间"

                if show_list:
                    lines.append(f"{idx}. [{time_str}] ID={tid}")
                    lines.append(f"   {content_preview}")

                if read_only or not tid:
                    continue

                owner_qq = str(data.get("uin", "") or "") or None
                is_self_feed = bool(current_qq and owner_qq and owner_qq == current_qq)

                if (not no_like) and random.random() <= like_probability:
                    like_attempt_count += 1
                    like_result = await service.like_shuoshuo(shuoshuo_id=tid, qq_number="", owner_qq=owner_qq)
                    if like_result.is_success:
                        liked_count += 1
                    else:
                        reason = self._classify_interaction_failure(service, getattr(like_result, "error_message", ""))
                        like_failure_reasons[reason] += 1

                if is_self_feed and (not allow_self_interact):
                    self_comment_skipped_count += 1
                    continue

                if (not no_comment) and random.random() <= comment_probability:
                    comment_attempt_count += 1
                    comment_text = "路过~"
                    try:
                        if hasattr(service, "_generate_comment_text"):
                            generated = await service._generate_comment_text(str(data.get("content", "")), str(data.get("nickname", "") or data.get("uin", "")))
                            if generated:
                                comment_text = generated
                    except Exception:
                        pass

                    comment_result = await service.comment_shuoshuo(
                        shuoshuo_id=tid,
                        content=comment_text,
                        qq_number="",
                        owner_qq=owner_qq,
                        comment_id=None,
                    )
                    if comment_result.is_success:
                        commented_count += 1
                    else:
                        reason = self._classify_interaction_failure(service, getattr(comment_result, "error_message", ""))
                        comment_failure_reasons[reason] += 1

            if (not read_only) and require_like and like_attempt_count == 0:
                forced_like_ok, forced_like_reason = await self._force_like_once_if_needed(
                    service,
                    feed_list,
                    current_qq=current_qq,
                    allow_self_interact=allow_self_interact,
                )
                if forced_like_ok is not None:
                    like_attempt_count += 1
                if forced_like_ok is True:
                    liked_count += 1
                elif forced_like_ok is False and forced_like_reason:
                    like_failure_reasons[forced_like_reason] += 1

            if (not read_only) and require_comment and comment_attempt_count == 0:
                forced_comment_ok, forced_comment_reason = await self._force_comment_once_if_needed(
                    service,
                    feed_list,
                    current_qq=current_qq,
                    allow_self_interact=allow_self_interact,
                )
                if forced_comment_ok is not None:
                    comment_attempt_count += 1
                if forced_comment_ok is True:
                    commented_count += 1
                elif forced_comment_ok is False and forced_comment_reason:
                    comment_failure_reasons[forced_comment_reason] += 1

            seen_preview = []
            for item in feed_list[:2]:
                text = str(item.get("content", "") or "[无文字内容]").replace("\n", " ").strip()
                seen_preview.append(text[:30] + ("..." if len(text) > 30 else ""))

            if read_only:
                lines.append("\n已启用 --read-only，本次仅阅读未互动。")
                lines.append("我刚刚看了这些动态，暂时没有做点赞或评论。")
            else:
                lines.append(f"\n本次互动：点赞 {liked_count} 条，评论 {commented_count} 条。")
                if no_like:
                    lines.append("点赞：已按参数关闭（--no-like）。")
                elif like_attempt_count:
                    lines.append(f"点赞尝试 {like_attempt_count} 次，成功 {liked_count} 次。")
                    if require_like and like_attempt_count == 1 and like_probability <= 0.0:
                        lines.append("已启用流程保障：由于点赞概率为 0，本轮补执行了 1 次点赞。")

                if no_comment:
                    lines.append("评论：已按参数关闭（--no-comment）。")
                elif comment_attempt_count:
                    lines.append(f"评论尝试 {comment_attempt_count} 次，成功 {commented_count} 次。")
                    if require_comment and comment_attempt_count == 1 and comment_probability <= 0.0:
                        lines.append("已启用流程保障：由于评论概率为 0，本轮补执行了 1 次评论。")

                if like_failure_reasons:
                    like_reason_text = "、".join(f"{k}×{v}" for k, v in like_failure_reasons.items())
                    lines.append(f"点赞失败摘要：{like_reason_text}")

                if comment_failure_reasons:
                    comment_reason_text = "、".join(f"{k}×{v}" for k, v in comment_failure_reasons.items())
                    lines.append(f"评论失败摘要：{comment_reason_text}")

                if like_prob_note:
                    lines.append(like_prob_note)
                if comment_prob_note:
                    lines.append(comment_prob_note)

                if self_comment_skipped_count > 0 and not allow_self_interact:
                    lines.append(f"本轮有 {self_comment_skipped_count} 条为本人动态，已按默认策略跳过自评论。")

                lines.append(
                    f"当前互动参数：like_prob={like_probability:.2f}, comment_prob={comment_probability:.2f}, "
                    f"no_like={no_like}, no_comment={no_comment}, allow_self_interact={allow_self_interact}"
                )
                lines.append("我刚刚已经看完并执行了互动。")

            if show_list and seen_preview:
                lines.append(f"我注意到的内容大概是：{'；'.join(seen_preview)}")

            finalize_claim = getattr(service, "finalize_read_claim", None)
            if callable(finalize_claim):
                try:
                    finalize_claim(feed_list, processed=True)
                    lines.append("未读标记：本轮已处理内容已标记为已读。")
                except Exception:
                    lines.append("未读标记：本轮处理完成，但标记已读时发生异常。")
            else:
                mark_read_batch = getattr(service, "mark_shuoshuo_read_batch", None)
                if callable(mark_read_batch):
                    try:
                        mark_read_batch(feed_list)
                        lines.append("未读标记：本轮已处理内容已标记为已读。")
                    except Exception:
                        lines.append("未读标记：本轮处理完成，但标记已读时发生异常。")

            mark_manual_activity = getattr(service, "mark_manual_activity", None)
            if callable(mark_manual_activity):
                try:
                    maybe_result = mark_manual_activity(source="read_feed")
                    if inspect.isawaitable(maybe_result):
                        await maybe_result
                except Exception as e:
                    logger.debug(f"[read_feed] 手动活动计时重置失败（已忽略）: {e}")

            lines.append("你要不要我继续看下一批，或者针对某条内容给你更详细的观察？")

            return True, "\n".join(lines)
        except Exception as e:
            if has_claim_lock:
                finalize_claim = getattr(service, "finalize_read_claim", None)
                if callable(finalize_claim):
                    try:
                        finalize_claim(feed_list, processed=False)
                    except Exception:
                        pass
            logger.error(f"[read_feed] 读取流程异常: {e}")
            return False, f"❌ 读取流程异常: {e}"
