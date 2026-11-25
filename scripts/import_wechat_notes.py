# -*- coding: utf-8 -*-
"""把微信收到的 Markdown 学习资料导入本机知识库和练习题库。

当前支持两份资料：
  1. 深度学习与UNet学习笔记.md
     - 全文按标题切分后进入知识库；
     - 16.11 的 28 道扩展题进入练习题库。
  2. 面试高频题_答题骨架_深度学习基础_修订版.md
     - 全文进入知识库；
     - 56 道题的「一句话答案 / 骨架 / 比方 / 追问」进入练习题库。

输出只写本机：
    %APPDATA%\\InterviewMonkey\\knowledge\\wechat_notes.jsonl
    %APPDATA%\\InterviewMonkey\\knowledge\\wechat_bank.json
    %APPDATA%\\InterviewMonkey\\knowledge\\wechat_import.json

不写入项目仓库，也不打包进 exe。重复运行会原子覆盖以上三个文件。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_UNET = Path(
    r"D:\ruanjian\wechat\xwechat_files\wxid_re30w7qb0iwi22_8c4a"
    r"\msg\file\2026-11\深度学习与UNet学习笔记.md"
)
DEFAULT_QA = Path(
    r"D:\ruanjian\wechat\xwechat_files\wxid_re30w7qb0iwi22_8c4a"
    r"\msg\file\2026-11\面试高频题_答题骨架_深度学习基础_修订版.md"
)

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_HR = re.compile(r"^\s*-{3,}\s*$")


def _is_fence(line: str) -> str:
    match = _FENCE.match(line)
    return match.group(1)[0] if match else ""


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """返回 (行号, 级别, 标题)；忽略代码块里的 # 注释。"""
    result: list[tuple[int, int, str]] = []
    fence = ""
    for index, line in enumerate(lines):
        marker = _is_fence(line)
        if marker:
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            continue
        if fence:
            continue
        match = _HEADING.match(line)
        if match:
            result.append((index, len(match.group(1)), match.group(2).strip()))
    return result


def _sections(text: str) -> list[dict[str, str]]:
    """按 Markdown 标题切段，section 保留完整标题路径。"""
    lines = text.splitlines()
    headings = _headings(lines)
    stack: list[tuple[int, str]] = []
    result: list[dict[str, str]] = []
    for position, (line_index, level, title) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        body_lines = lines[line_index + 1:end]
        while body_lines and (
            not body_lines[-1].strip() or _HR.match(body_lines[-1])
        ):
            body_lines.pop()
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        path = " > ".join(item[1] for item in stack)
        result.append({"path": path, "body": body, "title": title})
    return result


def _blocks(body: str) -> list[str]:
    """按空行切块；代码块内部的空行不切。"""
    result: list[str] = []
    current: list[str] = []
    fence = ""
    for line in body.splitlines():
        marker = _is_fence(line)
        if marker:
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            current.append(line)
            continue
        if not fence and not line.strip() and current:
            block = "\n".join(current).strip()
            if block:
                result.append(block)
            current = []
        else:
            current.append(line)
    if current:
        block = "\n".join(current).strip()
        if block:
            result.append(block)
    return result


def _split_long(block: str, max_chars: int) -> list[str]:
    """超长普通文本按行拆；代码块尽量保持完整。"""
    if len(block) <= max_chars:
        return [block]
    if _is_fence(block.splitlines()[0] if block.splitlines() else ""):
        return [block]
    result: list[str] = []
    current: list[str] = []
    size = 0
    for line in block.splitlines():
        addition = len(line) + 1
        if current and size + addition > max_chars:
            result.append("\n".join(current).strip())
            current = []
            size = 0
        current.append(line)
        size += addition
    if current:
        result.append("\n".join(current).strip())
    return [item for item in result if item]


def _chunk_body(body: str, max_chars: int = 1800) -> list[str]:
    """把一段正文打包成不超过 max_chars 的块。"""
    pieces = []
    for block in _blocks(body):
        pieces.extend(_split_long(block, max_chars))
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = piece if not current else current + "\n\n" + piece
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            chunks.append(current.strip())
            current = piece
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _knowledge_chunks(
    path: Path,
    *,
    slug: str,
    category: str,
    source: str,
) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    result: list[dict[str, Any]] = []
    for section in _sections(text):
        for chunk in _chunk_body(section["body"]):
            index = len(result)
            digest = hashlib.sha1(
                (section["path"] + "\n" + str(index)).encode("utf-8")
            ).hexdigest()[:12]
            result.append({
                "id": f"wechat-{slug}-{digest}",
                "source": source,
                "section": section["path"],
                "text": f"【{section['path']}】\n{chunk}",
                "metadata": {
                    "chunk_index": index,
                    "category": category,
                    "source_file": path.name,
                },
            })
    return result


def _strip_md(value: str) -> str:
    """把练习题库里不适合显示 Markdown 的标记压成纯文本。"""
    text = html.unescape(str(value or ""))
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    raw_lines = text.splitlines()
    lines: list[str] = []
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index].strip()
        if line.startswith("|") and line.endswith("|"):
            rows: list[list[str]] = []
            while index < len(raw_lines):
                candidate = raw_lines[index].strip()
                if not (candidate.startswith("|") and candidate.endswith("|")):
                    break
                if not _TABLE_SEP.match(candidate):
                    rows.append([
                        cell.strip()
                        for cell in candidate.strip("|").split("|")
                    ])
                index += 1
            if rows:
                header = rows[0]
                body = rows[1:]
                if not body:
                    lines.append("；".join(cell for cell in header if cell))
                for row in body:
                    parts = []
                    for column, cell in enumerate(row):
                        if not cell:
                            continue
                        head = header[column] if column < len(header) else ""
                        if head and head != cell:
                            parts.append(f"{head}={cell}")
                        else:
                            parts.append(cell)
                    if parts:
                        lines.append("；".join(parts))
            continue
        index += 1
        if not line or _HR.match(line):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        lines.append(line)
    text = "\n".join(lines)
    text = text.replace("**", "").replace("__", "").replace("~~", "").replace("`", "")
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip(" ：:;；")


def _answer_parts(body: str) -> dict[str, str]:
    pattern = re.compile(r"\*\*([①②③④][^*\n]*)\*\*\s*")
    matches = list(pattern.finditer(body))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        label = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = _strip_md(body[start:end])
        key = label[0]
        if content:
            result[key] = content
    return result


def _parse_qa_skeleton(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    headings = _headings(lines)
    result: list[dict[str, Any]] = []
    section_letter = ""
    section_title = ""
    question_headings = []
    for line_index, level, title in headings:
        if level == 1:
            match = re.match(r"^([A-H])\.\s*(.+)$", title)
            if match:
                section_letter = match.group(1)
                section_title = match.group(2).strip()
            continue
        if level == 2:
            match = re.match(r"^第\s*(\d+)\s*题\s*★?\s*(.+)$", title)
            if match:
                question_headings.append((
                    line_index,
                    int(match.group(1)),
                    match.group(2).strip(),
                    section_letter,
                    section_title,
                ))
    for position, (
        line_index, number, title, current_letter, current_title
    ) in enumerate(question_headings):
        end = (
            question_headings[position + 1][0]
            if position + 1 < len(question_headings)
            else len(lines)
        )
        # 到下一个一级标题（附录）为止；一级标题后的内容不属于本题。
        for offset in range(line_index + 1, end):
            candidate = lines[offset]
            if not _is_fence(candidate) and re.match(r"^#\s+", candidate):
                end = offset
                break
        body = "\n".join(lines[line_index + 1:end])
        parts = _answer_parts(body)
        ref: list[str] = []
        if parts.get("①"):
            ref.append("一句话答案：" + parts["①"])
        if parts.get("②"):
            ref.append("答题骨架：" + parts["②"])
        if parts.get("③"):
            ref.append("打个比方：" + parts["③"])
        if parts.get("④"):
            ref.append("可能追问：" + parts["④"])
        if not ref:
            cleaned = _strip_md(body)
            if cleaned:
                ref.append("答题要点：" + cleaned)
        if not ref:
            continue
        source_section = (
            f"{current_letter} {current_title}"
            if current_letter and current_title
            else "答题骨架"
        )
        result.append({
            "cat": "微信资料 · 深度学习答题骨架",
            "from": f"微信资料 · {source_section} · 第{number}题",
            "q": f"第 {number} 题 · {_strip_md(title)}",
            "ref": ref,
        })
    return result


def _parse_notes_extra(text: str) -> list[dict[str, Any]]:
    q_start = text.find("### 16.11 扩展面试题库")
    a_start = text.find("### 16.11.1", q_start + 1)
    if q_start < 0 or a_start < 0:
        return []
    end = text.find("\n## 17.", a_start)
    if end < 0:
        end = len(text)
    question_block = text[q_start:a_start]
    answer_block = text[a_start:end]

    questions: dict[int, dict[str, str]] = {}
    group = ""
    for line in question_block.splitlines():
        group_match = re.match(r"^\*\*([A-Z])\.\s*(.+?)（\d+\s*道）\*\*\s*$", line.strip())
        if group_match:
            group = f"{group_match.group(1)} {group_match.group(2).strip()}"
            continue
        row = re.match(
            r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$",
            line.strip(),
        )
        if not row:
            continue
        number = int(row.group(1))
        questions[number] = {
            "q": _strip_md(row.group(2)),
            "point": _strip_md(row.group(3)),
            "chapter": _strip_md(row.group(4)),
            "group": group,
        }

    answers: dict[int, str] = {}
    current_number = 0
    for raw in answer_block.splitlines():
        line = raw.rstrip()
        match = re.match(r"^-\s*\*\*(\d+)\b(.*?)\*\*\s*[：:]\s*(.*)$", line.strip())
        if match:
            current_number = int(match.group(1))
            answers[current_number] = match.group(3).strip()
            continue
        if current_number and line.startswith((" ", "\t")) and line.strip():
            answers[current_number] += " " + line.strip()

    result: list[dict[str, Any]] = []
    for number in sorted(questions):
        answer = _strip_md(answers.get(number, ""))
        if not answer:
            continue
        row = questions[number]
        ref = []
        if row["point"]:
            ref.append("考察点：" + row["point"])
        if row["chapter"]:
            ref.append("对应章节：" + row["chapter"])
        ref.append("答案速记：" + answer)
        source_section = f"16.11 {row['group']}" if row["group"] else "16.11"
        result.append({
            "cat": "微信资料 · UNet笔记补充",
            "from": f"微信资料 · {source_section} · 第{number}题",
            "q": f"第 {number} 题 · {row['q']}",
            "ref": ref,
        })
    return result


def _question_key(question: str) -> str:
    value = re.sub(r"第\s*\d+\s*题", "", question)
    value = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    return value.lower()


def _dedupe(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for item in items:
        key = _question_key(str(item.get("q") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _category_counts(items: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        cat = str(item.get("cat") or "")
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unet", default=str(DEFAULT_UNET), help="深度学习与UNet学习笔记.md")
    parser.add_argument("--qa", default=str(DEFAULT_QA), help="面试高频题答题骨架.md")
    parser.add_argument("--output-dir", default="", help="默认 %%APPDATA%%/InterviewMonkey/knowledge")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    unet_path = Path(args.unet)
    qa_path = Path(args.qa)
    for path in (unet_path, qa_path):
        if not path.is_file():
            print("缺少文件: " + str(path))
            return 1

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(os.environ.get("APPDATA", Path.home())) / "InterviewMonkey" / "knowledge"
    )

    unet_chunks = _knowledge_chunks(
        unet_path,
        slug="unet-notes",
        category="深度学习与UNet学习笔记",
        source="微信资料/" + unet_path.name,
    )
    qa_chunks = _knowledge_chunks(
        qa_path,
        slug="qa-skeleton",
        category="面试高频题·答题骨架",
        source="微信资料/" + qa_path.name,
    )

    qa_text = qa_path.read_text(encoding="utf-8")
    unet_text = unet_path.read_text(encoding="utf-8")
    skeleton_bank = _parse_qa_skeleton(qa_text)
    notes_bank = _parse_notes_extra(unet_text)
    bank = _dedupe(skeleton_bank + notes_bank)

    knowledge = unet_chunks + qa_chunks
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_files": [
            {
                "name": unet_path.name,
                "chunks": len(unet_chunks),
                "bank_items": len(notes_bank),
            },
            {
                "name": qa_path.name,
                "chunks": len(qa_chunks),
                "bank_items": len(skeleton_bank),
            },
        ],
        "knowledge_chunks": len(knowledge),
        "bank_total": len(bank),
        "bank_sources": {
            "qa_skeleton": len(skeleton_bank),
            "notes_16_11": len(notes_bank),
            "skipped_duplicates": len(skeleton_bank) + len(notes_bank) - len(bank),
        },
        "bank_categories": _category_counts(bank),
    }
    print(json.dumps(manifest, ensure_ascii=True, indent=2))
    if args.dry_run:
        print("dry-run: not written")
        return 0

    knowledge_path = output_dir / "wechat_notes.jsonl"
    bank_path = output_dir / "wechat_bank.json"
    manifest_path = output_dir / "wechat_import.json"
    _atomic_write(
        knowledge_path,
        "\n".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            for row in knowledge
        ) + "\n",
    )
    _atomic_write(
        bank_path,
        json.dumps(bank, ensure_ascii=False, separators=(",", ":")),
    )
    _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    print("written:")
    print(" ", knowledge_path)
    print(" ", bank_path)
    print(" ", manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
