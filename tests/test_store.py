# -*- coding: utf-8 -*-
"""engine/store.py 的单元测试。

用临时数据库文件，不碰真实的 interview.db。
验收要求：写入 3 句转写 + 2 条问答后能完整读回，顺序正确，
重开连接后数据仍在。
"""

import json
import os
import tempfile
import unittest

from engine import store


class TestStore(unittest.TestCase):
    """覆盖 store.py 的全部公开函数。"""

    def setUp(self):
        """每个测试用例用独立的临时目录和库文件，互不干扰。"""
        self._tmpdir = tempfile.mkdtemp(prefix="im_test_")
        self._dbfile = os.path.join(self._tmpdir, "test.db")
        # 覆盖 store 模块里的 _db_path，让 connect() 指向临时库
        self._orig_path = store._db_path
        store._db_path = lambda: self._dbfile

    def tearDown(self):
        """还原 _db_path，清理临时目录。"""
        store._db_path = self._orig_path
        try:
            for fname in os.listdir(self._tmpdir):
                os.remove(os.path.join(self._tmpdir, fname))
            os.rmdir(self._tmpdir)
        except Exception:
            pass

    # ---------- 核心验收用例 ----------
    def test_write_and_readback(self):
        """写入 3 句转写 + 2 条问答后能完整读回，顺序正确。"""
        conn = store.connect()
        sid = store.new_session(conn, title="测试会话")

        # 写入 3 句转写
        seg_texts = ["第一句话", "第二句话来了", "最后第三句"]
        for t in seg_texts:
            store.add_segment(conn, sid, t)

        # 写入 2 条问答（含流式追加答案）
        qid1 = store.add_qa(conn, sid, "什么是闭包？", mode="normal")
        store.append_qa_answer(conn, qid1, "闭包是")
        store.append_qa_answer(conn, qid1, "一个函数连同其词法环境的引用。")
        store.finish_qa(conn, qid1, status="done", first_token_ms=120,
                        total_ms=850, sources_json='[{"title":"JS基础","text":"..."}]')

        qid2 = store.add_qa(conn, sid, "解释一下事件循环", mode="brief")
        store.append_qa_answer(conn, qid2, "事件循环是")
        store.append_qa_answer(conn, qid2, "JS 并发模型的核心机制。")
        store.finish_qa(conn, qid2, status="done", first_token_ms=95, total_ms=600)

        # 读回转写：顺序应与写入一致
        segs = store.list_segments(conn, sid)
        self.assertEqual(len(segs), 3)
        for i, seg in enumerate(segs):
            self.assertEqual(seg["text"], seg_texts[i])

        # 读回问答：顺序应与写入一致
        qas = store.list_qa(conn, sid)
        self.assertEqual(len(qas), 2)
        self.assertEqual(qas[0]["question"], "什么是闭包？")
        self.assertEqual(qas[0]["answer"], "闭包是一个函数连同其词法环境的引用。")
        self.assertEqual(qas[0]["mode"], "normal")
        self.assertEqual(qas[0]["status"], "done")
        self.assertEqual(qas[0]["first_token_ms"], 120)
        self.assertEqual(qas[0]["total_ms"], 850)
        srcs = json.loads(qas[0]["sources_json"])
        self.assertEqual(srcs[0]["title"], "JS基础")

        self.assertEqual(qas[1]["question"], "解释一下事件循环")
        self.assertEqual(qas[1]["answer"], "事件循环是JS 并发模型的核心机制。")
        self.assertEqual(qas[1]["mode"], "brief")
        self.assertEqual(qas[1]["status"], "done")
        self.assertEqual(qas[1]["sources_json"], "[]")

        conn.close()

    # ---------- 重开连接数据仍在 ----------
    def test_reopen_persists(self):
        """关闭连接后重新打开，数据应该还在。"""
        conn1 = store.connect()
        sid = store.new_session(conn1, title="持久化测试")
        store.add_segment(conn1, sid, "这是一句会被持久化的转写")
        qid = store.add_qa(conn1, sid, "持久化问题？", mode="algo")
        store.append_qa_answer(conn1, qid, "回答片段一")
        store.append_qa_answer(conn1, qid, "回答片段二")
        store.finish_qa(conn1, qid, status="done", total_ms=300)
        conn1.close()

        # 重新打开连接
        conn2 = store.connect()
        latest = store.latest_session(conn2)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], sid)
        self.assertEqual(latest["title"], "持久化测试")

        segs = store.list_segments(conn2, sid)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["text"], "这是一句会被持久化的转写")

        qas = store.list_qa(conn2, sid)
        self.assertEqual(len(qas), 1)
        self.assertEqual(qas[0]["question"], "持久化问题？")
        self.assertEqual(qas[0]["answer"], "回答片段一回答片段二")
        self.assertEqual(qas[0]["status"], "done")
        conn2.close()

    # ---------- latest_session 空库 ----------
    def test_latest_session_empty(self):
        """空库时 latest_session 应返回 None。"""
        conn = store.connect()
        self.assertIsNone(store.latest_session(conn))
        conn.close()

    # ---------- append_qa_answer 只追加不覆盖 ----------
    def test_append_does_not_overwrite(self):
        """多次 append_qa_answer 应拼接而非覆盖。"""
        conn = store.connect()
        sid = store.new_session(conn)
        qid = store.add_qa(conn, sid, "测试追加？")
        store.append_qa_answer(conn, qid, "A")
        store.append_qa_answer(conn, qid, "B")
        store.append_qa_answer(conn, qid, "C")
        store.finish_qa(conn, qid, status="done")
        qas = store.list_qa(conn, sid)
        self.assertEqual(qas[0]["answer"], "ABC")
        conn.close()


if __name__ == "__main__":
    unittest.main()
