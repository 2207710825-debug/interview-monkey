# -*- coding: utf-8 -*-
import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from engine.realtime.contracts import AudioFrame, Channel, Chunk, Transcript, TranscriptKind
from engine.realtime.events import AsyncEventBus
from engine.realtime.llm.service import RealtimeAnswerService
from engine.realtime.mocks import MockASRProvider, MockAudioSource, MockLLMProvider, MockRetriever
from engine.realtime.observability import QALogWriter
from engine.realtime.orchestrator import RealtimePipeline
from engine.realtime.qa.detector import RuleQuestionDetector
from engine.realtime.vad.adaptive import AdaptiveVadSegmenter


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


class TestRealtimeEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_adjacent_incomplete_finals_merge_before_answer(self):
        fixture = os.path.join(ROOT, "tests", "fixtures", "question_pcm16.b64")
        pcm = base64.b64decode(Path(fixture).read_text(encoding="ascii"))
        source = MockAudioSource([AudioFrame(pcm, 0, Channel.LOOPBACK)])
        asr = MockASRProvider([
            Transcript(TranscriptKind.PARTIAL, "请解释RA", Channel.LOOPBACK, 0, 200, "interviewer"),
            Transcript(TranscriptKind.FINAL, "请解释RAG的", Channel.LOOPBACK, 0, 350, "interviewer", 0.98),
            Transcript(TranscriptKind.FINAL, "作用是什么？", Channel.LOOPBACK, 700, 1100, "interviewer", 0.98),
        ])
        chunks = [Chunk("rag-1", "RAG 先检索知识，再增强生成。", "kb.md", 0.95, "RAG")]
        service = RealtimeAnswerService(
            MockRetriever(chunks),
            MockLLMProvider(["**结论**：", "**RAG** 是检索增强生成。", "\n**关键词**：**RAG** / **向量检索**"]),
            os.path.join(ROOT, "prompts", "answer_system.md"),
        )
        bus = AsyncEventBus()
        with tempfile.TemporaryDirectory() as directory:
            qa_log = os.path.join(directory, "qa.jsonl")
            pipeline = RealtimePipeline(
                AdaptiveVadSegmenter(energy_threshold=500, use_webrtc=False),
                asr,
                RuleQuestionDetector(),
                service,
                bus,
                qa_logger=QALogWriter(qa_log),
            )
            stream = bus.subscribe("answer")
            received = []

            async def collect():
                async for event in stream:
                    received.append(event)
                    if getattr(event, "event", "") == "done":
                        return

            started = time.perf_counter()
            collector = asyncio.create_task(collect())
            await pipeline.run(source, "offline-e2e")
            await asyncio.wait_for(collector, timeout=2.0)
            with open(qa_log, encoding="utf-8") as handle:
                record = json.loads(handle.readline())
            self.assertEqual(record["raw_text"], "请解释RAG的；作用是什么？")
            self.assertEqual(record["corrected_text"], "请解释RAG的，作用是什么？")
            self.assertEqual(record["status"], "done")
            self.assertEqual(record["retrieval_top_k"][0]["chunk_id"], "rag-1")
            self.assertEqual(record["audio_paths"], [])
            elapsed_ms = (time.perf_counter() - started) * 1000
            answer = "".join(event.data["delta"] for event in received if event.event == "token")
            self.assertLess(elapsed_ms, 2000)
            self.assertIn("**RAG**", answer)
            self.assertIn("请解释RAG的，作用是什么？", service.llm_provider.messages[-1]["content"])
            self.assertEqual(received[0].event, "meta")
            self.assertEqual(received[-1].event, "done")
            self.assertIsNotNone(received[-1].data["metrics"]["server_e2e_ms"])


if __name__ == "__main__":
    unittest.main()
