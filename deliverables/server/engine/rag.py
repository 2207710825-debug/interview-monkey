# -*- coding: utf-8 -*-
"""第 3 段：上下文与知识检索（RAG / Context）

本地知识检索，零重依赖实现：把知识库文档切块，用「字 n-gram + TF-IDF」建向量，
查询时算余弦相似度取最相关片段。接口留好，后续可无缝换成语义向量库
（sentence-transformers + chromadb/faiss）获得更好的召回。

知识库位置：knowledge/ 目录下的 .txt / .md 文件。
"""
import os
import re
import math
import json
from collections import Counter

_KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")


def _tokenize(text):
    """中文 n-gram 分词（1~2 字），英文按空白切。"""
    toks = []
    for w in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", text):
        if re.match(r"^[a-zA-Z0-9]+$", w):
            toks.append(w.lower())
        else:
            toks.append(w)
            for i in range(len(w) - 1):
                toks.append(w[i:i + 2])
    return toks


# 相似度阈值：低于此分数的检索结果视为不相关，返回空列表
MIN_SCORE = 0.15


class _Index:
    def __init__(self):
        self.chunks = []        # [{id, title, text}]
        self.df = Counter()     # 词 -> 文档频
        self.vectors = []       # 每个 chunk 的 {term: tfidf}
        self.norms = []         # 每个 chunk 向量的 L2 范数（预计算）
        self.inverted = {}      # 倒排索引：term -> [chunk_idx, ...]

    def add_doc(self, title, text, chunk_size=300):
        """把文档切块建索引：统计词频、更新文档频。"""
        for i in range(0, len(text), chunk_size):
            seg = text[i:i + chunk_size].strip()
            if not seg:
                continue
            terms = _tokenize(seg)
            if not terms:
                continue
            tf = Counter(terms)
            for t in tf:
                self.df[t] += 1
            self.chunks.append({"id": len(self.chunks), "title": title, "text": seg, "tf": tf})
            self.vectors.append(None)  # 稍后统一算
            self.norms.append(0.0)

    def finalize(self):
        """建库完成：计算 TF-IDF 向量、预计算范数、建倒排索引。"""
        n = max(1, len(self.chunks))
        for idx, c in enumerate(self.chunks):
            vec = {}
            for t, f in c["tf"].items():
                idf = math.log((n + 1) / (1 + self.df[t])) + 1.0
                vec[t] = f * idf
            self.vectors[idx] = vec
            # 预计算 L2 范数，避免每次查询都重算
            self.norms[idx] = math.sqrt(sum(v * v for v in vec.values()))
            # 倒排索引：该 term 出现在哪些 chunk
            for t in vec:
                self.inverted.setdefault(t, []).append(idx)

    def search(self, query, top_k=3):
        """检索：用倒排索引只扫含查询词的 chunk，过滤相似度 < MIN_SCORE。"""
        qv = Counter(_tokenize(query))
        if not qv:
            return []
        qnorm = math.sqrt(sum(v * v for v in qv.values())) or 1.0
        # 倒排索引：只算跟查询有交集的 chunk，跳过全量遍历
        cand = set()
        for t in qv:
            cand.update(self.inverted.get(t, ()))
        scored = []
        for idx in cand:
            vec = self.vectors[idx]
            dot = 0.0
            for t, q in qv.items():
                w = vec.get(t)
                if w is not None:
                    dot += q * w
            denom = self.norms[idx] or 1.0
            sim = dot / (qnorm * denom)
            if sim >= MIN_SCORE:
                scored.append((sim, idx))
        scored.sort(reverse=True)
        return [self.chunks[i] for _, i in scored[:top_k]]


def load_knowledge(directory=None):
    """加载知识库目录，返回 _Index。目录不存在则返回空索引。"""
    directory = directory or _KNOWLEDGE_DIR
    idx = _Index()
    if not os.path.isdir(directory):
        return idx
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith((".txt", ".md")):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        idx.add_doc(os.path.splitext(name)[0], text)
    idx.finalize()
    return idx


class Retriever:
    """可换后端：默认 TF-IDF，未来可替换为 embedding 语义检索。"""

    def __init__(self, directory=None):
        self.index = load_knowledge(directory)

    def retrieve(self, query, top_k=3):
        return self.index.search(query, top_k)


def load_bank(bank_path, top_limit=None):
    """把 5000 题题库（renderer/bank.js）建成可检索索引。

    题库每条是 {cat, from, q, ref[]}，转成一段带参考答案的文本，
    这样问答时能命中"同类面试题 + 标准要点"，避免小模型自由发挥出错。
    """
    idx = _Index()
    if not bank_path or not os.path.isfile(bank_path):
        return idx
    try:
        with open(bank_path, "r", encoding="utf-8") as f:
            raw = f.read()
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < 0:
            return idx
        items = json.loads(raw[start:end + 1])
    except Exception:
        return idx

    if top_limit:
        items = items[:top_limit]
    for it in items:
        q = (it.get("q") or "").strip()
        if not q:
            continue
        refs = it.get("ref") or []
        if isinstance(refs, str):
            refs = [refs]
        body = q
        if refs:
            body += "\n参考答案要点：" + "；".join(str(r) for r in refs[:6])
        cat = (it.get("cat") or "综合").strip()
        idx.add_doc("题库·" + cat, body, chunk_size=1000)
    idx.finalize()
    return idx
