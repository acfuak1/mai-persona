"""为 MaiBot WebUI 构造只读的插件状态快照。"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any


def build_dashboard_snapshot(
    store: Any,
    *,
    enabled: bool,
    now: datetime,
    current_slot: str,
    timezone: Any,
    event_limit: int,
    missing_config: list[str] | None = None,
) -> dict[str, Any]:
    missing = missing_config or []
    snapshot: dict[str, Any] = {
        "status": "已停用" if not enabled else "等待配置" if missing else "等待初始化",
        "updated_at": "未记录",
        "mood_label": "平常",
        "emotion_label": "平常",
        "mood": {"p": 0.0, "a": 0.0, "d": 0.0},
        "emotion": {"p": 0.0, "a": 0.0, "d": 0.0},
        "activity": "尚无今日安排",
        "event_count": 0,
        "todo_count": 0,
        "relationship_count": 0,
        "event_lines": [],
        "diaries": [],
        "relationships": [],
        "todos": [],
    }
    if store is None:
        return snapshot

    def number(value: Any) -> float:
        try:
            parsed = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return round(parsed, 2) if isfinite(parsed) else 0.0

    def display_time(value: Any) -> str:
        if value is None:
            return "未设置"
        try:
            return datetime.fromtimestamp(float(value), timezone).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, TypeError, ValueError):
            return "未记录"

    emotion_row = store.emotion() or {}
    snapshot.update(
        {
            "status": "运行中" if enabled else "已停用",
            "updated_at": display_time(emotion_row.get("updated_at")),
            "mood_label": str(emotion_row.get("mood_label") or "平常"),
            "emotion_label": str(emotion_row.get("emo_label") or "平常"),
            "mood": {
                "p": number(emotion_row.get("mood_p")),
                "a": number(emotion_row.get("mood_a")),
                "d": number(emotion_row.get("mood_d")),
            },
            "emotion": {
                "p": number(emotion_row.get("emo_p")),
                "a": number(emotion_row.get("emo_a")),
                "d": number(emotion_row.get("emo_d")),
            },
            "activity": (store.current_activity(now.date().isoformat(), current_slot) or "尚无今日安排")[:240],
        }
    )

    event_lines = store.active_event_lines(event_limit)
    snapshot["event_count"] = len(event_lines)
    snapshot["event_lines"] = [
        {
            "title": str(row.get("title") or ""),
            "stage": str(row.get("stage") or ""),
            "content": str(row.get("content") or "")[:180],
            "talkable": number(row.get("talkable")),
            "started_at": display_time(row.get("started_at")),
        }
        for row in event_lines
    ]

    diary_rows = store.fetchall(
        "SELECT date, content, created_at FROM diaries ORDER BY date DESC LIMIT 14"
    )
    snapshot["diaries"] = [
        {
            "date": str(row.get("date") or ""),
            "preview": str(row.get("content") or "")[:80],
            "content": str(row.get("content") or "")[:200],
            "created_at": display_time(row.get("created_at")),
        }
        for row in diary_rows
    ]

    relationship_count = store.fetchone("SELECT COUNT(*) AS count FROM relationship") or {}
    snapshot["relationship_count"] = int(relationship_count.get("count") or 0)
    relationship_rows = store.fetchall(
        """
        SELECT r.user_id, r.stage, r.free_text, r.updated_at,
               a.trust, a.intimacy, a.rapport
        FROM relationship AS r
        LEFT JOIN affection AS a ON a.user_id=r.user_id
        ORDER BY r.updated_at DESC
        LIMIT 50
        """
    )
    snapshot["relationships"] = [
        {
            "user_id": str(row.get("user_id") or ""),
            "stage": str(row.get("stage") or ""),
            "free_text": str(row.get("free_text") or "")[:120],
            "trust": number(row.get("trust")),
            "intimacy": number(row.get("intimacy")),
            "rapport": number(row.get("rapport")),
            "updated_at": display_time(row.get("updated_at")),
        }
        for row in relationship_rows
    ]

    todo_count = store.fetchone("SELECT COUNT(*) AS count FROM todos WHERE status='open'") or {}
    snapshot["todo_count"] = int(todo_count.get("count") or 0)
    todo_rows = store.fetchall(
        """
        SELECT user_id, content, due_hint, due_at, created_at
        FROM todos
        WHERE status='open'
        ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, created_at DESC
        LIMIT 50
        """
    )
    snapshot["todos"] = [
        {
            "user_id": str(row.get("user_id") or ""),
            "content": str(row.get("content") or "")[:120],
            "due": display_time(row.get("due_at"))
            if row.get("due_at") is not None
            else str(row.get("due_hint") or "未设置"),
            "created_at": display_time(row.get("created_at")),
        }
        for row in todo_rows
    ]
    return snapshot
