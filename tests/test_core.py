from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sqlite3
import sys
import tempfile
import tomllib
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from maibot_sdk import MaiBotPlugin
from maibot_sdk.config import generate_plugin_config_schema


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from mai_persona.config import MaiPersonaConfig
from mai_persona.engine_affection import apply_changes
from mai_persona.engine_diary import validate_diary
from mai_persona.engine_emotion import (
    PAD,
    decay,
    explicit_emotion_delta,
    has_explicit_emotion,
    interaction_pulse_delta,
    regulated_expression,
)
from mai_persona.engine_life import validate_life_result
from mai_persona.engine_relationship import validate_transition
from mai_persona.engine_humanlike import (
    carry_over_scale,
    closeness,
    detect_boundary,
    detect_friction,
    detect_peak,
    disclosure_depth,
    disclosure_hint,
    drain_energy,
    grudge_decay,
    recover_energy,
    scale_by_closeness,
    style_hint,
    update_style_stats,
)
from mai_persona.engine_schedule import (
    hard_guard,
    is_conversation_end,
    user_filter_allows,
)
from mai_persona.prompting import persona_block
from mai_persona.engine_todos import apply_todo_result
from mai_persona.maitrace_schedule import (
    format_schedule_summary,
    normalize_schedule,
    normalize_snapshot,
)
from mai_persona.mem_adapter import MemoryAdapter, _flatten_result
from mai_persona.storage import PersonaStore
from mai_persona.webui import build_dashboard_snapshot


class EmotionTests(unittest.TestCase):
    def test_exponential_decay_and_regulation(self) -> None:
        current = PAD(1.0, 0.0, 0.0)
        baseline = PAD(0.0, 0.0, 0.0)
        self.assertAlmostEqual(decay(current, baseline, 60.0, 60.0).p, 0.367879, places=5)
        expressed = regulated_expression(PAD(0.2, 0.0, 0.0), current, 0.65)
        self.assertAlmostEqual(expressed.p, 0.48)

    def test_explicit_emotion_detection_is_narrow(self) -> None:
        self.assertTrue(has_explicit_emotion("我今天真的很焦虑"))
        self.assertTrue(has_explicit_emotion("这件事让我很开心"))
        self.assertFalse(has_explicit_emotion("我今天下班后去买菜"))

    def test_explicit_emotion_delta_is_small_and_directional(self) -> None:
        positive = explicit_emotion_delta("我今天很开心")
        negative = explicit_emotion_delta("我现在很焦虑")
        self.assertGreater(positive.p, 0)
        self.assertGreater(positive.a, 0)
        self.assertLess(negative.p, 0)
        self.assertGreater(negative.a, 0)
        self.assertLessEqual(max(abs(positive.p), abs(positive.a), abs(positive.d)), 0.16)
        pulse = interaction_pulse_delta("普通的一条消息")
        self.assertGreater(pulse.a, 0)
        self.assertLessEqual(max(abs(pulse.p), abs(pulse.a), abs(pulse.d)), 0.04)


class DomainTests(unittest.TestCase):
    def test_conversation_end_detection_is_narrow(self) -> None:
        self.assertTrue(is_conversation_end("晚安"))
        self.assertTrue(is_conversation_end("那我先睡了，晚安啦～"))
        self.assertTrue(is_conversation_end("拜拜！"))
        self.assertTrue(is_conversation_end("88"))
        self.assertTrue(is_conversation_end("明天见"))
        self.assertTrue(is_conversation_end("先去忙了"))
        self.assertFalse(is_conversation_end("晚安是什么意思"))
        self.assertFalse(is_conversation_end("这个价格是 88"))
        self.assertFalse(is_conversation_end(""))
        self.assertFalse(is_conversation_end(None))

    def test_humanlike_signals(self) -> None:
        stages = ["陌生", "熟悉", "朋友", "亲密"]
        self.assertEqual(disclosure_depth("陌生", stages), 0)
        self.assertEqual(disclosure_depth("熟悉", stages), 1)
        self.assertEqual(disclosure_depth("朋友", stages), 2)
        self.assertEqual(disclosure_depth("亲密", stages), 2)
        self.assertEqual(disclosure_depth("未知", stages), 0)
        self.assertEqual(disclosure_depth("陌生", stages, enabled=False), 2)
        self.assertIn("表层", disclosure_hint(0))
        self.assertIn("私密", disclosure_hint(2))
        self.assertTrue(detect_friction("你根本不想听我说话"))
        self.assertFalse(detect_friction("今天路上有点堵"))
        self.assertEqual(carry_over_scale(1000, 2000), 2.5)
        self.assertEqual(carry_over_scale(3000, 2000), 1.0)
        self.assertEqual(carry_over_scale(3000, 0), 1.0)

    def test_closeness_scales_humanlike_effects(self) -> None:
        stages = ["陌生", "熟悉", "朋友", "亲密"]
        stranger = closeness("陌生", stages, {"trust": -0.5, "intimacy": -0.5, "rapport": -0.5})
        close = closeness("亲密", stages, {"trust": 0.6, "intimacy": 0.5, "rapport": 0.4})
        self.assertLess(stranger, 0.2)
        self.assertGreater(close, 0.8)
        self.assertLess(scale_by_closeness(0.08, stranger), scale_by_closeness(0.08, close))
        self.assertAlmostEqual(scale_by_closeness(0.1, 0.0), 0.01)

    def test_style_stats_and_hints(self) -> None:
        median, samples = update_style_stats(0.0, 0, 7)
        self.assertEqual((median, samples), (7.0, 1))
        median, _ = update_style_stats(7.0, 1, 13)
        self.assertAlmostEqual(median, 8.2)
        self.assertEqual(style_hint(8, 10, 20, 1.0), "")
        self.assertIn("一句话", style_hint(8, 20, 20, 0.9))
        self.assertIn("一两句", style_hint(20, 20, 20, 0.9))
        self.assertEqual(style_hint(8, 20, 20, 0.0), "")

    def test_humanlike_detectors_and_decay(self) -> None:
        self.assertTrue(detect_boundary("这个不想说，换个话题"))
        self.assertFalse(detect_boundary("今天天气不错"))
        self.assertTrue(detect_peak("我面试通过了，太开心了"))
        self.assertFalse(detect_peak("开心"))
        self.assertLess(grudge_decay(0.3, 3 * 86400, 3.0), 0.12)
        self.assertEqual(recover_energy(0.0, 10**9, 8), 1.0)
        self.assertAlmostEqual(drain_energy(0.5, 0.02), 0.48)

    def test_chat_end_user_filter_modes(self) -> None:
        self.assertTrue(user_filter_allows("off", "u1", ["u1"], ["u1"]))
        self.assertTrue(user_filter_allows("whitelist", "u1", ["u1"], []))
        self.assertFalse(user_filter_allows("whitelist", "u2", ["u1"], []))
        self.assertFalse(user_filter_allows("blacklist", "u1", [], ["u1"]))
        self.assertTrue(user_filter_allows("blacklist", "u2", [], ["u1"]))
        self.assertTrue(user_filter_allows("unknown", "u2", [], ["u2"]))

    def test_memory_no_result_placeholder_is_not_a_fact(self) -> None:
        self.assertEqual(
            _flatten_result(
                {
                    "success": True,
                    "content": "你不太了解有关3510472225 最近 24 小时的具体互动事实的知识",
                }
            ),
            [],
        )
        self.assertEqual(
            _flatten_result({"success": True, "content": "你知道这些知识: 事实A\n事实B"}),
            ["事实A", "事实B"],
        )
        self.assertEqual(_flatten_result({"success": True, "content": ""}), [])

    def test_maitrace_schedule_snapshot_is_normalized_and_private_items_filtered(self) -> None:
        snapshot = normalize_snapshot(
            {
                "has_activity": True,
                "activity": {
                    "name": "图书馆自习",
                    "description": "复习课程内容",
                    "time_window": "14:00-16:00",
                },
                "next_activities": [
                    {"time": "18:00", "name": "晚饭"},
                    {"time": "20:00", "name": "私人约定", "private": True},
                ],
                "error": None,
            }
        )
        self.assertEqual(snapshot["current_activity"]["name"], "图书馆自习")
        self.assertEqual(len(snapshot["next_activities"]), 1)
        summary = format_schedule_summary(snapshot)
        self.assertIn("复习课程内容", summary)
        self.assertIn("18:00 晚饭", summary)
        self.assertNotIn("私人约定", summary)
        timeline = normalize_schedule(
            [
                {
                    "start": "2026-10-31T10:00:00+08:00",
                    "end": "2026-10-31T11:00:00+08:00",
                    "name": "阅读",
                    "description": "读书",
                }
            ]
        )
        self.assertIn("10:00-11:00 阅读：读书", format_schedule_summary({}, schedule=timeline))

    def test_maitrace_disabled_schedule_response_is_not_treated_as_empty_success(self) -> None:
        with self.assertRaisesRegex(ValueError, "schedule_disabled"):
            normalize_snapshot(
                {
                    "has_activity": False,
                    "activity": None,
                    "next_activities": [],
                    "error": "schedule_disabled",
                }
            )

    def test_relationship_only_moves_one_adjacent_stage(self) -> None:
        stages = ["陌生", "熟悉", "朋友", "亲密"]
        self.assertEqual(validate_transition(stages, "", "陌生"), (True, ""))
        self.assertFalse(validate_transition(stages, "陌生", "朋友")[0])
        self.assertTrue(validate_transition(stages, "陌生", "熟悉")[0])

    def test_affection_requires_matching_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                changed = apply_changes(
                    store,
                    [
                        {
                            "user_id": "u1",
                            "d_trust": 0.08,
                            "d_intimacy": 0.0,
                            "d_rapport": 0.02,
                            "reason": "事实支持的变化",
                            "facts_refs": ["不存在的事实"],
                        }
                    ],
                    {"u1": ["用户明确分享了近期计划"]},
                    0.1,
                    1000.0,
                )
                self.assertEqual(changed, 0)
                self.assertIsNone(store.affection("u1"))

                changed = apply_changes(
                    store,
                    [
                        {
                            "user_id": "u1",
                            "d_trust": 0.2,
                            "d_intimacy": 0.0,
                            "d_rapport": 0.02,
                            "reason": "用户明确分享了计划",
                            "facts_refs": ["用户明确分享了近期计划"],
                        }
                    ],
                    {"u1": ["用户明确分享了近期计划"]},
                    0.1,
                    1001.0,
                )
                self.assertEqual(changed, 1)
                self.assertEqual(store.affection("u1")["trust"], 0.1)

                placeholder = "你不太了解有关u1 最近 24 小时的具体互动事实的知识"
                changed = apply_changes(
                    store,
                    [
                        {
                            "user_id": "u1",
                            "d_trust": 0.1,
                            "d_intimacy": 0.0,
                            "d_rapport": 0.0,
                            "reason": "引用了占位句",
                            "facts_refs": [placeholder],
                        }
                    ],
                    {"u1": [placeholder]},
                    0.1,
                    1002.0,
                )
                self.assertEqual(changed, 0)
                self.assertEqual(store.affection("u1")["trust"], 0.1)
            finally:
                store.close()

    def test_life_result_is_validated_and_bias_clamped(self) -> None:
        schedule, events = validate_life_result(
            {
                "schedule": [{"slot": "morning", "content": "安排"}],
                "micro_events": [
                    {
                        "slot": "evening",
                        "content": "小事件",
                        "mood_bias": 0.9,
                        "micro_type": "discovery",
                    },
                    {"slot": "invalid", "content": "忽略", "mood_bias": 0, "micro_type": "none"},
                ],
            },
            0.15,
        )
        self.assertEqual(len(schedule), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["mood_bias"], 0.15)

    def test_diary_minimum_length(self) -> None:
        self.assertEqual(validate_diary("太短"), "")
        self.assertEqual(len(validate_diary("日" * 240)), 200)

    def test_todo_updates_cannot_change_another_users_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                own_id = store.add_todo("u1", "自己的待办", "", None, 1000)
                other_id = store.add_todo("u2", "其他用户的待办", "", None, 1000)
                apply_todo_result(
                    store,
                    {
                        "todos": [],
                        "updates": [
                            {"id": own_id, "status": "resolved"},
                            {"id": other_id, "status": "resolved"},
                        ],
                    },
                    {"u1"},
                    1001,
                )
                self.assertEqual(store.fetchone("SELECT status FROM todos WHERE id=?", (own_id,))["status"], "resolved")
                self.assertEqual(store.fetchone("SELECT status FROM todos WHERE id=?", (other_id,))["status"], "open")
            finally:
                store.close()

    def test_proactive_hard_guard_blocks_silent_hours_and_zero_daily_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                store.add_interaction(1000, "u1", "stream-1", "user", "你好")
                allowed, reason = hard_guard(
                    store,
                    now=datetime(2026, 10, 30, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    timezone=ZoneInfo("Asia/Shanghai"),
                    min_interval_hours=4,
                    daily_limit=3,
                    silent_hours=[1, 7],
                    target_user="u1",
                )
                self.assertFalse(allowed)
                self.assertIn("静默", reason)
                allowed, _ = hard_guard(
                    store,
                    now=datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    timezone=ZoneInfo("Asia/Shanghai"),
                    min_interval_hours=4,
                    daily_limit=0,
                    silent_hours=[1, 7],
                    target_user="u1",
                )
                self.assertFalse(allowed)
            finally:
                store.close()


class StorageTests(unittest.TestCase):
    def test_humanlike_storage_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                store.upsert_user_style("u1", 12.0, 21, 1000)
                self.assertEqual(store.user_style("u1")["samples"], 21)

                store.add_boundary("u1", "家庭", "不想说家里的事", 1000)
                self.assertEqual(len(store.active_boundaries("u1", 1000, 30)), 1)
                self.assertEqual(store.active_boundaries("u1", 1000 + 31 * 86400, 30), [])

                store.add_absence(
                    "u1", "away", 1000, 1000 + 72 * 3600, 1000 + 72 * 3600, "出差三天", 1000
                )
                self.assertIsNotNone(store.current_absence("u1", 2000))
                self.assertIsNotNone(store.upcoming_absence("u1", 900))
                self.assertEqual(store.mark_absence_returned("u1", 2000), 1)
                self.assertIsNone(store.current_absence("u1", 2000))

                store.bump_grudge("u1", 0.5, "吵架", 1000, 0.3)
                self.assertAlmostEqual(store.get_grudge("u1")["value"], 0.3)
                store.set_grudge("u1", 0.0, 2000)
                self.assertEqual(store.get_grudge("u1")["value"], 0.0)

                store.add_peak("u1", "面试通过", "我面试通过了", 0.8, 1000)
                self.assertIsNotNone(store.pick_peak("u1", 1000 + 3 * 86400, 5))
                self.assertIsNone(store.pick_peak("u1", 1000 + 3 * 86400, 5))

                summary = store.humanlike_summary()
                self.assertEqual(summary["peaks"], 1)
            finally:
                store.close()

    def test_relationship_events_dedupe_resolve_and_expire(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                first = store.add_relationship_event("followup", "u1", "周五面试结果", now=1000, due_at=2000)
                again = store.add_relationship_event("followup", "u1", "周五面试结果", now=1001, due_at=2100)
                self.assertEqual(first, again)
                self.assertEqual(store.pending_relationship_events("followup", 1999), [])
                self.assertEqual(len(store.pending_relationship_events("followup", 2000)), 1)
                self.assertTrue(store.resolve_relationship_event(first, 2100))
                self.assertEqual(store.pending_relationship_events("followup", 3000), [])

                repair = store.add_relationship_event("repair", "u1", "摩擦", now=3000, due_at=3000)
                self.assertEqual(store.resolve_user_events("repair", "u1", 3100), 1)
                later = store.add_relationship_event("repair", "u1", "摩擦", now=4000, due_at=4000)
                self.assertNotEqual(repair, later)
                self.assertEqual(store.expire_relationship_events("repair", 4000 + 25 * 3600, 24), 1)
                self.assertEqual(store.pending_relationship_events("repair", 4000 + 25 * 3600), [])
            finally:
                store.close()

    def test_recent_interactions_returns_chronological_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                for index in range(5):
                    store.add_interaction(
                        1000 + index,
                        "u1",
                        "s1",
                        "user" if index % 2 == 0 else "assistant",
                        f"m{index}",
                    )
                store.add_interaction(2000, "u2", "s2", "user", "其他会话")
                rows = store.recent_interactions("s1", limit=3)
                self.assertEqual([row["content"] for row in rows], ["m2", "m3", "m4"])
                self.assertEqual(store.recent_interactions("missing", limit=3), [])
            finally:
                store.close()

    def test_clearing_pending_life_items_preserves_occurred_micro_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                date = "2026-10-31"
                store.add_life_items(
                    date,
                    [{"slot": "morning", "content": "旧日程"}],
                    [
                        {
                            "slot": "morning",
                            "content": "已发生的小事",
                            "mood_bias": 0.0,
                            "micro_type": "none",
                        },
                        {
                            "slot": "evening",
                            "content": "尚未发生的小事",
                            "mood_bias": 0.0,
                            "micro_type": "none",
                        },
                    ],
                )
                occurred = store.fetchone(
                    "SELECT id FROM daily_life WHERE content='已发生的小事'"
                )
                store.mark_life_occurred(int(occurred["id"]))

                store.clear_pending_life_items(date)

                rows = store.fetchall("SELECT kind, content, occurred FROM daily_life WHERE date=?", (date,))
                self.assertEqual(
                    rows,
                    [{"kind": "micro_event", "content": "已发生的小事", "occurred": 1}],
                )
            finally:
                store.close()

    def test_planner_work_deduplicates_by_key_and_returns_existing_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                first_id = store.queue_planner_work(
                    "todo_extract",
                    "todo_extract",
                    "todo_extract:1,2",
                    {"interaction_ids": [1, 2]},
                    1000,
                )
                duplicate_id = store.queue_planner_work(
                    "todo_extract",
                    "todo_extract",
                    "todo_extract:1,2",
                    {"interaction_ids": [1, 2]},
                    1001,
                )
                next_id = store.queue_planner_work(
                    "todo_extract",
                    "todo_extract",
                    "todo_extract:3",
                    {"interaction_ids": [3]},
                    1002,
                )
                self.assertEqual(duplicate_id, first_id)
                self.assertNotEqual(next_id, first_id)
                self.assertEqual(len(store.pending_planner_work()), 2)
            finally:
                store.close()

    def test_webui_snapshot_is_bounded_and_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            now = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            try:
                store.initialize_emotion(PAD(0.2, 0.1, -0.1), now.timestamp())
                self.assertEqual(store.emotion()["mood_label"], "轻松")
                store.update_relationship("user-1", "朋友", "备注", now.timestamp())
                store.add_affection("user-1", (0.2, 0.1, -0.1), "依据", ["事实"], now.timestamp())
                store.add_todo("user-1", "待办内容", "", None, now.timestamp())
                store.insert_diary(now.date().isoformat(), "日记正文" * 60, now.timestamp())
                snapshot = build_dashboard_snapshot(
                    store,
                    enabled=True,
                    now=now,
                    current_slot="afternoon",
                    timezone=ZoneInfo("Asia/Shanghai"),
                    event_limit=3,
                )
                self.assertEqual(snapshot["status"], "运行中")
                self.assertEqual(snapshot["mood"]["p"], 0.2)
                self.assertEqual(snapshot["todo_count"], 1)
                self.assertEqual(snapshot["relationships"][0]["trust"], 0.2)
                self.assertEqual(len(snapshot["diaries"][0]["preview"]), 80)
                self.assertEqual(len(snapshot["diaries"][0]["content"]), 200)
                json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
            finally:
                store.close()

    def test_webui_reports_missing_persona_configuration(self) -> None:
        snapshot = build_dashboard_snapshot(
            None,
            enabled=True,
            missing_config=["角色名称", "长期生活方向"],
            now=datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            current_slot="afternoon",
            timezone=ZoneInfo("Asia/Shanghai"),
            event_limit=3,
        )
        self.assertEqual(snapshot["status"], "等待配置")

    def test_schema_budget_interactions_and_diary_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = PersonaStore(Path(temp) / "persona.db")
            try:
                store.initialize_emotion(PAD(0, 0, 0), 1000)
                self.assertTrue(store.consume_budget("occ_evaluate", "2026-10-30", 1))
                self.assertFalse(store.consume_budget("occ_evaluate", "2026-10-30", 1))
                store.add_interaction(1001, "user-1", "stream-1", "user", "你好")
                self.assertEqual(len(store.pending_interactions()), 1)
                first_interaction_id = store.add_interaction(
                    1002,
                    "user-1",
                    "stream-1",
                    "user",
                    "内容",
                    message_id="message-1",
                )
                duplicate_interaction_id = store.add_interaction(
                    1003,
                    "user-1",
                    "stream-1",
                    "user",
                    "重复投递",
                    message_id="message-1",
                )
                self.assertEqual(first_interaction_id, duplicate_interaction_id)
                self.assertEqual(
                    store.fetchone(
                        "SELECT COUNT(*) AS n FROM interaction_queue WHERE message_id='message-1'"
                    )["n"],
                    1,
                )
                self.assertEqual(store.latest_user_id("stream-1"), "user-1")
                first_id, first_inserted = store.insert_diary("2026-10-30", "日记", 1002)
                second_id, second_inserted = store.insert_diary("2026-10-30", "重复", 1003)
                self.assertEqual(first_id, second_id)
                self.assertTrue(first_inserted)
                self.assertFalse(second_inserted)
            finally:
                store.close()

    def test_interaction_message_id_migration_and_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "persona.db"
            connection = sqlite3.connect(database_path)
            connection.execute(
                """
                CREATE TABLE interaction_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL, user_id TEXT NOT NULL, stream_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL,
                    evaluated INTEGER NOT NULL DEFAULT 0, todo_checked INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.commit()
            connection.close()
            store = PersonaStore(database_path)
            try:
                columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(interaction_queue)")}
                self.assertIn("message_id", columns)
                first = store.add_interaction(1000, "u1", "stream", "user", "你好", "msg-1")
                duplicate = store.add_interaction(1001, "u1", "stream", "user", "你好", "msg-1")
                self.assertEqual(first, duplicate)
            finally:
                store.close()


class PluginContractTests(unittest.TestCase):
    def test_default_config_is_complete_and_disabled(self) -> None:
        config = MaiPersonaConfig()
        self.assertFalse(config.plugin.enabled)
        self.assertFalse(hasattr(MemoryAdapter, "ingest_diary"))
        self.assertFalse(hasattr(MemoryAdapter, "ingest_text"))
        self.assertEqual(len(config.validate_persona()), 6)
        self.assertEqual(config.exposure.daily_limit, 2)
        example = tomllib.loads((PLUGIN_ROOT / "config.example.toml").read_text(encoding="utf-8"))
        loaded = MaiPersonaConfig.model_validate(example)
        self.assertEqual(example["plugin"]["enabled"], False)
        self.assertEqual(example["emotion"]["tau_emotion_min"], 20.0)
        self.assertEqual(example["exposure"]["daily_limit"], 2)
        self.assertEqual(loaded.batch.life_generate_at, "08:00")
        self.assertFalse(loaded.batch.use_maitrace_schedule)
        self.assertIn(
            "/config.toml",
            (PLUGIN_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines(),
        )
        excluded_fields = {
            "memory_plugin_id",
            "memory_search_api",
            "memory_profile_api",
            "llm_task_name",
            "llm_temperature",
            "llm_max_tokens",
        }
        self.assertTrue(excluded_fields.isdisjoint(MaiPersonaConfig.model_fields))
        plugin_fields = MaiPersonaConfig.model_fields["plugin"].annotation.model_fields
        self.assertTrue(excluded_fields.isdisjoint(plugin_fields))

    def test_legacy_nested_config_migrates_and_settings_are_easy_to_fill(self) -> None:
        legacy = MaiPersonaConfig.model_validate(
            {
                "plugin": {
                    "enabled": True,
                    "memory_plugin_id": "legacy-memory",
                    "memory_search_api": "legacy.search",
                    "memory_profile_api": "legacy.profile",
                    "llm_task_name": "legacy-task",
                },
                "persona": {
                    "name": "旧角色",
                    "viewpoint": ["先看事实"],
                    "goals": "保持自己的生活",
                    "relationship_stages": ["陌生", "熟悉"],
                    "event_line_domains": ["学习"],
                    "micro_event_domains": ["休息"],
                    "emotion": {"tau_emotion_min": 30.0},
                    "affection": {"max_delta": 0.2},
                    "event_lines": {"max_active": 5},
                    "schedule": {"daily_limit": 1},
                    "batch": {"life_generate_at": "09:00"},
                    "exposure": {"daily_limit": 4},
                    "budget": {"occ_evaluate": 0},
                },
            }
        )
        self.assertEqual(legacy.emotion.tau_emotion_min, 30.0)
        self.assertEqual(legacy.affection.max_delta, 0.2)
        self.assertEqual(legacy.event_lines.max_active, 5)
        self.assertEqual(legacy.schedule.daily_limit, 1)
        self.assertEqual(legacy.batch.life_generate_at, "09:00")
        self.assertEqual(legacy.exposure.daily_limit, 4)
        self.assertNotIn("memory_plugin_id", type(legacy.plugin).model_fields)

        schema = generate_plugin_config_schema(MaiPersonaConfig)
        rendered_fields = {
            field_name
            for section in schema["sections"].values()
            for field_name in section["fields"]
        }
        self.assertTrue(
            {
                "memory_plugin_id",
                "memory_search_api",
                "memory_profile_api",
            }.isdisjoint(rendered_fields)
        )
        persona_fields = schema["sections"]["persona"]["fields"]
        self.assertEqual(persona_fields["goals"]["ui_type"], "textarea")
        self.assertEqual(persona_fields["goals"]["rows"], 5)
        self.assertEqual(persona_fields["name"]["placeholder"], "例如：小麦")
        self.assertEqual(persona_fields["viewpoint"]["label"], "状态评价视角")
        self.assertEqual(persona_fields["goals"]["label"], "长期生活方向")
        self.assertEqual(
            persona_fields["viewpoint"]["example"],
            ["先区分事实与猜测，再评价情绪", "单次未回复不直接视为关系变差"],
        )
        self.assertIn("状态判断依据", persona_fields["viewpoint"]["hint"])
        self.assertTrue(schema["sections"]["plugin"]["fields"]["config_version"]["hidden"])
        self.assertTrue(schema["sections"]["plugin"]["fields"]["enabled"]["hidden"])
        self.assertEqual(schema["sections"]["persona"]["order"], 0)
        self.assertEqual(schema["sections"]["plugin"]["order"], 1)
        self.assertEqual(schema["sections"]["plugin"]["fields"]["enable_proactive_messages"]["order"], 1)
        self.assertEqual(
            schema["sections"]["batch"]["fields"]["use_maitrace_schedule"]["label"],
            "使用 MaiTrace 日程",
        )

    def test_enabled_plugin_waits_for_persona_configuration_without_failing_load(self) -> None:
        module_name = "mai_persona_incomplete_config_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            plugin = module.create_plugin()
            plugin.set_plugin_config(
                {"plugin": {"enabled": True, "config_version": "1.0.0"}}
            )
            logger = Mock(spec=logging.Logger)
            plugin._set_context(
                types.SimpleNamespace(
                    logger=logger,
                    paths=types.SimpleNamespace(data_dir=Path(tempfile.gettempdir())),
                )
            )

            async def exercise_lifecycle() -> None:
                await plugin.on_load()
                self.assertIsNone(plugin._store)
                self.assertIsNone(plugin._loop_task)
                self.assertFalse(plugin._active())

                await plugin.on_config_update("self", {}, "1.0.0")
                self.assertIsNone(plugin._store)
                self.assertIsNone(plugin._loop_task)
                self.assertFalse(plugin._active())

            asyncio.run(exercise_lifecycle())
            self.assertEqual(logger.warning.call_count, 2)
            self.assertIn("角色名称", logger.warning.call_args.args[1])
        finally:
            sys.modules.pop(module_name, None)

    def test_invalid_times_and_duplicate_relationship_stages_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MaiPersonaConfig.model_validate({"batch": {"life_generate_at": "25:00"}})
        with self.assertRaises(ValueError):
            MaiPersonaConfig.model_validate(
                {"persona": {"relationship_stages": ["陌生", " 熟悉 ", "熟悉"]}}
            )
        with self.assertRaises(ValueError):
            MaiPersonaConfig.model_validate({"schedule": {"silent_hours": [5, 5]}})

    def test_persona_prefix_uses_only_configured_identity(self) -> None:
        config = MaiPersonaConfig().persona
        config.name = "测试角色"
        config.viewpoint = ["优先观察事实"]
        config.goals = "完成自己的长期计划"
        prompt = persona_block(config)
        self.assertIn("测试角色", prompt)
        self.assertIn("优先观察事实", prompt)
        self.assertIn("完成自己的长期计划", prompt)
        self.assertIn("不用于重新定义 MaiBot", prompt)
        self.assertIn("以 MaiBot 原生配置和本轮任务为准", prompt)
        self.assertIn("不是需要向用户宣讲", prompt)
        self.assertNotIn("她", prompt)

    def test_manifest_and_dynamic_entrypoint(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "mai-persona.mai-persona")
        self.assertEqual(manifest["sdk"]["min_version"], "2.20.0")
        self.assertEqual(
            set(manifest["capabilities"]),
            {
                "knowledge.search",
                "message.get_by_id",
                "api.call",
                "send.text",
                "maisaka.proactive.trigger",
            },
        )
        self.assertNotIn("llm.generate", manifest["capabilities"])
        webui = json.loads((PLUGIN_ROOT / "webui.json").read_text(encoding="utf-8"))
        self.assertEqual(webui["schema_version"], 2)
        self.assertEqual(webui["required_capabilities"], ["declarative_basic"])
        module_name = "mai_persona_contract_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            plugin = module.create_plugin()
            self.assertIsInstance(plugin, MaiBotPlugin)
            self.assertIs(type(plugin).on_load, module.MaiPersonaPlugin.on_load)
            self.assertTrue(callable(plugin.on_unload))
            component_names = {component["name"] for component in plugin.get_components()}
            self.assertIn("persona_reply_context", component_names)
            self.assertIn("persona_reply_observer", component_names)
            self.assertIn("persona_planner_context", component_names)
            self.assertIn("get_persona_pending_work", component_names)
            self.assertIn("complete_persona_work", component_names)
            self.assertIn("get_inner_state", component_names)
            self.assertIn("update_relationship", component_names)
            self.assertFalse((PLUGIN_ROOT / "mai_persona" / "engine_llm.py").exists())
            self.assertEqual(
                module._strict_json('```json\n{"ok":true}\n```'),
                {"ok": True},
            )
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": True, "config_version": "1.0.0"},
                    "persona": {
                        "name": "测试角色",
                        "viewpoint": ["先理解事实"],
                        "goals": "保持自己的生活",
                        "relationship_stages": ["陌生", "熟悉"],
                        "event_line_domains": ["学习"],
                        "micro_event_domains": ["休息"],
                    },
                }
            )
            with tempfile.TemporaryDirectory() as temp:
                plugin._store = PersonaStore(Path(temp) / "persona.db")
                try:
                    work_id = plugin._store.queue_planner_work(
                        "life_generate",
                        "life_generate",
                        "life_generate:test",
                        {
                            "date": "2026-10-30",
                            "event_line_domains": ["学习"],
                            "micro_event_domains": ["休息"],
                            "micro_event_bias_limit": 0.15,
                        },
                        1000,
                    )
                    task_response = asyncio.run(plugin.get_persona_pending_work())
                    planner_tasks = json.loads(task_response["content"])
                    self.assertEqual(planner_tasks[0]["work_id"], work_id)
                    self.assertIn("测试角色", planner_tasks[0]["instruction"])
                    self.assertIn("严格 JSON", planner_tasks[0]["instruction"])
                    plugin._store.initialize_emotion(PAD(0.0, 0.0, 0.0), 1000)
                    plugin._store.update_relationship("user-1", "熟悉", "", 1000)
                    plugin._record_user_message(
                        user_id="user-1",
                        session_id="stream-1",
                        stream_id="stream-1",
                        text="我今天很开心",
                        timestamp=1001,
                        message_id="message-1",
                    )
                    emotion = plugin._store.emotion()
                    self.assertGreater(emotion["emo_p"], 0)
                    self.assertEqual(
                        plugin._store.fetchone(
                            "SELECT evaluated FROM interaction_queue WHERE message_id='message-1'"
                        )["evaluated"],
                        1,
                    )
                    modifier = plugin._state_modifier(
                        datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                        "user-1",
                    )
                    self.assertIn("即时情绪", modifier)
                    self.assertIn("PAD=", modifier)
                    self.assertGreater(len(modifier), 200)
                    reply_hook = asyncio.run(
                        plugin.handle_replyer_before_request(
                            session_id="stream-1",
                            reply_message_id="",
                            extra_prompt="原有要求",
                        )
                    )
                    self.assertIn("即时情绪", reply_hook["modified_kwargs"]["extra_prompt"])
                    planner_hook = asyncio.run(
                        plugin.handle_planner_before_request(
                            items=[
                                {
                                    "item_type": "SystemMessageItem",
                                    "meta": {"item_id": "system"},
                                    "parts": [{"type": "text", "text": "MaiBot 原生人格"}],
                                }
                            ],
                            session_id="stream-1",
                        )
                    )
                    planner_text = json.dumps(planner_hook["modified_kwargs"]["items"], ensure_ascii=False)
                    self.assertIn("当前状态", planner_text)
                    self.assertIn("即时情绪", planner_text)
                finally:
                    plugin._store.close()
                    plugin._store = None
            api_names = {
                component["name"]
                for component in plugin.get_components()
                if component["type"] == "API"
            }
            self.assertIn("webui_snapshot", api_names)
            query_names = {query["api"] for query in webui["pages"][0]["queries"].values()}
            self.assertLessEqual(query_names, api_names)
        finally:
            sys.modules.pop(module_name, None)

    def test_humanlike_followup_and_repair_flow(self) -> None:
        module_name = "mai_persona_humanlike_flow_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp:
                plugin = module.create_plugin()
                plugin.set_plugin_config(
                    {"plugin": {"enabled": True, "config_version": "1.0.0"}}
                )
                plugin._set_context(
                    types.SimpleNamespace(api=Mock(), logger=Mock(spec=logging.Logger))
                )
                store = PersonaStore(Path(temp) / "persona.db")
                plugin._store = store
                try:
                    now = plugin._now()
                    base = now.timestamp() - 120

                    def record(text: str, message_id: str, offset: int) -> None:
                        plugin._record_user_message(
                            user_id="u1",
                            session_id="s1",
                            stream_id="s1",
                            text=text,
                            timestamp=base + offset,
                            message_id=message_id,
                        )

                    record("你根本不想听我说话", "m1", 0)
                    repairs = store.pending_relationship_events("repair", now.timestamp())
                    self.assertEqual(len(repairs), 1)

                    record("今天挺开心的", "m2", 30)
                    self.assertEqual(
                        store.pending_relationship_events("repair", now.timestamp()), []
                    )

                    record("我有点难过", "m3", 60)
                    self.assertGreater(
                        float(store.get_meta("carry_over_until", "0") or 0),
                        base + 60,
                    )

                    payload = {
                        "known_user_ids": ["u1"],
                        "messages": [{"user_id": "u1", "content": "周五要面试"}],
                        "max_due_hours": 72,
                    }
                    applied = asyncio.run(
                        plugin._apply_planner_work(
                            {"task_type": "followup_extract", "payload": payload},
                            {
                                "followups": [
                                    {
                                        "user_id": "u1",
                                        "summary": "周五面试结果",
                                        "due_in_hours": 6,
                                        "source": "周五要面试",
                                    },
                                    {
                                        "user_id": "u1",
                                        "summary": "编造的事项",
                                        "due_in_hours": 6,
                                        "source": "不存在的原句",
                                    },
                                ]
                            },
                            plugin._now(),
                        )
                    )
                    self.assertTrue(applied)
                    events = store.pending_relationship_events(
                        "followup", now.timestamp() + 7 * 3600
                    )
                    self.assertEqual([item["summary"] for item in events], ["周五面试结果"])
                    self.assertIn(
                        "跟进事项",
                        plugin._planner_context(plugin._now() + timedelta(hours=7), "u1"),
                    )
                finally:
                    plugin._store = None
                    store.close()
        finally:
            sys.modules.pop(module_name, None)

    def test_absence_holds_followup_and_shows_in_snapshot(self) -> None:
        module_name = "mai_persona_absence_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp:
                plugin = module.create_plugin()
                plugin.set_plugin_config(
                    {"plugin": {"enabled": True, "config_version": "1.0.0"}}
                )
                plugin._set_context(
                    types.SimpleNamespace(api=Mock(), logger=Mock(spec=logging.Logger))
                )
                store = PersonaStore(Path(temp) / "persona.db")
                plugin._store = store
                try:
                    now = plugin._now()
                    store.initialize_emotion(PAD(0.0, 0.0, 0.0), now.timestamp())
                    payload = {
                        "known_user_ids": ["u1"],
                        "messages": [{"user_id": "u1", "content": "明天出差三天"}],
                        "max_due_hours": 72,
                    }
                    applied = asyncio.run(
                        plugin._apply_planner_work(
                            {"task_type": "followup_extract", "payload": payload},
                            {
                                "followups": [
                                    {
                                        "user_id": "u1",
                                        "summary": "出差是否顺利",
                                        "due_in_hours": 6,
                                        "source": "明天出差三天",
                                    }
                                ],
                                "absences": [
                                    {
                                        "user_id": "u1",
                                        "kind": "away",
                                        "start_in_hours": 24,
                                        "duration_hours": 72,
                                        "source": "明天出差三天",
                                    }
                                ],
                            },
                            now,
                        )
                    )
                    self.assertTrue(applied)
                    absence = store.upcoming_absence("u1", now.timestamp())
                    self.assertIsNotNone(absence)
                    self.assertEqual(absence["kind"], "away")
                    events = store.fetchall(
                        "SELECT * FROM relationship_events WHERE kind='followup'"
                    )
                    self.assertGreater(
                        float(events[0]["due_at"]), float(absence["expected_return_at"])
                    )

                    snapshot = asyncio.run(plugin.webui_snapshot())
                    self.assertIn("humanlike_energy", snapshot)
                    self.assertEqual(snapshot["humanlike_followups"], "1")
                    self.assertEqual(snapshot["humanlike_absence"], "计划缺席中")
                finally:
                    plugin._store = None
                    store.close()
        finally:
            sys.modules.pop(module_name, None)

    def test_setup_wizard_builds_valid_config_snippet(self) -> None:
        module_name = "mai_persona_setup_wizard_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            plugin = module.create_plugin()
            plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}})

            status = asyncio.run(plugin.webui_setup_status())
            self.assertEqual(status["completion"], 0)
            self.assertIn("角色名称", status["missing"])
            self.assertEqual(asyncio.run(plugin.webui_config_preview())["status"], "尚未生成")
            result = asyncio.run(
                plugin.webui_compose_config(
                    name="小熙",
                    viewpoint="先区分事实与猜测\n单次未回复不视为关系变差",
                    stages="陌生、熟悉、朋友",
                    proactive="on",
                )
            )
            self.assertIn("已生成 4 项修改", result["status"])
            self.assertIn("仍有", result["status"])
            self.assertIn("[persona]", result["body"])
            self.assertIn('name = "小熙"', result["body"])
            self.assertIn("[\"陌生\", \"熟悉\", \"朋友\"]", result["body"])
            self.assertIn("[plugin]", result["body"])
            self.assertIn("enable_proactive_messages = true", result["body"])
            self.assertIn("config.toml", result["body"])

            # 生成的是待粘贴片段，不会直接改写当前配置
            status = asyncio.run(plugin.webui_setup_status())
            self.assertEqual(status["completion"], 0)
            self.assertIn("角色名称", status["missing"])

            self.assertEqual(
                asyncio.run(plugin.webui_compose_config(daily_limit="abc"))["status"],
                "校验失败",
            )
            single_stage = asyncio.run(plugin.webui_compose_config(stages="只有一段"))
            self.assertIn("仍有", single_stage["status"])
            self.assertIn("关系阶段", single_stage["status"])
        finally:
            sys.modules.pop(module_name, None)

    def test_private_chat_end_queues_targeted_schedule_decision(self) -> None:
        module_name = "mai_persona_chat_end_decide_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp:
                plugin = module.create_plugin()
                plugin.set_plugin_config(
                    {
                        "plugin": {
                            "enabled": True,
                            "config_version": "1.0.0",
                            "enable_proactive_messages": True,
                        },
                        "schedule": {
                            "decide_after_private_chat_end": True,
                            "decide_user_filter_mode": "whitelist",
                            "decide_user_whitelist": ["u1"],
                        },
                    }
                )
                plugin._set_context(
                    types.SimpleNamespace(api=Mock(), logger=Mock(spec=logging.Logger))
                )
                store = PersonaStore(Path(temp) / "persona.db")
                plugin._store = store
                try:
                    store.initialize_emotion(PAD(0.0, 0.0, 0.0), 1000)
                    store.add_interaction(1000, "u1", "s1", "user", "晚安")
                    store.add_interaction(1001, "u1", "s1", "assistant", "晚安，明天见")

                    async def scenario() -> None:
                        plugin._maybe_decide_after_private_chat_end(
                            "s1",
                            {
                                "user_id": "u1",
                                "stream_id": "s1",
                                "is_private": True,
                                "last_user_text": "晚安",
                            },
                            "晚安，明天见",
                        )
                        await asyncio.gather(*list(plugin._background_tasks))
                        plugin._maybe_decide_after_private_chat_end(
                            "s2",
                            {
                                "user_id": "u1",
                                "stream_id": "s2",
                                "is_private": False,
                                "last_user_text": "晚安",
                            },
                            "晚安",
                        )
                        plugin._maybe_decide_after_private_chat_end(
                            "s3",
                            {
                                "user_id": "u2",
                                "stream_id": "s3",
                                "is_private": True,
                                "last_user_text": "拜拜",
                            },
                            "拜拜",
                        )

                    asyncio.run(scenario())

                    works = store.pending_planner_work(limit=5)
                    self.assertEqual(len(works), 1)
                    self.assertEqual(works[0]["task_type"], "schedule_decide")
                    payload = works[0]["payload"]
                    self.assertEqual(payload["trigger"], "private_chat_end")
                    self.assertEqual(payload["focus_user_id"], "u1")
                    self.assertEqual(payload["candidate_user_ids"], ["u1"])
                    self.assertEqual(payload["date"], plugin._now().date().isoformat())
                    self.assertEqual(
                        [item["content"] for item in payload["conversation"]],
                        ["晚安", "晚安，明天见"],
                    )

                    store.set_schedule(
                        fire_at=plugin._now().timestamp() + 3600,
                        reason="别的用户",
                        topics=[],
                        target_user="u2",
                        kind="share",
                        decided_at=plugin._now().timestamp(),
                    )
                    applied = asyncio.run(
                        plugin._apply_planner_work(
                            {
                                "task_type": "schedule_decide",
                                "payload": {
                                    "focus_user_id": "u1",
                                    "candidate_user_ids": ["u1"],
                                },
                            },
                            {"skip": True, "reason": "这段对话不需要再联系"},
                            plugin._now(),
                        )
                    )
                    self.assertTrue(applied)
                    self.assertEqual(store.get_schedule()["target_user"], "u2")

                    applied = asyncio.run(
                        plugin._apply_planner_work(
                            {
                                "task_type": "schedule_decide",
                                "payload": {
                                    "focus_user_id": "u1",
                                    "candidate_user_ids": ["u1"],
                                },
                            },
                            {
                                "skip": False,
                                "target_user": "u1",
                                "fire_at": (plugin._now() + timedelta(hours=2)).isoformat(),
                                "reason": "问问近况",
                                "topics": ["近况"],
                                "kind": "ask",
                            },
                            plugin._now(),
                        )
                    )
                    self.assertTrue(applied)
                    self.assertEqual(store.get_schedule()["target_user"], "u2")
                finally:
                    plugin._store = None
                    store.close()
        finally:
            sys.modules.pop(module_name, None)

    def test_affection_evaluation_falls_back_to_local_interactions(self) -> None:
        module_name = "mai_persona_affection_fallback_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory() as temp:
                plugin = module.create_plugin()
                plugin.set_plugin_config(
                    {"plugin": {"enabled": True, "config_version": "1.0.0"}}
                )
                plugin._set_context(
                    types.SimpleNamespace(api=Mock(), logger=Mock(spec=logging.Logger))
                )
                store = PersonaStore(Path(temp) / "persona.db")
                plugin._store = store
                plugin._memory = None
                try:
                    now = plugin._now()
                    store.initialize_emotion(PAD(0.0, 0.0, 0.0), now.timestamp())
                    store.add_interaction(
                        now.timestamp() - 60, "u1", "s1", "user", "我今天面试通过了"
                    )
                    queued = asyncio.run(
                        plugin._evaluate_affection_for_users(["u1"], now, "affection:test")
                    )
                    self.assertEqual(queued, 1)
                    works = store.pending_planner_work(limit=3)
                    self.assertEqual(works[0]["task_type"], "affection_evaluate")
                    payload = works[0]["payload"]
                    self.assertEqual(payload["available_facts"]["u1"], ["我今天面试通过了"])

                    self.assertEqual(
                        asyncio.run(
                            plugin._evaluate_affection_for_users(
                                ["u2"], now, "affection:test-empty"
                            )
                        ),
                        0,
                    )
                finally:
                    plugin._store = None
                    store.close()
        finally:
            sys.modules.pop(module_name, None)

    def test_planner_hook_never_adds_second_system_message(self) -> None:
        module_name = "mai_persona_planner_system_merge_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            plugin = module.create_plugin()
            plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}})
            plugin._store = object()
            plugin._planner_context = lambda now, user_id: "[Mai Persona 角色设定] 测试角色"
            plugin._session_context["stream-1"] = {"user_id": "user-1", "stream_id": "stream-1"}

            merged_result = asyncio.run(
                plugin.handle_planner_before_request(
                    items=[
                        {
                            "item_type": "SystemMessageItem",
                            "meta": {"item_id": "system"},
                            "parts": [{"type": "text", "text": "MaiBot 原生人格"}],
                        },
                        {
                            "item_type": "UserMessageItem",
                            "meta": {"item_id": "user"},
                            "parts": [{"type": "text", "text": "你好"}],
                        },
                        {
                            "item_type": "SystemMessageItem",
                            "meta": {"item_id": "mai-persona:legacy"},
                            "parts": [{"type": "text", "text": "旧注入"}],
                        },
                    ],
                    session_id="stream-1",
                )
            )
            items = merged_result["modified_kwargs"]["items"]
            system_items = [item for item in items if item["item_type"] == "SystemMessageItem"]
            self.assertEqual(len(system_items), 1)
            self.assertEqual(items[0]["meta"]["item_id"], "system")
            merged = "".join(part["text"] for part in items[0]["parts"])
            self.assertIn("MaiBot 原生人格", merged)
            self.assertIn("[Mai Persona 角色设定] 测试角色", merged)
            self.assertNotIn("旧注入", merged)
            self.assertFalse(
                any(str(item["meta"].get("item_id", "")).startswith("mai-persona:") for item in items)
            )

            inserted_result = asyncio.run(
                plugin.handle_planner_before_request(
                    items=[
                        {
                            "item_type": "UserMessageItem",
                            "meta": {"item_id": "user-only"},
                            "parts": [{"type": "text", "text": "只有用户消息"}],
                        }
                    ],
                    session_id="stream-1",
                )
            )
            inserted = inserted_result["modified_kwargs"]["items"]
            self.assertEqual(inserted[0]["item_type"], "SystemMessageItem")
            self.assertEqual(inserted[0]["parts"][0]["text"], "[Mai Persona 角色设定] 测试角色")
            self.assertEqual(inserted[1]["meta"]["item_id"], "user-only")
        finally:
            sys.modules.pop(module_name, None)

    def test_maitrace_schedule_uses_public_plugin_api(self) -> None:
        module_name = "mai_persona_maitrace_api_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        class FakeAPI:
            def __init__(self) -> None:
                self.calls = []

            async def call(self, api_name: str, **kwargs):
                self.calls.append((api_name, kwargs))
                if api_name.endswith(".get_schedule"):
                    return [
                        {
                            "start": "2026-10-31T10:00:00+08:00",
                            "end": "2026-10-31T11:00:00+08:00",
                            "name": "阅读",
                        }
                    ]
                return {
                    "has_activity": True,
                    "activity": {"name": "阅读", "time_window": "10:00-11:00"},
                    "next_activities": [{"time": "13:00", "name": "散步"}],
                    "error": None,
                }

        try:
            spec.loader.exec_module(module)
            plugin = module.create_plugin()
            plugin.set_plugin_config(
                {
                    "plugin": {"enabled": True, "config_version": "1.0.0"},
                    "batch": {"use_maitrace_schedule": True},
                }
            )
            api = FakeAPI()
            plugin._set_context(types.SimpleNamespace(api=api, logger=Mock(spec=logging.Logger)))
            now = datetime(2026, 10, 31, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
            asyncio.run(plugin._refresh_maitrace_schedule(now))
            self.assertEqual(len(api.calls), 2)
            self.assertEqual(
                api.calls[0],
                ("Rabbit-Jia-Er.MaiTrace.get_current_activity", {"version": "1", "chat_id": "global"}),
            )
            self.assertEqual(
                api.calls[1],
                ("Rabbit-Jia-Er.MaiTrace.get_schedule", {"version": "1", "day": "2026-10-31"}),
            )
            self.assertEqual(plugin._activity_snapshot(now)[0], "阅读（10:00-11:00）")
            self.assertIn("10:00-11:00 阅读", plugin._maitrace_schedule_context(now))

            class FakeStore:
                saved = None

                def add_life_items(self, date, schedule, micro_events):
                    self.saved = (date, schedule, micro_events)

                def set_meta(self, key, value):
                    pass

            plugin._store = FakeStore()
            applied = asyncio.run(
                plugin._apply_planner_work(
                    {"task_type": "life_generate", "payload": {"date": "2026-10-31"}},
                    {
                        "schedule": [{"slot": "morning", "content": "本地重复日程"}],
                        "micro_events": [
                            {
                                "slot": "afternoon",
                                "content": "读到有趣的文章",
                                "mood_bias": 0.05,
                                "micro_type": "discovery",
                            }
                        ],
                    },
                    now,
                )
            )
            self.assertTrue(applied)
            self.assertEqual(plugin._store.saved[1], [])
            self.assertEqual(len(plugin._store.saved[2]), 1)
        finally:
            sys.modules.pop(module_name, None)

    def test_disabled_proactive_schedule_clears_pending_plan(self) -> None:
        module_name = "mai_persona_schedule_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            PLUGIN_ROOT / "plugin.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        class FakeStore:
            cleared = False

            def clear_schedule(self) -> None:
                self.cleared = True

        try:
            spec.loader.exec_module(module)
            plugin = module.create_plugin()
            plugin.set_plugin_config(
                {"plugin": {"enabled": True, "config_version": "1.0.0"}}
            )
            store = FakeStore()
            plugin._store = store
            asyncio.run(
                plugin._decide_schedule_if_due(
                    datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
                )
            )
            self.assertTrue(store.cleared)
        finally:
            sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
