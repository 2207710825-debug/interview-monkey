"""比较当前 ASR 评测结果与已验证的质量基准，供本地和 CI 共用。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "eval" / "asr" / "reports" / "phase6_postprocess_fast.json"


def load_summary(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and isinstance(data.get("summary"), dict):
        return data["summary"]
    if isinstance(data, dict) and isinstance(data.get("best", {}).get("summary"), dict):
        return data["best"]["summary"]
    raise ValueError(f"评测报告缺少 summary: {path}")


def compare_summaries(candidate: dict, baseline: dict) -> list[str]:
    failures = []
    candidate_cer = float(candidate["macro_cer"])
    baseline_cer = float(baseline["macro_cer"])
    candidate_recall = float(candidate["hotword_recall"])
    baseline_recall = float(baseline["hotword_recall"])
    if candidate_cer > baseline_cer + 1e-12:
        failures.append(f"CER 回退：{candidate_cer:.6%} > 基准 {baseline_cer:.6%}")
    if candidate_recall + 1e-12 < baseline_recall:
        failures.append(f"术语召回率回退：{candidate_recall:.6%} < 基准 {baseline_recall:.6%}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASR CER 与术语召回率非回退门禁")
    parser.add_argument("--candidate", type=Path, required=True, help="evaluate.py 生成的 JSON 报告")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)
    candidate = load_summary(args.candidate)
    baseline = load_summary(args.baseline)
    failures = compare_summaries(candidate, baseline)
    print(f"CER: candidate={candidate['macro_cer']:.6%}, baseline={baseline['macro_cer']:.6%} (越低越好)")
    print(
        "术语召回率: "
        f"candidate={candidate['hotword_recall']:.6%}, baseline={baseline['hotword_recall']:.6%} (越高越好)"
    )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: CER 和术语召回率均未相对已验证基准回退")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
