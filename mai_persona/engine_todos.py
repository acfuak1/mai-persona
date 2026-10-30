"""对话待办抽取和状态更新。"""

from __future__ import annotations

from typing import Any


def apply_todo_result(store: Any, result: Any, known_users: set[str], now: float) -> int:
    if not isinstance(result, dict):
        return 0
    changed = 0
    raw_todos = result.get("todos")
    if isinstance(raw_todos, list):
        for item in raw_todos:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id", "")).strip()
            content = str(item.get("content", "")).strip()
            if user_id not in known_users or not content:
                continue
            due_hint = str(item.get("due_hint", "")).strip()[:240]
            store.add_todo(user_id, content[:500], due_hint, None, now)
            changed += 1
    raw_updates = result.get("updates")
    if isinstance(raw_updates, list):
        allowed_ids = {
            int(todo["id"])
            for user_id in known_users
            for todo in store.open_todos(user_id)
        }
        for item in raw_updates:
            if not isinstance(item, dict):
                continue
            try:
                todo_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if todo_id not in allowed_ids:
                continue
            status = str(item.get("status", "")).strip().lower()
            if status in {"resolved", "cancelled"}:
                store.set_todo_status(todo_id, status)
                changed += 1
    return changed
