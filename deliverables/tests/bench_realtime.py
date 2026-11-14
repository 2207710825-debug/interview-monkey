# -*- coding: utf-8 -*-
"""离线 Mock 延迟基线；只测编排开销，不代表真实 ASR、网络或模型。"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from engine.realtime.contracts import Chunk
from engine.realtime.latency import LatencyHistogramRegistry, LatencyTracker
from engine.realtime.llm.service import RealtimeAnswerService
from engine.realtime.mocks import MockLLMProvider, MockRetriever


async def main(iterations: int = 30):
    registry = LatencyHistogramRegistry()
    chunks = [Chunk("rag-1", "RAG 通过检索增强生成。", "kb.md", 0.9)]
    service = RealtimeAnswerService(
        MockRetriever(chunks),
        MockLLMProvider(["**结论**：", "使用 **RAG**。"], delay_seconds=0.005),
        os.path.join(ROOT, "prompts", "answer_system.md"),
    )
    for index in range(iterations):
        tracker = LatencyTracker(trace_id=f"bench-{index}", registry=registry)
        tracker.mark("speech_end")
        tracker.mark("asr_final")
        async for _ in service.stream_answer("什么是RAG", session_id=f"bench-{index}", tracker=tracker):
            pass
    print(json.dumps({
        "kind": "mock_orchestration_only",
        "iterations": iterations,
        "histograms": registry.snapshot(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
