# -*- coding: utf-8 -*-
"""stream 单元测试：假后端逐字 yield，验证 delta 顺序、取消后 interrupted、正常 done。

直接测 Assistant.ask_stream 的 cancel 行为，不依赖 pywebview / Api 层。
"""
import unittest
import sys
import os
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import assistant as asst_mod
from engine import llm as llm_mod
from engine import store as store_mod


class FakeBackend:
    """假后端：逐字 yield 固定文本，可控制每次 yield 之间的延迟。"""

    def __init__(self, chunks, delay=0.01):
        self._chunks = list(chunks)
        self._delay = delay
        self.label = "假后端"

    def generate(self, prompt, contexts=None):
        return "".join(self.stream(prompt, cancel=None))

    def generate_stream(self, prompt, contexts=None):
        return self.stream(prompt, cancel=None)

    def stream(self, prompt, cancel=None, num_predict=None):
        for ch in self._chunks:
            if llm_mod._is_cancelled(cancel):
                break
            if self._delay:
                time.sleep(self._delay)
            yield ch


class TestStream(unittest.TestCase):

    def _make_assistant(self, chunks, delay=0.01):
        """用假后端构造一个 Assistant（跳过知识库/题库加载）。"""
        backend = FakeBackend(chunks, delay=delay)
        a = asst_mod.Assistant.__new__(asst_mod.Assistant)
        a.retriever = type("R", (), {"retrieve": lambda self, q, top_k=3: [],
                                      "index": type("I", (), {"chunks": []})()})()
        a.asr = None
        a.llm = backend
        a.bank = None
        a.last_error = ""
        a._ollama_fb = None
        a.top_k = 3
        return a

    # ---- (a) delta 顺序正确 ----
    def test_delta_order_correct(self):
        """逐字 yield 的假后端，收到的 piece 顺序应与输入一致。"""
        chunks = ["CIoU", "是一种", "边界框", "回归损失"]
        a = self._make_assistant(chunks, delay=0)
        pieces = []
        full, ctxs = a.ask_stream(
            "什么是CIoU", recent=None, mode="normal",
            on_chunk=lambda piece, acc: pieces.append(piece),
        )
        # 合并后应等于所有 chunk 拼接
        self.assertEqual("".join(pieces), "".join(chunks))
        self.assertEqual(full.strip(), "".join(chunks))

    # ---- (b) 中途取消后部分答案已落库且 status=interrupted ----
    def test_cancel_preserves_partial(self):
        """取消后：已 yield 的部分保留，后续不再 yield。"""
        chunks = ["第一段", "第二段", "第三段", "第四段"]
        a = self._make_assistant(chunks, delay=0.05)
        pieces = []
        cancel = threading.Event()

        def _on_chunk(piece, acc):
            pieces.append(piece)
            # 收到第二段后触发取消
            if len(pieces) == 2:
                cancel.set()

        full, ctxs = a.ask_stream(
            "测试取消", recent=None, mode="normal",
            on_chunk=_on_chunk, cancel=cancel,
        )
        # 应只收到前两段（取消后第三段不再 emit）
        self.assertLessEqual(len(pieces), 2)
        self.assertIn("第一段", pieces)
        # full 应包含已生成的部分
        self.assertIn("第一段", full)

    # ---- (c) 正常结束 status=done ----
    def test_normal_complete(self):
        """不取消时：全部 chunk 都 yield，full 包含完整文本。"""
        chunks = ["完整", "回答", "内容"]
        a = self._make_assistant(chunks, delay=0)
        pieces = []
        full, ctxs = a.ask_stream(
            "测试正常", recent=None, mode="normal",
            on_chunk=lambda piece, acc: pieces.append(piece),
        )
        self.assertEqual(len(pieces), len(chunks))
        self.assertEqual(full.strip(), "".join(chunks))


# ---------------------------------------------------------------------------
# 落库状态验收：真实调用 Api.ask_async，检查 qa 表的 status / answer / 耗时。
# pywebview 在测试环境没有安装，用 stub 模块顶掉，只加载模块不启动窗口。
# ---------------------------------------------------------------------------

def _load_app_module():
    import types
    import importlib.util
    if "webview" not in sys.modules:
        stub = types.ModuleType("webview")
        stub.windows = []
        sys.modules["webview"] = stub
    app_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "面试猴.py"))
    spec = importlib.util.spec_from_file_location("_mh_app_for_test", app_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _DbAssistant:
    """按问题分发的假后端；half_evt 用来确定性地在生成中途插入新问题。"""

    def __init__(self, plans):
        self.plans = plans          # question -> (chunks, delay, half_evt, half_at)
        self.last_error = ""

    def ask_stream(self, question, recent=None, on_chunk=None,
                   mode="normal", cancel=None):
        chunks, delay, half_evt, half_at = self.plans[question]
        parts = []
        for i, ch in enumerate(chunks, 1):
            if cancel is not None and cancel.is_set():
                break               # 取消后停止产出，但已产出的部分要保留
            if delay:
                time.sleep(delay)
            parts.append(ch)
            if on_chunk:
                on_chunk(ch, "".join(parts))
            if half_evt is not None and i == half_at:
                half_evt.set()
        return "".join(parts), []


class TestDbStatus(unittest.TestCase):
    """覆盖：中途取消 -> 部分答案落库 + interrupted；正常结束 -> done。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # 走真实的 store.connect()：它带跨线程锁，和桌面 App 用的是同一条路径
        self._orig_db_path = store_mod._db_path
        store_mod._db_path = lambda: self.path
        self.db = store_mod.connect()
        self._sid = store_mod.new_session(self.db, "test")

        self.mod = _load_app_module()
        self.pushes = []
        self.mod._push = lambda js: self.pushes.append(js)

        # 跟踪 ask_async 起的后台线程：join 到线程真正结束再读库，
        # 只数 onAnswerDone 推送会早于事务收尾，出现偶发 status 读到 streaming。
        import types
        self.threads = []
        outer = self

        class _TrackedThread(threading.Thread):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                outer.threads.append(self)

        self._real_threading = self.mod.threading
        self.mod.threading = types.SimpleNamespace(
            Thread=_TrackedThread, Event=threading.Event, Lock=threading.Lock)

        self.api = self.mod.Api.__new__(self.mod.Api)
        self.api._db = self.db
        self.api._cancel_lock = threading.Lock()
        self.api._cur_cancel = None
        self.api.assistant = None

    def tearDown(self):
        if hasattr(self, "_real_threading"):
            self.mod.threading = self._real_threading
        store_mod._db_path = self._orig_db_path
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _qa(self, qa_id):
        return self.db.execute(
            "SELECT status, answer, first_token_ms, total_ms FROM qa WHERE id=?",
            (qa_id,)).fetchone()

    def _wait_threads(self, n, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.threads) >= n and all(not t.is_alive() for t in self.threads):
                return True
            time.sleep(0.01)
        return False

    # ---- (d) 新问题取消上一条：部分答案保留，status=interrupted ----
    def test_cancel_persists_partial_with_interrupted_status(self):
        half = threading.Event()
        self.api.assistant = _DbAssistant({
            "问题一": (["甲", "乙", "丙", "丁"], 0.03, half, 2),
            "问题二": (["新答案", "完整"], 0.01, None, 0),
        })
        q1 = store_mod.add_qa(self.db, self._sid, "问题一")
        q2 = store_mod.add_qa(self.db, self._sid, "问题二")
        self.api.ask_async(q1, "问题一")
        self.assertTrue(half.wait(2), "假后端没走到第二段")
        self.api.ask_async(q2, "问题二")     # 新问题到来，取消上一条
        self.assertTrue(self._wait_threads(2), "两条生成没有都结束")

        r1, r2 = self._qa(q1), self._qa(q2)
        self.assertEqual(r1["status"], "interrupted")
        self.assertIn("甲", r1["answer"])            # 已生成的部分必须留下
        self.assertNotIn("丁", r1["answer"])         # 取消后的不再写入
        self.assertIsNotNone(r1["first_token_ms"])
        self.assertIsNotNone(r1["total_ms"])
        self.assertEqual(r2["status"], "done")
        self.assertEqual(r2["answer"], "新答案完整")

    # ---- (e) 正常结束：status=done，答案与耗时齐全 ----
    def test_normal_finish_writes_done(self):
        self.api.assistant = _DbAssistant({
            "正常问题": (["完整", "回答"], 0.01, None, 0),
        })
        qa_id = store_mod.add_qa(self.db, self._sid, "正常问题")
        self.api.ask_async(qa_id, "正常问题")
        self.assertTrue(self._wait_threads(1), "生成没有结束")
        row = self._qa(qa_id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["answer"], "完整回答")
        self.assertIsNotNone(row["first_token_ms"])
        self.assertGreaterEqual(row["total_ms"], 0)


if __name__ == "__main__":
    unittest.main()
