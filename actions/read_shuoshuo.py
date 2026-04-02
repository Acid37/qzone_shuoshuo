"""阅读/浏览QQ空间说说动作"""

from __future__ import annotations

from typing import Annotated, Any

from src.core.components.base import BaseAction
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")


class ReadShuoshuoAction(BaseAction):
    """阅读/浏览QQ空间说说动作

    让 AI 主动前往 QQ 空间阅读说说内容，返回说说列表供分析决策。
    AI 可以根据说说内容决定是否评论、点赞等操作。
    """

    action_name = "read_shuoshuo"
    action_description = (
        "前往QQ空间阅读/浏览说说内容。\n"
        "获取指定用户的说说列表，供 AI 分析和决策后续操作（评论、点赞等）。\n"
        "注意：默认只阅读；但当最近用户消息中明确要求点赞/评论时，本动作会在阅读后自动补执行要求的互动部分，避免流程中断。\n"
        "参数说明：\n"
        "- qq_number: QQ号（可选），留空则查看 Bot 自己的说说\n"
        "- count: 获取数量（可选），默认 10 条\n"
        "- offset: 偏移量（可选），用于翻页\n"
        "使用示例：\n"
        "1. 查看自己的说说：action=read_shuoshuo, count=10\n"
        "2. 查看某用户说说：action=read_shuoshuo, qq_number='123456789', count=20\n"
        "3. 翻页查看：action=read_shuoshuo, offset=10, count=10\n"
    )

    _LIKE_INTENT_KEYWORDS = ("点赞", "点个赞", "赞一下", "点个❤️", "点个❤")
    _COMMENT_INTENT_KEYWORDS = ("评论", "回复", "回一句", "回一下", "夸夸")
    _INTERACT_INTENT_KEYWORDS = ("互动", "回他", "理他", "搭话")

    def _get_plugin_config(self) -> Any:
        """获取插件配置"""
        try:
            plugin_obj = getattr(self, "plugin", None)
            config_obj = getattr(plugin_obj, "config", None)
            return config_obj
        except Exception:
            return None

    def _extract_recent_user_text(self) -> str:
        """提取最近一条用户侧文本，用于判断是否有明确互动意图。"""
        try:
            context = getattr(self.chat_stream, "context", None)
            if context is None:
                return ""

            candidates: list[Any] = []
            unread = getattr(context, "unread_messages", None) or []
            history = getattr(context, "history_messages", None) or []
            current = getattr(context, "current_message", None)

            if unread:
                candidates.append(unread[-1])
            if current is not None:
                candidates.append(current)
            if history:
                candidates.append(history[-1])

            for msg in candidates:
                text = str(getattr(msg, "processed_plain_text", "") or getattr(msg, "content", "") or "").strip()
                if text:
                    return text
        except Exception:
            return ""

        return ""

    def _detect_interaction_intent(self) -> tuple[bool, bool, bool, str]:
        """检测最近用户消息中的互动意图。

        Returns:
            tuple[bool, bool, bool, str]:
            (是否需要互动, 是否需要点赞, 是否需要评论, 触发文本)
        """
        text = self._extract_recent_user_text()
        if not text:
            return False, False, False, ""

        normalized = text.replace(" ", "")
        want_like = any(kw in normalized for kw in self._LIKE_INTENT_KEYWORDS)
        want_comment = any(kw in normalized for kw in self._COMMENT_INTENT_KEYWORDS)
        weak_interact = any(kw in normalized for kw in self._INTERACT_INTENT_KEYWORDS)
        want_interact = bool(want_like or want_comment or weak_interact)

        return want_interact, want_like, want_comment, text

    async def _build_comment_text_for_auto_interaction(self, service: Any, item: dict[str, Any]) -> str:
        """构造自动互动评论文本，优先调用服务层 AI 生成。"""
        default_text = "路过支持一下，祝你今天顺顺利利～"
        try:
            generator = getattr(service, "_generate_comment_text", None)
            if callable(generator):
                generated = await generator(
                    str(item.get("content", "") or ""),
                    str(item.get("nickname", "") or item.get("uin", "") or "好友"),
                )
                text = str(generated or "").strip()
                if text:
                    return text
        except Exception:
            return default_text

        return default_text

    async def _auto_interact_if_requested(self, service: Any, item: dict[str, Any]) -> list[str]:
        """在检测到明确互动意图时，自动补执行点赞/评论。"""
        lines: list[str] = []
        want_interact, want_like, want_comment, intent_text = self._detect_interaction_intent()
        if not want_interact:
            return lines

        tid = str(item.get("tid", "") or "").strip()
        owner_qq = str(item.get("uin", "") or "").strip() or None
        if not tid:
            lines.append("【流程守卫】检测到互动意图，但当前说说缺少 tid，无法自动补执行。")
            return lines

        # 仅出现泛互动词（如“互动一下”）时，默认执行点赞 + 评论闭环。
        if not want_like and not want_comment:
            want_like = True
            want_comment = True

        lines.append("【流程守卫】interaction_required=true")
        lines.append(f"【用户意图】{intent_text}")
        lines.append("【下一轮约束】互动已被用户明确要求：请先汇报本轮互动结果，再决定是否继续下一批。")

        if want_like:
            like_result = await service.like_shuoshuo(
                shuoshuo_id=tid,
                qq_number="",
                owner_qq=owner_qq,
            )
            if getattr(like_result, "is_success", False):
                lines.append(f"【自动补执行】点赞已完成 tid={tid}")
            else:
                err = str(getattr(like_result, "error_message", "未知错误") or "未知错误")
                lines.append(f"【自动补执行】点赞失败 tid={tid}, reason={err}")

        if want_comment:
            comment_text = await self._build_comment_text_for_auto_interaction(service, item)
            comment_result = await service.comment_shuoshuo(
                shuoshuo_id=tid,
                content=comment_text,
                qq_number="",
                owner_qq=owner_qq,
                comment_id=None,
            )
            if getattr(comment_result, "is_success", False):
                lines.append(f"【自动补执行】评论已完成 tid={tid}")
            else:
                err = str(getattr(comment_result, "error_message", "未知错误") or "未知错误")
                lines.append(f"【自动补执行】评论失败 tid={tid}, reason={err}")

        lines.append("【执行回执】task=interaction_followup,status=done_or_attempted")
        return lines

    async def execute(
        self,
        count: Annotated[int | None, "获取数量，默认10条"] = 10,
        offset: Annotated[int | None, "偏移量，用于翻页，默认0"] = 0,
        qq_number: Annotated[str | None, "QQ号，留空查看自己的说说"] = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行阅读说说动作

        Args:
            count: 获取数量
            offset: 偏移量
            qq_number: QQ号
            **kwargs: 其他参数
        """
        logger.info(f"[ReadShuoshuo] 开始执行, qq={qq_number or '自动获取'}, count={count}, offset={offset}")

        # 验证数量
        effective_count = min(max(1, count or 10), 50)  # 限制 1-50
        effective_offset = max(0, offset or 0)

        # 获取服务
        try:
            from src.app.plugin_system.api.service_api import get_service
            from src.core.components.base import BaseService

            logger.debug("[ReadShuoshuo] 获取 Qzone 服务")
            service = get_service("qzone_shuoshuo:service:qzone")
            if service is None or not isinstance(service, BaseService):
                logger.error("[ReadShuoshuo] 服务未启动")
                return False, "阅读说说失败：服务未启动，请检查插件是否正确加载"
            logger.debug("[ReadShuoshuo] 服务获取成功")
        except Exception as e:
            logger.error(f"[ReadShuoshuo] 获取服务异常: {e}", exc_info=True)
            return False, f"阅读说说失败：获取服务异常 {str(e)}"

        # 调用服务获取说说列表
        claimed_items: list[dict[str, Any]] = []
        has_claim_lock = False
        try:
            logger.info(f"[ReadShuoshuo] 获取说说列表, count={effective_count}, offset={effective_offset}")
            result = await service.get_shuoshuo_list(
                qq_number=qq_number or "",
                count=effective_count + effective_offset,  # 服务层只支持 count，需要自行偏移
            )

            if not result.is_success:
                error_msg = result.error_message or "未知错误"
                logger.error(f"[ReadShuoshuo] 获取说说列表失败: {error_msg}")
                return False, f"阅读说说失败: {error_msg}"

            shuoshuo_list = result.data or []
            logger.info(f"[ReadShuoshuo] 获取到 {len(shuoshuo_list)} 条说说")

            # 应用偏移
            if effective_offset > 0:
                if len(shuoshuo_list) <= effective_offset:
                    shuoshuo_list = []
                else:
                    shuoshuo_list = shuoshuo_list[effective_offset:]

            claim_unread = getattr(service, "claim_unread_shuoshuo", None)
            if callable(claim_unread):
                has_claim_lock = True
                try:
                    shuoshuo_list = claim_unread(shuoshuo_list, effective_count)
                    claimed_items = list(shuoshuo_list)
                except Exception:
                    shuoshuo_list = []
                    claimed_items = []
            else:
                # 兼容旧实现（无 claim/finalize）
                filter_unread = getattr(service, "filter_unread_shuoshuo", None)
                if callable(filter_unread):
                    try:
                        shuoshuo_list = filter_unread(shuoshuo_list)
                        shuoshuo_list = shuoshuo_list[:effective_count]
                    except Exception:
                        pass

            if not shuoshuo_list:
                return True, "暂无未读说说内容（已全部处理）"

            # 格式化输出
            import datetime
            lines = []
            lines.append(f"📖 QQ空间说说列表（共 {len(shuoshuo_list)} 条）\n")
            lines.append("=" * 40)

            for i, item in enumerate(shuoshuo_list, 1):
                tid = item.get("tid", "")
                content = item.get("content", "")
                create_time = item.get("created_time") or item.get("createTime", "")
                nickname = item.get("nickname", "") or item.get("uin", "")
                commentlist = item.get("commentlist", []) or []

                # 格式化时间
                time_str = str(create_time)
                if str(create_time).isdigit():
                    try:
                        time_str = datetime.datetime.fromtimestamp(int(create_time)).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass

                # 获取图片信息
                pic_list = item.get("pic", []) or []
                pic_info = f" [含 {len(pic_list)} 张图片]" if pic_list else ""

                # 内容截断
                display_content = content[:100] + "..." if len(content) > 100 else content
                if not display_content:
                    display_content = "[无文字内容]" + pic_info

                lines.append(f"\n{i}. 【{nickname}】{time_str}")
                lines.append(f"   ID: {tid}")
                lines.append(f"   {display_content}{pic_info}")
                lines.append(f"   评论数: {len(commentlist)}")

            lines.append("\n" + "=" * 40)
            preview_list = []
            for item in shuoshuo_list[:2]:
                preview = str(item.get("content", "") or "[无文字内容]").replace("\n", " ").strip()
                preview_list.append(preview[:30] + ("..." if len(preview) > 30 else ""))

            lines.append("我刚刚已经完成阅读（这是子步骤完成，不代表互动任务已结束）。")
            if preview_list:
                lines.append(f"我看到的重点大概是：{'；'.join(preview_list)}")
            lines.append("【执行回执】task=read_shuoshuo,status=step_done")
            lines.append("【流程状态】workflow=interaction_pending_if_requested")
            lines.append("【流程守卫】interaction_required=false,report_required=true")
            lines.append("【下一轮约束】请先向用户汇报本次阅读结论；在未完成汇报前，禁止重复调用 read_shuoshuo。")
            lines.append("【下一轮上下文】我刚刚已经完成“阅读说说”；如果用户要求互动，本任务仍未完成，必须继续调用 comment_shuoshuo 或 like_shuoshuo，而不是重复 read_shuoshuo。")

            import time
            snapshot = {
                "ts": time.time(),
                "target_qq": qq_number or "",
                "count": len(shuoshuo_list),
                "preview": preview_list,
                "first_tid": str(shuoshuo_list[0].get("tid", "") or "").strip(),
            }
            remember_snapshot = getattr(service, "remember_last_read_snapshot", None)
            if callable(remember_snapshot):
                try:
                    remember_snapshot(snapshot)
                    lines.append("【会话缓存】read_snapshot_saved=true")
                except Exception:
                    lines.append("【会话缓存】read_snapshot_saved=false")

            lines.append("如果你希望，我可以继续下一步：")
            lines.append("- 帮你挑一条更适合互动的说说")
            lines.append("- 直接去点赞或生成自然评论")

            first_item = shuoshuo_list[0]
            auto_followup_lines = await self._auto_interact_if_requested(service, first_item)
            if auto_followup_lines:
                lines.extend(["", *auto_followup_lines])

            finalize_claim = getattr(service, "finalize_read_claim", None)
            if callable(finalize_claim):
                try:
                    finalize_claim(shuoshuo_list, processed=True)
                    lines.append("【读取标记】unread_marked=true")
                except Exception:
                    lines.append("【读取标记】unread_marked=false")
            else:
                mark_read_batch = getattr(service, "mark_shuoshuo_read_batch", None)
                if callable(mark_read_batch):
                    try:
                        mark_read_batch(shuoshuo_list)
                        lines.append("【读取标记】unread_marked=true")
                    except Exception:
                        lines.append("【读取标记】unread_marked=false")

            first_tid = str(first_item.get("tid", "") or "").strip()
            first_owner = str(first_item.get("uin", "") or "").strip()
            if first_tid:
                lines.append("\n建议下一步（若用户要求评论）：")
                if first_owner:
                    lines.append(
                        f"action=comment_shuoshuo, shuoshuo_id='{first_tid}', owner_qq='{first_owner}', content='...'")
                else:
                    lines.append(f"action=comment_shuoshuo, shuoshuo_id='{first_tid}', content='...'")

            result_text = "\n".join(lines)
            logger.info(f"[ReadShuoshuo] 阅读成功，共 {len(shuoshuo_list)} 条说说")
            return True, result_text

        except Exception as e:
            if has_claim_lock and claimed_items:
                finalize_claim = getattr(service, "finalize_read_claim", None)
                if callable(finalize_claim):
                    try:
                        finalize_claim(claimed_items, processed=False)
                    except Exception:
                        pass
            logger.error(f"[ReadShuoshuo] 读取说说时发生异常: {e}", exc_info=True)
            return False, f"阅读说说时发生异常: {str(e)}"
