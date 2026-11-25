# -*- coding: utf-8 -*-
import unittest

from engine.realtime.asr.hotwords import is_decoder_term
from engine.realtime.asr.termbank import build_termbank, merge_hotwords
from engine.realtime.contracts import Chunk, Hotword


class TestRealtimeTermBank(unittest.TestCase):
    def test_extracts_english_terms_and_filters_code_noise(self):
        chunks = [
            Chunk(
                "c1",
                "YOLO 的 Backbone、FPN、PAN、CNN 和 Conv 结构；NMS 与 BCE 损失；"
                "U-Net 与 Dice；YOLOX、MobileNet、ShuffleNet。",
                "notes.md",
                0.0,
                "目标检测",
            ),
            Chunk(
                "c2",
                "```python\nimport numpy as np\nfor i in range(10):\n    text = frac\n```\n"
                "mermaid flowchart N1 N2 只是代码噪声。",
                "notes.md",
                0.0,
                "课堂程序与代码解析",
            ),
        ]
        bank = build_termbank(chunks, limit=50)
        by_key = {word.term.casefold(): word for word in bank.hotwords}
        for term in (
            "yolo", "backbone", "fpn", "pan", "cnn", "conv", "nms", "bce",
            "unet", "dice", "yolox", "mobilenet", "shufflenet",
        ):
            self.assertIn(term, by_key)
            self.assertGreaterEqual(by_key[term].weight, 6)
        for noise in ("import", "text", "frac", "mermaid", "n1", "n2"):
            self.assertNotIn(noise, by_key)
        self.assertGreater(bank.decoder_count, 0)
        self.assertGreaterEqual(bank.unique_terms, 8)

    def test_manual_terms_win_and_low_weight_can_be_upgraded(self):
        chunks = [
            Chunk("c1", "CNN CNN CNN YOLO YOLO", "notes.md", 0.0, "检测"),
        ]
        manual = [
            Hotword("CNN", 5, ("cnn",), "目标检测"),
            Hotword("YOLO", 9, ("yolo",), "目标检测"),
        ]
        bank = build_termbank(chunks, manual, limit=20)
        by_key = {word.term.casefold(): word for word in bank.hotwords}
        self.assertEqual(by_key["yolo"].weight, 9)
        self.assertGreaterEqual(by_key["cnn"].weight, 6)
        self.assertEqual(by_key["cnn"].aliases, ("cnn",))
        self.assertEqual(by_key["cnn"].category, "目标检测")

    def test_merge_keeps_manual_aliases_when_extracted_weight_is_higher(self):
        manual = [Hotword("CNN", 5, ("cnn", "卷积网络"), "cv")]
        extracted = [Hotword("CNN", 10, (), "knowledge")]
        merged = merge_hotwords(manual, extracted)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].weight, 10)
        self.assertEqual(merged[0].aliases, ("cnn", "卷积网络"))
        self.assertEqual(merged[0].category, "cv")

    def test_no_chunks_returns_manual_only(self):
        manual = [Hotword("YOLO", 8)]
        bank = build_termbank([], manual)
        self.assertEqual(bank.hotwords, tuple(manual))
        self.assertEqual(bank.decoder_count, 1)

    def test_decoder_term_filter_matches_sherpa_token_constraints(self):
        for term in ("YOLO", "YOLOv5", "ResNet50", "Backbone", "RoI Align", "Focal Loss"):
            self.assertTrue(is_decoder_term(term), term)
        for term in ("C++", "D+1", "t-SNE", "3DGS", "a_b", "YOLOv5.0", "中文词"):
            self.assertFalse(is_decoder_term(term), term)


if __name__ == "__main__":
    unittest.main()
