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
from datetime import datetime
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
from mai_persona.engine_schedule import hard_guard
from mai_persona.prompting import persona_block
from mai_persona.engine_todos import apply_todo_result
from mai_persona.maitrace_schedule import (
    format_schedule_summary,
    normalize_schedule,
    normalize_snapshot,
)
from mai_persona.mem_adapter import MemoryAdapter
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
