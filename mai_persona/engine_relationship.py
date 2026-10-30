"""关系阶段迁移规则。"""

from __future__ import annotations

from typing import Any


def validate_transition(stages: list[str], current: str, target: str) -> tuple[bool, str]:
    normalized = [stage.strip() for stage in stages if stage.strip()]
    if target not in normalized:
        return False, "目标关系阶段不在配置序列中"
    if not current:
        if target == normalized[0]:
            return True, ""
        return False, f"首次设置只能从初始阶段「{normalized[0]}」开始"
    if current not in normalized:
        return False, "当前关系阶段已不在配置序列中，请先修正配置"
    distance = abs(normalized.index(target) - normalized.index(current))
    if distance != 1:
        return False, "关系阶段只允许向相邻阶段迁移"
    return True, ""


def update_relationship(
    store: Any,
    stages: list[str],
    user_id: str,
    target: str,
    free_text: str,
    now: float,
) -> tuple[bool, str]:
    current_row = store.relationship(user_id)
    current = str(current_row["stage"]) if current_row else ""
    allowed, error = validate_transition(stages, current, target)
    if not allowed:
        return False, error
    store.update_relationship(user_id, target, free_text.strip()[:1000], now)
    return True, f"关系阶段已更新为「{target}」"
