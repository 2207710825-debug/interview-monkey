"""正式领域词库、候选提取与用户确认后的别名维护。"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from ..contracts import Hotword
from .hotwords import load_hotwords


_LOCK = threading.RLock()
_LATIN_TERM = re.compile(r"\b[A-Za-z][A-Za-z0-9+_.-]{1,30}(?:\s+[A-Za-z][A-Za-z0-9+_.-]{1,30}){0,2}\b")
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]{2,24}")
_MARKDOWN = re.compile(r"[`*_>#\[\]()]|「|」")
_HEADING = re.compile(r"(?m)^#{1,6}\s*([^\r\n#]{2,30})")
_LABEL = re.compile(r"(?:^|[。；;\r\n])\s*([^：:\r\n]{2,20})[：:]")
_QUOTED = re.compile(r"[「“《]([^」”》]{2,20})[」”》]")
_BOUNDARY_STOP = set("的是了和与在把被及或而并对将为以中上下来可会能要时后前这那你我他它其")
_COMMON_STOP = {
    "这个", "那个", "我们", "你们", "可以", "一个", "以及", "进行", "使用", "支持", "通过",
    "什么", "怎么", "如何", "主要", "相关", "内容", "系统", "功能", "问题", "例如", "不会", "所有",
}


def _atomic_yaml(path: Path, value) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def append_alias(path: str | Path, term: str, alias: str) -> bool:
    """只在用户确认后写入 alias；不允许覆盖其他术语或别名。"""
    import yaml

    glossary_path = Path(path)
    term = (term or "").strip()
    alias = (alias or "").strip()
    if not term or not alias:
        raise ValueError("term 和 alias 不能为空")
    if len(term) > 100 or len(alias) > 100:
        raise ValueError("term 或 alias 过长")
    if term.casefold() == alias.casefold():
        return False

    with _LOCK:
        raw = yaml.safe_load(glossary_path.read_text(encoding="utf-8")) or []
        items = raw.get("hotwords", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("正式词库格式错误")
        target = None
        for item in items:
            if not isinstance(item, dict):
                continue
            current_term = str(item.get("term", "")).strip()
            current_aliases = [str(value).strip() for value in item.get("aliases", []) or []]
            occupied = {current_term.casefold(), *(value.casefold() for value in current_aliases)}
            if alias.casefold() in occupied and current_term.casefold() != term.casefold():
                raise ValueError(f"alias 已属于其他术语: {current_term}")
            if current_term.casefold() == term.casefold():
                target = item
        if target is None:
            raise KeyError(f"正式词库中不存在术语: {term}")
        aliases = [str(value).strip() for value in target.get("aliases", []) or []]
        if alias.casefold() in {value.casefold() for value in aliases}:
            return False
        aliases.append(alias)
        target["aliases"] = aliases
        _atomic_yaml(glossary_path, raw)
    return True


def record_correction(path: str | Path, raw: str, corrected: str) -> dict[str, str]:
    """在本机记录人工纠错对，不保存音频，也不向日志输出正文。"""
    raw = (raw or "").strip()
    corrected = (corrected or "").strip()
    if not raw or not corrected or raw == corrected:
        raise ValueError("raw 与 corrected 必须非空且不同")
    if len(raw) > 4000 or len(corrected) > 4000:
        raise ValueError("纠错文本过长")
    item = {
        "id": uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "raw": raw,
        "corrected": corrected,
    }
    output = Path(path)
    with _LOCK:
        output.parent.mkdir(parents=True, exist_ok=True)
        previous = output.read_text(encoding="utf-8") if output.is_file() else ""
        temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
        temporary.write_text(previous + json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, output)
    return item


def suggest_alias(raw: str, corrected: str, hotwords: list[Hotword]) -> dict[str, str] | None:
    """从人工修改的差异中找出“已知正式术语 <- 原误识片段”的单个建议。"""
    matcher = SequenceMatcher(None, raw.casefold(), corrected.casefold(), autojunk=False)
    opcodes = matcher.get_opcodes()
    for word in sorted(hotwords, key=lambda item: (-len(item.term), -item.weight)):
        term_key = word.term.casefold()
        corrected_start = corrected.casefold().find(term_key)
        if corrected_start < 0 or term_key in raw.casefold():
            continue
        corrected_end = corrected_start + len(word.term)
        source_spans = [
            (i1, i2)
            for _tag, i1, i2, j1, j2 in opcodes
            if j1 < corrected_end and j2 > corrected_start
        ]
        if not source_spans:
            continue
        source_start = min(span[0] for span in source_spans)
        source_end = max(span[1] for span in source_spans)
        alias = raw[source_start:source_end].strip(" ，。！？、；：,.!?;:")
        if not alias or alias.casefold() == term_key:
            continue
        if alias.casefold() in {value.casefold() for value in word.aliases}:
            continue
        return {"term": word.term, "alias": alias}
    return None


def _document_terms(text: str) -> Counter[str]:
    terms: Counter[str] = Counter()
    for pattern in (_HEADING, _LABEL, _QUOTED):
        for match in pattern.findall(text):
            term = re.sub(r"\s+", " ", match).strip(" ，。！？、；：,.!?;:")
            if term and term not in _COMMON_STOP:
                terms[term] += 4
    cleaned = _MARKDOWN.sub(" ", text)
    for match in _LATIN_TERM.findall(cleaned):
        term = re.sub(r"\s+", " ", match).strip()
        if len(term) > 1:
            terms[term] += 1
    for run in _CHINESE_RUN.findall(cleaned):
        if 2 <= len(run) <= 8 and run not in _COMMON_STOP:
            terms[run] += 2
        for width in range(2, min(12, len(run)) + 1):
            for start in range(len(run) - width + 1):
                term = run[start:start + width]
                if term in _COMMON_STOP or term[0] in _BOUNDARY_STOP or term[-1] in _BOUNDARY_STOP:
                    continue
                terms[term] += 1
    return terms


def extract_tfidf_candidates(
    knowledge_root: str | Path,
    glossary_path: str | Path,
    *,
    limit: int = 80,
) -> list[dict]:
    """从本地 Markdown/TXT 知识库抽取待人工确认的 TF-IDF 候选。"""
    root = Path(knowledge_root)
    documents = sorted(path for path in root.rglob("*") if path.suffix.lower() in {".md", ".txt"})
    per_document: list[tuple[Path, Counter[str]]] = [
        (path, _document_terms(path.read_text(encoding="utf-8", errors="ignore")))
        for path in documents
    ]
    existing = load_hotwords(glossary_path)
    excluded = {
        value.casefold()
        for word in existing
        for value in (word.term, *word.aliases)
    }
    document_frequency: Counter[str] = Counter()
    sources: dict[str, set[str]] = defaultdict(set)
    for path, counts in per_document:
        for term in counts:
            document_frequency[term.casefold()] += 1
            sources[term.casefold()].add(path.name)

    total_documents = max(1, len(per_document))
    scored: dict[str, tuple[str, float, int]] = {}
    for path, counts in per_document:
        for term, frequency in counts.items():
            key = term.casefold()
            if key in excluded or len(term.strip()) < 2 or frequency < 2:
                continue
            tf = 1.0 + math.log(max(1, frequency))
            idf = math.log((1.0 + total_documents) / (1.0 + document_frequency[key])) + 1.0
            score = tf * idf * (1.0 + min(len(term), 8) * 0.03)
            previous = scored.get(key)
            if previous is None or score > previous[1]:
                scored[key] = (term, score, document_frequency[key])

    # 字符 n-gram 会同时产生“视觉算法工程师 / 法工程师 / 工程师”等嵌套片段。
    # 先按长度保留信息更完整的候选，再按 TF-IDF 排序，降低人工审核噪声。
    maximal: list[tuple[str, float, int]] = []
    for candidate in sorted(scored.values(), key=lambda item: (-len(item[0]), -item[1], item[0])):
        term, _score, frequency = candidate
        key = term.casefold()
        if any(
            key in accepted_term.casefold()
            and sources[key] <= sources[accepted_term.casefold()]
            and frequency <= accepted_frequency
            for accepted_term, _accepted_score, accepted_frequency in maximal
        ):
            continue
        maximal.append(candidate)
    ranked = sorted(maximal, key=lambda item: (-item[1], -len(item[0]), item[0]))[:limit]
    return [
        {
            "term": term,
            "score": round(score, 4),
            "document_frequency": frequency,
            "sources": sorted(sources[term.casefold()]),
            "status": "pending",
        }
        for term, score, frequency in ranked
    ]


def write_candidate_file(path: str | Path, candidates: list[dict]) -> None:
    _atomic_yaml(Path(path), {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review_required": True,
        "note": "仅供人工确认；不会自动写入 config/glossary.yaml。",
        "candidates": candidates,
    })
