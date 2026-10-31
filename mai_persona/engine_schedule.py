"""主动排程的时间解析、对话结束识别与硬性发送护栏。"""

from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

_END_PUNCTUATION = "。．.！!？?～~…·、,，;；:： \t\r\n　\"'“”‘’（）()[]【】<>《》"
_END_PARTICLES = "啦呀啊哦喔咯喽嘛吧了呢哈哇唷哟"
_CONVERSATION_END_PHRASES = (
    "晚安",
    "拜拜",
    "再见",
    "明天见",
    "回头聊",
    "下次聊",
    "改天聊",
    "先这样",
    "先不聊了",
    "先不聊",
    "不聊了",
    "我先忙了",
    "先忙了",
    "我忙去了",
    "去忙了",
    "我睡了",
    "先睡了",
    "去睡了",
    "睡了",
    "我下了",
    "下线了",
    "我撤了",
    "先撤了",
)
_WHOLE_CONVERSATION_ENDS = {"88", "886", "掰掰"}


def is_conversation_end(text: Any) -> bool:
    """判断一句话是否在明确结束对话；只在句尾匹配，避免“晚安是什么意思”类误判。"""

    value = re.sub(r"\s+", "", str(text or "")).strip(_END_PUNCTUATION)
    if not value:
        return False
    if value in _WHOLE_CONVERSATION_ENDS:
        return True
    stripped = value.rstrip(_END_PARTICLES).strip(_END_PUNCTUATION)
    for candidate in (value, stripped):
        if candidate and any(candidate.endswith(phrase) for phrase in _CONVERSATION_END_PHRASES):
            return True
    return False


def user_filter_allows(mode: str, user_id: str, whitelist: list[str], blacklist: list[str]) -> bool:
    """按 off/whitelist/blacklist 判断某个用户是否参与私聊结束排程。"""

    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode == "whitelist":
        return user_id in {str(item).strip() for item in whitelist}
    if normalized_mode == "blacklist":
        return user_id not in {str(item).strip() for item in blacklist}
    return True


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
