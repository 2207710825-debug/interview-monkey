import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from engine.realtime.api.app import create_app
from engine.realtime.contracts import AudioFrame, Channel, Chunk
from engine.realtime.llm.service import RealtimeAnswerService
from engine.realtime.mocks import MockLLMProvider, MockRetriever
from engine.realtime.observability import QALogWriter
from engine.realtime.orchestrator import RealtimePipeline
from scripts.replay import WaveFileAudioSource, _audio_paths, load_records


class TestQALogWriter(unittest.TestCase):
    def test_appends_structured_jsonl_and_keeps_audio_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "qa.jsonl"
            writer = QALogWriter(log_path)
            writer.append({"raw_text": "先验狂", "corrected_text": "先验框", "retrieval_top_k": []})
            row = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["schema_version"], 1)
            self.assertEqual(row["raw_text"], "先验狂")
            self.assertEqual(row["corrected_text"], "先验框")
            self.assertIsNone(writer.save_audio(b"\0\0", 16_000, "loopback", 0, 20))

    def test_audio_archive_is_valid_pcm16_wav_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = QALogWriter(Path(directory) / "qa.jsonl", record_audio=True)
            path = writer.save_audio(b"\x01\x00" * 320, 16_000, "loopback", 0, 20)
            self.assertIsNotNone(path)
            with wave.open(str(path), "rb") as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16_000))

    def test_replay_loader_reads_jsonl_and_evaluation_report_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jsonl = root / "qa.jsonl"
            jsonl.write_text('{"trace_id":"t1","audio_paths":[] }\n', encoding="utf-8")
            report = root / "report.json"
            report.write_text('{"summary":{},"rows":[{"id":"asr001"}]}', encoding="utf-8")
            self.assertEqual(load_records(jsonl)[0]["trace_id"], "t1")
            self.assertEqual(load_records(report)[0]["id"], "asr001")
            self.assertEqual(_audio_paths({}, root / "qa.jsonl"), [])

    def test_wave_source_requires_supported_sample_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mono.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16_000)
                wav.writeframes(b"\0\0" * 320)
            source = WaveFileAudioSource([path])

            async def collect():
                return [frame async for frame in source.frames()]

            import asyncio
            frames = asyncio.run(collect())
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0].sample_rate, 16_000)

    def test_text_answer_stream_is_also_logged(self):
        async def run_request(log_path: Path):
            service = RealtimeAnswerService(
                MockRetriever([Chunk("kb-1", "RAG 文档", "kb.md", 0.9)]),
                MockLLMProvider(["回答内容"]),
                Path(__file__).resolve().parents[1] / "prompts" / "answer_system.md",
            )
            with patch.dict(os.environ, {"MH_QA_LOG_PATH": str(log_path)}):
                app = create_app(service=service, runtime=object())
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post("/api/answer/stream", json={"question": "什么是 RAG？"})
            self.assertEqual(response.status_code, 200)

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "text-qa.jsonl"
            import asyncio
            asyncio.run(run_request(log_path))
            record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["source"], "text")
            self.assertEqual(record["raw_text"], "什么是 RAG？")
            self.assertEqual(record["corrected_text"], "什么是 RAG？")
            self.assertEqual(record["status"], "done")
            self.assertEqual(record["retrieval_top_k"][0]["chunk_id"], "kb-1")


class TestRealtimeAudioArchive(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_archives_only_vad_segment_frames_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = QALogWriter(Path(directory) / "qa.jsonl", record_audio=True)
            pipeline = RealtimePipeline(SimpleNamespace(on_boundary=None), None, None, None, qa_logger=writer)
            pipeline._audio_segments = []

            async def frames():
                yield AudioFrame(b"\x01\x00" * 320, 100, Channel.LOOPBACK)
                yield AudioFrame(b"", 120, Channel.LOOPBACK, utterance_end=True)

            received = [frame async for frame in pipeline._record_audio_frames(frames())]
            self.assertEqual(len(received), 2)
            self.assertEqual(len(pipeline._audio_segments), 1)
            self.assertNotIn("path", pipeline._audio_segments[0])
            refs = pipeline._audio_references(SimpleNamespace(
                channel=Channel.LOOPBACK, start_ms=100, end_ms=120,
            ))
            self.assertEqual(len(refs), 1)
            with wave.open(str(refs[0]["path"]), "rb") as wav:
                self.assertEqual(wav.getnframes(), 320)


if __name__ == "__main__":
    unittest.main()
