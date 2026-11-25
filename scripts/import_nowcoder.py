# -*- coding: utf-8 -*-
"""把牛客专项练习的原始报告 JSON 导入本机知识库和练习题库。

用法：
    python scripts/import_nowcoder.py --raw-dir "E:\\面试猴\\import\\nowcoder_raw"

输入是 ``test/report`` 返回的 JSON（每个分类一个文件），输出：
    %APPDATA%\\InterviewMonkey\\knowledge\\nowcoder.jsonl
    %APPDATA%\\InterviewMonkey\\knowledge\\nowcoder_bank.json
    %APPDATA%\\InterviewMonkey\\knowledge\\nowcoder_import.json

数据仅写本机，不写入项目、不打包进 exe。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CATEGORIES = (
    {
        "name": "计算机视觉",
        "files": ("cv_full_raw.json",),
        "bank_cat": "Nowcoder · 计算机视觉",
        "source": "Nowcoder/计算机视觉",
    },
    {
        "name": "深度学习基础",
        "files": ("dl_full_raw.json", "dl_test_raw.json"),
        "bank_cat": "Nowcoder · 深度学习基础",
        "source": "Nowcoder/深度学习基础",
    },
    {
        "name": "大模型概念",
        "files": ("llm_full_raw.json",),
        "bank_cat": "Nowcoder · 大模型概念",
        "source": "Nowcoder/大模型概念",
    },
    {
        "name": "Python",
        "files": ("python_full_raw.json",),
        "bank_cat": "Nowcoder · Python",
        "source": "Nowcoder/Python",
    },
)

_IMAGE = re.compile(r"<img[^>]*src=[\"']([^\"']+)[\"'][^>]*>", re.I)
_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_BLOCK_CLOSE = re.compile(r"</(?:div|p|li|tr|pre|h[1-6])>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_LLM_KEYWORDS = re.compile(
    r"transformer|llm|大模型|大语言模型|注意力|微调|rag|检索增强|prompt|提示词|"
    r"推理|agent|token|embedding|对齐|rlhf|sft|lora|量化|蒸馏|上下文|幻觉|"
    r"向量|moe|deepspeed|vllm",
    re.I,
)


def _clean_html(value: Any) -> str:
    text = str(value or "")
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _IMAGE.sub(lambda match: " [图片: " + match.group(1) + "] ", text)
    text = _BR.sub("\n", text)
    text = _BLOCK_CLOSE.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_report(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    data = payload.get("data") or {}
    questions = data.get("doneQuestionDetails") or []
    return [item for item in questions if isinstance(item, dict)]


def _correct_letters(answers: Iterable[dict[str, Any]]) -> list[str]:
    result = []
    for index, option in enumerate(answers):
        if int(option.get("type") or 0) == 1:
            result.append(chr(ord("A") + index))
    return result


def _question_record(item: dict[str, Any], category: dict[str, str]) -> dict[str, Any] | None:
    question_id = item.get("questionId")
    if question_id is None:
        return None
    uuid = str(item.get("uuid") or "").strip()
    content = _clean_html(item.get("content") or item.get("title"))
    if not content:
        return None
    answers = [answer for answer in (item.get("answers") or []) if isinstance(answer, dict)]
    options = [_clean_html(answer.get("content")) for answer in answers]
    correct = _correct_letters(answers)
    question_type = int(item.get("type") or 1)
    type_label = "单选题" if question_type == 1 else "多选题" if question_type == 2 else "题目"
    analysis = _clean_html(item.get("analysis")) or "暂无官方解析。"
    reference = _clean_html(item.get("referenceAnswer"))
    tags = []
    for tag in item.get("knowledgeTags") or []:
        name = str(tag.get("name") or "").strip()
        if name and name not in tags:
            tags.append(name)
    if category["name"] not in tags:
        tags.append(category["name"])
    url = f"https://www.nowcoder.com/questionTerminal/{uuid}" if uuid else ""
    option_lines = [
        f"{chr(ord('A') + index)}. {option}"
        for index, option in enumerate(options)
        if option
    ]
    parts = [f"【题型】{type_label}", f"【题目】{content}"]
    if option_lines:
        parts.append("【选项】\n" + "\n".join(option_lines))
    if correct:
        parts.append("【正确答案】" + "、".join(correct))
    if reference:
        parts.append("【参考答案】" + reference)
    parts.append("【解析】" + analysis)
    parts.append("【知识点】" + "、".join(tags))
    if url:
        parts.append("【来源】" + url)
    knowledge_text = "\n\n".join(parts)

    bank_question = content
    if option_lines:
        bank_question += "\n" + "\n".join(option_lines)
    bank_ref = []
    if correct:
        bank_ref.append("正确答案：" + "、".join(correct))
    else:
        bank_ref.append("正确答案：未导出")
    if reference:
        bank_ref.append("参考答案：" + reference)
    bank_ref.append("解析：" + analysis)
    bank_ref.append("知识点：" + "、".join(tags))
    if url:
        bank_ref.append("来源：" + url)

    metadata = {
        "category": category["source"],
        "question_id": question_id,
        "uuid": uuid,
        "url": url,
        "tags": tags,
        "type": question_type,
        "correct": correct,
        "provider": "nowcoder",
        "llm_keyword_hit": bool(_LLM_KEYWORDS.search(
            " ".join([content, analysis, reference, " ".join(tags)])
        )),
    }
    return {
        "id": f"nowcoder-{question_id}",
        "source": category["source"],
        "section": "Nowcoder " + category["name"],
        "text": knowledge_text,
        "metadata": metadata,
        "bank": {
            "cat": category["bank_cat"],
            "from": f"牛客网 · {category['name']} · 题号{question_id}",
            "q": bank_question,
            "ref": bank_ref,
        },
    }


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-dir",
        default=os.path.join("E:\\面试猴", "import", "nowcoder_raw"),
        help="raw test/report JSON directory",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="defaults to %%APPDATA%%/InterviewMonkey/knowledge",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(os.environ.get("APPDATA", Path.home())) / "InterviewMonkey" / "knowledge"
    )
    records: dict[int, dict[str, Any]] = {}
    category_counts: dict[str, int] = {}
    missing: list[str] = []
    for category in CATEGORIES:
        source = None
        for filename in category["files"]:
            candidate = raw_dir / filename
            if candidate.is_file():
                source = candidate
                break
        if source is None:
            missing.append(category["name"] + " (" + " / ".join(category["files"]) + ")")
            continue
        items = _load_report(source)
        count = 0
        for item in items:
            record = _question_record(item, category)
            if record is None:
                continue
            question_id = int(record["metadata"]["question_id"])
            if question_id in records:
                continue
            records[question_id] = record
            count += 1
        category_counts[category["name"]] = count
        print(f"{category['name']}: {count} 题 ({source.name})")
    if missing:
        print("缺少原始文件: " + "; ".join(missing))
    if not records:
        print("没有可导入的数据。")
        return 1

    ordered = sorted(records.values(), key=lambda item: int(item["metadata"]["question_id"]))
    knowledge_lines = [
        json.dumps({key: item[key] for key in ("id", "source", "section", "text", "metadata")},
                   ensure_ascii=False, separators=(",", ":"))
        for item in ordered
    ]
    bank_items = [item["bank"] for item in ordered]
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": category_counts,
        "total": len(ordered),
        "llm_keyword_hits": sum(1 for item in ordered if item["metadata"].get("llm_keyword_hit")),
        "sources": [category["source"] for category in CATEGORIES],
    }
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("dry-run: 未写入文件")
        return 0

    knowledge_path = output_dir / "nowcoder.jsonl"
    bank_path = output_dir / "nowcoder_bank.json"
    manifest_path = output_dir / "nowcoder_import.json"
    _atomic_write(knowledge_path, "\n".join(knowledge_lines) + "\n")
    _atomic_write(bank_path, json.dumps(bank_items, ensure_ascii=False, separators=(",", ":")))
    _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    print("已写入:")
    print(" ", knowledge_path)
    print(" ", bank_path)
    print(" ", manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
