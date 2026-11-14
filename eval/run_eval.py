# -*- coding: utf-8 -*-
"""20 条离线评测：命中率 + 无资料依据陈述占比。

真实评测：python eval/run_eval.py --answers path/to/answers.jsonl
answers.jsonl 每行格式：{"id":"q01","answer":"..."}
``--mock`` 只验证评测器本身，不代表模型效果。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(dataset, answers):
    answer_map = {item["id"]: item.get("answer", "") for item in answers}
    hit, expected, unsupported, statements = 0, 0, 0, 0
    details = []
    for item in dataset:
        answer = answer_map.get(item["id"], "")
        terms = item["required_terms"]
        matched = [term for term in terms if term.casefold() in answer.casefold()]
        hit += len(matched)
        expected += len(terms)
        support_terms = set(terms + re.findall(r"[A-Za-z][A-Za-z0-9_-]+|[\u4e00-\u9fff]{2,}", item["source"]))
        sentences = [part.strip() for part in re.split(r"[。！？!?\n]+", answer) if part.strip()]
        for sentence in sentences:
            statements += 1
            if not any(term.casefold() in sentence.casefold() for term in support_terms):
                unsupported += 1
        details.append({"id": item["id"], "matched": matched, "required": terms})
    return {
        "questions": len(dataset),
        "term_hit_rate": round(hit / expected, 4) if expected else 0.0,
        "hallucination_proxy_rate": round(unsupported / statements, 4) if statements else 0.0,
        "note": "hallucination_proxy_rate 是词项证据代理指标，必须结合人工复核",
        "details": details,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(Path(__file__).with_name("eval_set.jsonl")))
    parser.add_argument("--answers")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    dataset = load_jsonl(Path(args.dataset))
    if args.mock:
        answers = [
            {"id": item["id"], "answer": "；".join(item["required_terms"]) + "。" + item["source"]}
            for item in dataset
        ]
    elif args.answers:
        answers = load_jsonl(Path(args.answers))
    else:
        parser.error("请传 --answers，或用 --mock 仅验证评测器")
    print(json.dumps(evaluate(dataset, answers), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
