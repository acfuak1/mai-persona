"""日程与日常小事件生成结果的校验。"""

from __future__ import annotations

from typing import Any


SLOTS = ("morning", "afternoon", "evening", "night")
MICRO_TYPES = {"delight", "mild_annoyance", "discovery", "none"}


def validate_life_result(result: Any, max_micro_bias: float) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    if not isinstance(result, dict):
        return [], []
    schedule: list[dict[str, str]] = []
    raw_schedule = result.get("schedule")
    if isinstance(raw_schedule, list):
        for item in raw_schedule:
            if not isinstance(item, dict):
                continue
            slot = str(item.get("slot", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if slot in SLOTS and content:
                schedule.append({"slot": slot, "content": content[:500]})

    events: list[dict[str, Any]] = []
    raw_events = result.get("micro_events")
    if isinstance(raw_events, list):
        for item in raw_events[:5]:
            if not isinstance(item, dict):
                continue
            slot = str(item.get("slot", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            event_type = str(item.get("micro_type", "none")).strip()
            if slot not in SLOTS or not content or event_type not in MICRO_TYPES:
                continue
            try:
                bias = float(item.get("mood_bias", 0.0))
            except (TypeError, ValueError):
                bias = 0.0
            bias = max(-max_micro_bias, min(max_micro_bias, bias))
            events.append(
                {
                    "slot": slot,
                    "content": content[:500],
                    "mood_bias": bias,
                    "micro_type": event_type,
                }
            )
    return schedule, events


def slots_elapsed(hour: int) -> list[str]:
    if hour < 6:
        return ["night"]
    if hour < 12:
        return ["morning"]
    if hour < 17:
        return ["morning", "afternoon"]
    if hour < 21:
        return ["morning", "afternoon", "evening"]
    return ["morning", "afternoon", "evening", "night"]
