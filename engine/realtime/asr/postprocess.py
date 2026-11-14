"""ASR final 后、问题检测前的可审计文本后处理流水线。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, AsyncIterable, AsyncIterator, Iterable, Mapping, Sequence

from ..contracts import Hotword, Transcript, TranscriptKind


_CJK = re.compile(r"^[\u4e00-\u9fff]+$")
_EFFECTIVE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_FILLER_PREFIX = re.compile(
    r"^(?:(?:呃+|嗯+|啊+|那个|就是说|我想问一下|我想问|怎么说)"
    r"(?:\s*[，,、。！？!?；;：:]\s*|\s+|$))+"
)
_VOCAL_THAT_FILLER = re.compile(r"^(?:呃+|嗯+|啊+)[，,、。！？!?；;：:]?\s*那个(?=$|[\u4e00-\u9fff])")
_ASK_PREFIX = re.compile(r"^(?:就是说|我想问一下|我想问|怎么说)")
_REPEATED_PHRASE = re.compile(
    r"(?P<unit>[\u4e00-\u9fff]{2,8})(?P=unit)+"
)
_REPEATED_LATIN_WORD = re.compile(
    r"(?<![A-Za-z0-9])(?P<unit>[A-Za-z][A-Za-z0-9]{1,15})(?:\s+(?P=unit))+(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_SPACED_LETTERS = re.compile(r"(?<![A-Za-z])(?:[A-Za-z]\s+){1,}[A-Za-z](?![A-Za-z])")
_CHINESE_DECIMAL = re.compile(r"[零〇一二两三四五六七八九]+点[零〇一二两三四五六七八九]+")
_CHINESE_WITH_UNIT = re.compile(r"([零〇一二两三四五六七八九十百千万]+)(?=(?:个|次|年|月|日|点|分|秒|毫秒|元|块|%))")
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_DIGITS = {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


@dataclass(frozen=True, slots=True)
class PostprocessResult:
    raw_text: str
    corrected_text: str
    corrections: tuple[Mapping[str, Any], ...]
    suspicious: tuple[Mapping[str, Any], ...]
    is_fragment: bool


def _change(raw: str, corrected: str, score: float, reason: str) -> dict[str, Any]:
    return {
        "raw": raw,
        "corrected": corrected,
        "score": round(float(score), 4),
        "reason": reason,
    }


def clean_colloquial(text: str) -> tuple[str, tuple[Mapping[str, Any], ...]]:
    """去掉句首口癖并折叠常见重复片段，不改写其他语义。"""
    original = (text or "").strip()
    result = original
    while True:
        updated = _VOCAL_THAT_FILLER.sub("", result, count=1)
        if updated == result:
            updated = _ASK_PREFIX.sub("", result, count=1)
        if updated == result:
            updated = _FILLER_PREFIX.sub("", result, count=1)
        updated = updated.lstrip(" ，,、。！？!?；;：:")
        if updated == result:
            break
        result = updated
    result = _REPEATED_PHRASE.sub(lambda match: match.group("unit"), result)
    result = _REPEATED_LATIN_WORD.sub(lambda match: match.group("unit"), result)
    result = re.sub(r"\s+", " ", result).strip()
    changes = () if result == original else (_change(original, result, 1.0, "filler_cleanup"),)
    return result, changes


def _chinese_integer(value: str) -> str:
    if not any(char in _UNITS for char in value):
        return "".join(_DIGITS.get(char, char) for char in value)
    total = section = number = 0
    for char in value:
        if char in _DIGITS:
            number = int(_DIGITS[char])
            continue
        unit = _UNITS[char]
        if unit == 10000:
            section = (section + number) * unit
            total += section
            section = number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return str(total + section + number)


def normalize_itn(text: str) -> tuple[str, tuple[Mapping[str, Any], ...]]:
    """轻量 ITN：字母缩写、常用中文数字/小数和模型版本号。"""
    original = text or ""
    result = _SPACED_LETTERS.sub(lambda match: re.sub(r"\s+", "", match.group(0)), original)
    spoken_digit = re.compile(r"(?i)\bYOLO\s*[vV]\s*([零〇一二两三四五六七八九])")
    result = spoken_digit.sub(lambda match: f"YOLOv{_DIGITS[match.group(1)]}", result)

    def decimal(match: re.Match[str]) -> str:
        left, right = match.group(0).split("点", 1)
        return f"{_chinese_integer(left)}.{_chinese_integer(right)}"

    result = _CHINESE_DECIMAL.sub(decimal, result)
    result = _CHINESE_WITH_UNIT.sub(lambda match: _chinese_integer(match.group(1)), result)
    result = re.sub(r"(?i)\bYOLO\s*V\s*(\d+)\b", lambda match: f"YOLOv{match.group(1)}", result)
    result = re.sub(r"(?i)\bUSB\s*(\d+)\s*[.点]\s*(\d+)\b", r"USB \1.\2", result)
    changes = () if result == original else (_change(original, result, 1.0, "itn"),)
    return result, changes


def _alias_pattern(alias: str) -> re.Pattern[str]:
    parts = alias.split()
    source = r"\s*".join(re.escape(part) for part in parts) if len(parts) > 1 else re.escape(alias)
    return re.compile(source, re.IGNORECASE)


def apply_explicit_aliases(
    text: str,
    hotwords: Iterable[Hotword],
) -> tuple[str, tuple[Mapping[str, Any], ...]]:
    result = text or ""
    changes: list[Mapping[str, Any]] = []
    for word in hotwords:
        for alias in sorted(word.aliases, key=len, reverse=True):
            pattern = _alias_pattern(alias)

            def replace(match: re.Match[str], target=word.term) -> str:
                raw = match.group(0)
                if raw.casefold() != target.casefold():
                    changes.append(_change(raw, target, 1.0, "glossary_alias"))
                return target

            result = pattern.sub(replace, result)
    return result, tuple(changes)


def _normalize_initial(value: str) -> str:
    groups = {
        "g": "gk", "k": "gk", "f": "fh", "h": "fh",
        "z": "z_zh", "zh": "z_zh", "c": "c_ch", "ch": "c_ch",
        "s": "s_sh", "sh": "s_sh",
    }
    return groups.get(value, value)


def _normalize_final(value: str) -> str:
    groups = {
        "an": "an_ang", "ang": "an_ang", "en": "en_eng", "eng": "en_eng",
        "in": "in_ing", "ing": "in_ing", "ian": "ian_iang", "iang": "ian_iang",
        "uan": "uan_uang", "uang": "uan_uang",
    }
    return groups.get(value, value)


@lru_cache(maxsize=4096)
def _phonemes(text: str) -> tuple[str, ...]:
    try:
        from pypinyin import Style, lazy_pinyin

        initials = lazy_pinyin(text, style=Style.INITIALS, strict=False, errors="ignore")
        finals = lazy_pinyin(text, style=Style.FINALS, strict=False, errors="ignore")
    except Exception:
        return ()
    return tuple(
        f"{_normalize_initial(initial)}:{_normalize_final(final)}"
        for initial, final in zip(initials, finals)
    )


def infer_category(text: str, hotwords: Iterable[Hotword]) -> str:
    scores: dict[str, int] = {}
    folded = (text or "").casefold()
    for word in hotwords:
        if word.category and word.term.casefold() in folded:
            scores[word.category] = scores.get(word.category, 0) + word.weight
    return max(scores, key=scores.get) if scores else ""


def fuzzy_hotword_correction(
    text: str,
    hotwords: Iterable[Hotword],
    *,
    threshold: float = 0.84,
    suspicious_threshold: float = 0.68,
    category: str = "",
) -> tuple[str, tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    """以拼音、字形、权重和类别综合打分；低于阈值绝不替换。"""
    words = tuple(hotwords)
    if not category:
        category = infer_category(text, words)
    protected = {
        value.casefold()
        for word in words
        for value in (word.term, *word.aliases)
    }
    matches: list[dict[str, Any]] = []
    for word in words:
        if word.weight < 6 or len(word.term) < 3 or not _CJK.fullmatch(word.term):
            continue
        target_key = word.term.casefold()
        if target_key in text.casefold():
            continue
        target_phonemes = _phonemes(word.term)
        if not target_phonemes:
            continue
        for width in range(max(2, len(word.term) - 1), len(word.term) + 2):
            for start in range(0, len(text) - width + 1):
                candidate = text[start:start + width]
                if not _CJK.fullmatch(candidate) or candidate.casefold() in protected:
                    continue
                candidate_phonemes = _phonemes(candidate)
                if not candidate_phonemes:
                    continue
                pinyin_score = SequenceMatcher(None, candidate_phonemes, target_phonemes).ratio()
                glyph_score = SequenceMatcher(None, candidate, word.term).ratio()
                category_score = 1.0 if category and category == word.category else 0.0
                score = (
                    0.65 * pinyin_score
                    + 0.15 * glyph_score
                    + 0.15 * (word.weight / 10.0)
                    + 0.05 * category_score
                )
                if score >= suspicious_threshold:
                    matches.append({
                        "start": start,
                        "end": start + width,
                        "raw": candidate,
                        "corrected": word.term,
                        "score": round(score, 4),
                        "reason": "pinyin_fuzzy",
                    })

    selected: list[dict[str, Any]] = []
    for item in sorted(matches, key=lambda value: (-value["score"], value["start"], -len(value["corrected"]))):
        if item["score"] < threshold:
            continue
        if any(item["start"] < kept["end"] and item["end"] > kept["start"] for kept in selected):
            continue
        selected.append(item)
    result = text
    for item in sorted(selected, key=lambda value: value["start"], reverse=True):
        result = result[:item["start"]] + item["corrected"] + result[item["end"]:]

    corrections = tuple(
        _change(item["raw"], item["corrected"], item["score"], item["reason"])
        for item in sorted(selected, key=lambda value: value["start"])
    )
    suspicious = tuple(
        _change(item["raw"], item["corrected"], item["score"], "pinyin_candidate")
        for item in sorted(matches, key=lambda value: -value["score"])
        if item["score"] < threshold
    )[:3]
    return result, corrections, suspicious


class TranscriptPostprocessor:
    def __init__(
        self,
        hotwords: Iterable[Hotword],
        *,
        fuzzy_threshold: float = 0.84,
        suspicious_threshold: float = 0.68,
        fragment_min_chars: int = 4,
    ):
        self.hotwords = tuple(hotwords)
        self.fuzzy_threshold = fuzzy_threshold
        self.suspicious_threshold = suspicious_threshold
        self.fragment_min_chars = fragment_min_chars

    def process(self, text: str) -> PostprocessResult:
        raw = (text or "").strip()
        cleaned, clean_changes = clean_colloquial(raw)
        normalized, itn_changes = normalize_itn(cleaned)
        aliased, alias_changes = apply_explicit_aliases(normalized, self.hotwords)
        corrected, fuzzy_changes, suspicious = fuzzy_hotword_correction(
            aliased,
            self.hotwords,
            threshold=self.fuzzy_threshold,
            suspicious_threshold=self.suspicious_threshold,
        )
        effective_chars = len(_EFFECTIVE.findall(corrected))
        return PostprocessResult(
            raw,
            corrected,
            clean_changes + itn_changes + alias_changes + fuzzy_changes,
            suspicious,
            effective_chars < self.fragment_min_chars,
        )


class PostprocessingASRProvider:
    """保持 ASRProvider 接口不变，只改写 final 并保留 raw/changes。"""
    def __init__(self, provider, processor: TranscriptPostprocessor):
        self.provider = provider
        self.processor = processor

    async def _ensure_models(self) -> None:
        warm = getattr(self.provider, "_ensure_models", None)
        if warm:
            await warm()

    def update_hotwords(self, hotwords: Iterable[Hotword]) -> None:
        words = tuple(hotwords)
        self.processor.hotwords = words
        update = getattr(self.provider, "update_hotwords", None)
        if update:
            update(words)

    async def transcribe(self, frames: AsyncIterable) -> AsyncIterator[Transcript]:
        async for transcript in self.provider.transcribe(frames):
            if transcript.kind is not TranscriptKind.FINAL:
                yield transcript
                continue
            result = self.processor.process(transcript.text)
            from dataclasses import replace

            yield replace(
                transcript,
                text=result.corrected_text,
                raw_text=transcript.raw_text or transcript.text,
                corrected=transcript.corrected or result.corrected_text != result.raw_text,
                corrections=result.corrections,
                suspicious=result.suspicious,
                is_fragment=result.is_fragment,
                preprocess_text=transcript.text,
            )


class LocalLLMTranscriptCorrector:
    """严格 JSON 的可选小模型纠错；调用方负责 300ms 超时。"""
    def __init__(self, llm_provider, hotwords: Iterable[Hotword], confidence_threshold: float = 0.7):
        self.llm_provider = llm_provider
        self.hotwords = tuple(hotwords)
        self.confidence_threshold = confidence_threshold

    async def correct(
        self,
        text: str,
        candidates: Sequence[Mapping[str, Any]],
        recent: Sequence[str] = (),
        *,
        raw_text: str | None = None,
        asr_alternatives: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        if not candidates:
            return None
        allowed_terms = {word.term.casefold() for word in self.hotwords}
        payload = {
            "original": text,
            "raw_asr": raw_text or text,
            "asr_alternatives": [value for value in asr_alternatives if value][:3],
            "asr_nbest": [],
            "glossary_candidates": list(candidates)[:3],
            "recent_context": list(recent)[-3:],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你只修正语音识别中的同音或近音错误，不得改写语义或添加信息。"
                    "raw_asr 是未经后处理的识别文本，asr_alternatives 是不同识别遍次的候选；"
                    "asr_nbest 为空时不得臆造候选。"
                    "只输出JSON：{\"corrected\":\"...\",\"changes\":[{\"from\":\"\",\"to\":\"\","
                    "\"reason\":\"\"}],\"confidence\":0.0}。无法确定时原样返回且confidence低于0.7。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        parts: list[str] = []
        async for piece in self.llm_provider.stream(messages):
            parts.append(piece)
        match = _JSON_OBJECT.search("".join(parts))
        if not match:
            return None
        try:
            result = json.loads(match.group(0))
            corrected = str(result.get("corrected", "")).strip()
            confidence = float(result.get("confidence", 0.0))
            changes = list(result.get("changes") or [])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not corrected or confidence < self.confidence_threshold or corrected == text:
            return None
        reconstructed = text
        for change in changes:
            source = str(change.get("from", ""))
            target = str(change.get("to", ""))
            if not source or source not in reconstructed or target.casefold() not in allowed_terms:
                return None
            reconstructed = reconstructed.replace(source, target, 1)
        if not changes or reconstructed != corrected:
            return None
        return {"corrected": corrected, "changes": changes, "confidence": confidence}
