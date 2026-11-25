# -*- coding: utf-8 -*-
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from import_nowcoder import CATEGORIES, _clean_html, _correct_letters, _question_record


class TestNowcoderImport(unittest.TestCase):
    def test_clean_html(self):
        value = _clean_html("<div>题目&nbsp;一</div><br><img src='https://x/y.png'>")
        self.assertIn("题目 一", value)
        self.assertIn("[图片: https://x/y.png]", value)
        self.assertNotIn("<div>", value)

    def test_correct_letters_marks_only_type_one(self):
        self.assertEqual(
            _correct_letters([
                {"content": "A", "type": 0},
                {"content": "B", "type": 1},
                {"content": "C", "type": 1},
            ]),
            ["B", "C"],
        )

    def test_question_record_builds_knowledge_and_bank(self):
        category = next(item for item in CATEGORIES if item["name"] == "计算机视觉")
        record = _question_record({
            "questionId": 123,
            "uuid": "uuid-123",
            "type": 2,
            "content": "<div>以下哪些是目标检测指标？</div>",
            "answers": [
                {"content": "mAP", "type": 1},
                {"content": "NMS", "type": 0},
                {"content": "IoU", "type": 1},
            ],
            "analysis": "<p>mAP 与 IoU 都是检测指标。</p>",
            "knowledgeTags": [{"name": "计算机视觉"}],
            "referenceAnswer": "",
        }, category)
        self.assertIsNotNone(record)
        self.assertEqual(record["metadata"]["correct"], ["A", "C"])
        self.assertEqual(record["metadata"]["provider"], "nowcoder")
        self.assertIn("【正确答案】A、C", record["text"])
        self.assertIn("mAP 与 IoU 都是检测指标", record["text"])
        self.assertEqual(record["bank"]["cat"], "Nowcoder · 计算机视觉")
        self.assertEqual(record["bank"]["ref"][0], "正确答案：A、C")
        self.assertEqual(record["bank"]["q"].splitlines()[0], "以下哪些是目标检测指标？")

    def test_missing_question_id_is_skipped(self):
        category = CATEGORIES[0]
        self.assertIsNone(_question_record({"content": "x"}, category))


if __name__ == "__main__":
    unittest.main()
