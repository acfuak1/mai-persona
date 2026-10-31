"""Mai Persona 第三方插件入口。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from maibot_sdk import API, Command, EventHandler, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    EventType,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .mai_persona.config import MaiPersonaConfig
from .mai_persona.engine_affection import apply_changes as apply_affection_changes
from .mai_persona.engine_diary import validate_diary
from .mai_persona.engine_emotion import (
    PAD,
    emotion_label,
    explicit_emotion_delta,
    has_explicit_emotion,
    interaction_pulse_delta,
    normalize_delta,
    regulated_expression,
    style_hint,
)
from .mai_persona.engine_events import apply_advances
from .mai_persona.engine_life import slots_elapsed, validate_life_result
from .mai_persona.engine_relationship import update_relationship
from .mai_persona.engine_schedule import hard_guard, parse_fire_at
from .mai_persona.engine_todos import apply_todo_result
from .mai_persona.mem_adapter import MemoryAdapter
from .mai_persona.prompting import persona_block, render_prompt
from .mai_persona.storage import PersonaStore
from .mai_persona.webui import build_dashboard_snapshot


class MaiPersonaPlugin(MaiBotPlugin):
    """角色内在状态与生活引擎。"""

    config_model = MaiPersonaConfig
    config_reload_subscriptions: ClassVar[Iterable[str]] = ()

    def __init__(self) -> None:
        super().__init__()
        self._store: PersonaStore | None = None
        self._memory: MemoryAdapter | None = None
        self._timezone = ZoneInfo("Asia/Shanghai")
        self._loop_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._empathy_inflight: set[int] = set()
        self._session_context: dict[str, dict[str, str]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._schedule_lock = asyncio.Lock()

    async def on_load(self) -> None:
        if self.config.plugin.enabled:
            await self._activate()
        else:
            self.ctx.logger.info("Mai Persona 已加载，当前配置为停用")

    async def on_unload(self) -> None:
        await self._deactivate()
        self.ctx.logger.info("Mai Persona 已停止")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data, version
        if scope == "self":
            if self.config.plugin.enabled:
                activated = await self._activate()
                if activated:
                    if self.config.plugin.enable_proactive_messages:
                        store = self._require_store()
                        store.set_meta("schedule_decision_retry_after", 0)
                        self._spawn_task(
                            self._decide_schedule_if_due(self._now(), force=True),
                            "persona-schedule-after-config-update",
                        )
                    elif self._store is not None:
                        self._store.clear_schedule()
            else:
                await self._deactivate()
            return
    async def _activate(self) -> bool:
        async with self._lifecycle_lock:
            missing = self.config.validate_persona()
            if missing:
                await self._deactivate_locked()
                self.ctx.logger.warning(
                    "Mai Persona 已加载，等待补全角色配置后启动；缺少：%s",
                    "、".join(missing),
                )
                return False
            try:
                timezone = ZoneInfo(self.config.plugin.timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"不支持的时区：{self.config.plugin.timezone}") from exc

            if self._store is None:
                data_dir = Path(self.ctx.paths.data_dir)
                self._store = PersonaStore(data_dir / "mai_persona.db")
            self._timezone = timezone
            trait = PAD(
                self.config.emotion.trait_p,
                self.config.emotion.trait_a,
                self.config.emotion.trait_d,
            ).bounded()
            self._store.initialize_emotion(trait, self._now().timestamp())
            self._memory = MemoryAdapter(self.ctx, self.config)
            if self._loop_task is None or self._loop_task.done():
                self._loop_task = asyncio.create_task(self._run_loop(), name="mai-persona-tick")
            self.ctx.logger.info("Mai Persona 已启用，数据库：%s", self._store.path)
            return True

    async def _deactivate(self) -> None:
        async with self._lifecycle_lock:
            await self._deactivate_locked()

    async def _deactivate_locked(self) -> None:
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        pending = list(self._background_tasks)
        for background_task in pending:
            background_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.clear()
        if self._store is not None:
            self._store.close()
            self._store = None
        self._memory = None

    def _spawn_task(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self.ctx.logger.error(
                    "后台任务 %s 失败：%s",
                    name,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(completed)

    def _now(self) -> datetime:
        return datetime.now(self._timezone)

    def _require_store(self) -> PersonaStore:
        if self._store is None:
            raise RuntimeError("Mai Persona 尚未启用")
        return self._store

    def _active(self) -> bool:
        return bool(self.config.plugin.enabled and self._store is not None)

    def _queue_planner_work(
        self,
        task_type: str,
        prompt_name: str,
        payload: dict[str, Any],
        dedupe_key: str,
        now: datetime,
    ) -> int:
        work_id = self._require_store().queue_planner_work(
            task_type,
            prompt_name,
            dedupe_key,
            payload,
            now.timestamp(),
        )
        self.ctx.logger.debug("已加入 Planner 待处理任务：%s #%d", task_type, work_id)
        return work_id

    def _planner_context(self, now: datetime, user_id: str) -> str:
        store = self._require_store()
        pending_work = store.pending_planner_work(limit=4)
        snapshot = self._state_snapshot(now, user_id)
        pending_summary = ""
        if pending_work:
            pending_summary = (
                f"\n当前有 {len(pending_work)} 项待处理维护任务："
                + "、".join(
                    f"#{int(work['id'])} {str(work['task_type'])}"
                    for work in pending_work
                )
                + "。请在本轮 Planner 中优先读取并完成这些任务；维护过程不要作为话题告诉用户。"
            )
        return (
            "[Mai Persona 角色设定]\n"
            f"{persona_block(self.config.persona)}\n\n"
            "[Mai Persona 当前状态：心境、即时情绪与表达 PAD]\n"
            f"{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'), default=str)}\n\n"
            "[Mai Persona Planner 协作规则]\n"
            "插件资料仅用于 Mai Persona 的内在状态、生活模拟、关系演进和相关事件判断；"
            "不要用它重新定义 MaiBot 的身份、总体人格、行为准则、回复风格或本轮用户任务。"
            "发生冲突时遵循 MaiBot 自有人格配置与本轮任务。"
            "仅在确有状态维护或查询需要时调用插件工具，不要为了展示插件能力而调用；"
            "关系、情绪和好感变化必须有事实依据，并遵守插件工具说明中的范围限制。"
            + pending_summary
            + (
                "请调用 get_persona_pending_work 获取任务说明，完成判断后调用 "
                "complete_persona_work 提交结果。"
                if pending_work
                else ""
            )
        )

    @HookHandler(
        "maisaka.planner.before_request",
        name="persona_planner_context",
        description="在 MaiBot Planner 决策前注入角色设定、内在状态和待办提示",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> dict[str, Any] | None:
        if not self._active() or not isinstance(kwargs.get("items"), list):
            return None
        session_id = str(kwargs.get("session_id", "")).strip()
        context = self._session_context.get(session_id, {})
        user_id = str(context.get("user_id", "")).strip() or self._require_store().latest_user_id(session_id)
        items = [
            item
            for item in kwargs["items"]
            if not (
                isinstance(item, dict)
                and isinstance(item.get("meta"), dict)
                and str(item["meta"].get("item_id", "")).startswith("mai-persona:")
            )
        ]
        now = self._now()
        persona_item = {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"mai-persona:{uuid4().hex}",
                "logical_turn_id": None,
                "timestamp": now.isoformat(),
            },
            "parts": [{"type": "text", "text": self._planner_context(now, user_id)}],
        }
        insert_at = 1 if items and isinstance(items[0], dict) and items[0].get("item_type") == "SystemMessageItem" else 0
        items.insert(insert_at, persona_item)
        modified = dict(kwargs)
        modified["items"] = items
        return {"action": "continue", "modified_kwargs": modified}

    @Tool(
        "get_persona_pending_work",
        description="读取待处理的 Mai Persona 状态维护任务；仅在 Planner 上下文提示有任务时调用",
    )
    async def get_persona_pending_work(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        if not self._active():
            return {"name": "get_persona_pending_work", "content": "Mai Persona 当前未启用"}
        work_items = self._require_store().pending_planner_work(limit=8)
        rendered = []
        for work in work_items:
            prompt = render_prompt(
                str(work["prompt_name"]),
                self.config.persona,
                work["payload"],
            )
            rendered.append(
                {
                    "work_id": int(work["id"]),
                    "task_type": str(work["task_type"]),
                    "instruction": prompt,
                }
            )
        return {
            "name": "get_persona_pending_work",
            "content": json.dumps(rendered, ensure_ascii=False, separators=(",", ":")),
        }

    @Tool(
        "complete_persona_work",
        description="提交 Planner 完成的 Mai Persona 维护任务结果，结果必须符合任务说明中的 JSON 格式",
        parameters=[
            ToolParameterInfo(
                name="work_id",
                param_type=ToolParamType.INTEGER,
                description="get_persona_pending_work 返回的任务 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="result_json",
                param_type=ToolParamType.STRING,
                description="按任务要求生成的 JSON 字符串；日记任务提交纯文本",
                required=True,
            ),
        ],
    )
    async def complete_persona_work(self, work_id: int, result_json: str, **kwargs: Any) -> dict[str, str]:
        del kwargs
        if not self._active():
            return {"name": "complete_persona_work", "content": "Mai Persona 当前未启用"}
        work = self._require_store().planner_work(int(work_id))
        if work is None:
            return {"name": "complete_persona_work", "content": "任务已不存在或已完成"}
        try:
            result = result_json if work["prompt_name"] == "diary_generate" else _strict_json(result_json)
            applied = await self._apply_planner_work(work, result, self._now())
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {"name": "complete_persona_work", "content": f"结果格式无效，任务保留待重试：{exc}"}
        if not applied:
            return {"name": "complete_persona_work", "content": "结果未通过校验，任务保留待重试"}
        self._require_store().complete_planner_work(int(work_id))
        return {"name": "complete_persona_work", "content": "维护任务已校验并保存"}

    async def _apply_planner_work(self, work: dict[str, Any], result: Any, now: datetime) -> bool:
        store = self._require_store()
        task_type = str(work.get("task_type", ""))
        payload = work.get("payload") if isinstance(work.get("payload"), dict) else {}

        if task_type == "life_generate":
            if not isinstance(result, dict) or not isinstance(result.get("schedule"), list) or not isinstance(result.get("micro_events"), list):
                return False
            date = str(payload.get("date", now.date().isoformat()))
            schedule, micro_events = validate_life_result(result, self.config.emotion.max_micro_event_bias)
            store.add_life_items(date, schedule, micro_events)
            self._complete_daily(f"life_generate:{date}", now)
            self.ctx.logger.info("Planner 已保存日常安排：日程 %d 项，小事件 %d 项", len(schedule), len(micro_events))
            return True

        if task_type in {"micro_event_appraisal", "interaction_appraisal", "empathy_appraisal", "event_line_appraisal"}:
            events = payload.get("events")
            facts_by_user = payload.get("facts_by_user")
            if not isinstance(events, list) or not isinstance(facts_by_user, dict):
                return False
            expected_sources = {
                (str(item.get("user_id", "")), str(item.get("event", ""))): str(item.get("source", ""))
                for item in events
                if isinstance(item, dict)
            }
            if not expected_sources or not self._apply_evaluations(result, facts_by_user, now, expected_sources):
                return False
            interaction_ids = payload.get("interaction_ids", [])
            if isinstance(interaction_ids, list):
                store.mark_interactions_evaluated([int(item) for item in interaction_ids])
            interaction_id = payload.get("interaction_id")
            if interaction_id is not None:
                store.mark_interactions_evaluated([int(interaction_id)])
            life_item_ids = payload.get("life_item_ids", [])
            if isinstance(life_item_ids, list):
                for item_id in life_item_ids:
                    store.mark_life_occurred(int(item_id))
            last_advanced_at = payload.get("last_advanced_at")
            if last_advanced_at is not None:
                store.set_meta("event_lines_appraised_at", float(last_advanced_at))
            if task_type == "interaction_appraisal":
                store.set_meta("last_appraisal_at", now.timestamp())
            return True

        if task_type == "todo_extract":
            if not isinstance(result, dict) or not isinstance(result.get("todos"), list) or not isinstance(result.get("updates"), list):
                return False
            user_ids = {str(item) for item in payload.get("known_user_ids", [])}
            changed = apply_todo_result(store, result, user_ids, now.timestamp())
            interaction_ids = payload.get("interaction_ids", [])
            if isinstance(interaction_ids, list):
                store.mark_todo_checked([int(item) for item in interaction_ids])
            store.set_meta("last_todo_extract_at", now.timestamp())
            self.ctx.logger.info("Planner 完成待办整理：新增或更新 %d 项", changed)
            return True

        if task_type == "affection_evaluate":
            if not isinstance(result, dict) or not isinstance(result.get("changes"), list):
                return False
            available = payload.get("available_facts")
            if not isinstance(available, dict):
                return False
            changed = apply_affection_changes(
                store,
                result["changes"],
                available,
                self.config.affection.max_delta,
                now.timestamp(),
            )
            daily_key = str(payload.get("daily_key", ""))
            if daily_key:
                self._complete_daily(daily_key, now)
            self.ctx.logger.info("Planner 完成好感评估：有依据的用户变更 %d 个", changed)
            return True

        if task_type == "event_advance":
            if not isinstance(result, dict) or not isinstance(result.get("lines"), list):
                return False
            changed = apply_advances(
                store,
                result,
                now.timestamp(),
                self.config.event_lines.max_active,
            )
            daily_key = str(payload.get("daily_key", ""))
            if daily_key:
                self._complete_daily(daily_key, now)
            if changed:
                await self._evaluate_changed_event_lines(now)
                self._spawn_task(
                    self._decide_schedule_if_due(now, force=True),
                    "persona-schedule-after-event-advance",
                )
            self.ctx.logger.info("Planner 完成事件线推进：更新 %d 条", changed)
            return True

        if task_type == "diary_generate":
            date = str(payload.get("date", now.date().isoformat()))
            diary = validate_diary(str(result or ""))
            if not diary:
                return False
            store.insert_diary(date, diary, now.timestamp())
            daily_key = str(payload.get("daily_key", ""))
            if daily_key:
                self._complete_daily(daily_key, now)
            self.ctx.logger.info("Planner 已保存当日日记")
            return True

        if task_type == "schedule_decide":
            if not isinstance(result, dict) or not isinstance(result.get("skip"), bool):
                return False
            store.set_meta("last_schedule_decision_at", now.timestamp())
            store.set_meta("schedule_decision_retry_after", 0)
            if result["skip"]:
                store.set_schedule(
                    fire_at=None,
                    reason=str(result.get("reason", "当前没有合适的主动话题"))[:500],
                    topics=[],
                    target_user="",
                    kind="share",
                    decided_at=now.timestamp(),
                )
                return True
            allowed_users = {str(item) for item in payload.get("candidate_user_ids", [])}
            target_user = str(result.get("target_user", "")).strip()
            fire_at = parse_fire_at(result.get("fire_at"), self._timezone)
            if target_user not in allowed_users or fire_at is None:
                return False
            minimum_fire = now.timestamp() + self.config.schedule.min_interval_hours * 3600
            fire_at = max(fire_at, minimum_fire)
            kind = str(result.get("kind", "share")).strip().lower()
            if kind not in {"ask", "share"}:
                kind = "share"
            topics = result.get("topics") if isinstance(result.get("topics"), list) else []
            store.set_schedule(
                fire_at=fire_at,
                reason=str(result.get("reason", ""))[:500],
                topics=[str(item)[:120] for item in topics[:5]],
                target_user=target_user,
                kind=kind,
                decided_at=now.timestamp(),
            )
            self.ctx.logger.info("Planner 已保存主动排程：%s", datetime.fromtimestamp(fire_at, self._timezone).isoformat())
            return True

        return False

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("Mai Persona 周期任务失败：%s", exc, exc_info=True)
            await asyncio.sleep(self.config.plugin.tick_seconds)

    async def _tick(self) -> None:
        if not self._active():
            return
        now = self._now()
        store = self._require_store()
        emotion_cfg = self.config.emotion
        state = store.decay_emotion(
            now.timestamp(),
            emotion_cfg.tau_emotion_min * 60,
            emotion_cfg.tau_mood_hours * 3600,
        )
        if state:
            self.ctx.logger.debug(
                "PAD 状态 P/A/D trait=(%.2f,%.2f,%.2f) mood=(%.2f,%.2f,%.2f) emotion=(%.2f,%.2f,%.2f) 标签=%s/%s",
                state["trait_p"],
                state["trait_a"],
                state["trait_d"],
                state["mood_p"],
                state["mood_a"],
                state["mood_d"],
                state["emo_p"],
                state["emo_a"],
                state["emo_d"],
                state["mood_label"],
                state["emo_label"],
            )

        await self._generate_daily_life(now)
        await self._evaluate_due_micro_events(now)
        await self._evaluate_changed_event_lines(now)
        await self._evaluate_pending_interactions(now)
        await self._extract_todos_if_due(now)
        await self._run_daily_jobs(now)
        await self._run_proactive_schedule(now)
        await self._decide_schedule_if_due(now)

    @staticmethod
    def _parse_hhmm(value: str) -> time | None:
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        return time(hour, minute)

    def _is_time_due(self, now: datetime, value: str) -> bool:
        target = self._parse_hhmm(value)
        return target is not None and now.timetz().replace(tzinfo=None) >= target

    def _retry_allowed(self, key: str, now: datetime) -> bool:
        store = self._require_store()
        last_attempt = float(store.get_meta(key, "0") or 0.0)
        cooldown = self.config.plugin.retry_cooldown_minutes * 60
        if now.timestamp() - last_attempt < cooldown:
            return False
        store.set_meta(key, now.timestamp())
        return True

    def _claim_daily(self, key: str, now: datetime) -> bool:
        store = self._require_store()
        done_key = f"done:{key}"
        if store.get_meta(done_key) == now.date().isoformat():
            return False
        return self._retry_allowed(f"attempt:{key}", now)

    def _complete_daily(self, key: str, now: datetime) -> None:
        self._require_store().set_meta(f"done:{key}", now.date().isoformat())

    @staticmethod
    def _slot_for_hour(hour: int) -> str:
        if hour < 6:
            return "night"
        if hour < 12:
            return "morning"
        if hour < 17:
            return "afternoon"
        if hour < 21:
            return "evening"
        return "night"

    async def _generate_daily_life(self, now: datetime) -> None:
        if not self._is_time_due(now, self.config.batch.life_generate_at):
            return
        store = self._require_store()
        date_key = f"life_generate:{now.date().isoformat()}"
        if store.get_meta(f"done:{date_key}") == now.date().isoformat():
            return
        if not self._retry_allowed(f"attempt:{date_key}", now):
            return
        lines = store.active_event_lines(self.config.event_lines.max_active)
        emotion = store.emotion() or {}
        payload = {
            "date": now.date().isoformat(),
            "event_lines": lines,
            "yesterday_emotion_summary": {
                "mood_label": emotion.get("mood_label", ""),
                "emotion_label": emotion.get("emo_label", ""),
            },
            "event_line_domains": self.config.persona.event_line_domains,
            "micro_event_domains": self.config.persona.micro_event_domains,
            "micro_event_bias_limit": self.config.emotion.max_micro_event_bias,
        }
        self._queue_planner_work(
            "life_generate",
            "life_generate",
            payload,
            date_key,
            now,
        )

    async def _evaluate_due_micro_events(self, now: datetime) -> None:
        store = self._require_store()
        due = store.unoccurred_micro_events(
            now.date().isoformat(),
            slots_elapsed(now.hour),
        )
        queued_ids = {
            int(item_id)
            for work in store.pending_planner_work(50)
            if work["task_type"] == "micro_event_appraisal"
            for item_id in work["payload"].get("life_item_ids", [])
        }
        due = [row for row in due if int(row["id"]) not in queued_ids]
        if not due:
            return
        payload = {
            "events": [
                {
                    "source": "life",
                    "event": row["content"],
                    "mood_bias": row["mood_bias"],
                    "micro_type": row["micro_type"],
                    "user_id": "",
                }
                for row in due
            ],
            "facts_by_user": {},
            "max_delta": self.config.emotion.max_delta_per_event,
            "life_item_ids": [int(row["id"]) for row in due],
        }
        self._queue_planner_work(
            "micro_event_appraisal",
            "occ_evaluate",
            payload,
            "micro_event_appraisal:" + ",".join(str(row["id"]) for row in due),
            now,
        )

    async def _evaluate_pending_interactions(self, now: datetime) -> None:
        store = self._require_store()
        window = self.config.emotion.appraisal_window_hours * 3600
        last_run = float(store.get_meta("last_appraisal_at", "0") or 0.0)
        if now.timestamp() - last_run < window:
            return
        pending = store.pending_interactions(now.timestamp() - 24 * 3600)
        queued_ids = {
            int(item_id)
            for work in store.pending_planner_work(50)
            if work["task_type"] == "interaction_appraisal"
            for item_id in work["payload"].get("interaction_ids", [])
        }
        pending = [row for row in pending if int(row["id"]) not in queued_ids]
        if not pending:
            return
        user_ids = sorted({str(row["user_id"]) for row in pending if row["user_id"] and row["role"] == "user"})
        facts_by_user: dict[str, list[str]] = {}
        if self._memory:
            for user_id in user_ids:
                recent_text = " ".join(
                    str(row["content"]) for row in pending if row["user_id"] == user_id and row["role"] == "user"
                )[-600:]
                known = await self._memory.person_profile(user_id)
                facts = await self._memory.search_facts(
                    f"对方已知状态；{recent_text}",
                    user_id=user_id,
                    limit=8,
                )
                facts_by_user[user_id] = list(dict.fromkeys([*known, *facts]))
        events = [
            {
                "source": "interaction",
                "user_id": row["user_id"],
                "event": row["content"],
                "role": row["role"],
            }
            for row in pending
            if row["role"] == "user"
        ]
        if events:
            payload = {
                "events": events,
                "facts_by_user": facts_by_user,
                "max_delta": self.config.emotion.max_delta_per_event,
                "interaction_ids": [int(row["id"]) for row in pending],
            }
            self._queue_planner_work(
                "interaction_appraisal",
                "occ_evaluate",
                payload,
                "interaction_appraisal:" + ",".join(str(row["id"]) for row in pending),
                now,
            )
        elif not store.pending_interactions(now.timestamp() - 24 * 3600):
            store.set_meta("last_appraisal_at", now.timestamp())

    def _apply_evaluations(
        self,
        result: Any,
        facts_by_user: dict[str, list[str]],
        now: datetime,
        expected_sources: dict[tuple[str, str], str] | None = None,
    ) -> bool:
        if not isinstance(result, dict) or not isinstance(result.get("evaluations"), list):
            return False
        store = self._require_store()
        accepted: dict[tuple[str, str], dict[str, Any]] = {}
        for item in result["evaluations"]:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            user_id = str(item.get("user_id", "")).strip()
            event_desc = str(item.get("event", ""))
            if source not in {"life", "empathy", "interaction"}:
                continue
            key = (user_id, event_desc)
            if expected_sources is not None and expected_sources.get(key) != source:
                continue
            accepted[key] = item
        if expected_sources is not None and set(accepted) != set(expected_sources):
            return False
        old_state = store.emotion() or {}
        for item in accepted.values():
            source = str(item.get("source", "")).strip()
            user_id = str(item.get("user_id", "")).strip()
            event_desc = str(item.get("event", ""))
            refs = item.get("facts_used") if isinstance(item.get("facts_used"), list) else []
            known = set(facts_by_user.get(user_id, []))
            matched = [str(ref).strip() for ref in refs if str(ref).strip() in known]
            raw_delta = item.get("deltas", {})
            delta = normalize_delta(raw_delta, self.config.emotion.max_delta_per_event)
            if source == "interaction" and not matched:
                delta = PAD(0.0, 0.0, 0.0)
            layer = str(item.get("layer", "emotion")).strip()
            if layer not in {"mood", "emotion"}:
                layer = "emotion"
            current = store.emotion() or {}
            mood = PAD(
                current.get("mood_p", 0.0),
                current.get("mood_a", 0.0),
                current.get("mood_d", 0.0),
            )
            emotion = PAD(
                current.get("emo_p", 0.0),
                current.get("emo_a", 0.0),
                current.get("emo_d", 0.0),
            )
            if layer == "mood":
                mood = PAD(mood.p + delta.p, mood.a + delta.a, mood.d + delta.d).bounded()
            else:
                emotion = PAD(emotion.p + delta.p, emotion.a + delta.a, emotion.d + delta.d).bounded()
            store.apply_emotion(
                now=now.timestamp(),
                source=source,
                event_desc=event_desc[:1000],
                appraisal=str(item.get("appraisal", ""))[:1500],
                facts_refs=matched,
                delta=delta,
                layer=layer,
                label=str(item.get("label", ""))[:80],
                mood_label=emotion_label(mood),
                emotion_label=emotion_label(emotion),
            )
        current_state = store.emotion() or {}
        mood_shift = max(
            abs(float(current_state.get(key, 0.0)) - float(old_state.get(key, 0.0)))
            for key in ("mood_p", "mood_a", "mood_d")
        )
        if mood_shift > 0.2:
            self._spawn_task(
                self._decide_schedule_if_due(now, force=True),
                "persona-schedule-after-mood-change",
            )
        self.ctx.logger.info(
            "完成认知评价：事件数=%d，当前心境=%s，即时情绪=%s",
            len(result["evaluations"]),
            current_state.get("mood_label", ""),
            current_state.get("emo_label", ""),
        )
        return True

    async def _evaluate_immediate_empathy(self, interaction_id: int, user_id: str, text: str) -> None:
        try:
            if not self._active():
                return
            now = self._now()
            facts = [text]
            if self._memory:
                profile = await self._memory.person_profile(user_id)
                known = await self._memory.search_facts(
                    f"对方已知状态；{text}",
                    user_id=user_id,
                    limit=8,
                )
                facts = list(dict.fromkeys([*profile, *known, text]))
            events = [{"source": "empathy", "user_id": user_id, "event": text, "role": "user"}]
            self._queue_planner_work(
                "empathy_appraisal",
                "occ_evaluate",
                {
                    "events": events,
                    "facts_by_user": {user_id: facts},
                    "max_delta": self.config.emotion.max_delta_per_event,
                    "interaction_id": interaction_id,
                },
                f"empathy_appraisal:{interaction_id}",
                now,
            )
        finally:
            self._empathy_inflight.discard(interaction_id)

    async def _extract_todos_if_due(self, now: datetime) -> None:
        store = self._require_store()
        last = float(store.get_meta("last_todo_extract_at", "0") or 0.0)
        if now.timestamp() - last < self.config.batch.todo_extract_hours * 3600:
            return
        rows = store.unprocessed_todo_interactions(now.timestamp() - 30 * 86400)
        queued_ids = {
            int(item_id)
            for work in store.pending_planner_work(50)
            if work["task_type"] == "todo_extract"
            for item_id in work["payload"].get("interaction_ids", [])
        }
        rows = [row for row in rows if int(row["id"]) not in queued_ids]
        if not rows:
            return
        user_ids = {str(row["user_id"]) for row in rows if row["user_id"]}
        payload = {
            "messages": [
                {"user_id": row["user_id"], "role": row["role"], "content": row["content"]}
                for row in rows
            ],
            "open_todos": store.open_todos(),
            "known_user_ids": sorted(user_ids),
            "interaction_ids": [int(row["id"]) for row in rows],
        }
        self._queue_planner_work(
            "todo_extract",
            "todo_extract",
            payload,
            "todo_extract:" + ",".join(str(row["id"]) for row in rows),
            now,
        )

    async def _run_daily_jobs(self, now: datetime) -> None:
        await self._run_affection_job(now)
        await self._advance_event_lines_if_due(now)
        await self._generate_diary_if_due(now)

    async def _run_affection_job(self, now: datetime) -> None:
        if not self._is_time_due(now, self.config.affection.evaluate_daily_at):
            return
        key = f"affection:{now.date().isoformat()}"
        if self._require_store().get_meta(f"done:{key}") == now.date().isoformat():
            return
        await self._evaluate_affection_for_users(
            self._require_store().active_users(now.timestamp() - self.config.affection.evidence_hours * 3600),
            now,
            key,
        )

    async def _evaluate_affection_for_users(
        self,
        user_ids: list[str],
        now: datetime,
        dedupe_key: str | None = None,
    ) -> int:
        if not user_ids or self._memory is None:
            return 0
        since = now.timestamp() - self.config.affection.evidence_hours * 3600
        available: dict[str, list[str]] = {}
        user_data: list[dict[str, Any]] = []
        for user_id in user_ids:
            facts = await self._memory.search_facts(
                f"{user_id} 最近 24 小时的具体互动事实",
                user_id=user_id,
                limit=10,
                time_start=since,
                time_end=now.timestamp(),
            )
            if not facts:
                continue
            available[user_id] = facts
            user_data.append(
                {
                    "user_id": user_id,
                    "facts": facts,
                    "affection": self._require_store().affection(user_id) or {},
                    "relationship": self._require_store().relationship(user_id) or {},
                }
            )
        if not user_data:
            return 0
        self._queue_planner_work(
            "affection_evaluate",
            "affection_evaluate",
            {
                "users": user_data,
                "available_facts": available,
                "max_delta": self.config.affection.max_delta,
                "daily_key": dedupe_key or f"affection:{now.date().isoformat()}",
            },
            dedupe_key or f"affection:{now.date().isoformat()}",
            now,
        )
        return 1

    async def _advance_event_lines_if_due(self, now: datetime) -> None:
        store = self._require_store()
        for configured_time in self.config.event_lines.advance_times:
            if not self._is_time_due(now, configured_time):
                continue
            key = f"event_advance:{now.date().isoformat()}:{configured_time}"
            if store.get_meta(f"done:{key}") == now.date().isoformat():
                continue
            if not self._claim_daily(key, now):
                continue
            self._queue_planner_work(
                "event_advance",
                "event_advance",
                {
                    "event_lines": store.active_event_lines(self.config.event_lines.max_active),
                    "emotion": store.emotion() or {},
                    "event_line_domains": self.config.persona.event_line_domains,
                    "max_active": self.config.event_lines.max_active,
                    "daily_key": key,
                },
                key,
                now,
            )

    async def _evaluate_changed_event_lines(self, now: datetime) -> None:
        store = self._require_store()
        timestamp = now.timestamp()
        last_success = float(store.get_meta("event_lines_appraised_at", "0") or 0.0)
        if timestamp <= last_success:
            return
        rows = store.fetchall(
            "SELECT id, title, content, stage, last_advanced_at FROM event_lines "
            "WHERE last_advanced_at>? ORDER BY last_advanced_at, id",
            (last_success,),
        )
        events = [
            {
                "source": "life",
                "user_id": "",
                "event": f"事件线「{row['title']}」当前阶段为「{row['stage']}」：{row['content']}",
                "id": row["id"],
            }
            for row in rows
        ]
        if not events:
            return
        if not self._retry_allowed("attempt:event_line_appraisal", now):
            return
        prompt_events = [{key: value for key, value in event.items() if key != "id"} for event in events]
        self._queue_planner_work(
            "event_line_appraisal",
            "occ_evaluate",
            {
                "events": prompt_events,
                "facts_by_user": {},
                "max_delta": self.config.emotion.max_delta_per_event,
                "event_line_ids": [int(row["id"]) for row in rows],
                "last_advanced_at": max(float(row["last_advanced_at"]) for row in rows),
            },
            f"event_line_appraisal:{max(float(row['last_advanced_at']) for row in rows)}",
            now,
        )

    async def _generate_diary_if_due(self, now: datetime) -> None:
        if not self._is_time_due(now, self.config.batch.diary_generate_at):
            return
        key = f"diary:{now.date().isoformat()}"
        if not self._claim_daily(key, now):
            return
        store = self._require_store()
        lines = store.fetchall(
            "SELECT event_desc, appraisal, delta_p, delta_a, delta_d, applied_layer "
            "FROM emotion_events WHERE ts>=? ORDER BY ts",
            (datetime.combine(now.date(), time.min, tzinfo=self._timezone).timestamp(),),
        )
        self._queue_planner_work(
            "diary_generate",
            "diary_generate",
            {
                "date": now.date().isoformat(),
                "emotion_events": lines,
                "event_lines": store.fetchall(
                    "SELECT title, content, stage FROM event_lines WHERE started_at>=? OR closed_at>=?",
                    (
                        datetime.combine(now.date(), time.min, tzinfo=self._timezone).timestamp(),
                        datetime.combine(now.date(), time.min, tzinfo=self._timezone).timestamp(),
                    ),
                ),
                "interactions": store.fetchall(
                    "SELECT role, content FROM interaction_queue WHERE ts>=? ORDER BY ts",
                    (datetime.combine(now.date(), time.min, tzinfo=self._timezone).timestamp(),),
                ),
                "daily_key": key,
            },
            key,
            now,
        )

    async def _decide_schedule_if_due(self, now: datetime, force: bool = False) -> None:
        async with self._schedule_lock:
            if not self.config.plugin.enable_proactive_messages:
                if self._store is not None:
                    self._store.clear_schedule()
                return
            await self._decide_schedule_locked(now, force)

    async def _decide_schedule_locked(self, now: datetime, force: bool) -> None:
        store = self._require_store()
        current = store.get_schedule()
        if current and current.get("fire_at") is not None:
            return
        retry_after = float(store.get_meta("schedule_decision_retry_after", "0") or 0.0)
        if now.timestamp() < retry_after:
            return
        last = float(current["decided_at"]) if current else float(store.get_meta("last_schedule_decision_at", "0") or 0.0)
        fallback = self.config.schedule.fallback_replan_hours * 3600
        if not force and now.timestamp() - last < fallback:
            return
        users = store.active_users(now.timestamp() - 7 * 86400)
        if not users:
            store.set_meta("last_schedule_decision_at", now.timestamp())
            return
        candidates: list[dict[str, Any]] = []
        for user_id in users[:20]:
            facts = await self._memory.search_facts(
                f"{user_id} 近期近况、活跃时段、上次互动",
                user_id=user_id,
                limit=6,
            ) if self._memory else []
            candidates.append(
                {
                    "user_id": user_id,
                    "facts": facts,
                    "relationship": store.relationship(user_id) or {},
                    "affection": store.affection(user_id) or {},
                    "todos": store.open_todos(user_id),
                    "last_interaction_at": store.last_user_interaction_at(user_id),
                }
            )
        start = datetime.combine(now.date() - timedelta(days=3), time.min, tzinfo=self._timezone).timestamp()
        schedule_key = f"schedule_decide:{int(last)}"
        payload = {
            "now": now.isoformat(),
            "state": self._state_snapshot(now, ""),
            "users": candidates,
            "candidate_user_ids": users,
            "recent_outbound_count": store.outbound_count_today(start),
            "min_interval_hours": self.config.schedule.min_interval_hours,
            "daily_limit": self.config.schedule.daily_limit,
            "silent_hours": self.config.schedule.silent_hours,
        }
        self._queue_planner_work(
            "schedule_decide",
            "schedule_decide",
            payload,
            schedule_key,
            now,
        )

    async def _run_proactive_schedule(self, now: datetime) -> None:
        if not self.config.plugin.enable_proactive_messages:
            return
        store = self._require_store()
        schedule = store.get_schedule()
        if not schedule or schedule.get("fire_at") is None or float(schedule["fire_at"]) > now.timestamp():
            return
        target_user = str(schedule.get("target_user", ""))
        if not target_user:
            store.clear_schedule()
            return
        if schedule.get("kind") == "ask" and store.last_user_interaction_at(target_user) > float(schedule["decided_at"]):
            store.clear_schedule()
            self.ctx.logger.info("排程属于询问型，期间用户已互动，已取消")
            return
        allowed, reason = hard_guard(
            store,
            now=now,
            timezone=self._timezone,
            min_interval_hours=self.config.schedule.min_interval_hours,
            daily_limit=self.config.schedule.daily_limit,
            silent_hours=self.config.schedule.silent_hours,
            target_user=target_user,
        )
        if not allowed:
            self.ctx.logger.debug("主动消息护栏阻止发送：%s", reason)
            return
        stream_id = store.latest_stream(target_user)
        if not stream_id:
            self.ctx.logger.warning("目标用户没有可用聊天流，主动消息未发送")
            store.clear_schedule()
            return
        try:
            topics = json.loads(str(schedule.get("topics", "[]")))
        except (TypeError, ValueError):
            topics = []
        context = self._session_context.setdefault(stream_id, {})
        context["user_id"] = target_user
        context["stream_id"] = stream_id
        context["persona_scheduled_outbound"] = {
            "reason": str(schedule.get("reason", "")),
            "target_user": target_user,
            "stream_id": stream_id,
        }
        result = await self.ctx.maisaka.proactive.trigger(
            stream_id=stream_id,
            intent=(
                "按照 Mai Persona 的角色设定和当前状态，自然地主动联系这位用户。"
                f"缘由：{schedule.get('reason', '')}；可考虑的话题：{topics}。"
                "请通过本轮 Planner 判断是否适合回复以及如何表达，不要提及插件或排程。"
            ),
            reason=str(schedule.get("reason", "Mai Persona 主动排程")),
            priority="normal",
            metadata={"source": "mai-persona", "target_user": target_user},
        )
        if not isinstance(result, dict) or not result.get("success"):
            context.pop("persona_scheduled_outbound", None)
            store.set_schedule(
                fire_at=now.timestamp() + self.config.plugin.retry_cooldown_minutes * 60,
                reason=schedule["reason"],
                topics=topics,
                target_user=target_user,
                kind=schedule["kind"],
                decided_at=schedule["decided_at"],
            )
            self.ctx.logger.warning("Planner 主动任务未能排入，会在冷却后重试")
            return
        store.clear_schedule()
        self.ctx.logger.info("主动联系任务已交给 MaiBot Planner")

    def _state_snapshot(self, now: datetime, user_id: str) -> dict[str, Any]:
        store = self._require_store()
        row = store.emotion() or {}
        mood = PAD(row.get("mood_p", 0.0), row.get("mood_a", 0.0), row.get("mood_d", 0.0))
        emotion = PAD(row.get("emo_p", 0.0), row.get("emo_a", 0.0), row.get("emo_d", 0.0))
        expression = regulated_expression(mood, emotion, self.config.emotion.regulation)
        return {
            "mood": mood.as_dict(),
            "emotion": emotion.as_dict(),
            "expression": expression.as_dict(),
            "mood_label": row.get("mood_label", "平常"),
            "emotion_label": row.get("emo_label", "平常"),
            "style_hint": style_hint(expression),
            "activity": store.current_activity(now.date().isoformat(), self._slot_for_hour(now.hour)),
            "event_lines": store.active_event_lines(self.config.event_lines.max_active),
            "relationship": store.relationship(user_id) if user_id else None,
            "affection": store.affection(user_id) if user_id else None,
            "todos": store.open_todos(user_id) if user_id else [],
        }

    def _state_modifier(self, now: datetime, user_id: str) -> str:
        state = self._state_snapshot(now, user_id)
        activity = state["activity"] or "有自己的日常安排"
        lines = [
            str(item.get("title", "")).strip()
            for item in state["event_lines"]
            if float(item.get("talkable", 0.0)) >= self.config.event_lines.talkable_threshold
        ]
        text = (
            "[Mai Persona 当前状态，仅作为本轮表达倾向参考]\n"
            f"心境：{state['mood_label']}，PAD={json.dumps(state['mood'], ensure_ascii=False, separators=(',', ':'))}；"
            f"即时情绪：{state['emotion_label']}，PAD={json.dumps(state['emotion'], ensure_ascii=False, separators=(',', ':'))}；"
            f"对外表达：PAD={json.dumps(state['expression'], ensure_ascii=False, separators=(',', ':'))}。"
            f"\n表达提示：{state['style_hint']}。"
            f"\n当前活动：{activity}"
        )
        if lines:
            text += f"（事件线：{'、'.join(lines[:2])}）"
        relation = state["relationship"]
        if relation:
            text += f"\n当前用户关系：{relation['stage']}"
            affection = state["affection"] or {}
            tendencies = []
            for key, label in (("trust", "信任"), ("intimacy", "亲密"), ("rapport", "默契")):
                value = float(affection.get(key, 0.0))
                if value >= 0.2:
                    tendencies.append(f"{label}偏正向")
                elif value <= -0.2:
                    tendencies.append(f"{label}偏谨慎")
            if tendencies:
                text += f"；{'、'.join(tendencies)}"
        todos = state["todos"]
        if todos:
            text += f"\n近期待办线索：惦记着用户提到的「{todos[0]['content']}」。"
        text += (
            "\n这些状态不覆盖 MaiBot 自有人格、行为准则、回复风格或用户本轮明确任务；"
            "只用于让表达与角色当前状态保持一致。"
        )
        return text[:1200]

    @staticmethod
    def _extract_message_context(
        message: Any,
        kwargs: dict[str, Any],
    ) -> tuple[str, str, str, str, str, str]:
        if not isinstance(message, dict):
            return "", "", "", "", "", ""
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        user_info = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        user_id = str(kwargs.get("user_id") or user_info.get("user_id") or "").strip()
        session_id = str(kwargs.get("session_id") or message.get("session_id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or message.get("stream_id") or session_id).strip()
        text = str(message.get("processed_plain_text") or message.get("plain_text") or "").strip()
        if not text:
            text = _plain_text(message.get("raw_message"))
        timestamp = message.get("timestamp") or kwargs.get("timestamp")
        try:
            ts = str(float(timestamp))
        except (TypeError, ValueError):
            ts = ""
        message_id = str(kwargs.get("message_id") or message.get("message_id") or "").strip()
        return user_id, session_id, stream_id, text, ts, message_id

    def _record_user_message(
        self,
        *,
        user_id: str,
        session_id: str,
        stream_id: str,
        text: str,
        timestamp: float,
        message_id: str,
    ) -> int:
        store = self._require_store()
        existing = None
        if message_id:
            existing = store.fetchone(
                "SELECT id FROM interaction_queue WHERE stream_id=? AND message_id=? AND role='user'",
                (stream_id, message_id),
            )
        is_new_interaction = existing is None
        interaction_id = store.add_interaction(
            timestamp,
            user_id,
            stream_id,
            "user",
            text,
            message_id=message_id,
        )
        if not store.relationship(user_id) and self.config.persona.relationship_stages:
            store.update_relationship(
                user_id,
                self.config.persona.relationship_stages[0],
                "",
                timestamp,
            )
        if session_id:
            self._session_context[session_id] = {"user_id": user_id, "stream_id": stream_id}
        if (
            is_new_interaction
            and interaction_id
            and has_explicit_emotion(text)
            and interaction_id not in self._empathy_inflight
        ):
            if self._apply_immediate_emotion_seed(text, timestamp):
                store.mark_interactions_evaluated([interaction_id])
            else:
                row = store.fetchone("SELECT evaluated FROM interaction_queue WHERE id=?", (interaction_id,))
                if row and not bool(row["evaluated"]):
                    self._empathy_inflight.add(interaction_id)
                    self._spawn_task(
                        self._evaluate_immediate_empathy(interaction_id, user_id, text),
                        "persona-immediate-empathy",
                    )
        elif is_new_interaction and interaction_id:
            self._apply_interaction_pulse(text, timestamp)
        return interaction_id

    def _apply_immediate_emotion_seed(self, text: str, timestamp: float) -> bool:
        """先把明确情绪变成小幅即时状态，避免必须等待 Planner 才首次变化。"""

        delta = explicit_emotion_delta(text, min(self.config.emotion.max_delta_per_event, 0.16))
        if not any((delta.p, delta.a, delta.d)):
            return False
        store = self._require_store()
        current = store.emotion() or {}
        mood = PAD(
            float(current.get("mood_p", 0.0)),
            float(current.get("mood_a", 0.0)),
            float(current.get("mood_d", 0.0)),
        )
        emotion = PAD(
            float(current.get("emo_p", 0.0)) + delta.p,
            float(current.get("emo_a", 0.0)) + delta.a,
            float(current.get("emo_d", 0.0)) + delta.d,
        ).bounded()
        store.apply_emotion(
            now=timestamp,
            source="empathy",
            event_desc=f"用户明确表达情绪：{text[:1000]}",
            appraisal="根据用户当前明确情绪表达产生即时共情反应；不据此改变关系阶段。",
            facts_refs=[text[:500]],
            delta=delta,
            layer="emotion",
            label=emotion_label(emotion),
            mood_label=emotion_label(mood),
            emotion_label=emotion_label(emotion),
        )
        return True

    def _apply_interaction_pulse(self, text: str, timestamp: float) -> None:
        """记录普通新互动带来的轻微即时唤醒，不改变关系或长期心境。"""

        delta = interaction_pulse_delta(text)
        if not any((delta.p, delta.a, delta.d)):
            return
        store = self._require_store()
        current = store.emotion() or {}
        mood = PAD(
            float(current.get("mood_p", 0.0)),
            float(current.get("mood_a", 0.0)),
            float(current.get("mood_d", 0.0)),
        )
        emotion = PAD(
            float(current.get("emo_p", 0.0)) + delta.p,
            float(current.get("emo_a", 0.0)) + delta.a,
            float(current.get("emo_d", 0.0)) + delta.d,
        ).bounded()
        store.apply_emotion(
            now=timestamp,
            source="interaction",
            event_desc="收到一条新的用户互动",
            appraisal="普通互动产生轻微即时唤醒；不据此改变关系阶段或长期心境。",
            facts_refs=[],
            delta=delta,
            layer="emotion",
            label=emotion_label(emotion),
            mood_label=emotion_label(mood),
            emotion_label=emotion_label(emotion),
        )

    async def _sync_hook_message(self, session_id: str, message_id: str) -> None:
        if not session_id or not message_id or not self._active():
            return
        try:
            message = await self.ctx.message.get_by_id(message_id, chat_id=session_id)
        except Exception as exc:
            self.ctx.logger.debug("无法通过回复 Hook 查询入站消息：%s", exc)
            return
        if not isinstance(message, dict) or message.get("success") is False:
            return
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        user_info = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        user_id = str(user_info.get("user_id", "")).strip()
        text = str(message.get("processed_plain_text") or message.get("plain_text") or "").strip()
        if not text:
            text = _plain_text(message.get("raw_message"))
        if not user_id or not text or message.get("is_command"):
            return
        try:
            timestamp = float(message.get("timestamp") or self._now().timestamp())
        except (TypeError, ValueError):
            timestamp = self._now().timestamp()
        self._record_user_message(
            user_id=user_id,
            session_id=session_id,
            stream_id=session_id,
            text=text[: self.config.plugin.max_interaction_chars],
            timestamp=timestamp,
            message_id=str(message.get("message_id") or message_id),
        )

    @EventHandler(
        "persona_on_start",
        description="MaiBot 启动后立即执行一次内在状态检查",
        event_type=EventType.ON_START,
    )
    async def handle_on_start(self, **kwargs: Any) -> None:
        del kwargs
        if self._active():
            await self._tick()

    @EventHandler(
        "persona_interaction_observer",
        description="记录用户消息摘要，供周期评价、待办抽取与关系状态使用",
        event_type=EventType.ON_MESSAGE,
    )
    async def handle_message(self, message: Any = None, **kwargs: Any) -> None:
        if not self._active():
            return
        user_id, session_id, stream_id, text, raw_ts, message_id = self._extract_message_context(message, kwargs)
        if not user_id or not text:
            return
        if isinstance(message, dict) and message.get("is_command"):
            return
        text = text[: self.config.plugin.max_interaction_chars]
        now_ts = float(raw_ts) if raw_ts else self._now().timestamp()
        self._record_user_message(
            user_id=user_id,
            session_id=session_id,
            stream_id=stream_id,
            text=text,
            timestamp=now_ts,
            message_id=message_id,
        )
        return

    @HookHandler(
        "maisaka.replyer.before_request",
        name="persona_reply_context",
        description="在 Maisaka 回复前注入当前活动、情绪与用户关系摘要",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_before_request(self, **kwargs: Any) -> dict[str, Any] | None:
        if not self._active():
            return None
        session_id = str(kwargs.get("session_id", "")).strip()
        await self._sync_hook_message(session_id, str(kwargs.get("reply_message_id", "")).strip())
        user_id = self._session_context.get(session_id, {}).get("user_id", "")
        if not user_id:
            user_id = self._require_store().latest_user_id(session_id)
        modifier = self._state_modifier(self._now(), user_id)
        modified = dict(kwargs)
        original = str(modified.get("extra_prompt", "") or "").strip()
        modified["extra_prompt"] = f"{original}\n\n{modifier}".strip() if original else modifier
        return {"action": "continue", "modified_kwargs": modified}

    @HookHandler(
        "maisaka.replyer.after_response",
        name="persona_reply_observer",
        description="记录 Maisaka 回复结果，补充近期交互上下文",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=5500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_after_response(self, **kwargs: Any) -> None:
        if not self._active():
            return
        session_id = str(kwargs.get("session_id", "")).strip()
        context = self._session_context.get(session_id, {})
        user_id = context.get("user_id", "")
        stream_id = context.get("stream_id", "")
        response = str(kwargs.get("response", "") or "").strip()
        scheduled = context.pop("persona_scheduled_outbound", None)
        if isinstance(scheduled, dict):
            if response:
                self._require_store().log_outbound(
                    self._now().timestamp(),
                    str(scheduled.get("target_user", user_id)),
                    str(scheduled.get("stream_id", stream_id)),
                    response[:1000],
                    str(scheduled.get("reason", "")),
                )
                self.ctx.logger.info("MaiBot Planner 已完成 Mai Persona 主动联系")
            else:
                self._require_store().set_schedule(
                    fire_at=self._now().timestamp() + self.config.plugin.retry_cooldown_minutes * 60,
                    reason=str(scheduled.get("reason", "")),
                    topics=[],
                    target_user=str(scheduled.get("target_user", user_id)),
                    kind="share",
                    decided_at=self._now().timestamp(),
                )
                self.ctx.logger.info("Planner 本轮选择不发送，主动排程进入冷却")
        if user_id and response:
            self._require_store().add_interaction(
                self._now().timestamp(),
                user_id,
                stream_id,
                "assistant",
                response[: self.config.plugin.max_interaction_chars],
            )
        if session_id:
            self._session_context.pop(session_id, None)

    @Tool(
        "get_inner_state",
        description="查询角色当前活动、情绪、对用户的关系和未完成事项",
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="目标用户 ID；当前对话用户可留空",
                required=False,
            )
        ],
    )
    async def get_inner_state(self, user_id: str = "", **kwargs: Any) -> dict[str, str]:
        if not self._active():
            return {"name": "get_inner_state", "content": "Mai Persona 当前未启用"}
        resolved_user = str(user_id or kwargs.get("target_user") or "").strip()
        session_id = str(kwargs.get("session_id", "")).strip()
        if not resolved_user:
            resolved_user = self._session_context.get(session_id, {}).get("user_id", "")
        exposure_key = f"state_exposure:{self._now().date().isoformat()}"
        exposure_count = int(self._require_store().get_meta(exposure_key, "0") or 0)
        if exposure_count >= self.config.exposure.daily_limit:
            return {"name": "get_inner_state", "content": "今日内在状态查询次数已达到上限"}
        self._require_store().set_meta(exposure_key, exposure_count + 1)
        state = self._state_snapshot(self._now(), resolved_user)
        return {
            "name": "get_inner_state",
            "content": json.dumps(state, ensure_ascii=False, default=str),
        }

    @Tool(
        "update_relationship",
        description=(
            "仅在用户明确提出关系变化，或对话中关系已明显演进时调用。"
            "每次只能迁移到配置阶段序列中的相邻阶段；不要用它确认普通友好表达。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="目标用户 ID；当前对话用户可留空",
                required=False,
            ),
            ToolParameterInfo(
                name="target_stage",
                param_type=ToolParamType.STRING,
                description="目标关系阶段，必须来自插件配置",
                required=True,
            ),
            ToolParameterInfo(
                name="free_text",
                param_type=ToolParamType.STRING,
                description="可选的自由语义关系备注",
                required=False,
            ),
        ],
    )
    async def update_relationship_tool(
        self,
        target_stage: str,
        user_id: str = "",
        free_text: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        if not self._active():
            return {"name": "update_relationship", "content": "Mai Persona 当前未启用"}
        session_id = str(kwargs.get("session_id", "")).strip()
        resolved_user = str(user_id or self._session_context.get(session_id, {}).get("user_id", "")).strip()
        if not resolved_user:
            return {"name": "update_relationship", "content": "用户 ID 不能为空"}
        now = self._now()
        allowed, message = update_relationship(
            self._require_store(),
            self.config.persona.relationship_stages,
            resolved_user,
            str(target_stage).strip(),
            free_text,
            now.timestamp(),
        )
        if not allowed:
            return {"name": "update_relationship", "content": message}
        await self._evaluate_affection_for_users([resolved_user], now)
        self._spawn_task(
            self._decide_schedule_if_due(now, force=True),
            "persona-schedule-after-relationship-change",
        )
        return {"name": "update_relationship", "content": message}

    @API("webui_snapshot", description="只读读取 Mai Persona 工作区概览", version="1")
    async def webui_snapshot(self) -> dict[str, Any]:
        now = self._now()
        return build_dashboard_snapshot(
            self._store,
            enabled=self.config.plugin.enabled,
            missing_config=self.config.validate_persona(),
            now=now,
            current_slot=self._slot_for_hour(now.hour),
            timezone=self._timezone,
            event_limit=self.config.event_lines.max_active,
        )

    @Command("persona_status", description="查看 Mai Persona 当前运行状态", pattern=r"^/persona_status$")
    async def handle_persona_status(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        if not self._active():
            return False, "Mai Persona 当前未启用", True
        state = self._state_snapshot(self._now(), "")
        emotion = state["emotion"]
        summary = (
            f"心境：{state['mood_label']}；即时情绪：{state['emotion_label']}；"
            f"活动：{state['activity'] or '尚无今日安排'}；"
            f"活跃事件线：{len(state['event_lines'])} 条；待办：{len(state['todos'])} 项。"
        )
        if stream_id:
            await self.ctx.send.text(summary, stream_id)
        return True, f"{summary} PAD={emotion}", True


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if str(item.get("type", "")).lower() in {"text", "plain"}:
                    content = item.get("content", item.get("text", ""))
                    if isinstance(content, str):
                        parts.append(content)
        return " ".join(part.strip() for part in parts if part.strip())
    if isinstance(value, dict):
        for key in ("plain_text", "text", "content"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested.strip()
            if isinstance(nested, (list, dict)):
                text = _plain_text(nested)
                if text:
                    return text
        return ""
    return ""


def _strict_json(text: str) -> Any:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ValueError("代码围栏未闭合")
        cleaned = "\n".join(lines[1:-1]).strip()
    return json.loads(cleaned)


def create_plugin() -> MaiPersonaPlugin:
    """创建 Mai Persona 插件实例。"""

    return MaiPersonaPlugin()
