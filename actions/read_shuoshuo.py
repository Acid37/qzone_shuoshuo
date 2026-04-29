"""阅读/浏览QQ空间说说动作"""

from __future__ import annotations

import datetime
import time
from typing import Annotated

from src.app.plugin_system.api.log_api import get_logger

from ._base import QzoneBaseAction

logger = get_logger("qzone_shuoshuo")


class ReadShuoshuoAction(QzoneBaseAction):
    """阅读/浏览QQ空间说说动作

    获取指定用户的说说列表，返回格式化内容供 AI 分析决策。
    """

    action_name = "read_shuoshuo"
    action_description = (
        "读取指定QQ用户的空间说说列表，返回每条说说的正文、图片数、评论数。\n"
        "一般用于查看好友近况或了解某人的动态。"
    )

    async def execute(
        self,
        count: Annotated[int | None, "获取数量，默认10条"] = 10,
        offset: Annotated[int | None, "偏移量，用于翻页，默认0"] = 0,
        qq_number: Annotated[str | None, "QQ号，留空查看自己的说说"] = None,
        **kwargs,
    ) -> tuple[bool, str]:
        """执行阅读说说动作"""
        effective_count = min(max(1, count or 10), 50)
        effective_offset = max(0, offset or 0)

        service = await self._get_service()
        if service is None:
            return False, "阅读说说失败：服务未启动，请检查插件是否正确加载"

        result = await service.get_shuoshuo_list(
            qq_number=qq_number or "",
            count=effective_count + effective_offset,
        )

        if not result.is_success:
            return False, f"阅读说说失败: {result.error_message or '未知错误'}"

        shuoshuo_list = list(result.data or [])
        if effective_offset > 0:
            shuoshuo_list = shuoshuo_list[effective_offset:]

        # 尝试 claim unread（如果服务支持）
        claim_unread = getattr(service, "claim_unread_shuoshuo", None)
        if callable(claim_unread):
            try:
                shuoshuo_list = list(claim_unread(shuoshuo_list, effective_count))
            except Exception:
                pass
        else:
            filter_unread = getattr(service, "filter_unread_shuoshuo", None)
            if callable(filter_unread):
                try:
                    shuoshuo_list = list(filter_unread(shuoshuo_list))[:effective_count]
                except Exception:
                    pass

        if not shuoshuo_list:
            return True, "暂无未读说说内容（已全部处理）"

        # 格式化输出
        lines = [f"📖 QQ空间说说列表（共 {len(shuoshuo_list)} 条）\n", "=" * 40]

        for i, item in enumerate(shuoshuo_list, 1):
            tid = item.get("tid", "")
            content = item.get("content", "")
            create_time = item.get("created_time") or item.get("createTime", "")
            nickname = item.get("nickname", "") or item.get("uin", "")
            comment_count = len(item.get("commentlist", []) or [])
            pic_count = len(item.get("pic", []) or [])

            time_str = str(create_time)
            if str(create_time).isdigit():
                try:
                    time_str = datetime.datetime.fromtimestamp(int(create_time)).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            pic_info = f" [含 {pic_count} 张图片]" if pic_count else ""
            display = content[:100] + "..." if len(content) > 100 else content
            if not display:
                display = "[无文字内容]" + pic_info

            lines.append(f"\n{i}. 【{nickname}】{time_str}")
            lines.append(f"   ID: {tid}")
            lines.append(f"   {display}{pic_info}")
            lines.append(f"   评论数: {comment_count}")

        lines.append("\n" + "=" * 40)

        preview_list = []
        for item in shuoshuo_list[:2]:
            p = str(item.get("content", "") or "[无文字内容]").replace("\n", " ").strip()
            preview_list.append(p[:30] + ("..." if len(p) > 30 else ""))

        lines.append("阅读完成，可根据内容自行决定是否进行评论或点赞。")
        if preview_list:
            lines.append(f"内容摘要：{'；'.join(preview_list)}")

        # 保存快照供后续 finalize
        snapshot = {
            "ts": time.time(),
            "target_qq": qq_number or "",
            "count": len(shuoshuo_list),
            "preview": preview_list,
            "first_tid": str(shuoshuo_list[0].get("tid", "") or "").strip(),
        }
        try:
            setattr(self, "_last_read_snapshot", snapshot)
        except Exception:
            pass

        return True, "\n".join(lines)
