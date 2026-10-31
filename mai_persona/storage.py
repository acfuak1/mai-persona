"""Mai Persona 的 SQLite 持久化层。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .engine_emotion import PAD, decay, emotion_label


SCHEMA = """
CREATE TABLE IF NOT EXISTS emotion_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    trait_p REAL NOT NULL, trait_a REAL NOT NULL, trait_d REAL NOT NULL,
    mood_p REAL NOT NULL, mood_a REAL NOT NULL, mood_d REAL NOT NULL,
    emo_p REAL NOT NULL, emo_a REAL NOT NULL, emo_d REAL NOT NULL,
    mood_label TEXT NOT NULL DEFAULT '平常', emo_label TEXT NOT NULL DEFAULT '平常',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS emotion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, source TEXT NOT NULL, event_desc TEXT NOT NULL,
    appraisal TEXT NOT NULL, facts_refs TEXT NOT NULL DEFAULT '[]',
    delta_p REAL NOT NULL, delta_a REAL NOT NULL, delta_d REAL NOT NULL,
    applied_layer TEXT NOT NULL, label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS daily_life (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, kind TEXT NOT NULL, slot TEXT NOT NULL,
    content TEXT NOT NULL, mood_bias REAL NOT NULL DEFAULT 0,
    micro_type TEXT NOT NULL DEFAULT 'none', occurred INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_daily_life_date_slot ON daily_life(date, slot);
CREATE TABLE IF NOT EXISTS event_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, content TEXT NOT NULL, stage TEXT NOT NULL,
    mood_tag REAL NOT NULL DEFAULT 0, talkable REAL NOT NULL DEFAULT 0,
    started_at REAL NOT NULL, closed_at REAL, last_advanced_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS affection (
    user_id TEXT PRIMARY KEY,
    trust REAL NOT NULL DEFAULT 0, intimacy REAL NOT NULL DEFAULT 0,
    rapport REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS affection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, ts REAL NOT NULL, d_trust REAL NOT NULL,
    d_intimacy REAL NOT NULL, d_rapport REAL NOT NULL, reason TEXT NOT NULL,
    facts_refs TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relationship (
    user_id TEXT PRIMARY KEY, stage TEXT NOT NULL, free_text TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
    content TEXT NOT NULL, due_at REAL, due_hint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, asked_at REAL
);
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY CHECK (id = 1), fire_at REAL, reason TEXT NOT NULL DEFAULT '',
    topics TEXT NOT NULL DEFAULT '[]', target_user TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'share', decided_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS diaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS interaction_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT NOT NULL,
    stream_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    evaluated INTEGER NOT NULL DEFAULT 0, todo_checked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_interaction_queue_pending ON interaction_queue(evaluated, ts);
CREATE INDEX IF NOT EXISTS idx_interaction_queue_user ON interaction_queue(user_id, ts);
CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS planner_work (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    prompt_name TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    task TEXT NOT NULL, date TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task, date)
);
CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT NOT NULL,
    stream_id TEXT NOT NULL, content TEXT NOT NULL, reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_ts ON outbound_messages(ts);
"""


class PersonaStore:
    """提供短事务 SQLite 操作；仅由插件内单一数据库连接访问。"""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = database_path
        self.connection = sqlite3.connect(str(database_path), timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(interaction_queue)").fetchall()
        }
        if "message_id" not in columns:
            self.connection.execute("ALTER TABLE interaction_queue ADD COLUMN message_id TEXT NOT NULL DEFAULT ''")
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_message_unique "
            "ON interaction_queue(stream_id, message_id, role) WHERE message_id<>''"
        )
        self.connection.commit()
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cursor = self.connection.execute(sql, tuple(params))
            self.connection.commit()
            return int(cursor.lastrowid or 0)

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(sql, tuple(params)).fetchone()
            return dict(row) if row is not None else None

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def initialize_emotion(self, trait: PAD, now: float) -> None:
        with self._lock:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO emotion_state
                    (id, trait_p, trait_a, trait_d, mood_p, mood_a, mood_d,
                     emo_p, emo_a, emo_d, mood_label, emo_label, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trait.p,
                    trait.a,
                    trait.d,
                    trait.p,
                    trait.a,
                    trait.d,
                    trait.p,
                    trait.a,
                    trait.d,
                    emotion_label(trait),
                    emotion_label(trait),
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE emotion_state SET trait_p=?, trait_a=?, trait_d=? WHERE id=1",
                (trait.p, trait.a, trait.d),
            )
            self.connection.commit()

    def emotion(self) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM emotion_state WHERE id=1")

    def decay_emotion(self, now: float, tau_emotion_seconds: float, tau_mood_seconds: float) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM emotion_state WHERE id=1").fetchone()
            if row is None:
                return None
            current = dict(row)
            elapsed = max(0.0, now - float(current["updated_at"]))
            trait = PAD(current["trait_p"], current["trait_a"], current["trait_d"])
            mood = decay(PAD(current["mood_p"], current["mood_a"], current["mood_d"]), trait, elapsed, tau_mood_seconds)
            emotion = decay(
                PAD(current["emo_p"], current["emo_a"], current["emo_d"]),
                mood,
                elapsed,
                tau_emotion_seconds,
            )
            self.connection.execute(
                """
                UPDATE emotion_state
                SET mood_p=?, mood_a=?, mood_d=?, emo_p=?, emo_a=?, emo_d=?,
                    mood_label=?, emo_label=?, updated_at=?
                WHERE id=1
                """,
                (
                    mood.p,
                    mood.a,
                    mood.d,
                    emotion.p,
                    emotion.a,
                    emotion.d,
                    emotion_label(mood),
                    emotion_label(emotion),
                    now,
                ),
            )
            self.connection.commit()
            return self.emotion()

    def apply_emotion(
        self,
        *,
        now: float,
        source: str,
        event_desc: str,
        appraisal: str,
        facts_refs: list[str],
        delta: PAD,
        layer: str,
        label: str,
        mood_label: str,
        emotion_label: str,
    ) -> None:
        target = "mood" if layer == "mood" else "emo"
        columns = (f"{target}_p", f"{target}_a", f"{target}_d")
        with self._lock:
            self.connection.execute(
                f"UPDATE emotion_state SET {columns[0]}=MAX(-1, MIN(1, {columns[0]} + ?)), "
                f"{columns[1]}=MAX(-1, MIN(1, {columns[1]} + ?)), "
                f"{columns[2]}=MAX(-1, MIN(1, {columns[2]} + ?)), "
                "mood_label=?, emo_label=?, updated_at=? WHERE id=1",
                (delta.p, delta.a, delta.d, mood_label, emotion_label, now),
            )
            self.connection.execute(
                """
                INSERT INTO emotion_events
                    (ts, source, event_desc, appraisal, facts_refs, delta_p, delta_a, delta_d, applied_layer, label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    source,
                    event_desc,
                    appraisal,
                    json.dumps(facts_refs, ensure_ascii=False),
                    delta.p,
                    delta.a,
                    delta.d,
                    target,
                    label,
                ),
            )
            self.connection.commit()

    def add_interaction(
        self,
        now: float,
        user_id: str,
        stream_id: str,
        role: str,
        content: str,
        message_id: str = "",
    ) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO interaction_queue
                    (ts, user_id, stream_id, role, content, message_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now, user_id, stream_id, role, content, message_id),
            )
            if cursor.rowcount == 1:
                self.connection.commit()
                return int(cursor.lastrowid)
            row = self.connection.execute(
                "SELECT id FROM interaction_queue WHERE stream_id=? AND message_id=? AND role=?",
                (stream_id, message_id, role),
            ).fetchone()
            self.connection.commit()
            return int(row["id"]) if row else 0

    def pending_interactions(self, since: float = 0.0, user_id: str = "") -> list[dict[str, Any]]:
        if user_id:
            return self.fetchall(
                "SELECT * FROM interaction_queue WHERE evaluated=0 AND ts>=? AND user_id=? ORDER BY ts",
                (since, user_id),
            )
        return self.fetchall("SELECT * FROM interaction_queue WHERE evaluated=0 AND ts>=? ORDER BY ts", (since,))

    def mark_interactions_evaluated(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.execute(f"UPDATE interaction_queue SET evaluated=1 WHERE id IN ({placeholders})", ids)

    def unprocessed_todo_interactions(self, since: float) -> list[dict[str, Any]]:
        return self.fetchall("SELECT * FROM interaction_queue WHERE todo_checked=0 AND ts>=? ORDER BY ts", (since,))

    def mark_todo_checked(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.execute(f"UPDATE interaction_queue SET todo_checked=1 WHERE id IN ({placeholders})", ids)

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.fetchone("SELECT value FROM runtime_meta WHERE key=?", (key,))
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO runtime_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def queue_planner_work(
        self,
        task_type: str,
        prompt_name: str,
        dedupe_key: str,
        payload: dict[str, Any],
        created_at: float,
    ) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO planner_work(task_type, prompt_name, dedupe_key, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_type,
                    prompt_name,
                    dedupe_key,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    created_at,
                ),
            )
            if cursor.rowcount == 1:
                work_id = int(cursor.lastrowid)
            else:
                row = self.connection.execute(
                    "SELECT id FROM planner_work WHERE dedupe_key=?",
                    (dedupe_key,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Planner 任务去重失败，未找到已存在的任务")
                work_id = int(row["id"])
            self.connection.commit()
            return work_id

    def pending_planner_work(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT * FROM planner_work ORDER BY created_at, id LIMIT ?",
            (max(1, min(int(limit), 50)),),
        )
        for row in rows:
            try:
                row["payload"] = json.loads(str(row["payload"]))
            except (TypeError, ValueError):
                row["payload"] = {}
        return rows

    def planner_work(self, work_id: int) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM planner_work WHERE id=?", (work_id,))
        if row is None:
            return None
        try:
            row["payload"] = json.loads(str(row["payload"]))
        except (TypeError, ValueError):
            row["payload"] = {}
        return row

    def complete_planner_work(self, work_id: int) -> None:
        self.execute("DELETE FROM planner_work WHERE id=?", (work_id,))

    def consume_budget(self, task: str, date: str, limit: int) -> bool:
        if limit <= 0:
            return False
        with self._lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO llm_usage(task, date, calls) VALUES (?, ?, 0)",
                (task, date),
            )
            row = self.connection.execute(
                "SELECT calls FROM llm_usage WHERE task=? AND date=?",
                (task, date),
            ).fetchone()
            calls = int(row["calls"]) if row else 0
            if calls >= limit:
                return False
            self.connection.execute(
                "UPDATE llm_usage SET calls=calls+1 WHERE task=? AND date=?",
                (task, date),
            )
            self.connection.commit()
            return True

    def add_life_items(self, date: str, schedule: list[dict[str, Any]], micro_events: list[dict[str, Any]]) -> None:
        with self._lock:
            for item in schedule:
                self.connection.execute(
                    "INSERT INTO daily_life(date, kind, slot, content) VALUES (?, 'schedule', ?, ?)",
                    (date, item["slot"], item["content"]),
                )
            for item in micro_events:
                self.connection.execute(
                    """
                    INSERT INTO daily_life(date, kind, slot, content, mood_bias, micro_type)
                    VALUES (?, 'micro_event', ?, ?, ?, ?)
                    """,
                    (date, item["slot"], item["content"], item["mood_bias"], item["micro_type"]),
                )
            self.connection.commit()

    def clear_pending_life_items(self, date: str) -> None:
        """Clear replaceable daily plans while retaining micro-events already applied."""

        self.execute(
            "DELETE FROM daily_life WHERE date=? AND (kind='schedule' OR (kind='micro_event' AND occurred=0))",
            (date,),
        )

    def unoccurred_micro_events(self, date: str, slots: list[str]) -> list[dict[str, Any]]:
        if not slots:
            return []
        placeholders = ",".join("?" for _ in slots)
        return self.fetchall(
            f"SELECT * FROM daily_life WHERE date=? AND kind='micro_event' AND occurred=0 AND slot IN ({placeholders})",
            [date, *slots],
        )

    def mark_life_occurred(self, item_id: int) -> None:
        self.execute("UPDATE daily_life SET occurred=1 WHERE id=?", (item_id,))

    def current_activity(self, date: str, current_slot: str) -> str:
        slot_order = ("morning", "afternoon", "evening", "night")
        if current_slot not in slot_order:
            return ""
        elapsed = slot_order[: slot_order.index(current_slot) + 1]
        placeholders = ",".join("?" for _ in elapsed)
        row = self.fetchone(
            f"""
            SELECT content FROM daily_life WHERE date=? AND kind='schedule' AND slot IN ({placeholders})
            ORDER BY CASE slot WHEN 'morning' THEN 0 WHEN 'afternoon' THEN 1
                               WHEN 'evening' THEN 2 ELSE 3 END DESC LIMIT 1
            """,
            [date, *elapsed],
        )
        return str(row["content"]) if row else ""

    def active_event_lines(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM event_lines WHERE closed_at IS NULL ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )

    def active_users(self, since: float) -> list[str]:
        rows = self.fetchall(
            "SELECT DISTINCT user_id FROM interaction_queue WHERE user_id<>'' AND ts>=? ORDER BY user_id",
            (since,),
        )
        return [str(row["user_id"]) for row in rows]

    def relationship(self, user_id: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM relationship WHERE user_id=?", (user_id,))

    def affection(self, user_id: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM affection WHERE user_id=?", (user_id,))

    def update_relationship(self, user_id: str, stage: str, free_text: str, now: float) -> None:
        self.execute(
            """
            INSERT INTO relationship(user_id, stage, free_text, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET stage=excluded.stage, free_text=excluded.free_text,
                updated_at=excluded.updated_at
            """,
            (user_id, stage, free_text, now),
        )

    def add_affection(
        self,
        user_id: str,
        deltas: tuple[float, float, float],
        reason: str,
        facts_refs: list[str],
        now: float,
    ) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO affection(user_id, updated_at) VALUES (?, ?)",
                (user_id, now),
            )
            self.connection.execute(
                """
                UPDATE affection SET trust=MAX(-1, MIN(1, trust+?)),
                    intimacy=MAX(-1, MIN(1, intimacy+?)), rapport=MAX(-1, MIN(1, rapport+?)),
                    updated_at=? WHERE user_id=?
                """,
                (*deltas, now, user_id),
            )
            self.connection.execute(
                """
                INSERT INTO affection_log(user_id, ts, d_trust, d_intimacy, d_rapport, reason, facts_refs)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, now, *deltas, reason, json.dumps(facts_refs, ensure_ascii=False)),
            )
            self.connection.commit()

    def add_todo(self, user_id: str, content: str, due_hint: str, due_at: float | None, now: float) -> int:
        return self.execute(
            """
            INSERT INTO todos(user_id, content, due_hint, due_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, content, due_hint, due_at, now),
        )

    def open_todos(self, user_id: str = "") -> list[dict[str, Any]]:
        if user_id:
            return self.fetchall("SELECT * FROM todos WHERE status='open' AND user_id=? ORDER BY due_at", (user_id,))
        return self.fetchall("SELECT * FROM todos WHERE status='open' ORDER BY due_at")

    def set_todo_status(self, todo_id: int, status: str) -> None:
        self.execute("UPDATE todos SET status=? WHERE id=?", (status, todo_id))

    def set_schedule(
        self,
        *,
        fire_at: float | None,
        reason: str,
        topics: list[str],
        target_user: str,
        kind: str,
        decided_at: float,
    ) -> None:
        self.execute(
            """
            INSERT INTO schedule(id, fire_at, reason, topics, target_user, kind, decided_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET fire_at=excluded.fire_at, reason=excluded.reason,
                topics=excluded.topics, target_user=excluded.target_user, kind=excluded.kind,
                decided_at=excluded.decided_at
            """,
            (fire_at, reason, json.dumps(topics, ensure_ascii=False), target_user, kind, decided_at),
        )

    def get_schedule(self) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM schedule WHERE id=1")

    def clear_schedule(self) -> None:
        self.execute("DELETE FROM schedule WHERE id=1")

    def insert_diary(self, date: str, content: str, now: float) -> tuple[int, bool]:
        with self._lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO diaries(date, content, created_at) VALUES (?, ?, ?)",
                (date, content, now),
            )
            row = self.connection.execute("SELECT id FROM diaries WHERE date=?", (date,)).fetchone()
            self.connection.commit()
            return (int(row["id"]) if row else 0, cursor.rowcount == 1)

    def log_outbound(self, now: float, user_id: str, stream_id: str, content: str, reason: str) -> None:
        self.execute(
            "INSERT INTO outbound_messages(ts, user_id, stream_id, content, reason) VALUES (?, ?, ?, ?, ?)",
            (now, user_id, stream_id, content, reason),
        )

    def last_outbound_at(self) -> float:
        row = self.fetchone("SELECT MAX(ts) AS ts FROM outbound_messages")
        return float(row["ts"] or 0.0) if row else 0.0

    def outbound_count_today(self, day_start: float) -> int:
        row = self.fetchone("SELECT COUNT(*) AS n FROM outbound_messages WHERE ts>=?", (day_start,))
        return int(row["n"] or 0) if row else 0

    def last_user_interaction_at(self, user_id: str) -> float:
        row = self.fetchone(
            "SELECT MAX(ts) AS ts FROM interaction_queue WHERE user_id=? AND role='user'",
            (user_id,),
        )
        return float(row["ts"] or 0.0) if row else 0.0

    def latest_stream(self, user_id: str) -> str:
        row = self.fetchone(
            "SELECT stream_id FROM interaction_queue WHERE user_id=? AND stream_id<>'' ORDER BY ts DESC LIMIT 1",
            (user_id,),
        )
        return str(row["stream_id"]) if row else ""

    def latest_user_id(self, stream_id: str) -> str:
        """按聊天流找到最近一条用户消息对应的用户。"""

        row = self.fetchone(
            "SELECT user_id FROM interaction_queue "
            "WHERE stream_id=? AND role='user' AND user_id<>'' ORDER BY ts DESC, id DESC LIMIT 1",
            (stream_id,),
        )
        return str(row["user_id"]) if row else ""

    def inner_state(self, user_id: str = "") -> dict[str, Any]:
        emotion = self.emotion() or {}
        relation = self.relationship(user_id) if user_id else None
        affection = self.affection(user_id) if user_id else None
        return {
            "emotion": emotion,
            "relationship": relation,
            "affection": affection,
            "activity": self.fetchone(
                "SELECT content FROM daily_life WHERE date=date('now', 'localtime') AND kind='schedule' "
                "ORDER BY CASE slot WHEN 'night' THEN 0 WHEN 'evening' THEN 1 "
                "WHEN 'afternoon' THEN 2 ELSE 3 END LIMIT 1"
            ),
            "event_lines": self.active_event_lines(3),
            "todos": self.open_todos(user_id),
        }
