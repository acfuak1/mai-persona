"""日记结果校验。"""

from __future__ import annotations


def validate_diary(text: str) -> str:
    normalized = " ".join(str(text or "").split()).strip()
    if len(normalized) < 80:
        return ""
    return normalized[:200]
