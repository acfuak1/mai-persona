"""拟人细节：披露深度、亲密度缩放、风格趋同、边界、遗怨与情绪残留。"""

from __future__ import annotations

import re
from math import exp
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

_BOUNDARY_MARKERS = (
    "不想说",
    "别问",
    "不要问",
    "换个话题",
    "不想聊这个",
    "不想谈",
    "别提这个",
    "不方便说",
    "这个不聊",
    "私人问题",
)

_PEAK_MARKERS = ("太开心", "超级开心", "兴奋", "太棒了", "考过了", "通过了", "拿到了", "成功了")

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


def closeness(stage: Any, stages: list[str], affection: dict[str, Any] | None) -> float:
    """把关系阶段与三维好感合成为一个 0~1 的亲密度，用于缩放所有拟人效果。"""

    normalized = [str(item).strip() for item in stages if str(item).strip()]
    stage_part = 0.0
    if normalized:
        try:
            stage_part = normalized.index(str(stage).strip()) / max(1, len(normalized) - 1)
        except ValueError:
            stage_part = 0.0
    affection = affection or {}
    values = []
    for key in ("trust", "intimacy", "rapport"):
        try:
            values.append(float(affection.get(key, 0.0) or 0.0))
        except (TypeError, ValueError):
            values.append(0.0)
    affection_part = (sum(values) / max(1, len(values)) + 1.0) / 2.0
    return max(0.0, min(1.0, stage_part * 0.6 + affection_part * 0.4))


def scale_by_closeness(base: float, value: float, floor: float = 0.1) -> float:
    """不熟时只保留很小一部分效果，避免用熟人量度对待陌生人。"""

    return float(base) * (float(floor) + (1.0 - float(floor)) * max(0.0, min(1.0, value)))


def update_style_stats(
    median_len: float,
    samples: int,
    length: int,
    *,
    alpha: float = 0.2,
) -> tuple[float, int]:
    """用指数滑动平均维护对方的消息长度，样本很少时不做判断。"""

    length = max(0, int(length))
    if samples <= 0:
        return float(length), 1
    updated = float(median_len) * (1.0 - alpha) + float(length) * alpha
    return updated, int(samples) + 1


def style_hint(median_len: float, samples: int, min_samples: int, value: float) -> str:
    """样本够多、且彼此不是完全陌生时，才按对方长度收短回复。"""

    if samples < max(1, int(min_samples)):
        return ""
    if 0.15 + 0.85 * max(0.0, min(1.0, value)) <= 0.3:
        return ""
    if median_len <= 12:
        return "对方消息很短，回复尽量一句话，不展开。"
    if median_len <= 25:
        return "对方消息偏短，回复控制在一两句。"
    return ""


def detect_boundary(text: Any) -> bool:
    """识别用户明确划出的谈话边界。"""

    value = str(text or "")
    if not value.strip():
        return False
    return any(marker in value for marker in _BOUNDARY_MARKERS)


def detect_peak(text: Any) -> bool:
    """识别明确的高唤醒正向事件，用于记录共同记忆的峰值。"""

    value = str(text or "")
    if len(value.strip()) < 6:
        return False
    return any(marker in value for marker in _PEAK_MARKERS)


def grudge_decay(value: float, elapsed_seconds: float, tau_days: float) -> float:
    """遗怨按天缓慢衰减，但不会归零得太快。"""

    tau = max(0.1, float(tau_days)) * 86400
    return max(0.0, float(value)) * exp(-max(0.0, float(elapsed_seconds)) / tau)


def recover_energy(value: float, elapsed_seconds: float, tau_hours: float) -> float:
    """表达能量按指数向满值恢复。"""

    tau = max(1.0, float(tau_hours)) * 3600
    current = max(0.0, min(1.0, float(value)))
    return min(1.0, current + (1.0 - current) * (1.0 - exp(-max(0.0, float(elapsed_seconds)) / tau)))


def drain_energy(value: float, amount: float = 0.02) -> float:
    return max(0.05, min(1.0, float(value) - max(0.0, float(amount))))
