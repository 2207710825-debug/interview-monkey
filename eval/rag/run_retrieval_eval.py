# -*- coding: utf-8 -*-
"""比较阶段 7 前后的检索召回；不调用 LLM，不代表答案质量评测。

用法：python -X utf8 eval/rag/run_retrieval_eval.py --json-out eval/rag/reports/phase7_retrieval.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.realtime.asr.hotwords import load_hotwords
from engine.realtime.config import load_config
from engine.realtime.llm.service import RealtimeAnswerService
from engine.realtime.rag.hybrid import HybridRetriever
from engine.realtime.rag.ingest import ingest_directory


def _contains_required(chunks, required_terms: list[str]) -> list[str]:
    text = "\n".join(chunk.text for chunk in chunks).casefold()
    return [term for term in required_terms if term.casefold() in text]


async def evaluate(dataset_path: Path) -> dict:
    cfg = load_config(ROOT / "config" / "config.yaml")
    knowledge = Path(cfg.rag.knowledge_path)
    if not knowledge.is_absolute():
        knowledge = ROOT / knowledge
    glossary = Path(cfg.asr.hotwords_path)
    if not glossary.is_absolute():
        glossary = ROOT / glossary

    chunks = ingest_directory(knowledge, cfg.rag.chunk_tokens, cfg.rag.overlap_ratio)
    retriever = HybridRetriever(chunks)
    hotwords = load_hotwords(glossary)
    service = RealtimeAnswerService(
        retriever,
        llm_provider=None,
        system_prompt_path=ROOT / "prompts" / "answer_system.md",
        top_k=cfg.rag.top_k,
        min_score=cfg.rag.min_score,
        hotwords=hotwords,
        generic_product_skip_score=cfg.rag.generic_product_skip_score,
    )
    dataset = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    baseline_rows = []
    phase7_rows = []
    for item in dataset:
        baseline = await retriever.retrieve(
            item["question"], top_k=cfg.rag.top_k, min_score=cfg.rag.min_score,
        )
        found, meta = await service.retrieve_context(item["question"])
        terms = item["required_terms"]
        baseline_rows.append({
            "id": item["id"],
            "required_terms_found": _contains_required(baseline, terms),
            "required_terms": terms,
            "chunks": [{"source": chunk.source, "category": chunk.metadata.get("category"), "score": chunk.score} for chunk in baseline],
        })
        phase7_rows.append({
            "id": item["id"],
            "required_terms_found": _contains_required(found, terms),
            "required_terms": terms,
            "queries": meta["queries"],
            "product_filtered": meta["product_filtered"],
            "chunks": [{"source": chunk.source, "category": chunk.metadata.get("category"), "score": chunk.score} for chunk in found],
        })

    def summarize(rows: list[dict]) -> dict:
        required = sum(len(row["required_terms"]) for row in rows)
        hits = sum(len(row["required_terms_found"]) for row in rows)
        return {
            "questions": len(rows),
            "questions_with_results": sum(bool(row["chunks"]) for row in rows),
            "required_term_hits": hits,
            "required_term_total": required,
            "required_term_recall": round(hits / required, 4) if required else 0.0,
            "product_filter_applied": sum(bool(row.get("product_filtered")) for row in rows),
            "product_chunks_returned": sum(
                chunk["category"] == "产品"
                for row in rows for chunk in row["chunks"]
            ),
        }

    return {
        "dataset": str(dataset_path),
        "knowledge_root": str(knowledge),
        "knowledge_files": sorted(str(path.relative_to(knowledge)) for path in knowledge.rglob("*") if path.is_file() and path.suffix.lower() in {".md", ".txt", ".pdf"}),
        "summary": {
            "single_query_baseline": summarize(baseline_rows),
            "phase7_dual_query_fusion_and_filter": summarize(phase7_rows),
        },
        "baseline": baseline_rows,
        "phase7": phase7_rows,
        "limitation": "仅评测检索片段是否包含标注术语，不调用 LLM；当前知识库文档范围会限制召回上限。",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "eval" / "eval_set.jsonl")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.dataset.resolve()))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        output = args.json_out.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(f"评测结果已写入: {output}")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(result["limitation"])


if __name__ == "__main__":
    main()
