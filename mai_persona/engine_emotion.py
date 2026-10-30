"""PAD 情绪状态机和衰减计算。"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp
from typing import Any


_EXPLICIT_EMOTION_MARKERS = (
    "开心",
    "高兴",
    "兴奋",
    "满足",
    "难过",
    "伤心",
    "委屈",
    "失望",
    "孤独",
    "害怕",
    "焦虑",
    "紧张",
    "生气",
    "愤怒",
    "烦躁",
    "崩溃",
    "绝望",
    "压力很大",
    "累得",
    "受不了了",
    "想哭",
    "想死",
    "自杀",
)


@dataclass(frozen=True)
class PAD:
    p: float
    a: float
    d: float

    def bounded(self) -> "PAD":
        return PAD(*(max(-1.0, min(1.0, value)) for value in (self.p, self.a, self.d)))

    def as_dict(self) -> dict[str, float]:
        return {"p": self.p, "a": self.a, "d": self.d}


def decay(current: PAD, baseline: PAD, elapsed_seconds: float, tau_seconds: float) -> PAD:
    """按指数衰减向目标层回归。"""

    if elapsed_seconds <= 0:
        return current.bounded()
    factor = exp(-elapsed_seconds / max(tau_seconds, 0.001))
    return PAD(
        baseline.p + (current.p - baseline.p) * factor,
        baseline.a + (current.a - baseline.a) * factor,
        baseline.d + (current.d - baseline.d) * factor,
    ).bounded()


def regulated_expression(mood: PAD, emotion: PAD, regulation: float) -> PAD:
    """按表里调节比例收敛即时情绪表达。"""

    visibility = 1.0 - max(0.0, min(1.0, regulation))
    return PAD(
        mood.p + (emotion.p - mood.p) * visibility,
        mood.a + (emotion.a - mood.a) * visibility,
        mood.d + (emotion.d - mood.d) * visibility,
    ).bounded()


def emotion_label(pad: PAD) -> str:
    """将 PAD 粗略映射为便于观测的简体中文标签。"""

    if pad.p >= 0.35 and pad.a >= 0.35:
        return "兴奋愉快"
    if pad.p >= 0.25 and pad.a < 0.2:
        return "平和满足"
    if pad.p <= -0.35 and pad.a >= 0.35:
        return "焦躁不悦"
    if pad.p <= -0.25 and pad.a < 0.2:
        return "低落安静"
    if pad.d >= 0.4:
        return "主动坚定"
    if pad.d <= -0.4:
        return "退让敏感"
    if pad.p > 0.15:
        return "轻松"
    if pad.p < -0.15:
        return "谨慎"
    return "平常"


def style_hint(pad: PAD) -> str:
    """将表达 PAD 转成简短的描述性提示。"""

    hints: list[str] = []
    hints.append("表达明亮、愿意分享" if pad.p >= 0.2 else "表达收敛、少作情绪渲染" if pad.p <= -0.2 else "保持自然")
    hints.append("节奏轻快、句子短一些" if pad.a >= 0.25 else "节奏舒缓" if pad.a <= -0.25 else "语速平稳")
    hints.append("可以主动带起话题" if pad.d >= 0.25 else "多倾听、少推动" if pad.d <= -0.25 else "交流保持平等")
    return "；".join(hints)


def normalize_delta(raw: Any, limit: float) -> PAD:
    """验证并限制一次评价的 PAD 变化。"""

    if not isinstance(raw, dict):
        return PAD(0.0, 0.0, 0.0)
    bounded_limit = max(0.0, min(1.0, limit))

    def value(key: str) -> float:
        try:
            number = float(raw.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0
        return max(-bounded_limit, min(bounded_limit, number))

    return PAD(value("p"), value("a"), value("d"))


def has_explicit_emotion(text: str) -> bool:
    """识别用户明确表达情绪的消息，以触发及时的共情评价。"""

    normalized = str(text or "").strip()
    return bool(normalized) and any(marker in normalized for marker in _EXPLICIT_EMOTION_MARKERS)
