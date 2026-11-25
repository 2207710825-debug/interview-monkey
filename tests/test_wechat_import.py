# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from import_wechat_notes import (
    _chunk_body,
    _dedupe,
    _parse_notes_extra,
    _parse_qa_skeleton,
    _sections,
    _strip_md,
)


class TestWechatImport(unittest.TestCase):
    def test_sections_ignore_code_comments_and_keep_heading_path(self):
        text = "\n".join([
            "# 总标题",
            "",
            "## 第一章",
            "",
            "正文一。",
            "",
            "```python",
            "# 这是代码注释，不是标题",
            "print(1)",
            "```",
            "",
            "### 1.1 小节",
            "",
            "正文二。",
        ])
        sections = _sections(text)
        paths = [item["path"] for item in sections]
        self.assertIn("总标题 > 第一章", paths)
        self.assertIn("总标题 > 第一章 > 1.1 小节", paths)
        self.assertFalse(any("代码注释" in path for path in paths))
        first = next(item for item in sections if item["path"].endswith("第一章"))
        self.assertIn("这是代码注释", first["body"])

    def test_parse_qa_skeleton_four_parts(self):
        text = "\n".join([
            "# 面试高频题",
            "",
            "# A. 张量与训练基础",
            "",
            "## 第 1 题 ★ 反向传播在做什么？",
            "",
            "**① 一句话答案**",
            "从损失往回走，逐层分责任。",
            "",
            "**② 骨架**",
            "1. 前向。",
            "2. 反向。",
            "",
            "**③ 打个比方**",
            "流水线追溯。",
            "",
            "**④ 面试官可能追问**",
            "- 为什么复用？",
            "",
            "---",
            "",
            "## 第 2 题 ★ 为什么用梯度下降？",
            "",
            "**① 一句话答案**",
            "没有闭式解。",
            "",
            "# 附录 A",
            "",
            "速查表。",
        ])
        items = _parse_qa_skeleton(text)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["cat"], "微信资料 · 深度学习答题骨架")
        self.assertIn("A 张量与训练基础", first["from"])
        self.assertEqual(first["q"], "第 1 题 · 反向传播在做什么？")
        self.assertEqual(len(first["ref"]), 4)
        self.assertTrue(first["ref"][0].startswith("一句话答案："))
        self.assertIn("前向", first["ref"][1])
        self.assertEqual(items[1]["q"], "第 2 题 · 为什么用梯度下降？")

    def test_parse_notes_extra_table_and_answer(self):
        text = "\n".join([
            "### 16.11 扩展面试题库（28 道 · 已附答案速记）",
            "",
            "**A. 张量与自动微分（4 道）**",
            "",
            "| # | 题目 | 考察点 | 对应章节 |",
            "|---|---|---|---|",
            "| 13 | `view()` 和 `reshape()` 有什么区别？ | 内存连续性 | 16.7 |",
            "",
            "---",
            "",
            "### 16.11.1 扩展 28 题答案速记",
            "",
            "- **13 `view()` vs `reshape()`**：`view` 要求内存连续，否则报错。 → 16.7",
            "",
            "## 17. 下一章",
        ])
        items = _parse_notes_extra(text)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["q"], "第 13 题 · view() 和 reshape() 有什么区别？")
        self.assertEqual(item["cat"], "微信资料 · UNet笔记补充")
        self.assertIn("A 张量与自动微分", item["from"])
        self.assertEqual(len(item["ref"]), 3)
        self.assertIn("内存连续性", item["ref"][0])
        self.assertIn("16.7", item["ref"][1])
        self.assertTrue(item["ref"][2].startswith("答案速记："))

    def test_dedupe_ignores_question_number_and_punctuation(self):
        items = [
            {"q": "第 1 题 · 反向传播在做什么？", "ref": ["a"]},
            {"q": "反向传播在做什么", "ref": ["b"]},
            {"q": "第 2 题 · 另一个问题？", "ref": ["c"]},
        ]
        result = _dedupe(items)
        self.assertEqual([item["ref"][0] for item in result], ["a", "c"])

    def test_strip_md_keeps_table_headers_readable(self):
        text = "\n".join([
            "| | 现象 | 判断依据 |",
            "|---|---|---|",
            "| 学习率过大 | loss 震荡 | 锯齿状 |",
            "| 学习率过小 | 收敛慢 | 几乎直线 |",
        ])
        cleaned = _strip_md(text)
        self.assertIn("学习率过大；现象=loss 震荡；判断依据=锯齿状", cleaned)
        self.assertNotIn("|", cleaned)

    def test_chunk_body_splits_long_text_but_keeps_code_fence(self):
        code = "```python\n" + "\n".join(f"print({i})" for i in range(80)) + "\n```"
        body = "前言。\n\n" + code + "\n\n" + ("长文本。" * 600)
        chunks = _chunk_body(body, max_chars=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any(chunk.startswith("```python") for chunk in chunks))
        self.assertTrue(all(chunk.strip() for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
