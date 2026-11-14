# -*- coding: utf-8 -*-
"""阶段 P6 验收：两段式会议提示词的构建、解析、静默合并与 Api 接线。

运行：python -m unittest tests.test_meeting_prompt -v
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import meeting_prompt as mp
from engine import store as store_mod


class TestPromptBuild(unittest.TestCase):

    def test_all_placeholders_filled(self):
        p = mp.build(["上一句", "这一句"], "IoU 是什么",
                     [{"title": "知识库", "text": "交并比用于衡量框重叠"}],
                     "CIoU、IoU", mode="brief")
        for token in ("{mode_instruction}", "{hotwords}", "{recent}",
                      "{last_question}", "{ctx}"):
            self.assertNotIn(token, p)
        self.assertIn("用一句话给出结论", p)          # brief 的模式指令
        self.assertIn("CIoU、IoU", p)
        self.assertIn("1. 上一句", p)
        self.assertIn("2. 这一句", p)
        self.assertIn("IoU 是什么", p)
        self.assertIn("[知识库] 交并比用于衡量框重叠", p)
        self.assertIn("【输出格式】必须严格按下面两段输出", p)

    def test_empty_inputs_have_placeholders_text(self):
        p = mp.build([], None, [], "", mode="normal")
        self.assertIn("（无）", p)
        self.assertIn("（无检索内容）", p)
        self.assertIn("先给结论", p)

    def test_all_five_modes(self):
        for mode in ("brief", "normal", "detail", "algo", "system"):
            self.assertIn(mp.MODE_INSTRUCTIONS[mode], mp.build("x", "", [], "", mode))
        self.assertEqual(mp.mode_instruction("unknown"),
                         mp.MODE_INSTRUCTIONS["normal"])

    def test_hotwords_from_file(self):
        self.assertIn("CIoU", mp.load_hotwords())


class TestParse(unittest.TestCase):

    def test_normal_two_sections(self):
        raw = ("【问题】请解释 CIoU 与普通 IoU 的区别是什么？\n"
               "【回答】CIoU 在 IoU 基础上补了中心点距离和长宽比惩罚。")
        r = mp.parse(raw)
        self.assertTrue(r["has_question"])
        self.assertEqual(r["question"], "请解释 CIoU 与普通 IoU 的区别是什么？")
        self.assertIn("中心点距离", r["answer"])

    def test_no_question_variants(self):
        for raw in ("【无问题】", "【问题】【无问题】", "", "   "):
            r = mp.parse(raw)
            self.assertFalse(r["has_question"], raw)
            self.assertEqual(r["answer"], "")

    def test_unformatted_output_degrades_to_answer(self):
        r = mp.parse("模型没按格式，直接给了一段结论。")
        self.assertTrue(r["has_question"])
        self.assertEqual(r["question"], "")
        self.assertIn("直接给了一段结论", r["answer"])


class TestSilenceMerger(unittest.TestCase):
    """只按静默合并；寒暄也要送出去，由 LLM 判【无问题】。"""

    def _merger(self, fired, quiet=1.5):
        clock = [0.0]
        m = mp.SilenceMerger(fired.append, quiet=quiet, now=lambda: clock[0])
        return m, clock

    def test_fragments_merge_into_one(self):
        fired = []
        m, clock = self._merger(fired)
        m.push("这是一个什么")
        clock[0] = 1.0
        m.push("是交并比")
        clock[0] = 2.0
        m.tick()                      # 距最后一句才 1.0s，不触发
        self.assertEqual(fired, [])
        clock[0] = 2.6
        m.tick()                      # 1.6s >= 1.5s，合并成一次
        self.assertEqual(fired, ["这是一个什么是交并比"])

    def test_small_talk_still_merged(self):
        """P7：碎片 <8 字不触发也不丢弃，等后续句子拼起来再送模型。"""
        fired = []
        m, clock = self._merger(fired)
        m.push("好的")
        clock[0] = 2.0
        m.tick()
        self.assertEqual(fired, [])                 # 太短，不触发
        self.assertEqual(m.buf, ["好的"])           # 留在缓冲里
        clock[0] = 2.5
        m.push("，那注意力机制是怎么工作的")
        clock[0] = 4.0
        m.tick()                                    # 1.5s >= 1.2s 且 >=8 字
        self.assertEqual(fired, ["好的，那注意力机制是怎么工作的"])


class TestLoopbackQuestionHook(unittest.TestCase):
    """Api._on_loop_question：无问题跳过，有问答则落库并推前端。"""

    def setUp(self):
        import types
        import importlib.util
        if "webview" not in sys.modules:
            stub = types.ModuleType("webview")
            stub.windows = []
            sys.modules["webview"] = stub
        app = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "面试猴.py"))
        spec = importlib.util.spec_from_file_location("_mh_app_meeting_test", app)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._orig_db_path = store_mod._db_path
        store_mod._db_path = lambda: self.path
        self.db = store_mod.connect()
        self.sid = store_mod.new_session(self.db, "test")

        self.pushes = []
        self.mod._push = lambda js: self.pushes.append(js)

        class FakeAssistant:
            def __init__(self, output):
                self.output, self.prompt = output, None
                self.num_predict = None

            def _contexts(self, q):
                return []

            def ask_raw(self, prompt, contexts=None, cancel=None):
                self.prompt = prompt
                return self.output

            def ask_extract_stream(self, prompt, contexts=None, cancel=None,
                                   on_event=None, num_predict=None):
                """模拟 P7 流式一体化：按 assistant.ask_extract_stream 的
                事件契约把 output 拆成 question/delta 事件。"""
                self.prompt = prompt
                self.num_predict = num_predict
                out = self.output or ""
                if not out.strip() or "【无问题】" in out:
                    return {"has_question": False, "question": "", "answer": ""}
                q_at, a_at = out.find("【问题】"), out.find("【回答】")
                if q_at >= 0 and a_at > q_at:
                    q = out[q_at + 4:a_at].strip()
                    ans = out[a_at + 4:].strip()
                    if on_event and q:
                        on_event("question", q)
                    if on_event and ans:
                        on_event("delta", ans, ans)
                    return {"has_question": True, "question": q, "answer": ans}
                if on_event:
                    on_event("question", "")
                    on_event("delta", out, out)
                return {"has_question": True, "question": "", "answer": out.strip()}

        self.FakeAssistant = FakeAssistant
        self.api = self.mod.Api.__new__(self.mod.Api)
        self.api._db = self.db
        self.api._cur_sid = self.sid
        self.api._ai_mode = "normal"
        self.api._loop_recent = []
        self.api._loop_last_question = None
        self.api._loop_hotwords = "CIoU、IoU"
        self.api.assistant = FakeAssistant("【无问题】")

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

    def test_no_question_does_nothing(self):
        self.api._on_loop_question("嗯，好的")
        time.sleep(0.15)
        self.assertEqual(store_mod.list_qa(self.db, self.sid), [])
        self.assertEqual(self.pushes, [])
        self.assertIn("【最近对话】", self.api.assistant.prompt)

    def test_answer_writes_qa_and_pushes_frontend(self):
        self.api.assistant = self.FakeAssistant(
            "【问题】请解释交并比和 CIoU 的区别是什么？\n"
            "【回答】CIoU 额外考虑中心点距离与长宽比。")
        self.api._on_loop_question("焦并比和西欧U有什么区别")
        self.assertTrue(self._wait_push("onAnswerDone"), self.pushes)
        rows = store_mod.list_qa(self.db, self.sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[0]["question"], "请解释交并比和 CIoU 的区别是什么？")
        self.assertIn("中心点距离", rows[0]["answer"])
        self.assertTrue(any("onQuestion" in p for p in self.pushes))
        self.assertTrue(any("onAnswerDelta" in p for p in self.pushes))
        # 整理后的问题要成为下一次的 {last_question}
        self.assertEqual(self.api._loop_last_question,
                         "请解释交并比和 CIoU 的区别是什么？")
        self.assertIn("【回答】先给结论", self.api.assistant.prompt)


if __name__ == "__main__":
    unittest.main()
