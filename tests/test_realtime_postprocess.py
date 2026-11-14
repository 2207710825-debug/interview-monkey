# -*- coding: utf-8 -*-
import asyncio
import unittest
from pathlib import Path

from engine.realtime.asr.hotwords import load_hotwords
from engine.realtime.asr.postprocess import (
    LocalLLMTranscriptCorrector,
    PostprocessingASRProvider,
    TranscriptPostprocessor,
    clean_colloquial,
    fuzzy_hotword_correction,
    normalize_itn,
)
from engine.realtime.contracts import Channel, Hotword, Transcript, TranscriptKind
from engine.realtime.events import AsyncEventBus
from engine.realtime.llm.service import RealtimeAnswerService
from engine.realtime.mocks import MockLLMProvider
from engine.realtime.orchestrator import RealtimePipeline
from engine.realtime.qa.detector import RuleQuestionDetector


ROOT = Path(__file__).resolve().parents[1]


class FakeASRProvider:
    async def transcribe(self, _frames):
        yield Transcript(TranscriptKind.PARTIAL, "嗯", Channel.LOOPBACK, 0, 100, utterance_id="u1")
        yield Transcript(
            TranscriptKind.FINAL, "呃，先验狂机制是什么啊", Channel.LOOPBACK,
            0, 500, utterance_id="u1",
        )


class FakeLLM:
    def __init__(self, chunks):
        self.chunks = chunks

    async def stream(self, _messages, cancel=None):
        for chunk in self.chunks:
            yield chunk


class TestPurePostprocessing(unittest.TestCase):
    def setUp(self):
        self.hotwords = load_hotwords(ROOT / "config" / "glossary.yaml")

    def test_colloquial_cleaning_and_duplicate_folding(self):
        cleaned, changes = clean_colloquial("嗯，呃，那个，但是但是我还是不明白")
        self.assertEqual(cleaned, "但是我还是不明白")
        self.assertEqual(changes[0]["reason"], "filler_cleanup")
        self.assertEqual(clean_colloquial("呃，那个先验框怎么生成")[0], "先验框怎么生成")
        self.assertEqual(clean_colloquial("嗯，我想问一下USB 有什么区别")[0], "USB 有什么区别")

    def test_itn_letter_names_chinese_digits_and_model_version(self):
        self.assertEqual(normalize_itn("U S B 三点零和 USB 二点零")[0], "USB 3.0和 USB 2.0")
        self.assertEqual(normalize_itn("YOLO v 八")[0], "YOLOv8")
        self.assertEqual(normalize_itn("第十二次训练")[0], "第12次训练")

    def test_explicit_alias_and_fuzzy_correction_report_changes(self):
        processor = TranscriptPostprocessor(self.hotwords, fuzzy_threshold=0.86)
        result = processor.process("呃，先验狂机制是什么啊")
        self.assertEqual(result.corrected_text, "先验框机制是什么啊")
        self.assertEqual(result.raw_text, "呃，先验狂机制是什么啊")
        self.assertTrue(result.corrections)
        self.assertFalse(result.is_fragment)

        word = Hotword("梯度消失", 9, (), "训练优化")
        corrected, changes, _ = fuzzy_hotword_correction(
            "踢度消失怎么产生", [word], threshold=0.86, suspicious_threshold=0.68,
        )
        self.assertEqual(corrected, "梯度消失怎么产生")
        self.assertEqual(changes[0]["raw"], "踢度消失")
        self.assertGreaterEqual(changes[0]["score"], 0.86)

    def test_below_threshold_never_replaces(self):
        word = Hotword("梯度消失", 6, (), "训练优化")
        corrected, changes, candidates = fuzzy_hotword_correction(
            "踢度消失怎么产生", [word], threshold=0.99, suspicious_threshold=0.68,
        )
        self.assertEqual(corrected, "踢度消失怎么产生")
        self.assertEqual(changes, ())
        self.assertTrue(candidates)

    def test_short_cleaned_text_is_a_fragment(self):
        processor = TranscriptPostprocessor(self.hotwords, fragment_min_chars=4)
        result = processor.process("嗯，呃")
        self.assertEqual(result.corrected_text, "")
        self.assertTrue(result.is_fragment)

    def test_question_detector_recognizes_zenyang_questions(self):
        async def check():
            detector = RuleQuestionDetector()
            transcript = Transcript(
                TranscriptKind.FINAL,
                "模型量化分别怎样提升推理速度",
                Channel.LOOPBACK,
                0,
                1000,
            )
            decision = await detector.evaluate(transcript, [])
            self.assertTrue(decision.is_question)

        asyncio.run(check())


class TestPostprocessingIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_provider_preserves_partial_and_enriches_final(self):
        processor = TranscriptPostprocessor(load_hotwords(ROOT / "config" / "glossary.yaml"))
        provider = PostprocessingASRProvider(FakeASRProvider(), processor)

        async def source():
            if False:
                yield None

        results = [item async for item in provider.transcribe(source())]
        self.assertEqual(results[0].text, "嗯")
        self.assertEqual(results[0].kind, TranscriptKind.PARTIAL)
        self.assertEqual(results[1].text, "先验框机制是什么啊")
        self.assertEqual(results[1].raw_text, "呃，先验狂机制是什么啊")
        self.assertEqual(results[1].preprocess_text, "呃，先验狂机制是什么啊")
        self.assertTrue(results[1].corrected)
        self.assertTrue(results[1].corrections)

    async def test_fragments_stay_in_context_but_skip_question_detection(self):
        class Source:
            async def frames(self):
                if False:
                    yield None

        class VAD:
            def __init__(self):
                self.on_boundary = None

            async def segment(self, frames):
                async for frame in frames:
                    yield frame

        class ASR:
            async def transcribe(self, _frames):
                yield Transcript(
                    TranscriptKind.FINAL, "嗯", Channel.LOOPBACK, 0, 100,
                    is_fragment=True, utterance_id="fragment-1",
                )

        class Detector:
            async def evaluate(self, *_args):
                raise AssertionError("碎片不应进入问题检测")

        class AnswerService:
            async def cancel(self, _session_id):
                return None

            async def stream_answer(self, *_args, **_kwargs):
                if False:
                    yield None

        bus = AsyncEventBus()
        pipeline = RealtimePipeline(VAD(), ASR(), Detector(), AnswerService(), bus)
        await pipeline.run(Source(), "fragment-test")
        self.assertEqual(pipeline.context.recent()[0].text, "嗯")

    async def test_llm_corrector_accepts_only_declared_glossary_replacements(self):
        word = Hotword("先验框", 8, (), "目标检测")
        valid = LocalLLMTranscriptCorrector(
            FakeLLM(['{"corrected":"先验框机制是什么", "changes":[{"from":"先验狂", "to":"先验框", "reason":"近音术语"}], "confidence":0.91}']),
            [word],
        )
        result = await valid.correct("先验狂机制是什么", [{"raw": "先验狂", "corrected": "先验框", "score": 0.72}])
        self.assertEqual(result["corrected"], "先验框机制是什么")

        invalid = LocalLLMTranscriptCorrector(
            FakeLLM(['{"corrected":"请详细解释先验框机制", "changes":[], "confidence":0.99}']),
            [word],
        )
        self.assertIsNone(await invalid.correct("先验狂机制", [{"raw": "先验狂", "corrected": "先验框"}]))

    async def test_llm_corrector_receives_raw_and_first_pass_alternative(self):
        captured = {}

        class CapturingLLM:
            async def stream(self, messages, cancel=None):
                import json

                captured.update(json.loads(messages[1]["content"]))
                yield '{"corrected":"先验框机制", "changes":[{"from":"先验狂","to":"先验框"}], "confidence":0.9}'

        corrector = LocalLLMTranscriptCorrector(CapturingLLM(), [Hotword("先验框", 8)])
        await corrector.correct(
            "先验狂机制",
            [{"raw": "先验狂", "corrected": "先验框"}],
            ["最近的问题"],
            raw_text="鲜艳矿机制",
            asr_alternatives=["先验框机制"],
        )
        self.assertEqual(captured["raw_asr"], "鲜艳矿机制")
        self.assertEqual(captured["asr_alternatives"], ["先验框机制"])
        self.assertEqual(captured["recent_context"], ["最近的问题"])

    async def test_retrieval_runs_while_optional_llm_correction_runs(self):
        started = asyncio.Event()

        class Retriever:
            async def retrieve(self, _query, **_kwargs):
                started.set()
                return []

        class Corrector:
            async def correct(self, _text, _candidates, _recent, **_kwargs):
                if not started.is_set():
                    raise AssertionError("检索与纠错没有并行启动")
                return {
                    "corrected": "先验框机制是什么",
                    "changes": [{"from": "先验狂", "to": "先验框", "reason": "近音"}],
                    "confidence": 0.9,
                }

        service = RealtimeAnswerService(
            Retriever(), MockLLMProvider(["回答"]), ROOT / "prompts" / "answer_system.md",
            transcript_corrector=Corrector(),
        )
        events = [event async for event in service.stream_answer(
            "先验狂机制是什么",
            suspicious_candidates=[{"raw": "先验狂", "corrected": "先验框"}],
        )]
        self.assertEqual(events[0].data["question"], "先验框机制是什么")

    async def test_llm_timeout_falls_back_to_original_question(self):
        class Retriever:
            async def retrieve(self, _query, **_kwargs):
                return []

        class SlowCorrector:
            async def correct(self, *_args, **_kwargs):
                await asyncio.sleep(0.05)
                return {"corrected": "先验框机制是什么", "changes": [], "confidence": 0.99}

        service = RealtimeAnswerService(
            Retriever(), MockLLMProvider(["继续回答"]), ROOT / "prompts" / "answer_system.md",
            transcript_corrector=SlowCorrector(),
            correction_timeout_ms=5,
        )
        events = [event async for event in service.stream_answer(
            "先验狂机制是什么",
            suspicious_candidates=[{"raw": "先验狂", "corrected": "先验框"}],
        )]
        self.assertEqual(events[0].data["question"], "先验狂机制是什么")
        self.assertIsNone(events[0].data["llm_correction"])


if __name__ == "__main__":
    unittest.main()
