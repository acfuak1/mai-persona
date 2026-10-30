"""基于可追溯事实的三维好感更新。"""

from __future__ import annotations

from typing import Any


def apply_changes(
    store: Any,
    changes: list[dict[str, Any]],
    available_facts: dict[str, list[str]],
    max_delta: float,
    now: float,
) -> int:
    applied = 0
    for change in changes:
        user_id = str(change.get("user_id", "")).strip()
        if not user_id:
            continue
        refs = change.get("facts_refs")
        if not isinstance(refs, list):
            continue
        evidence = [str(ref).strip() for ref in refs if str(ref).strip()]
        known = set(available_facts.get(user_id, []))
        matched = [ref for ref in evidence if ref in known]
        reason = str(change.get("reason", "")).strip()
        if not matched or len(reason) < 4:
            continue

        def bounded(key: str) -> float:
            try:
                value = float(change.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0
            return max(-max_delta, min(max_delta, value))

        deltas = (bounded("d_trust"), bounded("d_intimacy"), bounded("d_rapport"))
        if not any(deltas):
            continue
        store.add_affection(user_id, deltas, reason, matched, now)
        applied += 1
    return applied
