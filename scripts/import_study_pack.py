# -*- coding: utf-8 -*-
"""把 XMind 知识图谱和两份 PDF 面试资料导入本机知识库/练习题库。

输入：
    E:\\download\\面试涉及的知识点.xmind
    E:\\download\\01 神经网络(带目录-水印).pdf
    E:\\download\\02 DeekLearning面试问题_精简版.pdf

输出（只写本机，不进入项目、不打包进 exe）：
    %APPDATA%\\InterviewMonkey\\knowledge\\study_notes.jsonl
    %APPDATA%\\InterviewMonkey\\knowledge\\study_bank.json
    %APPDATA%\\InterviewMonkey\\knowledge\\study_import.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_XMIND = Path(r"E:\download\面试涉及的知识点.xmind")
DEFAULT_PDF1 = Path(r"E:\download\01 神经网络(带目录-水印).pdf")
DEFAULT_PDF2 = Path(r"E:\download\02 DeekLearning面试问题_精简版.pdf")

QUESTION_KEYS = (
    "区别", "什么", "如何", "为什么", "怎么", "哪些", "是否", "作用",
    "原理", "流程", "步骤", "方法", "特点", "优缺点", "比较", "介绍",
    "理解", "能不能", "要求",
)
GENERIC_TITLES = {"面试问题", "题目", "定义", "举例", "目录", "前言", "总结"}


def _norm(value: str) -> str:
    """压掉 PDF 抽取产生的汉字间空格，同时保留英文单词间空格。"""
    text = str(value or "").replace("\xa0", " ").replace("\u3000", " ")
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s+([，。；：？！、])", r"\1", text)
    text = re.sub(r"([，。；：？！、])\s+", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _smart_join(parts: Iterable[str]) -> str:
    result = ""
    for part in parts:
        part = str(part or "")
        if not part:
            continue
        if result:
            left, right = result[-1], part[0]
            if left.isascii() and left.isalnum() and right.isascii() and right.isalnum():
                result += " "
        result += part
    return result


def _chunk_text(text: str, max_chars: int = 1800) -> list[str]:
    text = _norm(text)
    if not text:
        return []
    blocks = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if len(block) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            lines = block.splitlines() or [block]
            buffer = ""
            for line in lines:
                # XMind 的某些节点是一整段没有换行的超长文本，按字符硬切。
                while len(line) > max_chars:
                    if buffer:
                        chunks.append(buffer.strip())
                        buffer = ""
                    cut = max(line.rfind(mark, 0, max_chars) for mark in ("。", "；", "，", " "))
                    if cut < max_chars // 2:
                        cut = max_chars
                    else:
                        cut += 1
                    chunks.append(line[:cut].strip())
                    line = line[cut:]
                candidate = line if not buffer else buffer + "\n" + line
                if len(candidate) > max_chars and buffer:
                    chunks.append(buffer.strip())
                    buffer = line
                else:
                    buffer = candidate
            if buffer.strip():
                chunks.append(buffer.strip())
            continue
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            chunks.append(current.strip())
            current = block
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def _split_ref(text: str, max_chars: int = 900, max_items: int = 4) -> list[str]:
    text = _norm(text)
    if not text:
        return []
    sentences = re.split(r"(?<=[。；！？!?])\s*|\n+", text)
    items: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = sentence if not current else current + " " + sentence
        if len(candidate) > max_chars and current:
            items.append(current.strip())
            current = sentence
        else:
            current = candidate
        if len(items) >= max_items:
            break
    if current.strip() and len(items) < max_items:
        items.append(current.strip())
    # 个别 PDF/XMind 段落没有句号，按字符硬切，避免单条参考答案过长。
    expanded: list[str] = []
    for item in items:
        if len(item) <= max_chars:
            expanded.append(item)
            continue
        start = 0
        while start < len(item) and len(expanded) < max_items:
            end = min(len(item), start + max_chars)
            if end < len(item):
                cut = max(item.rfind(mark, start, end) for mark in ("。", "；", "，", " "))
                if cut > start + max_chars // 2:
                    end = cut + 1
            expanded.append(item[start:end].strip())
            start = end
    return [item for item in expanded if item][:max_items]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _qkey(question: str) -> str:
    value = re.sub(r"第\s*\d+\s*题", "", str(question or ""))
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE).lower()


def _dedupe_bank(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        key = _qkey(item.get("q", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


# ------------------------------------------------------------------ XMind ---
def _topic_notes(topic: dict[str, Any]) -> str:
    notes = topic.get("notes")
    if not isinstance(notes, dict):
        return ""
    plain = notes.get("plain")
    if isinstance(plain, dict):
        return _norm(str(plain.get("content") or ""))
    if isinstance(plain, str):
        return _norm(plain)
    return ""


def _topic_children(topic: dict[str, Any]) -> list[dict[str, Any]]:
    children = topic.get("children")
    if not isinstance(children, dict):
        return []
    result = []
    for key in ("attached", "detached", "callout", "summary"):
        for child in children.get(key) or []:
            if isinstance(child, dict):
                result.append(child)
    return result


def _xmind_nodes(root: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def walk(topic: dict[str, Any], path: list[str], depth: int) -> None:
        title = _norm(str(topic.get("title") or ""))
        children = _topic_children(topic)
        node_path = path + ([title] if title else [])
        nodes.append({
            "title": title,
            "path": node_path,
            "depth": depth,
            "top": path[0] if path else title,
            "direct_children": [_norm(str(c.get("title") or "")) for c in children],
            "notes": _topic_notes(topic),
            "topic": topic,
        })
        for child in children:
            walk(child, node_path, depth + 1)

    for child in _topic_children(root):
        walk(child, [], 1)
    return nodes


def _xmind_category(node: dict[str, Any]) -> str:
    text = " ".join(node["path"][:2]).lower()
    if any(key in text for key in ("python", "numpy", "pytorch", "tensorflow")):
        return "XMind · Python/框架"
    if "c++" in text:
        return "XMind · C++"
    if any(key in text for key in ("opencv", "滤波", "边缘", "几何变换")):
        return "XMind · OpenCV"
    if any(key in text for key in ("transformer", "vit", "rnn", "大模型", "llm", "diffusion", "lora")):
        return "XMind · Transformer/LLM"
    if any(key in text for key in ("分割", "yolo", "检测", "cnn", "网络", "resnet", "vgg", "mobilenet", "shufflenet")):
        return "XMind · CV/网络"
    if any(key in text for key in ("梯度", "回归", "knn", "k-means", "机器学习", "损失", "归一化")):
        return "XMind · 机器学习"
    return "XMind · 其他"


def _xmind_knowledge(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for node in nodes:
        title = node["title"]
        if title in GENERIC_TITLES or title.startswith("面试问题"):
            continue
        child_titles = [item for item in node["direct_children"] if item]
        body_parts = [title, node["notes"]]
        if child_titles:
            body_parts.append("子知识点：" + "；".join(child_titles[:12]))
        body = _norm("\n".join(part for part in body_parts if part))
        if len(body) < 35 or not title:
            continue
        section = " > ".join(node["path"][:4])[:180]
        category = _xmind_category(node)
        for index, chunk in enumerate(_chunk_text(body)):
            digest = hashlib.sha1(
                (section + "\n" + str(index)).encode("utf-8")
            ).hexdigest()[:12]
            chunks.append({
                "id": "xmind-" + digest,
                "source": "资料/XMind/面试涉及的知识点.xmind",
                "section": section,
                "text": f"【{section}】\n{chunk}",
                "metadata": {
                    "chunk_index": index,
                    "category": category,
                    "source_file": "面试涉及的知识点.xmind",
                },
            })
    return chunks


def _subtree_text(node: dict[str, Any], max_chars: int = 1800) -> str:
    parts: list[str] = []

    def walk(topic: dict[str, Any], depth: int = 0) -> None:
        if sum(len(part) for part in parts) >= max_chars:
            return
        title = _norm(str(topic.get("title") or ""))
        notes = _topic_notes(topic)
        if title:
            parts.append(("  " * min(depth, 3)) + title)
        if notes:
            parts.append(("  " * min(depth, 3)) + notes)
        for child in _topic_children(topic):
            walk(child, depth + 1)

    walk(node["topic"])
    return "\n".join(parts)[:max_chars]


def _xmind_bank(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for node in nodes:
        title = node["title"]
        if not title or title in GENERIC_TITLES or len(title) > 90:
            continue
        questionish = (
            title.endswith(("？", "?"))
            or any(key in title for key in QUESTION_KEYS)
        )
        branch = node["depth"] <= 2 and len(title) <= 40 and bool(node["direct_children"])
        if not (questionish or branch):
            continue
        answer = _subtree_text(node)
        # 问题标题本身不能当答案；必须至少有一些子节点/说明。
        if len(answer) < 30:
            continue
        if questionish:
            question = title
        else:
            question = "请讲清楚：" + title
        question = question.strip()
        if not question.endswith(("？", "?")):
            question += "？"
        ref = _split_ref(answer)
        if not ref:
            continue
        items.append({
            "cat": _xmind_category(node),
            "from": "资料 · XMind · " + (node["top"] or "面试知识点")[:40],
            "q": question,
            "ref": ref,
        })
    return items


# -------------------------------------------------------------------- PDFs ---
def _read_pdf_pages(path: Path) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return pages


def _parse_toc_lines(lines: Iterable[str]) -> list[tuple[str, int]]:
    result = []
    seen = set()
    for raw in lines:
        line = _norm(raw)
        match = re.match(r"^(.{2,90}?)\s*\.{2,}\s*(\d{1,3})\s*$", line)
        if not match:
            continue
        title = re.sub(r"\s*[.．·]+\s*$", "", match.group(1)).strip()
        page = int(match.group(2))
        if len(title) < 2 or page < 1:
            continue
        key = (title, page)
        if key in seen:
            continue
        seen.add(key)
        result.append((title, page))
    return result


def _pdf1_sections(toc: list[tuple[str, int]]) -> list[dict[str, Any]]:
    sections = []
    chapter = ""
    for title, page in toc:
        if re.match(r"^[一二三四五六七八九十]+[\.、]", title):
            chapter = title
        sections.append({"title": title, "page": page, "chapter": chapter})
    return sections


def _section_at(sections: list[dict[str, Any]], page: int) -> dict[str, Any]:
    chosen = {"title": "正文", "chapter": ""}
    for section in sections:
        if section["page"] <= page:
            chosen = section
        else:
            break
    return chosen


def _pdf1_knowledge(pages: list[str], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks = []
    for page_number in range(6, len(pages) + 1):
        body = _norm(pages[page_number - 1])
        if len(body) < 25:
            continue
        section = _section_at(sections, page_number)
        # PDF1 的目录后半段没有完整的章节编号，使用最近的知识点标题，
        # 避免把轻量化/检测内容错误挂到“九.Transform”下。
        section_path = f"神经网络PDF > {section['title']} (p.{page_number})"
        for index, chunk in enumerate(_chunk_text(body)):
            digest = hashlib.sha1(
                ("pdf1" + str(page_number) + str(index)).encode("utf-8")
            ).hexdigest()[:12]
            chunks.append({
                "id": "pdf1-" + digest,
                "source": "资料PDF/01 神经网络(带目录-水印).pdf",
                "section": section_path,
                "text": f"【{section_path}】\n{chunk}",
                "metadata": {
                    "chunk_index": index,
                    "category": "神经网络PDF",
                    "source_file": "01 神经网络(带目录-水印).pdf",
                    "page": page_number,
                },
            })
    return chunks


def _find_title_page(pages: list[str], title: str, suggested: int) -> int | None:
    core = re.sub(r"[^0-9A-Za-z一-鿿]", "", title)
    if len(core) > 10:
        core = core[:10]
    if len(core) < 3:
        return suggested if 1 <= suggested <= len(pages) else None
    for delta in range(0, 9):
        for page in (suggested + delta, suggested - delta):
            if page < 6 or page > len(pages):
                continue
            haystack = re.sub(r"[^0-9A-Za-z一-鿿]", "", pages[page - 1])
            if core in haystack:
                return page
    return None


def _pdf1_bank(pages: list[str], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for section in sections:
        title = section["title"]
        page = section["page"]
        if page < 6 or page > len(pages):
            continue
        if title in GENERIC_TITLES or len(title) < 4 or len(title) > 70:
            continue
        actual_page = _find_title_page(pages, title, page)
        if actual_page is None:
            continue
        page = actual_page
        text = _norm(pages[page - 1])
        if len(text) < 50:
            continue
        question = f"请讲清楚：{title.rstrip('：:、')}？"
        ref = _split_ref(text, max_chars=900, max_items=3)
        if not ref:
            continue
        items.append({
            "cat": "资料 · 神经网络笔记复述",
            "from": f"资料 · 神经网络PDF · p{page}",
            "q": question,
            "ref": ref,
        })
    return items


def _pdf2_page_lines(page) -> list[tuple[float, str]]:
    spans = []

    def visitor(text, cm, tm, font, size):
        value = (text or "").strip()
        if not value:
            return
        try:
            x = float(tm[4])
            y = float(tm[5])
        except Exception:
            x = y = 0.0
        spans.append((x, y, round(float(size), 1), value))

    page.extract_text(visitor_text=visitor)
    lines: list[tuple[float, str]] = []
    current = []
    current_y = None
    for x, y, size, value in spans:
        if current_y is None or abs(y - current_y) <= 2.0:
            current.append((x, size, value))
            if current_y is None:
                current_y = y
        else:
            if current:
                current.sort(key=lambda item: item[0])
                text = _norm(_smart_join(item[2] for item in current))
                if text:
                    lines.append((max(item[1] for item in current), text))
            current = [(x, size, value)]
            current_y = y
    if current:
        current.sort(key=lambda item: item[0])
        text = _norm(_smart_join(item[2] for item in current))
        if text:
            lines.append((max(item[1] for item in current), text))
    return lines


def _pdf2_category(section: str) -> str:
    lower = section.lower()
    if "python" in lower:
        return "资料 · PDF2 Python"
    if "opencv" in lower:
        return "资料 · PDF2 OpenCV"
    if "c++" in lower:
        return "资料 · PDF2 C++"
    if "算法" in section:
        return "资料 · PDF2 算法/深度学习"
    if "深度学习" in section:
        return "资料 · PDF2 深度学习"
    return "资料 · PDF2 面试问题"


def _pdf2_questions_from_reader(reader) -> list[dict[str, Any]]:
    questions = []
    section = "面试问题"
    current = None
    for page_index, page in enumerate(reader.pages):
        for size, text in _pdf2_page_lines(page):
            if size >= 25:
                if current:
                    questions.append(current)
                    current = None
                section = text
                continue
            if 17 <= size < 25:
                if re.match(r"^/?\d{1,2}\s*[、.．?]?\s*\S", text):
                    if current:
                        questions.append(current)
                    current = {
                        "raw": text,
                        "section": section,
                        "page": page_index + 1,
                        "answer": [],
                    }
                elif current and not current["answer"]:
                    current["raw"] = _norm(current["raw"] + " " + text)
                continue
            if current:
                current["answer"].append(text)
    if current:
        questions.append(current)

    result = []
    for item in questions:
        question = re.sub(r"^[/\\\s]+", "", item["raw"])
        question = re.sub(r"^\d{1,2}\s*[、.．?]?\s*", "", question).strip()
        answer_lines = [line for line in item["answer"] if line not in ("面试问题", "")]
        if len(question) < 4 and answer_lines:
            question = _norm(question + " " + answer_lines.pop(0))
        if len(question) < 4:
            continue
        answer = _norm("\n".join(answer_lines))
        if len(answer) < 20:
            continue
        if not question.endswith(("？", "?", "。")):
            question = question.rstrip("，,；;：:") + "？"
        category = _pdf2_category(item["section"])
        ref = _split_ref(answer, max_chars=1000, max_items=4)
        if not ref:
            continue
        result.append({
            "cat": category,
            "from": f"资料 · PDF2 · p{item['page']} · {item['section'][:24]}",
            "q": question,
            "ref": ref,
            "_answer_chars": len(answer),
        })
    return result


def _pdf2_knowledge(reader) -> list[dict[str, Any]]:
    chunks = []
    section = "深度学习面试问题"
    for index, page in enumerate(reader.pages):
        body = _norm(page.extract_text() or "")
        if len(body) < 30:
            continue
        # 章节标题可能只出现在第一节，后续页沿用最近标题。
        for size, text in _pdf2_page_lines(page):
            if size >= 25:
                section = text
        section_path = f"深度学习面试PDF > {section} (p.{index + 1})"
        for part_index, chunk in enumerate(_chunk_text(body)):
            digest = hashlib.sha1(
                ("pdf2" + str(index) + str(part_index)).encode("utf-8")
            ).hexdigest()[:12]
            chunks.append({
                "id": "pdf2-" + digest,
                "source": "资料PDF/02 DeekLearning面试问题_精简版.pdf",
                "section": section_path,
                "text": f"【{section_path}】\n{chunk}",
                "metadata": {
                    "chunk_index": part_index,
                    "category": "深度学习面试PDF",
                    "source_file": "02 DeekLearning面试问题_精简版.pdf",
                    "page": index + 1,
                },
            })
    return chunks


# -------------------------------------------------------------------- main ---
def _remap_categories(items: list[dict[str, Any]]) -> None:
    """把十几类细分类收成三个练习题库分类，避免练习页 chip 太长。"""
    for item in items:
        category = str(item.get("cat") or "")
        if category.startswith("XMind"):
            item["cat"] = "资料 · XMind 面试知识点"
        elif category.startswith("资料 · PDF2"):
            item["cat"] = "资料 · PDF2 面试问答"
        elif category == "资料 · 神经网络笔记复述":
            item["cat"] = "资料 · PDF1 神经网络复述"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xmind", default=str(DEFAULT_XMIND))
    parser.add_argument("--pdf1", default=str(DEFAULT_PDF1))
    parser.add_argument("--pdf2", default=str(DEFAULT_PDF2))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    xmind_path = Path(args.xmind)
    pdf1_path = Path(args.pdf1)
    pdf2_path = Path(args.pdf2)
    for path in (xmind_path, pdf1_path, pdf2_path):
        if not path.is_file():
            print("missing:", path)
            return 1

    with zipfile.ZipFile(xmind_path) as archive:
        sheets = json.loads(archive.read("content.json").decode("utf-8"))
    root = sheets[0]["rootTopic"]
    xmind_nodes = _xmind_nodes(root)
    xmind_chunks = _xmind_knowledge(xmind_nodes)
    xmind_bank = _xmind_bank(xmind_nodes)

    pages1 = _read_pdf_pages(pdf1_path)
    toc = _parse_toc_lines(
        line
        for page in pages1[:6]
        for line in page.splitlines()
    )
    sections1 = _pdf1_sections(toc)
    pdf1_chunks = _pdf1_knowledge(pages1, sections1)
    pdf1_bank = _pdf1_bank(pages1, sections1)

    from pypdf import PdfReader

    reader2 = PdfReader(str(pdf2_path))
    pdf2_chunks = _pdf2_knowledge(reader2)
    pdf2_bank = _pdf2_questions_from_reader(reader2)

    knowledge = xmind_chunks + pdf1_chunks + pdf2_chunks
    bank_raw = xmind_bank + pdf2_bank + pdf1_bank
    _remap_categories(bank_raw)
    bank = _dedupe_bank(bank_raw)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [
            {"name": xmind_path.name, "knowledge_chunks": len(xmind_chunks), "bank_items": len(xmind_bank)},
            {"name": pdf1_path.name, "knowledge_chunks": len(pdf1_chunks), "bank_items": len(pdf1_bank)},
            {"name": pdf2_path.name, "knowledge_chunks": len(pdf2_chunks), "bank_items": len(pdf2_bank)},
        ],
        "knowledge_chunks": len(knowledge),
        "bank_before_dedupe": len(bank_raw),
        "bank_total": len(bank),
        "bank_categories": dict(Counter(item["cat"] for item in bank)),
        "toc_entries": len(toc),
    }
    print(json.dumps(manifest, ensure_ascii=True, indent=2))
    if args.dry_run:
        print("dry-run: not written")
        return 0

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(os.environ.get("APPDATA", Path.home())) / "InterviewMonkey" / "knowledge"
    )
    notes_path = output_dir / "study_notes.jsonl"
    bank_path = output_dir / "study_bank.json"
    manifest_path = output_dir / "study_import.json"
    _atomic_write(
        notes_path,
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in knowledge) + "\n",
    )
    _atomic_write(bank_path, json.dumps(bank, ensure_ascii=False, separators=(",", ":")))
    _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    print("written:")
    print(" ", notes_path)
    print(" ", bank_path)
    print(" ", manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
