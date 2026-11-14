"""无网络集成测试使用的 Mock 实现。"""
from __future__ import annotations

import asyncio
from typing import AsyncIterable, AsyncIterator, Mapping, Sequence

from .contracts import AudioFrame, Chunk, QuestionDecision, Transcript


class MockAudioSource:
    def __init__(self, frames: Sequence[AudioFrame], delay_seconds: float = 0.0):
        self._frames = tuple(frames)
        self.delay_seconds = delay_seconds

    async def frames(self) -> AsyncIterator[AudioFrame]:
        for frame in self._frames:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            yield frame


class MockASRProvider:
    def __init__(self, transcripts: Sequence[Transcript]):
        self._transcripts = tuple(transcripts)

    async def transcribe(self, frames: AsyncIterable[AudioFrame]) -> AsyncIterator[Transcript]:
        async for _ in frames:
            break
        for transcript in self._transcripts:
            await asyncio.sleep(0)
            yield transcript


class MockQuestionDetector:
    def __init__(self, decision: QuestionDecision):
        self.decision = decision

    async def evaluate(self, transcript: Transcript, context: Sequence[Transcript]) -> QuestionDecision:
        return self.decision


class MockRetriever:
    def __init__(self, chunks: Sequence[Chunk]):
        self.chunks = list(chunks)
        self.queries: list[str] = []

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 4,
        min_score: float = 0.35,
        exclude_categories: Sequence[str] = (),
    ) -> list[Chunk]:
        self.queries.append(query)
        excluded = {category.casefold() for category in exclude_categories}
        return [
            chunk for chunk in self.chunks
            if chunk.score >= min_score
            and str(chunk.metadata.get("category", "")).casefold() not in excluded
        ][:top_k]


class MockLLMProvider:
    def __init__(self, pieces: Sequence[str], delay_seconds: float = 0.0):
        self.pieces = tuple(pieces)
        self.delay_seconds = delay_seconds
        self.messages: Sequence[Mapping[str, str]] | None = None

    async def stream(self, messages: Sequence[Mapping[str, str]], cancel=None) -> AsyncIterator[str]:
        self.messages = messages
        for piece in self.pieces:
            if cancel is not None and cancel.is_set():
                return
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            yield piece
