"""Qzone 状态持久化管理器。

管理说说已读追踪、已评论追踪、已回复评论追踪、发布历史等运行时状态，
并提供 JSON 文件持久化与并发防重机制。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger("qzone_shuoshuo")


class StateManager:
    """运行时状态管理器。

    管理以下状态：
    - _read_tids: 已读说说追踪 (tid -> timestamp)
    - _commented_tids: 已评论说说追踪 (tid -> timestamp)
    - _replied_comments: 已回复评论追踪 (fid_comment_id)
    - _processing_read_tids: 正在处理中的说说（并发防重）
    - _processing_comments: 正在处理中的评论（并发防重）
    - _published_text_history: 发布文本历史
    - _last_tid: 最新说说 TID 基线
    - _last_read_snapshot: 最近阅读摘要
    - _last_published_content_hash / _last_published_at: 发布去重
    """

    STATE_FILENAME = "monitor_state.json"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

        # 说说追踪
        self._last_tid: str | None = None
        self._commented_tids: dict[str, float] = {}
        self._replied_comments: set[str] = set()
        self._read_tids: dict[str, float] = {}
        self._processing_read_tids: set[str] = set()
        self._processing_comments: set[str] = set()

        # 发布去重
        self._last_published_content_hash: str | None = None
        self._last_published_at: float = 0.0
        self._published_text_history: list[dict[str, Any]] = []

        # 阅读摘要
        self._last_read_snapshot: dict[str, Any] | None = None

        self._load_state()

    # ---- 持久化 ----

    def _state_file(self) -> Path:
        return self._data_dir / self.STATE_FILENAME

    def _load_state(self) -> None:
        state_file = self._state_file()
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_tid = data.get("last_tid")
                    self._commented_tids = data.get("commented_tids", {})
                    self._replied_comments = set(data.get("replied_comments", []))
                    self._last_read_snapshot = data.get("last_read_snapshot")
                    self._read_tids = data.get("read_tids", {})
                    self._published_text_history = data.get("published_text_history", [])
            except Exception as e:
                logger.warning(f"加载监控状态失败: {e}")

    def save_state(self) -> None:
        try:
            state_file = self._state_file()
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "last_tid": self._last_tid,
                    "commented_tids": self._commented_tids,
                    "replied_comments": list(self._replied_comments),
                    "last_read_snapshot": self._last_read_snapshot,
                    "read_tids": self._read_tids,
                    "published_text_history": self._published_text_history,
                }, f)
        except Exception as e:
            logger.error(f"保存监控状态失败: {e}")

    # ---- 已读追踪 ----

    def _trim_read_tids(self, keep_max: int = 2000) -> None:
        """限制已读追踪规模，避免状态无限增长。"""
        if len(self._read_tids) <= keep_max:
            return
        ordered = sorted(self._read_tids.items(), key=lambda kv: float(kv[1] or 0.0), reverse=True)
        self._read_tids = dict(ordered[:keep_max])

    def is_shuoshuo_read(self, tid: str) -> bool:
        """判断说说是否已读。"""
        key = str(tid or "").strip()
        if not key:
            return False
        return key in self._read_tids

    def mark_shuoshuo_read(self, tid: str) -> None:
        """标记说说为已读。"""
        key = str(tid or "").strip()
        if not key:
            return
        self._read_tids[key] = time.time()
        self._trim_read_tids()
        self.save_state()

    def mark_shuoshuo_read_batch(self, items: list[dict[str, Any]]) -> None:
        """批量标记说说为已读。"""
        changed = False
        now_ts = time.time()
        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue
            self._read_tids[tid] = now_ts
            changed = True

        if changed:
            self._trim_read_tids()
            self.save_state()

    def filter_unread_shuoshuo(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按已读追踪过滤出未读说说列表。"""
        unread: list[dict[str, Any]] = []
        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue
            if tid in self._read_tids:
                continue
            unread.append(item)
        return unread

    # ---- 候选统计 ----

    def count_pending_candidates(self, items: list[dict[str, Any]]) -> int:
        """统计列表中的候选数量（排除已读与处理中）。"""
        count = 0
        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue
            if tid in self._read_tids:
                continue
            if tid in self._processing_read_tids:
                continue
            count += 1
        return count

    def count_interactable_candidates(self, items: list[dict[str, Any]], *, current_qq: str | None) -> int:
        """统计可互动候选数量（未读且排除本人与已评论）。"""
        count = 0
        resolved_current_qq = str(current_qq or "").strip() or None

        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue

            if tid in self._read_tids:
                continue
            if tid in self._processing_read_tids:
                continue

            owner_qq = str(item.get("uin", "") or "").strip() or None
            if resolved_current_qq and owner_qq and owner_qq == resolved_current_qq:
                continue

            if self.is_commented(tid):
                continue

            count += 1

        return count

    # ---- 并发防重：说说领取 ----

    def claim_unread_shuoshuo(self, items: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
        """领取未读说说用于处理（并发防重）。

        规则：
        - 已读跳过
        - 已在处理中跳过
        - 领取后立刻加入处理中集合，直到 finalize
        """
        claimed: list[dict[str, Any]] = []
        max_count = int(limit) if limit is not None else None

        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue
            if tid in self._read_tids:
                continue
            if tid in self._processing_read_tids:
                continue

            self._processing_read_tids.add(tid)
            claimed.append(item)

            if max_count is not None and len(claimed) >= max_count:
                break

        return claimed

    def finalize_read_claim(self, items: list[dict[str, Any]], processed: bool = True) -> None:
        """结束未读领取。

        Args:
            items: 本轮领取并处理的说说
            processed: 是否处理成功；成功时写入已读，失败仅解锁处理中
        """
        changed = False
        now_ts = time.time()

        for item in items:
            tid = str(item.get("tid", "") or "").strip()
            if not tid:
                continue

            if tid in self._processing_read_tids:
                self._processing_read_tids.remove(tid)

            if processed:
                self._read_tids[tid] = now_ts
                changed = True

        if changed:
            self._trim_read_tids()
            self.save_state()

    # ---- 并发防重：评论处理 ----

    def is_comment_processing(self, comment_key: str) -> bool:
        return comment_key in self._processing_comments

    def lock_comment(self, comment_key: str) -> None:
        self._processing_comments.add(comment_key)

    def unlock_comment(self, comment_key: str) -> None:
        if comment_key in self._processing_comments:
            self._processing_comments.remove(comment_key)

    # ---- 评论追踪 ----

    def mark_commented(self, tid: str) -> None:
        """标记说说已被评论"""
        self._commented_tids[tid] = time.time()
        logger.debug(f"[评论追踪] 标记说说 {tid} 已评论")

    def is_commented(self, tid: str) -> bool:
        """检查说说是否已被评论"""
        return tid in self._commented_tids

    def mark_comment_replied(self, fid: str, comment_id: str) -> None:
        """标记评论已被回复"""
        key = f"{fid}_{comment_id}"
        self._replied_comments.add(key)
        self.save_state()
        logger.debug(f"[评论回复追踪] 标记 {fid}_{comment_id} 已回复")

    def has_replied_comment(self, fid: str, comment_id: str) -> bool:
        """检查评论是否已被回复"""
        key = f"{fid}_{comment_id}"
        return key in self._replied_comments

    # ---- 发布历史 ----

    def remember_published_text(self, text: str, keep_max: int = 20) -> None:
        """记录已发布文本历史（用于后续提示词防重复）。"""
        cleaned = str(text or "").strip()
        if not cleaned:
            return

        self._published_text_history.append({"text": cleaned, "ts": time.time()})
        if len(self._published_text_history) > keep_max:
            self._published_text_history = self._published_text_history[-keep_max:]
        self.save_state()

    def build_publish_history_block(self, limit: int = 5) -> str:
        """构建最近发布历史块，帮助模型避免语义重复。"""
        history = list(self._published_text_history or [])
        if not history:
            return ""

        recent = history[-max(1, limit):]
        lines: list[str] = []
        for item in reversed(recent):
            text = str(item.get("text", "") or "").replace("\n", " ").strip()
            if not text:
                continue
            lines.append(f"- {text[:120]}")

        if not lines:
            return ""

        return "最近已发布内容（请避免语义重复）：\n" + "\n".join(lines)

    # ---- 发布去重 ----

    def check_publish_duplicate(self, content_hash: str, window_seconds: float = 300) -> bool:
        """检查是否在时间窗口内重复发布。

        Returns:
            True 表示命中重复，应跳过。
        """
        now_ts = time.time()
        return (
            self._last_published_content_hash == content_hash
            and (now_ts - self._last_published_at) <= window_seconds
        )

    def record_publish(self, content_hash: str) -> None:
        """记录一次成功发布。"""
        self._last_published_content_hash = content_hash
        self._last_published_at = time.time()

    # ---- 阅读摘要 ----

    def remember_last_read_snapshot(self, snapshot: dict[str, Any]) -> None:
        """记录最近一次阅读摘要，供下一轮复用。"""
        self._last_read_snapshot = snapshot
        self.save_state()

    def get_last_read_snapshot(self) -> dict[str, Any] | None:
        """获取最近一次阅读摘要。"""
        return self._last_read_snapshot

    # ---- TID 基线 ----

    @property
    def last_tid(self) -> str | None:
        return self._last_tid

    @last_tid.setter
    def last_tid(self, value: str | None) -> None:
        self._last_tid = value