"""个人笔记知识库检索器。

与通用 ``HybridRetriever`` 的区别：
- 中文按 bigram 切分，避免单字 BM25 被“的/是/什么/怎么”淹没；
- section 标题参与索引并加权；
- 过滤“自测/闭卷/岗位追问/官方纪要”等元数据段；
- 技术词扩展与相关性门槛：弱相关时返回空，让上层退回通用知识，而不是硬塞引用。
"""
from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from dataclasses import replace
from typing import Iterable, Sequence

from ..contracts import Chunk


# 这些段落是题目清单/复习计划/原始纪要，不适合作为 RAG 依据。
META_SECTION_RE = re.compile(
    r"自测|闭卷|岗位高频|课堂明确提到的面试问题|官方智能纪要原文备查|逐字稿|"
    r"复习安排|待办|目录|课程地图|课程资料清单|月度能力|项目与面试证据|"
    r"学习重点规划|速查|思维导图|来源与验证|核验|课程时间轴|作业完成|"
    r"复习状态|能力缺口|面试重点图|复盘记录|学习记录|经验/技巧|踩坑扫描|"
    r"会议主题|发言人|官方智能纪要"
)

STOP_CHARS = set(
    "的了是在和与及或有为对从到中上下请说说什怎么为何如哪些区别改进分别一下简单"
    "解释介绍问题方法原理作用优缺点把被就都也很更最这那其以及一个这个那个可以需要"
)
STOP_WORDS = {
    "什么", "怎么", "为什么", "如何", "哪些", "区别", "改进", "分别", "一下",
    "简单", "解释", "介绍", "问题", "方法", "原理", "作用", "优点", "缺点",
    "请", "说说", "以及", "一个", "这个", "那个", "可以", "需要",
}

# 查询扩展：缩写/术语 → 笔记里可能使用的同义表达。
EXPANSIONS = {
    "icp": ["迭代最近点", "点到面", "gicp", "ndt", "点云配准", "fpfh"],
    "nerf": ["神经辐射场", "体渲染"],
    "3dgs": ["高斯溅射", "3d gaussian splatting"],
    "3d gaussian splatting": ["高斯溅射", "3dgs"],
    "nms": ["非极大值抑制", "soft-nms", "diou-nms", "soft nms"],
    "batchnorm": ["批归一化", "running", "均值", "方差"],
    "bn": ["批归一化", "running", "均值", "方差"],
    "focal loss": ["类别不平衡", "难易样本", "gamma"],
    "int8": ["量化", "校准", "截断"],
    "pnp": ["位姿", "epnp", "p3p", "ransac", "重投影"],
    "yolo": ["目标检测", "正负样本", "解耦头", "anchor"],
    "roi align": ["roipooling", "双线性插值", "量化"],
    "rpn": ["候选框", "anchors", "两阶段"],
    "极线": ["立体校正", "对极"],
}

# 中文技术词：用于判断“命中的 chunk 是否真的在讲这个问题”。
ANCHOR_TERMS = (
    "结构光", "标定", "视差", "点云", "配准", "位姿", "相机内参", "相机外参",
    "语义分割", "实例分割", "小目标", "遮挡", "量化", "蒸馏", "剪枝", "注意力",
    "归一化", "损失函数", "正负样本", "解码", "后处理", "重投影", "极线",
    "特征金字塔", "数据增强", "类别不平衡", "过拟合", "正则化", "梯度消失",
)
ANCHOR_TERM_ALIASES = {
    "极线": ("立体校正", "对极"),
}

LATIN_ANCHOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./-]{1,}")
LATIN_PHRASE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./-]*")
LATIN_SKIP = {
    "vs", "the", "and", "of", "in", "on", "to", "for", "with", "a", "an",
    "3d", "2d", "ai", "cv", "ml", "dl", "api", "gpu", "cpu", "sdk",
}
LATIN_PHRASE_BREAK = LATIN_SKIP - {"3d", "2d"}

ANCHOR_NORMALIZE = (
    ("batch normalization", "bn"),
    ("batchnorm", "bn"),
    ("3d gaussian splatting", "3dgs"),
    ("3d gs", "3dgs"),
    ("3dgs", "3dgs"),
)


def _normalize_anchor_text(text: str) -> str:
    text = (text or "").lower()
    for source, target in ANCHOR_NORMALIZE:
        text = text.replace(source, target)
    return text


def _normalize_phrase_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def _latin_phrase_anchors(text: str) -> list[str]:
    phrases: list[str] = []
    group: list[str] = []
    previous_end: int | None = None

    def flush() -> None:
        if 2 <= len(group) <= 3:
            phrases.append(_normalize_phrase_text(" ".join(group)))
        group.clear()

    for match in LATIN_PHRASE_TOKEN_RE.finditer(text or ""):
        if previous_end is not None and not text[previous_end:match.start()].isspace():
            flush()
        token = match.group(0)
        if token.casefold() in LATIN_PHRASE_BREAK:
            flush()
        else:
            group.append(token)
        previous_end = match.end()
    flush()
    return list(dict.fromkeys(phrases))


def _phrase_in_text(phrase: str, text: str) -> bool:
    phrase = _normalize_phrase_text(phrase)
    text = _normalize_phrase_text(text)
    if phrase in text:
        return True
    compact_phrase = re.sub(r"[\s-]+", "", phrase)
    compact_text = re.sub(r"[\s-]+", "", text)
    return bool(compact_phrase and compact_phrase in compact_text)


def _tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    out = re.findall(r"[a-z0-9_+.#/-]+", text)
    cn = "".join(re.findall(r"[一-鿿]", text))
    cn = "".join(ch for ch in cn if ch not in STOP_CHARS)
    out += [cn[i:i + 2] for i in range(len(cn) - 1)]
    return [t for t in out if t not in STOP_WORDS]


def _is_meta_section(section: str | None, source: str | None = None) -> bool:
    # 微信资料里的“速查”多为正文表格（结构标注、损失/指标清单），不是
    # 个人笔记里的复习清单；只过滤纯目录、自测题和闭卷提示，避免把
    # 有价值的知识整段挡在检索之外。
    if source and source.startswith("微信资料/"):
        return bool(re.search(r"目录|自测|闭卷|复习安排|待办", section or ""))
    return bool(META_SECTION_RE.search((section or "") + " " + (source or "")))


class VaultRetriever:
    """面向个人复习笔记的轻量 BM25 检索器。

    ``confidence_threshold`` 是归一化后的 top score 门槛；低于它直接返回空，
    让 RealtimeAnswerService 的 ``low_confidence`` 分支走通用知识。
    """

    def __init__(
        self,
        chunks: Iterable[Chunk],
        *,
        confidence_threshold: float = 0.5,
        heading_weight: int = 2,
        k: float = 1.2,
        b: float = 0.75,
    ):
        self.confidence_threshold = confidence_threshold
        self.heading_weight = max(1, int(heading_weight))
        self.k = k
        self.b = b
        self.chunks = [c for c in chunks if not _is_meta_section(c.section, c.source)]
        self._docs: list[tuple[Chunk, Counter[str], int]] = []
        for chunk in self.chunks:
            section = chunk.section or ""
            doc = (section + "\n") * self.heading_weight + chunk.text
            tokens = _tokenize(doc)
            self._docs.append((chunk, Counter(tokens), len(tokens)))
        self._n = len(self._docs)
        self._avg = sum(x[2] for x in self._docs) / max(self._n, 1)
        self._df: Counter[str] = Counter()
        for _, counter, _ in self._docs:
            self._df.update(set(counter))

    def _query_tokens(self, query: str) -> list[str]:
        tokens = _tokenize(query)
        lowered = (query or "").lower()
        for key, extras in EXPANSIONS.items():
            if key in lowered:
                tokens.extend(_tokenize(" ".join(extras)))
        seen = set()
        result = []
        for token in tokens:
            if token not in seen:
                seen.add(token)
                result.append(token)
        return result

    @staticmethod
    def _anchor_groups(query: str) -> tuple[list[str], list[str]]:
        phrases = _latin_phrase_anchors(query)
        phrase_tokens = {
            match.group(0).casefold()
            for phrase in phrases
            for match in LATIN_PHRASE_TOKEN_RE.finditer(
                phrase + " " + _normalize_anchor_text(phrase)
            )
        }
        lowered = _normalize_anchor_text(query)
        single_latin = [
            m.group(0)
            for m in LATIN_ANCHOR_RE.finditer(lowered)
            if m.group(0) not in LATIN_SKIP and m.group(0) not in phrase_tokens
        ]
        chinese = []
        for term in ANCHOR_TERMS:
            if term in lowered:
                chinese.extend((term, *ANCHOR_TERM_ALIASES.get(term, ())))
        return list(dict.fromkeys(phrases + single_latin)), list(dict.fromkeys(chinese))

    def _retrieve_sync(
        self,
        query: str,
        top_k: int,
        min_score: float,
        exclude_categories: Sequence[str] = (),
    ) -> list[Chunk]:
        if not self._docs or not (query or "").strip():
            return []
        q_tokens = self._query_tokens(query)
        if not q_tokens:
            return []
        excluded = {str(x).casefold() for x in exclude_categories}
        scored: list[tuple[float, Chunk]] = []
        for chunk, counter, length in self._docs:
            category = str((chunk.metadata or {}).get("category", "")).casefold()
            if category in excluded:
                continue
            score = 0.0
            for token in q_tokens:
                if token not in counter:
                    continue
                idf = math.log(1 + (self._n - self._df[token] + 0.5) / (self._df[token] + 0.5))
                score += idf * counter[token] * (self.k + 1) / (
                    counter[token] + self.k * (1 - self.b + self.b * length / max(self._avg, 1))
                )
            if score > 0:
                scored.append((score, chunk))
        if not scored:
            return []
        scored.sort(key=lambda item: -item[0])
        # 技术锚点重排：命中 query 的缩写/术语越多，越可能是真正讲这个问题的章节。
        # 只靠 BM25 时，长 chunk 里的通用词容易把“顺便提了一句 NMS”的页面顶上来。
        latin, chinese = self._anchor_groups(query)
        phrase_anchors = _latin_phrase_anchors(query)
        single_latin = [anchor for anchor in latin if anchor not in phrase_anchors]
        reranked: list[tuple[float, Chunk]] = []
        for score, chunk in scored[: max(top_k * 10, 40)]:
            raw_section = chunk.section or ""
            raw_text = raw_section + "\n" + chunk.text
            section = _normalize_anchor_text(raw_section)
            text = _normalize_anchor_text(raw_text)
            latin_section = sum(section.count(anchor) for anchor in single_latin)
            latin_text = sum(text.count(anchor) for anchor in single_latin)
            latin_section += sum(
                1 for phrase in phrase_anchors if _phrase_in_text(phrase, raw_section)
            )
            latin_text += sum(
                1 for phrase in phrase_anchors if _phrase_in_text(phrase, raw_text)
            )
            chinese_section = sum(section.count(anchor) for anchor in chinese)
            chinese_text = sum(text.count(anchor) for anchor in chinese)
            phrase_bonus = sum(1 for anchor in chinese if anchor in text)
            boost = (
                1.0
                + 0.8 * min(latin_section, 2)
                + 0.15 * min(latin_text, 6)
                + 1.2 * min(chinese_section, 2)
                + 0.25 * min(chinese_text, 6)
                + 0.8 * phrase_bonus
            )
            reranked.append((score * boost, chunk))
        reranked.sort(key=lambda item: -item[0])

        # 相关性门槛：query 的主技术锚点必须在候选 chunk 中出现。
        # 例如 NMS 题只接受真正含 NMS 的段落；Focal Loss 题如果笔记没有专门段落，
        # 直接返回空，让上层用通用知识，而不是拿“顺便提过 loss”的页面硬编引用。
        def anchor_ok(item: Chunk) -> bool:
            raw_section = item.section or ""
            raw_text = raw_section + "\n" + item.text
            section = _normalize_anchor_text(raw_section)
            text = _normalize_anchor_text(raw_text)

            def latin_hit(anchor: str) -> bool:
                return section.count(anchor) > 0 or text.count(anchor) >= 2

            if phrase_anchors:
                latin_ok = any(
                    _phrase_in_text(phrase, raw_section)
                    or _phrase_in_text(phrase, raw_text)
                    for phrase in phrase_anchors
                )
            else:
                latin_ok = all(latin_hit(anchor) for anchor in single_latin) if latin else True
            chinese_ok = any(anchor in text for anchor in chinese) if chinese else True
            if latin and chinese:
                return latin_ok or chinese_ok
            return latin_ok and chinese_ok

        reranked = [(score, item) for score, item in reranked if anchor_ok(item)]
        if not reranked:
            return []
        best = reranked[0][0] or 1.0
        if best <= 0:
            return []
        floor = max(float(min_score), self.confidence_threshold)
        result: list[Chunk] = []
        for score, chunk in reranked[:top_k]:
            normalized = min(1.0, score / best)
            if normalized < floor:
                continue
            result.append(replace(chunk, score=round(normalized, 6)))
        return result

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 4,
        min_score: float = 0.35,
        exclude_categories: Sequence[str] = (),
    ) -> list[Chunk]:
        return await asyncio.to_thread(
            self._retrieve_sync, query, top_k, min_score, exclude_categories,
        )
