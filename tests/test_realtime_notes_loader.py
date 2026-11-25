# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.realtime.api.app import _load_notes_chunks


class TestNotesLoader(unittest.TestCase):
    def test_loads_notes_index_from_appdata(self):
        with tempfile.TemporaryDirectory() as directory:
            knowledge = Path(directory) / "InterviewMonkey" / "knowledge"
            knowledge.mkdir(parents=True)
            (knowledge / "notes.jsonl").write_text(
                json.dumps({
                    "id": "n1", "text": "结构光标定：相机内参、投影仪内参、外参。",
                    "source": "39期课程复习笔记/3D学习笔记/结构光.md",
                    "section": "结构光标定",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"APPDATA": directory}):
                chunks = _load_notes_chunks()
            self.assertIsNotNone(chunks)
            self.assertEqual(len(chunks), 1)
            self.assertIn("结构光标定", chunks[0].text)
            self.assertEqual(chunks[0].section, "结构光标定")

    def test_loads_wechat_notes_alongside_personal_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            knowledge = Path(directory) / "InterviewMonkey" / "knowledge"
            knowledge.mkdir(parents=True)
            (knowledge / "notes.jsonl").write_text(
                json.dumps({
                    "id": "n1", "text": "个人笔记。",
                    "source": "个人笔记.md", "section": "个人笔记",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (knowledge / "wechat_notes.jsonl").write_text(
                json.dumps({
                    "id": "w1", "text": "U-Net 的跳跃连接把编码器特征拼到解码器。",
                    "source": "微信资料/深度学习与UNet学习笔记.md",
                    "section": "深度学习与 U-Net 学习笔记 > 5. U-Net 详细原理",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"APPDATA": directory}):
                chunks = _load_notes_chunks()
            self.assertIsNotNone(chunks)
            self.assertEqual(len(chunks), 2)
            self.assertEqual(
                sorted(chunk.id for chunk in chunks), ["n1", "w1"]
            )
            self.assertTrue(any("U-Net" in chunk.text for chunk in chunks))

    def test_missing_notes_index_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"APPDATA": directory}):
                self.assertIsNone(_load_notes_chunks())


if __name__ == "__main__":
    unittest.main()
