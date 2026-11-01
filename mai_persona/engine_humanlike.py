"""拟人细节：披露深度、摩擦识别与情绪残留。"""

from __future__ import annotations

import re
from typing import Any

_FRICTION_MARKERS = (
    "你根本",
    "你总是",
    "你从来",
    "烦死了",
    "别理我",
    "不想理你",
    "敷衍",
    "闭嘴",
    "有毛病",
    "失望透顶",
    "不想说",
)

_DISCLOSURE_HINTS = {
    0: "披露深度：表层。只聊日常与公共话题，不主动提自己的经历或感受。",
    1: "披露深度：个人经历。可以分享自己的日常见闻和过去的小事，但不谈私密感受。",
    2: "披露深度：私密感受。可以在合适时机说出真实的在意与脆弱，仍要克制，不一次说太多。",
}


def disclosure_depth(stage: str, stages: list[str], enabled: bool = True) -> int:
    """按关系阶段给出可披露深度：0 表层 / 1 个人经历 / 2 私密感受。"""

    if not enabled:
        return 2
    normalized = [str(item).strip() for item in stages if str(item).strip()]
    try:
        index = normalized.index(str(stage).strip())
    except ValueError:
        return 0
    return min(2, index)


def disclosure_hint(depth: int) -> str:
    return _DISCLOSURE_HINTS.get(max(0, min(2, int(depth))), _DISCLOSURE_HINTS[0])


def detect_friction(text: Any) -> bool:
    """识别带有指责、冷淡或对抗意味的发言；只用于提示修复，不做情绪判定。"""

    value = re.sub(r"\s+", "", str(text or ""))
    if not value:
        return False
    return any(marker in value for marker in _FRICTION_MARKERS)


def carry_over_scale(now: float, until: float, factor: float = 2.5) -> float:
    """情绪残留期间放大回归时间常数，让情绪多停留一会儿。"""

    try:
        active = float(until or 0.0) > float(now)
    except (TypeError, ValueError):
        return 1.0
    return float(factor) if active else 1.0
