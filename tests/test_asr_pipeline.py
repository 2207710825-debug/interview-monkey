# -*- coding: utf-8 -*-
"""阶段 P5 验收：拼音纠错、回环重采样、VAD 分段、Api 回环接线。

用 `python -m unittest tests.test_asr_pipeline -v` 可直接跑；
numpy / pypinyin 未安装的解释器会自动跳过相应用例（其余照样验）。
完整跑建议用打包环境：
    E:/面试猴/.workbuddy/gui-build/venv/Scripts/python.exe -m unittest tests.test_asr_pipeline -v
"""
import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HAS_PYPINYIN = importlib.util.find_spec("pypinyin") is not None

from engine import correct, pipeline
from engine import store as store_mod


class TestCorrection(unittest.TestCase):
    """字面映射 + 拼音近音纠错，以及 pipeline 的向后兼容。"""

    def test_mapping_replace(self):
        self.assertEqual(correct.fix("西欧U是什么", {}, {"西欧U": "CIoU"}),
                         "CIoU是什么")

    def test_empty_pinyin_degrades_to_replace(self):
        """没有 pypinyin 时只做 str.replace，不能改坏文本。"""
        self.assertEqual(correct.fix("焦并比和西欧U", {},
                                     {"焦并比": "交并比", "西欧U": "CIoU"}),
                         "交并比和CIoU")
        self.assertEqual(correct.fix("", {}, {"a": "b"}), "")

    def test_pipeline_backward_compatible(self):
        """P2 的 apply_corrections 行为不变：纠错表仍然生效。"""
        self.assertEqual(pipeline.apply_corrections("焦并比"), "交并比")
        self.assertEqual(pipeline.apply_corrections("西欧U"), "CIoU")
        self.assertEqual(pipeline.apply_corrections(""), "")

    @unittest.skipUnless(HAS_PYPINYIN, "pypinyin 未安装")
    def test_pinyin_homophone_fix(self):
        py, _ = correct.load_default()
        self.assertIn("交并比", py)
        # 「焦并比」未在纠错表里时，靠同音滑窗改成术语
        self.assertEqual(correct.fix("焦并比", py, {}), "交并比")
        # 字面映射先跑，再跑拼音，组合结果正确
        self.assertEqual(correct.fix("焦并比和西欧U是什么", py, {"西欧U": "CIoU"}),
                         "交并比和CIoU是什么")


@unittest.skipUnless(HAS_NUMPY, "numpy 未安装")
class TestCaptureAndASR2(unittest.TestCase):

    def test_polyphase_resample_length(self):
        import numpy as np
        from engine import capture
        x = np.ones(4410, dtype=np.float32)     # 0.1 秒 @44.1k
        y = capture.resample_pcm16(x, 44100, 16000)
        self.assertEqual(y.shape[0], 1600)       # 0.1 秒 @16k
        self.assertAlmostEqual(float(y.mean()), 1.0, places=3)

    def test_energy_vad_cuts_on_silence(self):
        """能量 VAD 兜底：语音 + 长静音后吐出一段，并交给识别引擎。"""
        import numpy as np
        from engine import asr2

        class FakeEngine:
            def transcribe(self, pcm, sample_rate=16000):
                return "识别到一句"

        fired = []
        vad = asr2.EnergyVadFallback(fired.append, asr_engine=FakeEngine())
        sr = 16000
        t = np.arange(int(sr * 0.3)) / sr
        speech = (np.sin(2 * np.pi * 220 * t) * 0.2 * 32768).astype(np.int16)
        silence = np.zeros(int(sr * 0.7), dtype=np.int16)
        vad.feed(speech)
        vad.feed(silence)
        self.assertEqual(fired, ["识别到一句"])

    def test_energy_vad_ignores_tiny_noise(self):
        import numpy as np
        from engine import asr2
        fired = []
        vad = asr2.EnergyVadFallback(fired.append, asr_engine=object())
        vad.feed(np.zeros(64, dtype=np.int16))
        vad.flush()
        self.assertEqual(fired, [])


@unittest.skipUnless(HAS_NUMPY, "numpy 未安装")
class TestLoopbackApi(unittest.TestCase):
    """验证回环链路真的接到了落库 + 纠错 + 聚合器，不只是新建了模块。"""

    def setUp(self):
        import types
        import importlib.util
        import threading
        if "webview" not in sys.modules:
            stub = types.ModuleType("webview")
            stub.windows = []
            sys.modules["webview"] = stub
        app = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "面试猴.py"))
        spec = importlib.util.spec_from_file_location("_mh_app_loopback_test", app)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig_db_path = store_mod._db_path
        store_mod._db_path = lambda: self.path
        self.db = store_mod.connect()
        self.sid = store_mod.new_session(self.db, "test")
        self.fired = []
        self.api = self.mod.Api.__new__(self.mod.Api)
        self.api._db = self.db
        self.api._cur_sid = self.sid
        self.api._aggregator = pipeline.QuestionAggregator(
            on_question=self.fired.append, now=lambda: 0.0)
        self.api._loop_cap = self.api._loop_q = None
        self.api._loop_stop = self.api._loop_sess = None
        self._cancel_lock = threading.Lock()
        self.api._cancel_lock = self._cancel_lock

    def tearDown(self):
        store_mod._db_path = self._orig_db_path
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_start_final_stop_and_correction(self):
        import numpy as np
        from engine import capture as cap_mod
        from engine import asr2

        class FakeCapture:
            def __init__(self, on_pcm, device_index=None):
                self.on_pcm, self.started = on_pcm, False

            def start(self):
                self.started = True
                self.on_pcm(np.zeros(160, dtype=np.int16))

            def stop(self):
                self.started = False

        class FakeSession:
            def __init__(self, on_final):
                self.on_final = on_final
                self.flushed = False

            def feed(self, pcm):
                pass

            def flush(self):
                self.flushed = True

        orig = (cap_mod.available, cap_mod.LoopbackCapture,
                asr2.build, asr2.status)
        cap_mod.available = lambda: True
        cap_mod.LoopbackCapture = FakeCapture
        asr2.build = lambda on_final: FakeSession(on_final)
        asr2.status = lambda: {"mode": "fallback_transducer", "loopback": True}
        try:
            r = self.api.loopback_start(None)
            self.assertTrue(r["ok"], r)
            self.assertIsNotNone(self.api._loop_cap)

            # 模拟工作线程里吐出一句定稿
            self.api._on_loopback_final("焦并比是什么")
            segs = store_mod.list_segments(self.db, self.sid)
            # P6 规格：final 流程为 识别 → 纠错 → 写 segments(speaker=对方) → 推送，
            # 落库的是纠错后的文本
            self.assertEqual([s["text"] for s in segs], ["交并比是什么"])
            self.assertEqual(segs[0]["speaker"], "对方")
            # 回环定稿句进 SilenceMerger（判定交给两段式提示词），
            # 不进正则聚合器；只有「对方」能触发自动问答。
            self.assertEqual(self.api._loop_merger.buf, ["交并比是什么"])
            self.assertEqual(self.api._aggregator.buf, [])

            self.assertTrue(self.api.loopback_stop()["ok"])
            self.assertIsNone(self.api._loop_cap)
        finally:
            cap_mod.available, cap_mod.LoopbackCapture, asr2.build, asr2.status = orig


if __name__ == "__main__":
    unittest.main()
