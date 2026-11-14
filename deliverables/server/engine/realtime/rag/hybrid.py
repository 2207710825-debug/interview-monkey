"""BM25 + 本地向量 + RRF 混合检索。

存在 faiss 时使用 ``IndexFlatIP``；未安装时退回等价的内存余弦索引，离线测试
和桌面端都不会因可选原生依赖缺失而崩溃。
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter
from dataclasses import replace
from typing import Iterable, Sequence

from ..contracts import Chunk


_TOKEN = re.compile(r"[A-Za-z0-9_+.#/-]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN.findall(text)]


class HashEmbedder:
    """零下载本地嵌入兜底；生产可注入 sentence-transformers/API embedder。"""

    def __init__(self, dimensions: int = 384):
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            tokens = tokenize(text)
            features = tokens + ["".join(tokens[index:index + 2]) for index in range(len(tokens) - 1)]
            for feature in features:
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "little") % self.dimensions
                sign = -1.0 if digest[4] & 1 else 1.0
                vector[bucket] += sign
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class _VectorIndex:
    def __init__(self, vectors: Sequence[Sequence[float]]):
        self.vectors = [list(vector) for vector in vectors]
        self._faiss = None
        try:
            import faiss
            import numpy as np
            if self.vectors:
                matrix = np.asarray(self.vectors, dtype="float32")
                self._faiss = faiss.IndexFlatIP(matrix.shape[1])
                self._faiss.add(matrix)
        except Exception:
            self._faiss = None

    def search(self, query: Sequence[float], limit: int) -> list[tuple[int, float]]:
        if self._faiss is not None:
            import numpy as np
            scores, ids = self._faiss.search(np.asarray([query], dtype="float32"), limit)
            return [(int(index), float(score)) for index, score in zip(ids[0], scores[0]) if index >= 0]
        scores = [sum(a * b for a, b in zip(query, vector)) for vector in self.vectors]
        return sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:limit]


class HybridRetriever:
    def __init__(self, chunks: Iterable[Chunk], embedder: HashEmbedder | None = None, rrf_k: int = 60):
        self.chunks = list(chunks)
        self.embedder = embedder or HashEmbedder()
        self.rrf_k = rrf_k
        self._tokens = [tokenize(chunk.text) for chunk in self.chunks]
        self._lengths = [len(tokens) for tokens in self._tokens]
        self._avg_len = sum(self._lengths) / max(len(self._lengths), 1)
        self._df: Counter[str] = Counter()
        for tokens in self._tokens:
            self._df.update(set(tokens))
        self._vectors = self.embedder.embed([chunk.text for chunk in self.chunks])
        self._vector_index = _VectorIndex(self._vectors)

    def _bm25(self, query: str) -> list[tuple[int, float]]:
        query_tokens = tokenize(query)
        n = len(self.chunks)
        scores = []
        for index, tokens in enumerate(self._tokens):
            counts = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                df = self._df.get(token, 0)
                if not df or not counts[token]:
                    continue
                idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
                numerator = counts[token] * 2.2
                denominator = counts[token] + 1.2 * (0.25 + 0.75 * self._lengths[index] / max(self._avg_len, 1))
                score += idf * numerator / denominator
            scores.append((index, score))
        return sorted(scores, key=lambda item: item[1], reverse=True)

    def _retrieve_sync(
        self,
        query: str,
        top_k: int,
        min_score: float,
        exclude_categories: Sequence[str] = (),
    ) -> list[Chunk]:
        if not self.chunks or not query.strip():
            return []
        excluded = {value.casefold() for value in exclude_categories}
        if excluded:
            excluded_count = sum(
                str(chunk.metadata.get("category", "通用")).casefold() in excluded
                for chunk in self.chunks
            )
        else:
            excluded_count = 0
        candidate_count = min(len(self.chunks), max(top_k * 4, 10))
        candidate_count = min(len(self.chunks), candidate_count + excluded_count)
        bm25 = self._bm25(query)[:candidate_count]
        query_vector = self.embedder.embed([query])[0]
        vector = self._vector_index.search(query_vector, candidate_count)
        fused: Counter[int] = Counter()
        for rank, (index, _score) in enumerate(bm25, 1):
            fused[index] += 1.0 / (self.rrf_k + rank)
        for rank, (index, _score) in enumerate(vector, 1):
            fused[index] += 1.0 / (self.rrf_k + rank)
        max_rrf = 2.0 / (self.rrf_k + 1)
        ranked = sorted(fused.items(), key=lambda item: item[1], reverse=True)
        output = []
        query_terms = set(tokenize(query))
        for index, rrf_score in ranked:
            category = str(self.chunks[index].metadata.get("category", "通用")).casefold()
            if category in excluded:
                continue
            lexical_overlap = len(query_terms.intersection(self._tokens[index])) / max(len(query_terms), 1)
            cosine = max(0.0, sum(a * b for a, b in zip(query_vector, self._vectors[index])))
            score = min(1.0, 0.45 * (rrf_score / max_rrf) + 0.35 * lexical_overlap + 0.20 * cosine)
            if score >= min_score:
                output.append(replace(self.chunks[index], score=round(score, 6)))
            if len(output) >= top_k:
                break
        return output

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
