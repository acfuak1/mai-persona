"""MaiBot 知识库的只读检索适配器。"""

from __future__ import annotations

from typing import Any


class MemoryAdapter:
    """通过 SDK knowledge.search 查询宿主已有记忆，不依赖插件 ID 或 API 名称。"""

    def __init__(self, ctx: Any, config: Any) -> None:
        self.ctx = ctx
        self.config = config

    async def search_facts(
        self,
        query: str,
        *,
        user_id: str = "",
        chat_id: str = "",
        limit: int = 8,
        time_start: float | None = None,
        time_end: float | None = None,
    ) -> list[str]:
        try:
            result = await self.ctx.call_capability(
                "knowledge.search",
                query=query,
                limit=max(1, min(limit, 20)),
                mode="hybrid",
                chat_id=chat_id,
                person_id=user_id,
                user_id=user_id,
                time_start=time_start,
                time_end=time_end,
                respect_filter=True,
            )
        except Exception:
            return []
        return _flatten_result(result)

    async def person_profile(self, user_id: str, chat_id: str = "") -> list[str]:
        del chat_id
        return await self.search_facts(
            f"{user_id} 的人物画像、近况与已知状态",
            user_id=user_id,
            limit=10,
        )


_KNOWLEDGE_PREFIX = "你知道这些知识:"
_NO_RESULT_MARKERS = ("你不太了解有关",)


def _content_lines(content: str) -> list[str]:
    """把宿主的检索文本拆成条目，并丢弃“没有检索到”的占位句。"""

    lines: list[str] = []
    for raw in content.splitlines():
        line = raw.strip(" \t-")
        if not line:
            continue
        if any(marker in line for marker in _NO_RESULT_MARKERS):
            continue
        if line.startswith(_KNOWLEDGE_PREFIX):
            line = line[len(_KNOWLEDGE_PREFIX) :].strip()
            if not line:
                continue
        lines.append(line)
    return lines


def _flatten_result(result: Any) -> list[str]:
    if isinstance(result, dict):
        if result.get("success") is False:
            return []
        facts = result.get("facts")
        if isinstance(facts, list):
            return [str(item).strip() for item in facts if str(item).strip()]
        content = result.get("content")
        if isinstance(content, str):
            return _content_lines(content)
        return []
    if isinstance(result, list):
        return [str(item).strip() for item in result if str(item).strip()]
    if isinstance(result, str):
        return _content_lines(result)
    return []
