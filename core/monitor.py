"""Qzone 监控调度模块。

负责自动监控的启动/停止、定时轮询、静默窗口判断、
冷却管理、启动重试、通知推送等调度逻辑。
"""

from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any, TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from .http_client import QzoneHttpClient
    from .state_manager import StateManager
    from .feed_ops import FeedOperations
    from .interaction import InteractionOps

logger = get_logger("qzone_shuoshuo")


class MonitorScheduler:
    """监控调度器。

    负责：
    - start_monitor / stop_monitor: 启动/停止定时监控
    - _run_auto_monitor: 单轮监控执行
    - check_new_shuoshuo: 检查新说说并触发互动
    - 静默窗口、冷却、启动重试等调度策略
    - 通知推送
    """

    MAX_PROCESS_PER_CYCLE = 5

    def __init__(
        self,
        http: "QzoneHttpClient",
        state: "StateManager",
        feeds: "FeedOperations",
        interact: "InteractionOps",
        get_config,
        get_qq_from_napcat,
    ) -> None:
        self._http = http
        self._state = state
        self._feeds = feeds
        self._interact = interact
        self._get_config = get_config
        self._get_qq_from_napcat = get_qq_from_napcat

        # 运行状态
        self._running: bool = False
        self._config: dict[str, Any] = {}
        self._cycle_lock: asyncio.Lock = asyncio.Lock()
        self._cooldown_until: float = 0.0

        # 可观测性
        self._last_run_at: float = 0.0
        self._last_source: str = ""
        self._last_force: bool = False
        self._last_result: str = "never"
        self._last_error: str = ""
        self._last_skip_reason: str = ""

        # 启动重试
        self._startup_retry_active: bool = False
        self._startup_retry_attempt: int = 0
        self._startup_retry_max_attempts: int = 0
        self._startup_retry_interval: int = 0
        self._startup_retry_job_name: str = ""
        self._startup_retry_last_reason: str = ""

    # ---- 属性访问 ----

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    @property
    def cooldown_until(self) -> float:
        return self._cooldown_until

    @cooldown_until.setter
    def cooldown_until(self, value: float) -> None:
        self._cooldown_until = value

    # ---- 启动/停止 ----

    async def start(self, config: dict[str, Any]) -> dict[str, Any]:
        """启动自动监控。"""
        try:
            from src.kernel.scheduler import get_unified_scheduler, TriggerType

            self._ensure_attrs()

            if not self._is_monitor_enabled():
                logger.warning("[自动监控] 启动被拒绝：监控总开关已关闭")
                return {
                    "success": False,
                    "message": "监控总开关已关闭，请在 config.toml 中将 [monitor].enabled 设为 true",
                }

            self._config = config.copy()
            self._resolve_config_defaults()

            self._cooldown_until = 0.0
            self._reset_observability()

            scheduler = get_unified_scheduler()
            job_name = f"qzone_auto_monitor_{id(self)}"

            if self._running:
                await self._remove_job(scheduler, job_name)

            interval = self._config["interval"]
            if hasattr(scheduler, "add_job"):
                interval_trigger = getattr(TriggerType, "INTERVAL", None) or TriggerType.TIME
                job_id = await scheduler.add_job(
                    func=self._run_auto_monitor,
                    trigger=interval_trigger,
                    seconds=interval,
                    id=job_name,
                    replace_existing=True,
                )
            elif hasattr(scheduler, "create_schedule"):
                job_id = await scheduler.create_schedule(
                    callback=self._run_auto_monitor,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": interval},
                    is_recurring=True,
                    task_name=job_name,
                    force_overwrite=True,
                )
            else:
                raise RuntimeError("当前调度器不支持 add_job/create_schedule 接口")

            self._running = True
            logger.info(f"[自动监控] 已启动，间隔 {interval} 秒")

            # 首轮执行
            connection_ready = await self._wait_for_connection()
            startup_result = "unknown"
            if connection_ready:
                try:
                    startup_result = await self._run_auto_monitor(force=True, source="startup_immediate")
                except Exception as e:
                    startup_result = f"error: {e}"
                    logger.warning(f"[自动监控] 首轮执行异常: {e}")

            if not connection_ready or startup_result == "skip_no_qq":
                await self._schedule_startup_retry(config)

            return {
                "success": True,
                "message": f"监控已启动，间隔 {interval} 秒",
                "job_id": job_id,
                "startup_retry_active": self._startup_retry_active,
                "startup_retry_attempt": self._startup_retry_attempt,
                "startup_retry_max_attempts": self._startup_retry_max_attempts,
                "startup_retry_interval": self._startup_retry_interval,
            }
        except Exception as e:
            logger.error(f"[自动监控] 启动失败: {e}")
            return {"success": False, "message": str(e)}

    async def stop(self) -> dict[str, Any]:
        """停止自动监控。"""
        try:
            from src.kernel.scheduler import get_unified_scheduler

            self._running = bool(getattr(self, "_running", False))
            self._cooldown_until = 0.0

            scheduler = get_unified_scheduler()
            await self._remove_job(scheduler, f"qzone_auto_monitor_{id(self)}")

            self._running = False
            await self._stop_startup_retry()
            logger.info("[自动监控] 已停止")
            return {"success": True, "message": "监控已停止"}
        except Exception as e:
            logger.error(f"[自动监控] 停止失败: {e}")
            return {"success": False, "message": str(e)}

    # ---- 单轮执行 ----

    async def _run_auto_monitor(self, *, force: bool = False, source: str = "scheduled") -> str:
        """执行自动监控任务。"""
        lock = getattr(self, "_cycle_lock", None)
        if not isinstance(lock, asyncio.Lock):
            lock = asyncio.Lock()
            self._cycle_lock = lock

        if lock.locked():
            self._record_skip("skip_busy", source, force)
            return "skip_busy"

        async with lock:
            logger.debug(f"[自动监控] 开始检查(force={force}, source={source})")
            self._last_run_at = time.time()
            self._last_source = source
            self._last_force = force
            self._last_error = ""
            self._last_skip_reason = ""

            try:
                result_code = await self.check_new_shuoshuo(force=force, source=source)
                normalized = str(result_code or "unknown")
                if normalized.startswith("skip_"):
                    self._last_result = "skipped"
                    self._last_skip_reason = normalized
                else:
                    self._last_result = "ok"

                self._log_heartbeat(source, force)
                return normalized
            except Exception as e:
                self._last_result = "error"
                self._last_error = str(e)
                logger.error(f"[自动监控] 本轮执行异常(source={source}): {e}")
                self._log_heartbeat(source, force)
                return "error"

    # ---- 检查新说说 ----

    async def check_new_shuoshuo(self, *, force: bool = False, source: str = "scheduled") -> str:
        """检查并广播新说说。"""
        if not self._is_monitor_enabled():
            logger.debug("[说说监控] 监控总开关关闭，跳过")
            return "skip_disabled"

        if not self._running:
            logger.debug("[说说监控] 监控未启动，跳过")
            return "skip_not_running"

        now_ts = time.time()
        if not force and self._cooldown_until > now_ts:
            remaining = int(self._cooldown_until - now_ts)
            logger.debug(f"[说说监控] 冷却中，剩余约 {remaining}s")
            return "skip_cooldown"

        if not force and self._is_in_quiet_hours():
            logger.debug("[说说监控] 处于静默时间窗口")
            return "skip_quiet_hours"

        if force:
            logger.info(f"[说说监控] 强制执行本轮（source={source}）")

        current_qq = await self._get_qq_from_napcat()
        if not current_qq:
            logger.warning("[说说监控] 无法获取 QQ 号")
            return "skip_no_qq"

        # 回复自己说说的评论
        monitor_cfg = self._get_config()
        if bool(getattr(monitor_cfg, "enable_auto_reply_comments", True)):
            await self._interact.check_and_reply_own_feed_comments(current_qq)

        # 获取动态源
        feed_source = str(getattr(monitor_cfg, "feed_source", "friend_flow") or "friend_flow").strip().lower()
        if feed_source == "friend_flow":
            friend_feed_count = max(5, min(int(getattr(monitor_cfg, "friend_feed_count", 20) or 20), 50))
            logger.info(f"[说说监控] 监控源=好友动态流, count={friend_feed_count}")
            result = await self._feeds.get_friend_feed_list(count=friend_feed_count)
        else:
            logger.info("[说说监控] 监控源=自己空间列表")
            result = await self._feeds.get_list(current_qq, count=5)

        if not result.is_success:
            logger.warning(f"[说说监控] 获取数据失败: {result.error_message}")
            return "skip_list_failed"

        latest_list = result.data or []
        if not latest_list:
            return "skip_empty_list"

        pending = self._state.count_pending_candidates(latest_list)
        interactable = self._state.count_interactable_candidates(latest_list, current_qq=current_qq)
        logger.info(
            f"[说说监控] 候选说说 {pending} 条，可互动 {interactable} 条"
        )

        latest_tid = latest_list[0].get("tid")

        if self._state.last_tid is None:
            self._state.last_tid = latest_tid
            self._state.save_state()
            logger.info(f"[说说监控] 初始化完成，基线 TID: {latest_tid}")
            return "baseline_initialized"

        if latest_tid != self._state.last_tid:
            new_items = []
            for item in latest_list:
                if item.get("tid") == self._state.last_tid:
                    break
                new_items.append(item)

            if new_items:
                await self._process_new_items(new_items, current_qq)
                self._state.last_tid = latest_tid
                self._state.save_state()
                return "processed_new_items"
            else:
                return "no_change"
        else:
            return "no_change"

    async def _process_new_items(self, new_items: list[dict], current_qq: str) -> None:
        """处理新检测到的说说。"""
        stats = {"detected": len(new_items), "notified": 0,
                 "like_success": 0, "like_failed": 0,
                 "comment_success": 0, "comment_failed": 0}
        processed = 0
        skipped_liked = 0

        logger.info(f"[说说监控] 检测到 {len(new_items)} 条新说说，单轮最多处理 {self.MAX_PROCESS_PER_CYCLE} 条")

        for item in reversed(new_items):
            if processed >= self.MAX_PROCESS_PER_CYCLE:
                remaining = len(new_items) - processed
                logger.info(f"[说说监控] 单轮已达上限，剩余 {remaining} 条下轮处理")
                break

            tid = item.get("tid", "")

            if item.get("is_liked") and self._config.get("auto_like"):
                logger.debug(f"[批量处理] 说说 {tid} 已点赞，跳过互动")
                skipped_liked += 1
                continue

            await self._notify_new_shuoshuo(item)
            stats["notified"] += 1

            if self._config.get("auto_comment"):
                cr = await self._interact.auto_comment_if_enabled(item, current_qq=current_qq)
                if cr is True:
                    stats["comment_success"] += 1
                elif cr is False:
                    stats["comment_failed"] += 1

            if self._config.get("auto_like"):
                lr = await self._interact.auto_like_if_enabled(item, current_qq=current_qq)
                if lr is True:
                    stats["like_success"] += 1
                elif lr is False:
                    stats["like_failed"] += 1

            feed_comments = item.get("comments", [])
            if feed_comments:
                await self._interact.process_feed_comments(item, feed_comments, current_qq=current_qq)

            processed += 1
            if processed < self.MAX_PROCESS_PER_CYCLE and processed < len(new_items):
                await self._http.random_human_delay(3.0, 8.0, "[批量处理]")

        if skipped_liked > 0:
            logger.info(f"[说说监控] 已过滤 {skipped_liked} 条已点赞说说")

        logger.info(
            f"[说说监控] 本轮完成: 新动态{stats['detected']}条, "
            f"通知{stats['notified']}条, 处理{processed}条, "
            f"点赞{stats['like_success']}/{stats['like_failed']}, "
            f"评论{stats['comment_success']}/{stats['comment_failed']}"
        )

    # ---- 通知推送 ----

    async def _notify_new_shuoshuo(self, item: dict) -> None:
        """发送新说说通知。"""
        target_group = self._config.get("target_group", "")
        target_user = self._config.get("target_user", "")

        if not target_group and not target_user:
            return

        content = item.get("content", "")
        tid = item.get("tid")
        pic_list = item.get("pic", [])
        pic_count_text = f"\n[包含 {len(pic_list)} 张图片]" if pic_list else ""
        create_time = item.get("created_time") or item.get("createTime", "")

        time_str = str(create_time)
        try:
            if str(create_time).isdigit():
                time_str = datetime.datetime.fromtimestamp(
                    int(create_time)
                ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        msg = (
            f"🔔【Qzone 新动态】\n----------------\n"
            f"{content}{pic_count_text}\n----------------\n"
            f"ID: {tid}\n时间: {time_str}"
        )

        logger.info(f"正在推送新说说: {tid}")
        try:
            from src.app.plugin_system.api import adapter_api
            from .http_client import ADAPTER_SIGNATURE

            if target_group:
                await adapter_api.send_group_message(
                    adapter_sign=ADAPTER_SIGNATURE, group_id=target_group, message=msg,
                )
            if target_user:
                await adapter_api.send_friend_message(
                    adapter_sign=ADAPTER_SIGNATURE, user_id=target_user, message=msg,
                )
        except Exception as e:
            logger.error(f"推送新说说通知失败: {e}")

    # ---- 静默窗口 ----

    def _is_in_quiet_hours(self, now: datetime.datetime | None = None) -> bool:
        """判断当前是否处于静默时间窗口。"""
        monitor_cfg = self._get_config()
        if not monitor_cfg:
            return False

        if not bool(getattr(monitor_cfg, "quiet_hours_enabled", True)):
            return False

        current_dt = now or datetime.datetime.now()
        current_hour = current_dt.hour

        start_hour = max(0, min(int(getattr(monitor_cfg, "quiet_hours_start", 23) or 23), 23))
        end_hour = max(0, min(int(getattr(monitor_cfg, "quiet_hours_end", 7) or 7), 23))

        if start_hour == end_hour:
            return False

        if start_hour < end_hour:
            return start_hour <= current_hour < end_hour

        return current_hour >= start_hour or current_hour < end_hour

    # ---- 手动活动标记 ----

    def mark_manual_activity(self, source: str = "manual") -> None:
        """记录手动触发行为，重置冷却。"""
        if not self._running:
            return

        interval = max(60, min(int(self._config.get("interval", 300) or 300), 86400))
        self._cooldown_until = time.time() + interval
        logger.info(f"[说说监控] 检测到手动触发({source})，监控计时已重置（{interval}s）")

    # ---- 状态查询 ----

    async def get_status(self) -> dict[str, Any]:
        """获取监控状态。"""
        monitor_cfg = self._get_config()
        monitor_enabled = bool(getattr(monitor_cfg, "enabled", True)) if monitor_cfg else True
        default_interval = int(getattr(monitor_cfg, "default_interval", 300)) if monitor_cfg else 300
        default_interval = max(60, min(default_interval, 86400))

        quiet_enabled = bool(getattr(monitor_cfg, "quiet_hours_enabled", True)) if monitor_cfg else True
        quiet_start = max(0, min(int(getattr(monitor_cfg, "quiet_hours_start", 23) or 23), 23)) if monitor_cfg else 23
        quiet_end = max(0, min(int(getattr(monitor_cfg, "quiet_hours_end", 7) or 7), 23)) if monitor_cfg else 7

        now_ts = time.time()
        cooldown_remaining = max(0, int(self._cooldown_until - now_ts))
        baseline_tid = str(self._state.last_tid or "").strip()

        return {
            "is_running": self._running,
            "enabled": monitor_enabled,
            "default_interval": default_interval,
            "interval": self._config.get("interval", default_interval),
            "target_group": self._config.get("target_group", ""),
            "target_user": self._config.get("target_user", ""),
            "auto_comment": bool(self._config.get("auto_comment", True)),
            "auto_like": bool(self._config.get("auto_like", True)),
            "like_probability": self._config.get("like_probability", 1.0),
            "comment_probability": self._config.get("comment_probability", 0.3),
            "quiet_hours_enabled": quiet_enabled,
            "quiet_window": f"{quiet_start:02d}:00-{quiet_end:02d}:00",
            "in_quiet_hours": self._is_in_quiet_hours(),
            "cooldown_remaining_seconds": cooldown_remaining,
            "baseline_initialized": bool(baseline_tid),
            "last_tid": baseline_tid,
            "last_run_at": int(self._last_run_at) if self._last_run_at > 0 else 0,
            "last_run_source": self._last_source,
            "last_run_force": self._last_force,
            "last_run_result": self._last_result,
            "last_run_skip_reason": self._last_skip_reason,
            "last_run_error": self._last_error,
            "startup_retry_active": self._startup_retry_active,
            "startup_retry_attempt": self._startup_retry_attempt,
            "startup_retry_max_attempts": self._startup_retry_max_attempts,
            "startup_retry_remaining_attempts": max(
                self._startup_retry_max_attempts - self._startup_retry_attempt, 0
            ),
            "startup_retry_interval": self._startup_retry_interval,
            "startup_retry_last_reason": self._startup_retry_last_reason,
        }

    # ---- 启动重试 ----

    async def _stop_startup_retry(self) -> None:
        """停止启动连接就绪重试任务。"""
        if not self._startup_retry_active and not self._startup_retry_job_name:
            return

        try:
            from src.kernel.scheduler import get_unified_scheduler
            scheduler = get_unified_scheduler()
            if self._startup_retry_job_name:
                if hasattr(scheduler, "remove_job"):
                    await scheduler.remove_job(self._startup_retry_job_name)
                elif hasattr(scheduler, "remove_schedule_by_name"):
                    await scheduler.remove_schedule_by_name(self._startup_retry_job_name)
        except Exception:
            pass
        finally:
            self._startup_retry_active = False
            self._startup_retry_job_name = ""

    async def _schedule_startup_retry(self, config: dict[str, Any]) -> None:
        """为启动首轮安排连接就绪重试。"""
        from src.kernel.scheduler import get_unified_scheduler, TriggerType

        await self._stop_startup_retry()

        max_attempts = max(1, min(int(config.get("startup_retry_max_attempts", 6) or 6), 60))
        retry_interval = max(5, min(int(config.get("startup_retry_interval", 10) or 10), 300))

        if max_attempts <= 0:
            return

        scheduler = get_unified_scheduler()
        retry_job_name = f"qzone_startup_retry_{id(self)}"

        if hasattr(scheduler, "add_job"):
            interval_trigger = getattr(TriggerType, "INTERVAL", None) or TriggerType.TIME
            await scheduler.add_job(
                func=self._run_startup_retry_tick,
                trigger=interval_trigger,
                seconds=retry_interval,
                id=retry_job_name,
                replace_existing=True,
            )
        elif hasattr(scheduler, "create_schedule"):
            await scheduler.create_schedule(
                callback=self._run_startup_retry_tick,
                trigger_type=TriggerType.TIME,
                trigger_config={"interval_seconds": retry_interval},
                is_recurring=True,
                task_name=retry_job_name,
                force_overwrite=True,
            )
        else:
            raise RuntimeError("当前调度器不支持 add_job/create_schedule 接口")

        self._startup_retry_active = True
        self._startup_retry_attempt = 0
        self._startup_retry_max_attempts = max_attempts
        self._startup_retry_interval = retry_interval
        self._startup_retry_job_name = retry_job_name
        self._startup_retry_last_reason = "startup_no_qq"

        logger.info(
            f"[自动监控] 已启用连接就绪重试：每 {retry_interval}s 一次，"
            f"最多 {max_attempts} 次"
        )

    async def _run_startup_retry_tick(self) -> None:
        """启动首轮连接就绪重试轮询。"""
        if not self._running:
            await self._stop_startup_retry()
            return

        if not self._startup_retry_active:
            return

        self._startup_retry_attempt += 1
        current_attempt = self._startup_retry_attempt
        max_attempts = max(self._startup_retry_max_attempts, 0)

        result_code = await self._run_auto_monitor(force=True, source="startup_retry")

        if result_code != "skip_no_qq":
            logger.info(f"[自动监控] 启动连接重试成功（第 {current_attempt}/{max_attempts} 次）")
            await self._stop_startup_retry()
            return

        if current_attempt >= max_attempts:
            self._startup_retry_last_reason = "max_attempts_reached"
            logger.warning("[自动监控] 启动连接重试已达上限，停止重试")
            await self._stop_startup_retry()

    # ---- 辅助方法 ----

    def _is_monitor_enabled(self) -> bool:
        """监控总开关是否开启。"""
        monitor_cfg = self._get_config()
        if not monitor_cfg:
            return True
        return bool(getattr(monitor_cfg, "enabled", True))

    def _resolve_config_defaults(self) -> None:
        """解析配置默认值。"""
        monitor_cfg = self._get_config()
        default_interval = int(getattr(monitor_cfg, "default_interval", 300)) if monitor_cfg else 300
        default_auto_like = bool(getattr(monitor_cfg, "auto_like", True)) if monitor_cfg else True
        default_auto_comment = bool(getattr(monitor_cfg, "auto_comment", True)) if monitor_cfg else True
        default_like_prob = float(getattr(monitor_cfg, "like_probability", 1.0)) if monitor_cfg else 1.0
        default_comment_prob = float(getattr(monitor_cfg, "comment_probability", 0.3)) if monitor_cfg else 0.3

        self._config.setdefault("interval", default_interval)
        self._config["interval"] = max(60, min(self._config["interval"], 86400))
        self._config.setdefault("auto_like", default_auto_like)
        self._config.setdefault("auto_comment", default_auto_comment)
        self._config.setdefault("like_probability", max(0.0, min(1.0, default_like_prob)))
        self._config.setdefault("comment_probability", max(0.0, min(1.0, default_comment_prob)))

    async def _wait_for_connection(self) -> bool:
        """等待适配器连接就绪。"""
        for attempt in range(3):
            current_qq = await self._get_qq_from_napcat()
            if current_qq:
                try:
                    from src.app.plugin_system.api import adapter_api
                    from .http_client import ADAPTER_SIGNATURE

                    adapter = adapter_api.get_adapter(ADAPTER_SIGNATURE)
                    if adapter and hasattr(adapter, "send_napcat_api"):
                        res = await adapter.send_napcat_api("get_login_info", {})
                        if res and res.get("status") == "ok":
                            logger.info("[自动监控] 适配器连接验证通过 (实时探测)")
                            return True
                    else:
                        logger.warning("[自动监控] 适配器不支持实时探测，假设已连接")
                        if attempt == 0:
                            await asyncio.sleep(3)
                        return True
                except Exception:
                    pass

            if attempt < 2:
                logger.info(f"[自动监控] 等待适配器连接... (尝试 {attempt + 1}/3)")
                await asyncio.sleep(5)

        return False

    def _ensure_attrs(self) -> None:
        """确保所有属性已初始化（兼容 object.__new__ 构造）。"""
        defaults = {
            "_running": False, "_config": {}, "_cooldown_until": 0.0,
            "_last_run_at": 0.0, "_last_source": "", "_last_force": False,
            "_last_result": "never", "_last_error": "", "_last_skip_reason": "",
            "_startup_retry_active": False, "_startup_retry_attempt": 0,
            "_startup_retry_max_attempts": 0, "_startup_retry_interval": 0,
            "_startup_retry_job_name": "", "_startup_retry_last_reason": "",
        }
        for attr, default in defaults.items():
            if not hasattr(self, attr):
                setattr(self, attr, default)

        lock = getattr(self, "_cycle_lock", None)
        if not isinstance(lock, asyncio.Lock):
            self._cycle_lock = asyncio.Lock()

    def _reset_observability(self) -> None:
        """重置可观测性状态。"""
        self._last_run_at = 0.0
        self._last_source = ""
        self._last_force = False
        self._last_result = "never"
        self._last_error = ""
        self._last_skip_reason = ""

    def _record_skip(self, reason: str, source: str, force: bool) -> None:
        """记录跳过事件。"""
        self._last_run_at = time.time()
        self._last_source = source
        self._last_force = force
        self._last_error = ""
        self._last_result = "skipped"
        self._last_skip_reason = reason
        self._log_heartbeat(source, force)

    def _log_heartbeat(self, source: str, force: bool) -> None:
        """输出心跳日志。"""
        monitor_cfg = self._get_config()
        if not (monitor_cfg and bool(getattr(monitor_cfg, "log_heartbeat", True))):
            return

        mode = "force" if force else "normal"
        if self._last_result == "skipped":
            reason = _describe_skip_reason(self._last_skip_reason, self._get_config, self._cooldown_until)
            logger.info(f"[自动监控][HB] {source} | {mode} | skipped:{reason} ({self._last_skip_reason})")
        elif self._last_result == "error":
            logger.info(f"[自动监控][HB] {source} | {mode} | error")
        else:
            logger.info(f"[自动监控][HB] {source} | {mode} | ok")

    @staticmethod
    async def _remove_job(scheduler, job_name: str) -> None:
        """移除调度任务。"""
        try:
            if hasattr(scheduler, "remove_job"):
                await scheduler.remove_job(job_name)
            elif hasattr(scheduler, "remove_schedule_by_name"):
                await scheduler.remove_schedule_by_name(job_name)
        except Exception:
            pass


def _describe_skip_reason(
    reason_code: str, get_config, cooldown_until: float
) -> str:
    """将监控跳过原因码转换为可读中文描述。"""
    code = str(reason_code or "").strip()
    if not code:
        return "未知原因"

    if code == "skip_quiet_hours":
        monitor_cfg = get_config()
        start_hour = max(0, min(int(getattr(monitor_cfg, "quiet_hours_start", 23) or 23), 23)) if monitor_cfg else 23
        end_hour = max(0, min(int(getattr(monitor_cfg, "quiet_hours_end", 7) or 7), 23)) if monitor_cfg else 7
        return f"处于静默时间窗口({start_hour:02d}:00-{end_hour:02d}:00)"

    if code == "skip_cooldown":
        remain = max(0, int(cooldown_until - time.time()))
        return f"手动触发冷却中(剩余约{remain}s)"

    mapping = {
        "skip_disabled": "监控总开关关闭",
        "skip_not_running": "监控未运行",
        "skip_busy": "上一轮监控仍在执行",
        "skip_no_qq": "未获取到登录QQ",
        "skip_list_failed": "读取说说列表失败",
        "skip_empty_list": "说说列表为空",
    }
    return mapping.get(code, "已跳过本轮")