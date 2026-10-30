"""主动排程的时间解析与硬性发送护栏。"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo


def parse_fire_at(value: Any, timezone: ZoneInfo) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.timestamp()


def hard_guard(
    store: Any,
    *,
    now: datetime,
    timezone: ZoneInfo,
    min_interval_hours: float,
    daily_limit: int,
    silent_hours: list[int],
    target_user: str,
) -> tuple[bool, str]:
    start = datetime.combine(now.date(), time.min, tzinfo=timezone).timestamp()
    if store.outbound_count_today(start) >= daily_limit:
        return False, "已达到每日主动消息上限"
    last_sent = store.last_outbound_at()
    if last_sent and now.timestamp() - last_sent < min_interval_hours * 3600:
        return False, "尚未达到主动消息最小间隔"
    if len(silent_hours) >= 2:
        start_hour, end_hour = int(silent_hours[0]) % 24, int(silent_hours[1]) % 24
        hour = now.hour
        silent = start_hour <= hour < end_hour if start_hour < end_hour else hour >= start_hour or hour < end_hour
        if silent:
            return False, "当前处于静默时段"
    if target_user and not store.last_user_interaction_at(target_user):
        return False, "目标用户没有已记录的交互"
    return True, ""
