"""ASR partial 稳定 300ms 后预取；final 变化不大时复用。"""
from __future__ import annotations

import asyncio
from difflib import SequenceMatcher


class SpeculativeRetriever:
    def __init__(self, retriever, stable_ms: int = 300, reuse_ratio: float = 0.85):
        self.retriever = retriever
        self.stable_ms = stable_ms
        self.reuse_ratio = reuse_ratio
        self._query = ""
        self._task: asyncio.Task | None = None

    def prefetch(self, partial_query: str, unchanged_ms: int, *, top_k: int = 4, min_score: float = 0.35) -> None:
        query = partial_query.strip()
        if unchanged_ms < self.stable_ms or not query or query == self._query:
            return
        if self._task and not self._task.done():
            self._task.cancel()
        self._query = query
        self._task = asyncio.create_task(self.retriever.retrieve(query, top_k=top_k, min_score=min_score))

    async def resolve(self, final_query: str, *, top_k: int = 4, min_score: float = 0.35):
        ratio = SequenceMatcher(None, self._query, final_query.strip()).ratio() if self._query else 0.0
        if self._task and ratio >= self.reuse_ratio:
            try:
                return await self._task, True
            finally:
                self._task = None
        if self._task and not self._task.done():
            self._task.cancel()
        return await self.retriever.retrieve(final_query, top_k=top_k, min_score=min_score), False
