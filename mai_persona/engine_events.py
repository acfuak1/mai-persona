"""跨日事件线推进规则。"""

from __future__ import annotations

from typing import Any


EVENT_STAGES = ("planning", "doing", "stuck", "breakthrough", "done")


def apply_advances(store: Any, result: Any, now: float, max_active: int) -> int:
    if not isinstance(result, dict) or not isinstance(result.get("lines"), list):
        return 0
    rows = {int(row["id"]): row for row in store.active_event_lines(max_active + 10)}
    active_count = len(rows)
    changed = 0
    for item in result["lines"]:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).strip().lower()
        progress = str(item.get("progress_desc", "")).strip()[:1000]
        if action == "new":
            if active_count >= max_active:
                continue
            title = str(item.get("title", "")).strip()[:160]
            if not title or not progress:
                continue
            stage = str(item.get("new_stage", "planning")).strip()
            if stage not in EVENT_STAGES:
                stage = "planning"
            mood_tag = _bounded(item.get("mood_tag", 0.0))
            talkable = _bounded(item.get("talkable", 0.0), 0.0, 1.0)
            store.execute(
                """
                INSERT INTO event_lines(title, content, stage, mood_tag, talkable, started_at, last_advanced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, progress, stage, mood_tag, talkable, now, now),
            )
            active_count += 1
            changed += 1
            continue

        try:
            line_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        row = rows.get(line_id)
        if row is None or action not in {"advance", "stall", "close"}:
            continue
        if action == "close":
            store.execute(
                "UPDATE event_lines SET stage='done', content=?, closed_at=?, last_advanced_at=? WHERE id=?",
                (progress or row["content"], now, now, line_id),
            )
            active_count = max(0, active_count - 1)
        else:
            stage = str(item.get("new_stage", row["stage"])).strip()
            if stage not in EVENT_STAGES:
                stage = str(row["stage"])
            store.execute(
                """
                UPDATE event_lines SET stage=?, content=?, mood_tag=?, talkable=?, last_advanced_at=?
                WHERE id=?
                """,
                (
                    stage,
                    progress or row["content"],
                    _bounded(item.get("mood_tag", row["mood_tag"])),
                    _bounded(item.get("talkable", row["talkable"]), 0.0, 1.0),
                    now,
                    line_id,
                ),
            )
        changed += 1
    return changed


def _bounded(value: Any, low: float = -1.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return max(low, min(high, number))
