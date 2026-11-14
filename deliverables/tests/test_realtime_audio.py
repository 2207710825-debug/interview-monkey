# -*- coding: utf-8 -*-
import asyncio
import os
import queue
import struct
import tempfile
import unittest

from engine.realtime.asr.hotwords import HotwordCorrector, extract_hotword_candidates, load_hotwords
from engine.realtime.capture.sources import CombinedAudioSource
from engine.realtime.contracts import AudioFrame, Channel, Hotword
from engine.realtime.mocks import MockAudioSource
from engine.realtime.vad.adaptive import AdaptiveVadSegmenter


def pcm(value, samples=320):
    return struct.pack("<" + "h" * samples, *([value] * samples))


class TestRealtimeAudio(unittest.IsolatedAsyncioTestCase):
    async def test_adaptive_vad_preroll_and_silence_end(self):
        boundaries = []
        frames = []
        ts = 0
        for value, count in ((0, 10), (2000, 5), (0, 15)):
            for _ in range(count):
                frames.append(AudioFrame(pcm(value), ts, Channel.LOOPBACK))
                ts += 20
        vad = AdaptiveVadSegmenter(
            energy_threshold=500,
            base_silence_ms=300,
            pre_roll_ms=200,
            on_boundary=boundaries.append,
            use_webrtc=False,
        )
        output = [frame async for frame in vad.segment(MockAudioSource(frames).frames())]
        self.assertGreaterEqual(len(output), 20)
        self.assertEqual([event.kind for event in boundaries], ["speech_start", "speech_end"])
        self.assertEqual(boundaries[-1].reason, "silence")
        self.assertTrue(output[-1].utterance_end)
        self.assertEqual(output[-1].pcm, b"")

    async def test_post_roll_has_explicit_minimum(self):
        boundaries = []
        frames = []
        for index, value in enumerate([2000] * 3 + [0] * 10):
            frames.append(AudioFrame(pcm(value), index * 20, Channel.LOOPBACK))
        vad = AdaptiveVadSegmenter(
            energy_threshold=500,
            base_silence_ms=100,
            post_roll_ms=150,
            pre_roll_ms=20,
            on_boundary=boundaries.append,
            use_webrtc=False,
        )
        output = [frame async for frame in vad.segment(MockAudioSource(frames).frames())]
        self.assertGreaterEqual(len(output), 3 + 8)
        self.assertEqual(boundaries[-1].kind, "speech_end")

    def test_dynamic_silence_threshold(self):
        vad = AdaptiveVadSegmenter()
        vad.observe_partial("这个问题因为", 1000)
        self.assertEqual(vad.silence_threshold_ms, 700)
        for incomplete in ("的", "和", "是", "那个", "就是"):
            with self.subTest(incomplete=incomplete):
                vad.observe_partial(f"请解释{incomplete}", 1000)
                self.assertEqual(vad.silence_threshold_ms, 700)
        vad.observe_partial("这是一个完整问题吗", 1000)
        self.assertEqual(vad.silence_threshold_ms, 300)
        vad.observe_partial("普通语速", 3000)
        self.assertEqual(vad.silence_threshold_ms, 500)
        vad.observe_partial("梯度消失是怎么产生的？", 3000)
        self.assertNotEqual(vad.silence_threshold_ms, 700)

    async def test_combined_source_preserves_channels(self):
        loopback = MockAudioSource([AudioFrame(pcm(1), 0, Channel.LOOPBACK)])
        mic = MockAudioSource([AudioFrame(pcm(1), 0, Channel.MIC)])
        source = CombinedAudioSource([loopback, mic])
        frames = [frame async for frame in source.frames()]
        self.assertEqual({frame.channel for frame in frames}, {Channel.LOOPBACK, Channel.MIC})

    def test_hotword_yaml_and_alias_correction(self):
        root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        words = load_hotwords(os.path.join(root, "config", "hotwords.yaml"))
        fixed = HotwordCorrector(words).correct("快福卡和拉格怎么配合")
        self.assertEqual(fixed, "Kafka和RAG怎么配合")

    def test_extract_hotword_candidates(self):
        items = dict(extract_hotword_candidates([
            "Kafka 使用 Broker，Kafka 支持高吞吐。",
            "RAG 通过向量检索增强 LLM。",
        ]))
        self.assertEqual(items["Kafka"], 2)
        self.assertIn("Broker", items)

    def test_stereo_average_and_polyphase_resample(self):
        import numpy as np
        from engine.capture import pcm16_to_mono_16k

        stereo = np.column_stack((
            np.full(480, 1000, dtype=np.int16),
            np.full(480, 3000, dtype=np.int16),
        )).reshape(-1)
        mono = pcm16_to_mono_16k(stereo.tobytes(), 48_000, 2)
        self.assertEqual(len(mono), 160)
        self.assertAlmostEqual(float(mono[20:-20].mean()), 2000.0, delta=5.0)

    def test_audio_conditioner_targets_rms_and_limits_peak(self):
        import numpy as np
        from engine.capture import AudioConditioner

        conditioner = AudioConditioner(True, -20.0, -1.0, 12.0)
        low = np.full(320, 1000, dtype=np.int16)
        output = low
        for _ in range(60):
            output = conditioner.process(low)
        self.assertGreater(float(np.sqrt(np.mean(output.astype(float) ** 2))), 2500.0)

        loud = conditioner.process(np.full(320, 32767, dtype=np.int16))
        peak_limit = 32768 * (10 ** (-1.0 / 20.0))
        self.assertLessEqual(int(np.abs(loud.astype(np.int32)).max()), int(peak_limit) + 1)

    def test_loopback_worker_converts_outside_callback(self):
        import numpy as np
        from engine.capture import LoopbackCapture

        output = []
        capture = LoopbackCapture(output.append)
        capture._reset_stats()
        capture._raw_queue = queue.Queue()
        stereo = np.column_stack((
            np.full(480, 1000, dtype=np.int16),
            np.full(480, 3000, dtype=np.int16),
        )).reshape(-1)
        capture._raw_queue.put((stereo.tobytes(), 48_000, 2))
        capture._raw_queue.put(None)
        capture._process_raw()
        self.assertEqual(len(output), 1)
        self.assertEqual(len(output[0]), 160)
        self.assertEqual(capture.stats()["processed_frames"], 1)


if __name__ == "__main__":
    unittest.main()
