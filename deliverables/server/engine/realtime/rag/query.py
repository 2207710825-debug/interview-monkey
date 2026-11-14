"""RAG query expansion and rank fusion helpers."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable, Mapping, Sequence

from ..contracts import Chunk, Hotword


_QUESTION_SHAPE = re.compile(r"(什么|为何|为什么|怎么|怎样|如何|哪些|有哪些|表示|因素|原理|区别|作用|优化|可靠|实现|计算|解释)")
_TECHNICAL_TERMS = re.compile(
    r"(算法|模型|数据库|消息|队列|协议|数据|并发|缓存|索引|向量|检索|训练|推理|"
    r"视觉|音频|窗口|捕获|存储|网络|线程|分布式|一致性|哈希|嵌入|背压|限流|退避|重试|"
    r"说话人|分离|首个token|WDA|EXCLUDEFROMCAPTURE|RAG|LLM|API|Kafka|TTFT|speaker|token|"
    r"BM25|MVCC|LSM|SSE|WebSocket|WASAPI|DWM|USB|NMS)",
    re.IGNORECASE,
)
_PRODUCT_WORDS = ("面试猴", "本应用", "本软件", "本产品", "产品功能", "快捷键", "安装", "账号", "价格")


def _contains(text: str, value: str) -> bool:
    return bool(value) and value.casefold() in (text or "").casefold()


def build_query_variants(
    question: str,
    *,
    raw_question: str | None = None,
    candidates: Sequence[Mapping[str, object]] = (),
    hotwords: Iterable[Hotword] = (),
    limit: int = 8,
) -> list[str]:
    """保留原句和纠正句，再对已命中词库术语做确定性别名扩展。"""
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", (value or "")).strip()
        key = value.casefold()
        if value and key not in seen and len(variants) < limit:
            seen.add(key)
            variants.append(value)

    bases = [value for value in (question, raw_question) if value and value.strip()]
    for value in bases:
        add(value)

    candidate_terms = {
        str(item.get("corrected", "")).strip().casefold()
        for item in candidates
        if item.get("corrected")
    }
    for word in hotwords:
        searchable = (word.term, *word.aliases)
        matched = [value for value in searchable if any(_contains(base, value) for base in bases)]
        if word.term.casefold() in candidate_terms:
            matched.append(word.term)
        for source in dict.fromkeys(matched):
            for base in bases:
                if not _contains(base, source):
                    continue
                for target in (word.term, *word.aliases):
                    if target.casefold() == source.casefold():
                        continue
                    expanded = re.sub(re.escape(source), lambda _match: target, base, count=1, flags=re.IGNORECASE)
                    add(expanded)
                    if len(variants) >= limit:
                        return variants
    return variants


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[Chunk]],
    *,
    limit: int = 4,
    rank_constant: int = 60,
) -> list[Chunk]:
    """按跨查询排名融合去重；Chunk.score 保留原检索器的相关度分数。"""
    fused: dict[str, tuple[float, Chunk]] = {}
    for ranked in ranked_lists:
        seen_in_list: set[str] = set()
        for rank, chunk in enumerate(ranked, 1):
            if chunk.id in seen_in_list:
                continue
            seen_in_list.add(chunk.id)
            score, best_chunk = fused.get(chunk.id, (0.0, chunk))
            if chunk.score > best_chunk.score:
                best_chunk = chunk
            fused[chunk.id] = (score + 1.0 / (rank_constant + rank), best_chunk)
    ordered = sorted(fused.values(), key=lambda item: item[0], reverse=True)
    return [replace(chunk, score=chunk.score) for _rrf, chunk in ordered[:limit]]


def is_product_category(category: str) -> bool:
    value = (category or "").casefold()
    return "产品" in value or "常见问题" in value or "faq" in value or "product" in value


def is_generic_technical_question(question: str, hotwords: Iterable[Hotword] = ()) -> bool:
    text = (question or "").strip()
    if not text or any(marker.casefold() in text.casefold() for marker in _PRODUCT_WORDS):
        return False
    for word in hotwords:
        if not word.category or is_product_category(word.category):
            continue
        if any(_contains(text, value) for value in (word.term, *word.aliases)):
            return True
    return bool(_QUESTION_SHAPE.search(text) and _TECHNICAL_TERMS.search(text))
