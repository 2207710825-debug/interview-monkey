# -*- coding: utf-8 -*-
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from engine.realtime.api.app import create_app
from engine.realtime.contracts import Channel, Transcript, TranscriptKind
from engine.realtime.history import RealtimeHistoryStore, valid_session_id
from engine.realtime.orchestrator import RealtimePipeline


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


class TestRealtimeHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.qa_path = root / "qa.jsonl"
        self.transcript_path = root / "transcripts.jsonl"
        _write_jsonl(self.qa_path, [
            {
                "recorded_at": "2026-11-22T10:00:00.000+00:00",
                "session_id": "session-a",
                "source": "speech",
                "question": "YOLOv5 的预处理过程是什么？",
                "raw_text": "YOLOv5 的预处理过程是什么",
                "corrected_text": "YOLOv5 的预处理过程是什么？",
                "answer": "先 letterbox 缩放，再归一化。",
                "status": "done",
                "retrieval_top_k": [],
                "latency_metrics": {"ttft_ms": 718},
                "utterance_id": "u-1",
            },
            {
                "recorded_at": "2026-11-22T10:01:00.000+00:00",
                "session_id": "session-a",
                "source": "text",
                "question": "那对小目标有什么影响？",
                "answer": "小目标会受 padding 影响。",
                "status": "done",
                "retrieval_top_k": [],
            },
            {
                "recorded_at": "2026-11-22T11:00:00.000+00:00",
                "session_id": "session-b",
                "source": "text",
                "question": "Redis 持久化有哪几种？",
                "answer": "RDB 和 AOF。",
                "status": "done",
            },
        ])
        _write_jsonl(self.transcript_path, [
            {
                "recorded_at": "2026-11-22T09:59:50.000+00:00",
                "session_id": "session-a",
                "utterance_id": "u-1",
                "channel": "loopback",
                "speaker_id": None,
                "text": "YOLOv5 的预处理过程是什么？",
                "start_ms": 1000,
                "end_ms": 2400,
            },
            {
                "recorded_at": "2026-11-22T09:59:50.100+00:00",
                "session_id": "session-a",
                "utterance_id": "u-1",
                "channel": "loopback",
                "speaker_id": None,
                "text": "YOLOv5 的预处理过程是什么？",
                "start_ms": 1000,
                "end_ms": 2400,
            },
        ])
        self.store = RealtimeHistoryStore(self.qa_path, self.transcript_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_sessions_groups_and_sorts(self):
        items = self.store.list_sessions()
        self.assertEqual([item["session_id"] for item in items], ["session-b", "session-a"])
        session_a = next(item for item in items if item["session_id"] == "session-a")
        self.assertEqual(session_a["qa_count"], 2)
        self.assertEqual(session_a["transcript_count"], 1)
        self.assertIn("YOLOv5", session_a["title"])
        self.assertEqual(session_a["started_at"], "2026-11-22T09:59:50.000+00:00")

    def test_get_session_returns_qa_and_deduped_transcript(self):
        detail = self.store.get_session("session-a")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["qa_count"], 2)
        self.assertEqual(detail["transcript_count"], 1)
        self.assertEqual(detail["qa"][0]["question"], "YOLOv5 的预处理过程是什么？")
        self.assertEqual(detail["transcript"][0]["utterance_id"], "u-1")

    def test_delete_session_only_removes_that_session(self):
        removed = self.store.delete_session("session-a")
        self.assertEqual(removed, 4)
        self.assertIsNone(self.store.get_session("session-a"))
        self.assertIsNotNone(self.store.get_session("session-b"))

    def test_invalid_session_id(self):
        self.assertTrue(valid_session_id("session-a"))
        self.assertFalse(valid_session_id("../secret"))
        with self.assertRaises(ValueError):
            self.store.get_session("../secret")

    def test_pipeline_transcript_logger_writes_record(self):
        pipeline = RealtimePipeline(
            SimpleNamespace(on_boundary=None), None, None, None,
            transcript_logger=self.store,
        )
        transcript = Transcript(
            TranscriptKind.FINAL,
            "这条是上下文转写。",
            Channel.LOOPBACK,
            3000,
            4200,
            utterance_id="u-2",
            raw_text="这条是上下文转写",
            asr_engine="sense_voice",
        )
        pipeline._log_transcript("session-a", transcript)
        detail = self.store.get_session("session-a")
        self.assertEqual(detail["transcript_count"], 2)
        logged = detail["transcript"][-1]
        self.assertEqual(logged["text"], "这条是上下文转写。")
        self.assertEqual(logged["utterance_id"], "u-2")


class TestRealtimeHistoryApi(unittest.IsolatedAsyncioTestCase):
    async def test_sessions_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_path = root / "qa.jsonl"
            transcript_path = root / "transcripts.jsonl"
            _write_jsonl(qa_path, [{
                "recorded_at": "2026-11-22T10:00:00.000+00:00",
                "session_id": "session-a",
                "source": "text",
                "question": "Redis 持久化有哪几种？",
                "answer": "RDB 和 AOF。",
                "status": "done",
            }])
            _write_jsonl(transcript_path, [{
                "recorded_at": "2026-11-22T09:59:50.000+00:00",
                "session_id": "session-a",
                "utterance_id": "u-1",
                "channel": "loopback",
                "text": "Redis 持久化有哪几种？",
                "start_ms": 1000,
                "end_ms": 2000,
            }])
            store = RealtimeHistoryStore(qa_path, transcript_path)
            app = create_app(mock=True, history_store=store)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                listing = await client.get("/api/sessions")
                self.assertEqual(listing.status_code, 200)
                self.assertEqual(listing.json()["items"][0]["session_id"], "session-a")

                detail = await client.get("/api/sessions/session-a")
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.json()["qa_count"], 1)
                self.assertEqual(detail.json()["transcript_count"], 1)

                invalid = await client.get("/api/sessions/bad%20id")
                self.assertEqual(invalid.status_code, 400)

                missing = await client.get("/api/sessions/missing")
                self.assertEqual(missing.status_code, 404)

                deleted = await client.delete("/api/sessions/session-a")
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(deleted.json()["removed"], 2)


if __name__ == "__main__":
    unittest.main()
