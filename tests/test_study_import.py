# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from import_study_pack import (
    _chunk_text,
    _norm,
    _parse_toc_lines,
    _smart_join,
    _split_ref,
    _xmind_bank,
    _xmind_category,
    _xmind_knowledge,
    _xmind_nodes,
)


class TestStudyImport(unittest.TestCase):
    def test_norm_removes_cjk_spacing_but_keeps_english_words(self):
        self.assertEqual(_norm("多 线 程 和 多 进 程"), "多线程和多进程")
        self.assertEqual(_smart_join(["YOLO", "v8", "和", "CNN"]), "YOLO v8和CNN")

    def test_parse_toc_lines_keeps_title_and_page(self):
        entries = _parse_toc_lines([
            "一.前言 ................ 6",
            "卷积神经网络 .......... 19",
            "一.前言 ................ 6",
            "不是目录的一行",
        ])
        self.assertEqual(entries, [("一.前言", 6), ("卷积神经网络", 19)])

    def test_chunk_text_and_long_ref_are_bounded(self):
        chunks = _chunk_text(("长文本。" * 800), max_chars=300)
        self.assertGreater(len(chunks), 1)
        refs = _split_ref(("没有句号的长段落" * 300), max_chars=200, max_items=3)
        self.assertTrue(refs)
        self.assertLessEqual(max(len(item) for item in refs), 200)

    def test_xmind_nodes_build_knowledge_and_question_bank(self):
        content = [{
            "rootTopic": {
                "title": "面试涉及的知识点",
                "children": {"attached": [{
                    "title": "Python基础",
                    "children": {"attached": [{
                        "title": "深copy和浅copy的区别",
                        "children": {"attached": [{
                            "title": "浅copy共享嵌套对象，深copy递归复制。",
                        }]},
                    }]},
                }]},
            },
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.xmind"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("content.json", json.dumps(content, ensure_ascii=False))
            with zipfile.ZipFile(path) as archive:
                root = json.loads(archive.read("content.json"))[0]["rootTopic"]
            nodes = _xmind_nodes(root)
            self.assertTrue(nodes)
            knowledge = _xmind_knowledge(nodes)
            self.assertTrue(knowledge)
            bank = _xmind_bank(nodes)
            self.assertTrue(any("copy" in item["q"] for item in bank))
            self.assertEqual(_xmind_category(nodes[0]), "XMind · Python/框架")


if __name__ == "__main__":
    unittest.main()
