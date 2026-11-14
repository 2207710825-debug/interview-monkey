# -*- coding: utf-8 -*-
import asyncio
import json
import os
import tempfile
from types import SimpleNamespace
import unittest

from engine.realtime.api.runtime import CaptureRuntime
from engine.realtime.config import AsrConfig, PrivacyConfig, RealtimeConfig, load_config
from engine.realtime.contracts import Channel
from engine.realtime.events import AsyncEventBus, DropOldestQueue
from engine.realtime.latency import LatencyTracker


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


class FakeClock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TestRealtimeCore(unittest.IsolatedAsyncioTestCase):
    def test_config_and_privacy_gate(self):
        cfg = load_config(os.path.join(ROOT, "config", "config.yaml"), environ={})
        self.assertEqual(cfg.audio.frame_ms, 20)
        self.assertTrue(cfg.privacy.local_mode)
        self.assertTrue(cfg.asr.two_pass.enabled)
        self.assertFalse(cfg.observability.record_audio_for_replay)
        self.assertEqual(cfg.asr.two_pass.engine, "sense_voice")
        self.assertEqual(cfg.asr.engine, "sherpa_transducer")
        self.assertLessEqual(cfg.llm.temperature, 0.3)
        self.assertEqual(cfg.postprocess.fuzzy_threshold, 0.86)
        self.assertEqual(cfg.postprocess.llm_correction.timeout_ms, 300)

        # 默认配置是本地路径，不应误报云传输。
        cfg.assert_cloud_allowed()
        cloud = RealtimeConfig(
            asr=AsrConfig(provider="ws_vendor", websocket_url="wss://example.invalid"),
            privacy=PrivacyConfig(local_mode=True, third_party_consent=False),
        )
        with self.assertRaises(PermissionError):
            cloud.assert_cloud_allowed()

    def test_vad_window_order_and_preroll_minimum_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            for content in (
                "vad:\n  fast_silence_ms: 400\n  base_silence_ms: 350\n",
                "vad:\n  pre_roll_ms: 199\n",
            ):
                with self.subTest(content=content):
                    with open(config_path, "w", encoding="utf-8") as handle:
                        handle.write(content)
                    with self.assertRaises(ValueError):
                        load_config(config_path, environ={})

    async def test_drop_oldest_queue_is_bounded(self):
        queue = DropOldestQueue(maxsize=2)
        queue.put_latest_nowait(1)
        queue.put_latest_nowait(2)
        self.assertTrue(queue.put_latest_nowait(3))
        self.assertEqual(queue.stats.dropped, 1)
        self.assertEqual([queue.get_nowait(), queue.get_nowait()], [2, 3])

    async def test_event_bus_delivers_and_closes(self):
        bus = AsyncEventBus(queue_size=2)
        stream = bus.subscribe("token")
        await bus.publish("token", "a")
        self.assertEqual(await anext(stream), "a")
        await bus.close()
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

    def test_latency_report_is_json_serializable(self):
        clock = FakeClock()
        tracker = LatencyTracker(trace_id="trace-1", clock=clock)
        tracker.mark("speech_end")
        tracker.mark("asr_final")
        clock.advance(0.12)
        tracker.mark("rag_start")
        clock.advance(0.04)
        tracker.mark("rag_end")
        clock.advance(0.2)
        tracker.mark("llm_first_token")
        clock.advance(0.01)
        tracker.mark("render_first")
        report = tracker.finish(answer_chars=12)
        self.assertEqual(report["trace_id"], "trace-1")
        self.assertEqual(report["metrics"]["rag_ms"], 40.0)
        self.assertEqual(report["metrics"]["ttft_ms"], 360.0)
        json.dumps(report, ensure_ascii=False)

    def test_runtime_reuses_warmed_provider_per_audio_channel(self):
        cfg = load_config(os.path.join(ROOT, "config", "config.yaml"), environ={})
        runtime = CaptureRuntime(cfg, answer_service=object(), project_root=ROOT)

        loopback_first = runtime._pipeline(SimpleNamespace(channel=Channel.LOOPBACK)).asr
        loopback_second = runtime._pipeline(SimpleNamespace(channel=Channel.LOOPBACK)).asr
        microphone = runtime._pipeline(SimpleNamespace(channel=Channel.MIC)).asr

        self.assertIs(loopback_first, loopback_second)
        self.assertIsNot(loopback_first, microphone)
        self.assertEqual(runtime.status()["asr"], "idle")


if __name__ == "__main__":
    unittest.main()
