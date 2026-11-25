# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from 面试猴 import Api


class TestPracticeBankLoader(unittest.TestCase):
    def _write(self, directory: Path, name: str, items) -> None:
        knowledge = directory / "InterviewMonkey" / "knowledge"
        knowledge.mkdir(parents=True, exist_ok=True)
        (knowledge / name).write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )

    def test_merges_nowcoder_and_wechat_and_dedupes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "nowcoder_bank.json", [
                {"cat": "Nowcoder · 计算机视觉", "from": "牛客网",
                 "q": "什么是 NMS？", "ref": ["按类别做"]},
                {"cat": "Nowcoder · 计算机视觉", "from": "牛客网",
                 "q": "重复题目", "ref": ["牛客版本"]},
            ])
            self._write(root, "wechat_bank.json", [
                {"cat": "微信资料 · 深度学习答题骨架", "from": "微信资料",
                 "q": "U-Net 的跳跃连接有什么用？", "ref": ["保留浅层细节"]},
                {"cat": "微信资料 · 深度学习答题骨架", "from": "微信资料",
                 "q": "重复题目", "ref": ["微信版本"]},
            ])
            self._write(root, "study_bank.json", [
                {"cat": "资料 · XMind 面试知识点", "from": "资料 · XMind",
                 "q": "BN 训练和推理有什么区别？", "ref": ["训练用 batch 统计量"]},
            ])
            api = Api.__new__(Api)
            with patch.dict(os.environ, {"APPDATA": directory}):
                result = api.get_practice_bank()
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 4)
            self.assertEqual(
                [item["q"] for item in result["items"]],
                ["什么是 NMS？", "重复题目", "U-Net 的跳跃连接有什么用？",
                 "BN 训练和推理有什么区别？"],
            )
            self.assertEqual(result["items"][1]["ref"], ["牛客版本"])
            self.assertNotIn("errors", result)

    def test_corrupt_file_is_skipped_and_others_still_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "nowcoder_bank.json", [
                {"cat": "Nowcoder", "from": "牛客网", "q": "有效题", "ref": ["a"]},
            ])
            knowledge = root / "InterviewMonkey" / "knowledge"
            (knowledge / "wechat_bank.json").write_text("{bad json", encoding="utf-8")
            api = Api.__new__(Api)
            with patch.dict(os.environ, {"APPDATA": directory}):
                result = api.get_practice_bank()
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["items"][0]["q"], "有效题")
            self.assertEqual(len(result["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
