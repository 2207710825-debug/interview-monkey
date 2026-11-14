# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path

from engine.realtime.contracts import Channel, Chunk, Hotword, Transcript, TranscriptKind
from engine.realtime.rag.context import ConversationContext, PromptContextManager, approximate_tokens
from engine.realtime.rag.hybrid import HybridRetriever
from engine.realtime.rag.ingest import document_category, ingest_directory, load_document
from engine.realtime.rag.query import (
    build_query_variants,
    is_generic_technical_question,
    reciprocal_rank_fusion,
)
from engine.realtime.llm.service import RealtimeAnswerService
from engine.realtime.mocks import MockLLMProvider
from engine.realtime.rag.speculative import SpeculativeRetriever


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


class TestRealtimeRag(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = os.path.join(self.tmp.name, "kb.md")
        with open(self.doc, "w", encoding="utf-8") as handle:
            handle.write(
                "# Kafka\n\nKafka Broker 负责接收和持久化消息。副本机制提高可靠性。\n\n"
                "# RAG\n\nRAG 通过向量检索召回知识片段，再将引用交给 LLM。"
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_markdown_ingest_keeps_metadata(self):
        chunks = load_document(self.doc, chunk_tokens=30)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(chunk.id and chunk.source == self.doc for chunk in chunks))
        self.assertEqual({chunk.section for chunk in chunks}, {"Kafka", "RAG"})

    async def test_hybrid_retrieval_and_threshold(self):
        retriever = HybridRetriever(load_document(self.doc, chunk_tokens=30))
        result = await retriever.retrieve("Kafka 如何保证消息可靠", top_k=2, min_score=0.35)
        self.assertTrue(result)
        self.assertEqual(result[0].section, "Kafka")
        self.assertGreaterEqual(result[0].score, 0.35)
        missing = await retriever.retrieve("量子引力弦理论", top_k=2, min_score=0.8)
        self.assertEqual(missing, [])

    def test_query_expansion_keeps_raw_corrected_and_glossary_aliases(self):
        hotword = Hotword("先验框", 8, ("先验狂", "anchor box"), "目标检测")
        queries = build_query_variants(
            "先验框机制是什么啊",
            raw_question="先验狂机制是什么啊",
            candidates=[{"raw": "先验狂", "corrected": "先验框", "score": 0.74}],
            hotwords=[hotword],
        )
        self.assertEqual(queries[0], "先验框机制是什么啊")
        self.assertEqual(queries[1], "先验狂机制是什么啊")
        self.assertTrue(any("anchor box" in query for query in queries))

    def test_generic_technical_question_classifier_covers_domain_terms(self):
        questions = (
            "背压机制有什么作用？",
            "BM25的核心因素有哪些？",
            "TTFT表示什么？",
            "限流错误应该如何重试？",
            "说话人分离有什么用？",
            "WDA_EXCLUDEFROMCAPTURE有什么作用？",
        )
        self.assertTrue(all(is_generic_technical_question(question) for question in questions))
        self.assertFalse(is_generic_technical_question("面试猴怎么记录会议？"))

    def test_reciprocal_rank_fusion_combines_and_deduplicates_lists(self):
        first = Chunk("a", "先验框", "cv.md", 0.7)
        second = Chunk("b", "anchor box", "cv.md", 0.6)
        fused = reciprocal_rank_fusion([[first, second], [second, first], [second]], limit=2)
        self.assertEqual([chunk.id for chunk in fused], ["b", "a"])
        self.assertEqual(fused[0].score, 0.6)

    def test_ingest_assigns_and_filters_knowledge_categories(self):
        root = Path(self.tmp.name)
        category_dir = root / "计算机视觉"
        category_dir.mkdir()
        (category_dir / "检测.md").write_text("YOLO 目标检测与先验框。", encoding="utf-8")
        (root / "产品说明.md").write_text("面试猴的使用功能。", encoding="utf-8")
        (root / "算法.md").write_text("通用工程算法知识。", encoding="utf-8")

        chunks = ingest_directory(root, chunk_tokens=40)
        self.assertEqual({chunk.metadata["category"] for chunk in chunks}, {"计算机视觉", "产品", "通用"})
        selected = ingest_directory(root, chunk_tokens=40, category="计算机视觉")
        self.assertTrue(selected)
        self.assertTrue(all(chunk.metadata["category"] == "计算机视觉" for chunk in selected))
        self.assertEqual(document_category(root / "产品说明.md", root), "产品")

    async def test_hybrid_retriever_can_exclude_product_category(self):
        product = Chunk("p", "面试猴的产品功能和快捷键说明。", "产品说明.md", 0.0, metadata={"category": "产品"})
        technical = Chunk("t", "Kafka 通过副本和 ACK 保证消息可靠。", "kafka.md", 0.0, metadata={"category": "工程术语"})
        retriever = HybridRetriever([product, technical])
        results = await retriever.retrieve(
            "Kafka 如何保证消息可靠",
            top_k=4,
            min_score=0.0,
            exclude_categories=("产品",),
        )
        self.assertTrue(results)
        self.assertTrue(all(chunk.metadata["category"] != "产品" for chunk in results))

    async def test_generic_low_confidence_filters_product_docs_and_expands_queries(self):
        product = Chunk("p", "面试猴产品设置说明。", "产品说明.md", 0.62, metadata={"category": "产品资料"})
        technical = Chunk("t", "Kafka 使用副本、ACK 和持久化保证可靠性。", "kafka.md", 0.58, metadata={"category": "消息队列"})

        class SpyRetriever:
            def __init__(self):
                self.calls = []

            async def retrieve(self, query, *, exclude_categories=(), **_kwargs):
                self.calls.append((query, tuple(exclude_categories)))
                return [technical] if exclude_categories else [product]

        retriever = SpyRetriever()
        service = RealtimeAnswerService(
            retriever,
            MockLLMProvider(["回答"]),
            Path(ROOT, "prompts", "answer_system.md"),
            hotwords=[Hotword("Kafka", 8, ("卡夫卡",), "工程术语")],
            generic_product_skip_score=0.75,
        )
        chunks, metadata = await service.retrieve_context(
            "Kafka 如何保证消息可靠？",
            raw_question="卡夫卡怎么保证消息可靠？",
        )
        self.assertTrue(metadata["product_filtered"])
        self.assertTrue(any(query.startswith("卡夫卡") for query, _ in retriever.calls))
        self.assertTrue(any(excluded for _query, excluded in retriever.calls))
        self.assertEqual([chunk.id for chunk in chunks], ["t"])

    async def test_product_question_keeps_product_docs(self):
        product = Chunk("p", "面试猴会议记录功能。", "产品说明.md", 0.5, metadata={"category": "产品"})

        class ProductRetriever:
            async def retrieve(self, *_args, **_kwargs):
                return [product]

        service = RealtimeAnswerService(
            ProductRetriever(),
            MockLLMProvider(["回答"]),
            Path(ROOT, "prompts", "answer_system.md"),
        )
        chunks, metadata = await service.retrieve_context("面试猴怎么记录会议？")
        self.assertFalse(metadata["product_filtered"])
        self.assertEqual([chunk.id for chunk in chunks], ["p"])

    async def test_speculative_retrieval_reuses_stable_partial(self):
        retriever = HybridRetriever(load_document(self.doc, chunk_tokens=30))
        speculative = SpeculativeRetriever(retriever)
        speculative.prefetch("Kafka 如何保证消息可靠", 350)
        result, reused = await speculative.resolve("Kafka如何保证消息可靠？")
        self.assertTrue(reused)
        self.assertTrue(result)

    def test_context_budget_never_cuts_system_prompt(self):
        chunks = load_document(self.doc, chunk_tokens=30)
        conversation = ConversationContext()
        for index in range(12):
            conversation.append(Transcript(
                TranscriptKind.FINAL,
                "历史对话" * 40,
                Channel.LOOPBACK,
                index * 1000,
                index * 1000 + 500,
                "speaker-1",
            ))
        manager = PromptContextManager(os.path.join(ROOT, "prompts", "answer_system.md"), conversation)
        messages = manager.build("什么是Kafka", chunks, budget_tokens=900)
        original = Path(ROOT, "prompts", "answer_system.md").read_text(encoding="utf-8").strip()
        self.assertEqual(messages[0]["content"], original)
        self.assertLessEqual(sum(approximate_tokens(item["content"]) for item in messages), 900)

    def test_prompt_includes_asr_candidates_and_no_evidence_fallback(self):
        manager = PromptContextManager(os.path.join(ROOT, "prompts", "answer_system.md"))
        messages = manager.build(
            "先验框机制是什么？",
            [],
            budget_tokens=900,
            glossary_candidates=[{"raw": "先验狂", "corrected": "先验框", "score": 0.73}],
        )
        self.assertIn("ASR 词库候选", messages[1]["content"])
        self.assertIn("先验狂 → 先验框", messages[1]["content"])
        self.assertIn("asr", messages[0]["content"].lower())
        self.assertIn("通用知识", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
