"""Markdown/PDF/TXT 摄入与语义边界切块。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from ..contracts import Chunk


_TOKEN = re.compile(r"[A-Za-z0-9_+.#/-]+|[\u4e00-\u9fff]")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _split_markdown(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append((match.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    return [(section, "\n".join(lines).strip()) for section, lines in sections if "\n".join(lines).strip()]


def _split_plain(text: str) -> list[tuple[str, str]]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    return [("", paragraph) for paragraph in paragraphs]


def _read_pdf(path: Path) -> list[tuple[str, str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("读取 PDF 需要 pypdf，请安装 requirements-realtime.txt") from exc
    reader = PdfReader(str(path))
    return [(f"第 {index} 页", page.extract_text() or "") for index, page in enumerate(reader.pages, 1)]


def document_category(path: str | Path, root: str | Path | None = None) -> str:
    """按知识库首层目录分类；根目录现有产品说明/FAQ 文档归入“产品”。"""
    doc = Path(path)
    if root is not None:
        try:
            relative = doc.resolve().relative_to(Path(root).resolve())
            if len(relative.parts) > 1:
                return relative.parts[0]
        except ValueError:
            pass
    stem = doc.stem.casefold()
    if any(marker in stem for marker in ("产品", "常见问题", "faq")):
        return "产品"
    return "通用"


def _chunk_sections(
    sections: Iterable[tuple[str, str]],
    source: str,
    chunk_tokens: int,
    overlap_ratio: float,
    category: str,
) -> list[Chunk]:
    overlap = max(0, int(chunk_tokens * overlap_ratio))
    result: list[Chunk] = []
    for section, text in sections:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        if not paragraphs:
            paragraphs = [text.strip()]
        buffer: list[str] = []
        buffer_tokens: list[str] = []

        def emit() -> None:
            nonlocal buffer, buffer_tokens
            body = "\n\n".join(buffer).strip()
            if not body:
                return
            digest = hashlib.sha1(f"{source}|{section}|{len(result)}|{body}".encode("utf-8")).hexdigest()[:12]
            result.append(Chunk(
                digest, body, source, 0.0, section or None,
                {"chunk_index": len(result), "category": category},
            ))
            if overlap:
                tail_tokens = buffer_tokens[-overlap:]
                buffer = ["".join(tail_tokens)] if tail_tokens else []
                buffer_tokens = tail_tokens
            else:
                buffer, buffer_tokens = [], []

        for paragraph in paragraphs:
            tokens = _tokens(paragraph)
            if len(tokens) > chunk_tokens:
                if buffer:
                    emit()
                step = max(1, chunk_tokens - overlap)
                for start in range(0, len(tokens), step):
                    part = tokens[start:start + chunk_tokens]
                    if not part:
                        break
                    body = "".join(part)
                    digest = hashlib.sha1(f"{source}|{section}|{len(result)}|{body}".encode("utf-8")).hexdigest()[:12]
                    result.append(Chunk(
                        digest, body, source, 0.0, section or None,
                        {"chunk_index": len(result), "category": category},
                    ))
                    if start + chunk_tokens >= len(tokens):
                        break
                buffer, buffer_tokens = [], []
                continue
            if buffer_tokens and len(buffer_tokens) + len(tokens) > chunk_tokens:
                emit()
            buffer.append(paragraph)
            buffer_tokens.extend(tokens)
        if buffer:
            emit()
    return result


def load_document(
    path: str | Path,
    chunk_tokens: int = 400,
    overlap_ratio: float = 0.15,
    *,
    category: str | None = None,
) -> list[Chunk]:
    doc = Path(path)
    suffix = doc.suffix.lower()
    if suffix == ".pdf":
        sections = _read_pdf(doc)
    elif suffix == ".md":
        sections = _split_markdown(doc.read_text(encoding="utf-8", errors="ignore"))
    elif suffix == ".txt":
        sections = _split_plain(doc.read_text(encoding="utf-8", errors="ignore"))
    else:
        raise ValueError(f"不支持的知识库文件: {doc.name}")
    return _chunk_sections(
        sections, str(doc), chunk_tokens, overlap_ratio,
        category or document_category(doc),
    )


def ingest_directory(
    directory: str | Path,
    chunk_tokens: int = 400,
    overlap_ratio: float = 0.15,
    *,
    category: str | None = None,
) -> list[Chunk]:
    root = Path(directory)
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".pdf"}:
            current_category = document_category(path, root)
            if category and current_category.casefold() != category.casefold():
                continue
            chunks.extend(load_document(path, chunk_tokens, overlap_ratio, category=current_category))
    return chunks
