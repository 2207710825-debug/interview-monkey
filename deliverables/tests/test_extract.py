# -*- coding: utf-8 -*-
"""阶段 P7 验收 (a)：一体化调用的流式解析（假 LLM 流）。

验证：
  1. 流里出现「【无问题】」→ 立即取消生成（后续片段不再消费）、
     不产生问题卡片（Api 层不建 qa 行、不推 onQuestion）；
  2. 「【问题】…【回答】…」→ 标题与正文被正确拆分，标题作为问题，
     原话进 qa.raw_text 字段，答案增量逐段外发。

运行：python -m unittest tests.test_extract -v
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.assistant import Assistant
from engine import store as store_mod


class FakeStreamBackend:
    """假 LLM 后端：按预定片段流式产出，记录 cancel 与 num_predict。"""

    label = "fake-stream"

    def __init__(self, pieces):
        self.pieces = list(pieces)
        self.consumed = 0            # 实际被消费的片段数（验证「立即取消」用）
        self.cancel_seen = None
        self.num_predict_seen = None

    def stream(self, prompt, cancel=None, num_predict=None):
        self.cancel_seen = cancel
        self.num_predict_seen = num_predict
        for p in self.pieces:
            if cancel is not None and callable(cancel) and cancel():
                return               # 模拟后端响应取消信号：直接停止
            self.consumed += 1
            yield p


def _events_collector(events):
    return lambda ev, *args: events.append((ev,) + args)


class TestExtractStream(unittest.TestCase):
    """assistant.ask_extract_stream 的流式解析单元测试。"""

    def _make(self, pieces):
        backend = FakeStreamBackend(pieces)
        a = Assistant(llm_backend=backend)
        return a, backend

    def test_no_question_cancels_generation(self):
        a, backend = self._make([
            "【无",                                  # 跨片段的标记
            "问题】对方只是在寒暄，不用回答。",
            "【问题】这段不该被消费",
            "【回答】不该出现的回答",
        ])
        events = []
        r = a.ask_extract_stream("p", on_event=_events_collector(events),
                                 num_predict=700)
        self.assertFalse(r["has_question"])
        self.assertEqual(r["answer"], "")
        # 没有任何卡片事件
        self.assertEqual([e[0] for e in events], [])
        # 立即取消：只消费到出现【无问题】的那一片，后面两片没被读
        self.assertEqual(backend.consumed, 2)
        self.assertIsNotNone(backend.cancel_seen)
        self.assertTrue(backend.cancel_seen())       # 取消信号已置位
        self.assertEqual(backend.num_predict_seen, 700)

    def test_question_and_answer_split(self):
        a, backend = self._make([
            "【问题】交并比是什么",
            "？\n【回答】",
            "两个框的",
            "交并比。",
        ])
        events = []
        r = a.ask_extract_stream("p", on_event=_events_collector(events),
                                 num_predict=1500)
        self.assertTrue(r["has_question"])
        self.assertEqual(r["question"], "交并比是什么？")
        self.assertEqual(r["answer"], "两个框的交并比。")
        # 事件序列：1 次 question + 2 次 delta，且顺序正确
        kinds = [e[0] for e in events]
        self.assertEqual(kinds, ["question", "delta", "delta"])
        self.assertEqual(events[0][1], "交并比是什么？")
        self.assertEqual(events[1][1], "两个框的")
        self.assertEqual(events[2][1], "交并比。")
        # delta 事件带全文参数，方便调用方做整体重渲
        self.assertEqual(events[2][2], "两个框的交并比。")
        self.assertEqual(backend.num_predict_seen, 1500)

    def test_answer_only_after_buffering(self):
        """缓冲阶段（出现【回答】之前）不外发任何字。"""
        a, _ = self._make([
            "【问题】", "请解释一下", "感受野", "这个概念",
            "\n【回答】", "感受野是……",
        ])
        events = []
        r = a.ask_extract_stream("p", on_event=_events_collector(events))
        self.assertEqual(r["question"], "请解释一下感受野这个概念")
        self.assertTrue(r["answer"].startswith("感受野是"))
        # 第一个 delta 之前没有 delta 事件泄漏问题标题
        deltas = [e for e in events if e[0] == "delta"]
        self.assertEqual("".join(d[1] for d in deltas), r["answer"])

    def test_unformatted_output_degrades_to_answer(self):
        a, _ = self._make(["模型没按格式，直接给了一段结论。"])
        events = []
        r = a.ask_extract_stream("p", on_event=_events_collector(events))
        self.assertTrue(r["has_question"])
        self.assertEqual(r["question"], "")
        self.assertIn("直接给了一段结论", r["answer"])
        self.assertTrue(any(e[0] == "question" and e[1] == "" for e in events))

    def test_reversed_sections_question_after_answer(self):
        """小模型乱序：【回答】在前、【问题】在后——回答里不得混入标记行。"""
        a, _ = self._make([
            "【回答】两个框的交并比就是 IoU。",
            "【问题】交并比是什么？",
        ])
        events = []
        r = a.ask_extract_stream("p", on_event=_events_collector(events))
        self.assertTrue(r["has_question"])
        self.assertEqual(r["question"], "交并比是什么？")
        self.assertEqual(r["answer"], "两个框的交并比就是 IoU。")
        # 流式通道外发的正文里不能出现【问题】标记
        deltas = [e for e in events if e[0] == "delta"]
        self.assertNotIn("【问题】", "".join(d[1] for d in deltas))

    def test_markerless_markdown_starts_streaming(self):
        """模型完全跳过标记直接输出 ## 小节：立即进入回答阶段，保持流式。"""
        a, backend = self._make([
            "## 核心结论\n", "答案是交并比。", "## 原理拆解\n", "逐条解释。"])
        events = []
        r = a.ask_extract_stream("p", on_event=_events_collector(events))
        self.assertTrue(r["has_question"])
        self.assertEqual(r["question"], "")
        self.assertEqual(r["answer"], "## 核心结论\n答案是交并比。## 原理拆解\n逐条解释。")
        # 每一片都是到达即外发（流式），不是攒到流结束
        deltas = [e for e in events if e[0] == "delta"]
        self.assertEqual(len(deltas), 4)
        self.assertEqual(backend.consumed, 4)


class TestApiLoopbackExtract(unittest.TestCase):
    """Api 层：【无问题】不建卡片；【问题】/【回答】建卡且 raw_text 存原话。"""

    @classmethod
    def setUpClass(cls):
        import types
        import importlib.util
        if "webview" not in sys.modules:
            stub = types.ModuleType("webview")
            stub.windows = []
            sys.modules["webview"] = stub
        app = os.path.normpath(os.path.join(os.path.dirname(__file__),
                                            "..", "面试猴.py"))
        spec = importlib.util.spec_from_file_location("_mh_app_extract_test", app)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def setUp(self):
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
        self.api._ai_mode = "detail"
        self.api._loop_recent = []
        self.api._loop_last_question = None
        self.api._loop_hotwords = "CIoU、IoU"

    def tearDown(self):
        store_mod._db_path = self._orig_db_path
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _wait_push(self, token, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(token in p for p in self.pushes):
                return True
            time.sleep(0.01)
        return False

    def test_no_question_creates_no_card(self):
        raw = "嗯嗯，这个我们回头再说"
        self.api.assistant = Assistant(
            llm_backend=FakeStreamBackend(["【无问题】", "不用回答。"]))
        self.api._on_loop_question(raw)
        time.sleep(0.3)                      # 等 worker 跑完
        self.assertEqual(store_mod.list_qa(self.db, self.sid), [])
        self.assertFalse(any("onQuestion" in p for p in self.pushes))
        self.assertFalse(any("onAnswerDelta" in p for p in self.pushes))

    def test_question_card_with_raw_text(self):
        raw = "焦并比和西欧U有什么区别"
        self.api.assistant = Assistant(llm_backend=FakeStreamBackend([
            "【问题】请解释交并比和 CIoU 的区别是什么？\n",
            "【回答】",
            "CIoU 额外考虑中心点距离与长宽比。",
        ]))
        self.api._on_loop_question(raw)
        self.assertTrue(self._wait_push("onAnswerDone"), self.pushes)
        rows = store_mod.list_qa(self.db, self.sid)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["question"], "请解释交并比和 CIoU 的区别是什么？")
        self.assertEqual(row["mode"], "detail")
        self.assertIn("中心点距离", row["answer"])
        # P7 新字段：qa.raw_text 保存触发这条问答的「对方」原话
        self.assertEqual(row["raw_text"], raw)
        self.assertTrue(any("onQuestion" in p for p in self.pushes))
        self.assertTrue(any("onAnswerDelta" in p for p in self.pushes))
        # 整理后的问题成为下一次的 {last_question}
        self.assertEqual(self.api._loop_last_question,
                         "请解释交并比和 CIoU 的区别是什么？")


if __name__ == "__main__":
    unittest.main()
