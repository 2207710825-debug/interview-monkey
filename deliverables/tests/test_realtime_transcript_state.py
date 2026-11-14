# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from engine.realtime.asr.providers import JsonWebSocketASRProvider, LocalSherpaASRProvider
from engine.realtime.contracts import AudioFrame, Channel, TranscriptKind
from engine.realtime.mocks import MockAudioSource


class FakeStreamingSession:
    """模拟底层兼容接口：feed 返回历史 final 与当前 partial 的累计文本。"""

    def __init__(self, *_args, **_kwargs):
        self.text = ""
        self._pending = None
        self.calls = 0

    def feed(self, _pcm, _sample_rate):
        self.calls += 1
        if self.calls == 1:
            return "第一"
        if self.calls == 2:
            return "第一句"
        if self.calls == 3:
            self.text = "第一句"
            self._pending = "第一句"
            return "第一句"
        return "第一句新"

    def finish(self):
        self.text = "第一句新问题"
        self._pending = "新问题"
        return self.text


class FakeSecondPassSession:
    def __init__(self, *_args, **_kwargs):
        self.text = ""
        self._pending = None

    def feed(self, _pcm, _sample_rate):
        return "先验狂机制是什么啊"

    def finish(self):
        self.text = "先验狂机制是什么啊"
        self._pending = self.text
        return self.text


class FakeSenseVoice:
    def transcribe(self, _pcm, _sample_rate):
        return "先验框机制是什么啊"


class BrokenSenseVoice:
    def transcribe(self, _pcm, _sample_rate):
        raise RuntimeError("decode failed")


class TestRealtimeTranscriptState(unittest.IsolatedAsyncioTestCase):
    async def test_partial_partial_final_then_new_partial_uses_two_ids(self):
        frames = [
            AudioFrame(b"\0\0" * 320, index * 20, Channel.LOOPBACK)
            for index in range(4)
        ]
        provider = LocalSherpaASRProvider()
        with (
            patch("engine.asr._SherpaOnnx", return_value=object()),
            patch("engine.asr.StreamingSession", FakeStreamingSession),
        ):
            events = [
                event async for event in provider.transcribe(MockAudioSource(frames).frames())
            ]

        self.assertEqual(
            [(event.kind, event.text) for event in events],
            [
                (TranscriptKind.PARTIAL, "第一"),
                (TranscriptKind.PARTIAL, "第一句"),
                (TranscriptKind.FINAL, "第一句"),
                (TranscriptKind.PARTIAL, "新"),
                (TranscriptKind.FINAL, "新问题"),
            ],
        )
        first_id = events[0].utterance_id
        second_id = events[3].utterance_id
        self.assertTrue(first_id)
        self.assertTrue(second_id)
        self.assertEqual({event.utterance_id for event in events[:3]}, {first_id})
        self.assertEqual({event.utterance_id for event in events[3:]}, {second_id})
        self.assertNotEqual(first_id, second_id)

    async def test_two_pass_emits_only_sense_voice_final(self):
        frames = [
            AudioFrame(b"\1\0" * 320, 0, Channel.LOOPBACK),
            AudioFrame(b"", 500, Channel.LOOPBACK, utterance_end=True),
        ]
        provider = LocalSherpaASRProvider(two_pass_enabled=True)
        with (
            patch("engine.asr._SherpaOnnx", return_value=object()),
            patch("engine.asr.StreamingSession", FakeSecondPassSession),
            patch("engine.asr2.SenseVoiceRecognizer", return_value=FakeSenseVoice()),
        ):
            events = [
                event async for event in provider.transcribe(MockAudioSource(frames).frames())
            ]

        finals = [event for event in events if event.kind is TranscriptKind.FINAL]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "先验框机制是什么啊")
        self.assertEqual(finals[0].raw_text, "先验框机制是什么啊")
        self.assertEqual(finals[0].pass1_text, "先验狂机制是什么啊")
        self.assertTrue(finals[0].corrected)
        self.assertEqual(finals[0].asr_engine, "sense_voice")
        self.assertIsNotNone(finals[0].pass2_ms)

    async def test_two_pass_failure_falls_back_to_streaming_final(self):
        frames = [
            AudioFrame(b"\1\0" * 320, 0, Channel.LOOPBACK),
            AudioFrame(b"", 500, Channel.LOOPBACK, utterance_end=True),
        ]
        provider = LocalSherpaASRProvider(two_pass_enabled=True)
        with (
            patch("engine.asr._SherpaOnnx", return_value=object()),
            patch("engine.asr.StreamingSession", FakeSecondPassSession),
            patch("engine.asr2.SenseVoiceRecognizer", return_value=BrokenSenseVoice()),
        ):
            events = [
                event async for event in provider.transcribe(MockAudioSource(frames).frames())
            ]

        final = [event for event in events if event.kind is TranscriptKind.FINAL][0]
        self.assertEqual(final.text, "先验狂机制是什么啊")
        self.assertEqual(final.raw_text, "先验狂机制是什么啊")
        self.assertEqual(final.pass1_text, "先验狂机制是什么啊")
        self.assertEqual(final.asr_engine, "sherpa_transducer")
        self.assertFalse(final.corrected)

    def test_websocket_parser_preserves_server_utterance_id(self):
        transcript = JsonWebSocketASRProvider._parse(
            '{"type":"partial","text":"测试","utterance_id":"utt-server"}'
        )
        self.assertIsNotNone(transcript)
        self.assertEqual(transcript.utterance_id, "utt-server")

    def test_websocket_builds_explicit_utterance_end_message(self):
        frame = AudioFrame(b"", 800, Channel.LOOPBACK, utterance_end=True)
        self.assertIn('"type":"utterance_end"', JsonWebSocketASRProvider._build_audio(frame))

    def test_frontend_keys_partial_nodes_by_utterance_id(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "realtime.html").read_text(encoding="utf-8")
        self.assertIn("const partialEls=new Map()", html)
        self.assertIn("partialEls.get(key)", html)
        self.assertIn("partialEls.delete(key)", html)
        self.assertIn("已校正", html)
        self.assertIn("原始：${t.raw_text}", html)
        self.assertIn("改动：${t.corrections.map", html)
        self.assertIn("修改转写", html)
        self.assertIn("/api/glossary/corrections", html)
        self.assertIn("/api/glossary/aliases", html)


if __name__ == "__main__":
    unittest.main()
