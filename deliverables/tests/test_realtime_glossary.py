# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

import httpx
import yaml

from engine.realtime.api.app import create_app
from engine.realtime.asr.glossary import (
    append_alias,
    extract_tfidf_candidates,
    record_correction,
    suggest_alias,
    write_candidate_file,
)
from engine.realtime.asr.hotwords import HotwordCorrector, load_hotwords


ROOT = Path(__file__).resolve().parents[1]


class FakeRuntime:
    def __init__(self):
        self.reloads = 0

    def reload_hotwords(self):
        self.reloads += 1
        return 1

    def status(self):
        return {"capture": "stopped", "asr": "idle"}


class TestGlossary(unittest.TestCase):
    def test_seed_glossary_has_required_size_and_categories(self):
        hotwords = load_hotwords(ROOT / "config" / "glossary.yaml")
        self.assertGreaterEqual(len(hotwords), 150)
        self.assertEqual(len({item.term.casefold() for item in hotwords}), len(hotwords))
        categories = {item.category for item in hotwords}
        self.assertTrue({"目标检测", "训练优化", "深度学习模型", "3D视觉"} <= categories)

    def test_every_glossary_entry_has_an_explicit_regression_case(self):
        hotwords = load_hotwords(ROOT / "config" / "glossary.yaml")
        data = yaml.safe_load((ROOT / "tests" / "fixtures" / "glossary_cases.yaml").read_text(encoding="utf-8"))
        cases = data["cases"]
        cases_by_term = {str(case["term"]).casefold(): case for case in cases}
        terms = {item.term.casefold() for item in hotwords}
        missing = sorted(terms - set(cases_by_term))
        extra = sorted(set(cases_by_term) - terms)
        self.assertEqual(missing, [], f"新增词库条目必须补充 glossary_cases.yaml 用例: {missing}")
        self.assertEqual(extra, [], f"词库测试用例存在已删除的术语: {extra}")
        self.assertEqual(len(cases_by_term), len(cases), "glossary_cases.yaml 中存在重复术语用例")
        corrector = HotwordCorrector(hotwords)
        for case in cases:
            with self.subTest(term=case["term"]):
                self.assertEqual(corrector.correct(case["input"]), case["expected"])

    def test_seed_aliases_fix_known_failures(self):
        corrector = HotwordCorrector(load_hotwords(ROOT / "config" / "glossary.yaml"))
        self.assertEqual(corrector.correct("先验狂机制是什么啊"), "先验框机制是什么啊")
        self.assertEqual(corrector.correct("U B 八接口"), "USB接口")
        self.assertEqual(corrector.correct("U S B接口"), "USB接口")
        self.assertEqual(corrector.correct("YOLOV巴的结偶头"), "YOLOv8的解耦头")
        self.assertEqual(corrector.correct("ba norm和ROIline"), "BatchNorm和RoI Align")

    def test_user_alias_requires_explicit_write_and_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            glossary = Path(directory) / "glossary.yaml"
            glossary.write_text(
                "- term: 先验框\n  weight: 8\n  aliases: [先验狂]\n  category: 目标检测\n",
                encoding="utf-8",
            )
            self.assertTrue(append_alias(glossary, "先验框", "鲜艳矿"))
            self.assertFalse(append_alias(glossary, "先验框", "鲜艳矿"))
            self.assertIn("鲜艳矿", load_hotwords(glossary)[0].aliases)

    def test_correction_record_and_alias_suggestion(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "corrections.jsonl"
            item = record_correction(output, "先验旷机制是什么", "先验框机制是什么")
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["id"], item["id"])
            suggestion = suggest_alias(
                rows[0]["raw"], rows[0]["corrected"],
                load_hotwords(ROOT / "config" / "glossary.yaml"),
            )
            self.assertEqual(suggestion, {"term": "先验框", "alias": "先验旷"})

    def test_tfidf_candidates_never_modify_formal_glossary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            (knowledge / "cv.md").write_text(
                "点云配准使用局部特征。局部特征可以辅助配准，Open3D 用于点云处理。",
                encoding="utf-8",
            )
            glossary = root / "glossary.yaml"
            glossary.write_text("- term: 点云配准\n  weight: 6\n", encoding="utf-8")
            before = glossary.read_bytes()
            candidates = extract_tfidf_candidates(knowledge, glossary, limit=20)
            candidate_file = root / "glossary.candidates.yaml"
            write_candidate_file(candidate_file, candidates)
            self.assertEqual(glossary.read_bytes(), before)
            self.assertNotIn("点云配准", {item["term"] for item in candidates})
            generated = yaml.safe_load(candidate_file.read_text(encoding="utf-8"))
            self.assertTrue(generated["review_required"])
            self.assertTrue(all(item["status"] == "pending" for item in generated["candidates"]))


class TestGlossaryApi(unittest.IsolatedAsyncioTestCase):
    async def test_correction_then_confirmed_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            glossary = Path(directory) / "glossary.yaml"
            glossary.write_text(
                "- term: 先验框\n  weight: 8\n  aliases: []\n  category: 目标检测\n",
                encoding="utf-8",
            )
            runtime = FakeRuntime()
            app = create_app(service=object(), runtime=runtime, glossary_path=glossary)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                saved = await client.post("/api/glossary/corrections", json={
                    "raw": "鲜验矿机制是什么", "corrected": "先验框机制是什么",
                })
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(
                    saved.json()["alias_suggestion"],
                    {"term": "先验框", "alias": "鲜验矿"},
                )
                confirmed = await client.post("/api/glossary/aliases", json={
                    "term": "先验框", "alias": "鲜验矿",
                })
            self.assertEqual(confirmed.status_code, 200)
            self.assertTrue(confirmed.json()["added"])
            self.assertEqual(runtime.reloads, 1)
            self.assertIn("鲜验矿", load_hotwords(glossary)[0].aliases)
            self.assertTrue(glossary.with_name("glossary.corrections.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
