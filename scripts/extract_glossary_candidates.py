# -*- coding: utf-8 -*-
"""从本地知识库生成待人工确认的领域术语候选。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.realtime.asr.glossary import extract_tfidf_candidates, write_candidate_file


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 glossary.candidates.yaml（不会修改正式词库）")
    parser.add_argument("--knowledge", type=Path, default=ROOT / "engine" / "knowledge")
    parser.add_argument("--glossary", type=Path, default=ROOT / "config" / "glossary.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "config" / "glossary.candidates.yaml")
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()
    candidates = extract_tfidf_candidates(args.knowledge, args.glossary, limit=max(1, args.limit))
    write_candidate_file(args.output, candidates)
    print(f"已生成 {len(candidates)} 个待确认候选: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
