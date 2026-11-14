# -*- coding: utf-8 -*-
"""阶段 P5 验收：音源列表、start/stop_capture、speaker 与「只有对方触发问答」。

运行：python -m unittest tests.test_capture_sources -v
不依赖真实音频设备：回环采集与设备枚举都用假对象替换。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import store as store_mod


def _load_app():
    import types
    import importlib.util
    if "webview" not in sys.modules:
        stub = types.ModuleType("webview")
        stub.windows = []
        sys.modules["webview"] = stub
    app = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "面试猴.py"))
    spec = importlib.util.spec_from_file_location("_mh_app_sources_test", app)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeAgg:
    def __init__(self):
        self.pushed = []

    def push_final(self, text):
        self.pushed.append(text)

    def tick(self):
        pass


class FakeMerger:
    def __init__(self):
        self.pushed = []

    def push(self, text):
        self.pushed.append(text)

    def tick(self):
        pass


class TestCaptureSources(unittest.TestCase):

    def setUp(self):
        self.mod = _load_app()
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig_db_path = store_mod._db_path
        store_mod._db_path = lambda: self.path
        self.db = store_mod.connect()
        self.sid = store_mod.new_session(self.db, "test")
        self.pushes = []
        self.mod._push = lambda js: self.pushes.append(js)
        self.api = self.mod.Api.__new__(self.mod.Api)
        self.api._db = self.db
        self.api._cur_sid = self.sid
        self.api._ai_mode = "normal"
        self.api._aggregator = FakeAgg()
        self.api._loop_merger = FakeMerger()
        self.api._loop_recent = []
        self.api._loop_last_question = None
        self.api._loop_hotwords = ""
        self.api._loop_cap = self.api._loop_q = None
        self.api._loop_stop = self.api._loop_sess = None
        self.api._cap_watch_stop = None
        self.api._cap_device_sig = None
        self.api._cap_on_pcm = None
        self.api._cap_device_index = None

    def tearDown(self):
        store_mod._db_path = self._orig_db_path
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_list_sources(self):
        from engine import capture as cap_mod
        orig = cap_mod.list_loopback_devices
        cap_mod.list_loopback_devices = lambda: [
            {"index": 3, "name": "扬声器回环 A", "sample_rate": 48000,
             "channels": 2, "is_default": True},
            {"index": 4, "name": "耳机回环 B", "sample_rate": 44100,
             "channels": 2, "is_default": False},
        ]
        try:
            r = self.api.list_sources()
        finally:
            cap_mod.list_loopback_devices = orig
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["names"], ["扬声器回环 A", "耳机回环 B"])
        self.assertEqual(r["default"], "扬声器回环 A")

    def test_loopback_speaker_is_other(self):
        self.api._on_loopback_final("请问交并比是什么")
        segs = store_mod.list_segments(self.db, self.sid)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["speaker"], "对方")
        self.assertEqual(self.api._loop_merger.pushed, ["请问交并比是什么"])
        self.assertTrue(any("onLoopbackFinal" in p for p in self.pushes))

    def test_mic_speaker_is_me_and_never_triggers_question(self):
        import base64

        class FakeSess:
            def __init__(self):
                self._pending = "我说了句话"

            def feed(self, pcm):
                return "我说了句话"

        self.api._asr_sess = FakeSess()
        r = self.api.asr_feed(base64.b64encode(b"\x00" * 320).decode())
        self.assertEqual(r["final"], "我说了句话")
        segs = store_mod.list_segments(self.db, self.sid)
        self.assertEqual(segs[0]["speaker"], "我")
        # 关键：「我」说的话绝不进聚合器，只有「对方」能触发自动问答
        self.assertEqual(self.api._aggregator.pushed, [])
        self.assertEqual(self.api._loop_merger.pushed, [])

    def test_start_stop_capture_and_legacy_aliases(self):
        from engine import capture as cap_mod
        from engine import asr2

        class FakeCap:
            def __init__(self, on_pcm, device_index=None):
                self.on_pcm, self.device_name = on_pcm, "Fake Loopback"

            def start(self):
                self.on_pcm(b"\x00" * 320)      # 回调只入队，不做识别

            def stop(self):
                pass

        class FakeSession:
            def feed(self, pcm):
                pass

            def flush(self):
                pass

        orig = (cap_mod.available, cap_mod.LoopbackCapture,
                cap_mod.list_loopback_devices, asr2.build, asr2.status)
        cap_mod.available = lambda: True
        cap_mod.LoopbackCapture = FakeCap
        cap_mod.list_loopback_devices = lambda: []
        asr2.build = lambda on_final: FakeSession()
        asr2.status = lambda: {"mode": "fallback_transducer"}
        try:
            r = self.api.start_capture(None)
            self.assertTrue(r["ok"], r)
            self.assertIsNotNone(self.api._loop_cap)
            self.assertIsNotNone(self.api._cap_watch_stop)
            self.assertTrue(self.api.stop_capture()["ok"])
            self.assertIsNone(self.api._loop_cap)
            # 旧名字仍然可用（兼容早期前端/脚本）
            self.assertTrue(self.api.loopback_start(None)["ok"])
            self.assertTrue(self.api.loopback_stop()["ok"])
        finally:
            (cap_mod.available, cap_mod.LoopbackCapture,
             cap_mod.list_loopback_devices, asr2.build, asr2.status) = orig
            try:
                self.api.stop_capture()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
