# -*- coding: utf-8 -*-
"""一条命令准备 TTS 样本并执行 ASR 基线评测。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.asr.evaluate import main as evaluate_main  # noqa: E402
from eval.asr.generate_tts import generate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成并运行 ASR 阶段 1 基线")
    parser.add_argument("--refresh-tts", action="store_true", help="强制重新生成全部 TTS 音频")
    parser.add_argument("--fast", action="store_true", help="跳过实时节奏等待，仅用于调试")
    parser.add_argument("--agc", action="store_true", help="启用轻量 AGC 做 A/B 评测")
    parser.add_argument("--two-pass", action="store_true", help="评测两遍识别")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    generate(force=args.refresh_tts)
    eval_args: list[str] = []
    if args.fast:
        eval_args.append("--fast")
    if args.agc:
        eval_args.append("--agc")
    if args.two_pass:
        eval_args.append("--two-pass")
    if args.report is not None:
        eval_args.extend(["--report", str(args.report)])
    return evaluate_main(eval_args)


if __name__ == "__main__":
    sys.exit(main())
