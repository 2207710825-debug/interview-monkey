"""YAML 热词、别名纠偏与知识库候选术语抽取。"""
from __future__ import annotations

import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Iterable

from ..contracts import Hotword


_LATIN_TERM = re.compile(r"\b[A-Za-z][A-Za-z0-9+_.-]{1,30}\b")
_CHINESE_TERM = re.compile(r"[\u4e00-\u9fff]{2,8}")
_ZH_STOP = {"这个", "那个", "我们", "你们", "可以", "一个", "以及", "进行", "使用", "支持", "通过"}


def load_hotwords(path: str | Path) -> list[Hotword]:
    import yaml

    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    items = raw if isinstance(raw, list) else raw.get("hotwords", [])
    if not isinstance(items, list):
        raise ValueError("词库顶层必须是列表，或包含 hotwords 列表")
    result: list[Hotword] = []
    seen_terms: set[str] = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or "term" not in item:
            raise ValueError(f"词库第 {index} 项缺少 term")
        term = str(item["term"]).strip()
        key = term.casefold()
        if key in seen_terms:
            raise ValueError(f"词库存在重复术语: {term}")
        aliases: list[str] = []
        seen_aliases: set[str] = set()
        for value in item.get("aliases", []) or []:
            alias = str(value).strip()
            alias_key = alias.casefold()
            if alias and alias_key != key and alias_key not in seen_aliases:
                aliases.append(alias)
                seen_aliases.add(alias_key)
        result.append(Hotword(
            term=term,
            weight=int(item.get("weight", 5)),
            aliases=tuple(aliases),
            category=str(item.get("category", "")).strip(),
        ))
        seen_terms.add(key)
    return result


def materialize_decoder_hotwords(
    hotwords: Iterable[Hotword],
    *,
    min_weight: int = 6,
) -> Path | None:
    """把正式词库中的高权重术语同步为 sherpa 可读取的 UTF-8 文件。"""
    terms = list(dict.fromkeys(
        word.term.strip() for word in hotwords
        if (
            word.weight >= min_weight
            and word.term.strip()
            # 当前 sherpa 词表不含连字符，也无法把以数字开头的短语编码为热词。
            # 这些词仍保留在文本级 aliases 中，只不传给解码器，避免启动警告。
            and "-" not in word.term
            and not word.term[0].isdigit()
        )
    ))
    if not terms:
        return None
    target = Path(tempfile.gettempdir()) / "interviewmonkey_asr" / "glossary_hotwords.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(terms) + "\n"
    if not target.is_file() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
    return target


def _pinyin_key(text: str) -> str:
    try:
        from pypinyin import lazy_pinyin
        return "".join(lazy_pinyin(text)).lower()
    except Exception:
        return text.casefold()


def _edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


class HotwordCorrector:
    def __init__(self, hotwords: Iterable[Hotword]):
        self.hotwords = tuple(hotwords)

    def correct(self, text: str) -> str:
        result = text or ""
        for hotword in self.hotwords:
            # 先替换长 alias，避免“U B”抢先吃掉“U B 八”的前半段。
            for alias in sorted(hotword.aliases, key=len, reverse=True):
                result = re.sub(re.escape(alias), hotword.term, result, flags=re.IGNORECASE)
        # 阶段 5 只执行用户可审计的显式 aliases；拼音模糊替换留给阶段 6 的
        # 带阈值与候选记录流水线，避免大词库下出现“先验框 -> 先锚框”类误伤。
        return result


def extract_hotword_candidates(texts: Iterable[str], limit: int = 30) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(term for term in _LATIN_TERM.findall(text) if len(term) > 1)
        counts.update(term for term in _CHINESE_TERM.findall(text) if term not in _ZH_STOP)
    # 频次相同时优先较长术语，便于人工确认更具体的候选。
    return sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))[:limit]
