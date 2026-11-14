# -*- coding: utf-8 -*-
"""在现有 ASR 评测集上扫描 base VAD 静音阈值并输出 CER 曲线。"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.realtime.config import load_config
from eval.asr.evaluate import evaluate


DEFAULT_REPORT = ROOT / "eval" / "asr" / "reports" / "phase8_vad_grid.md"


async def run_grid(values: list[int], *, realtime: bool = False) -> dict:
    cfg = load_config(ROOT / "config" / "config.yaml")
    if len(values) < 2 or len(values) != len(set(values)):
        raise ValueError("至少需要 2 个不同静音阈值")
    if any(not cfg.vad.fast_silence_ms <= value <= cfg.vad.incomplete_silence_ms for value in values):
        raise ValueError(
            "base 阈值必须位于当前 fast_silence_ms 与 incomplete_silence_ms 之间"
        )

    results = []
    for index, value in enumerate(sorted(values), 1):
        print(f"[{index}/{len(values)}] base_silence_ms={value}：运行 33 条本地 ASR 评测")
        with redirect_stdout(io.StringIO()):
            _rows, summary = await evaluate(
                realtime=realtime,
                agc=cfg.audio.loopback_agc_enabled,
                two_pass=True,
                vad_overrides={"base_silence_ms": value},
            )
        results.append({"base_silence_ms": value, "summary": summary})
        print(
            f"  CER={summary['macro_cer']:.4%}, "
            f"术语召回={summary['hotword_recall']:.2%}, "
            f"问题准确率={summary['question_accuracy']:.2%}"
        )

    # CER 优先；同 CER 时优先术语/问题准确率，再选更短的常规静音等待。
    best = min(results, key=lambda item: (
        item["summary"]["macro_cer"],
        -item["summary"]["hotword_recall"],
        -item["summary"]["question_accuracy"],
        item["base_silence_ms"],
    ))
    baseline_path = ROOT / "eval" / "asr" / "reports" / "phase6_postprocess_fast.json"
    baseline = None
    if baseline_path.is_file():
        baseline_summary = json.loads(baseline_path.read_text(encoding="utf-8"))["summary"]
        baseline = {
            "report": str(baseline_path.relative_to(ROOT)),
            "macro_cer": baseline_summary["macro_cer"],
            "hotword_recall": baseline_summary["hotword_recall"],
            "question_accuracy": baseline_summary["question_accuracy"],
        }
    return {
        "dataset_count": results[0]["summary"]["count"],
        "realtime": realtime,
        "phase6_fast_baseline": baseline,
        "fixed_parameters": {
            "fast_silence_ms": cfg.vad.fast_silence_ms,
            "incomplete_silence_ms": cfg.vad.incomplete_silence_ms,
            "pre_roll_ms": cfg.vad.pre_roll_ms,
            "post_roll_ms": cfg.vad.post_roll_ms,
        },
        "results": results,
        "best": best,
    }


def _write_svg(data: dict, path: Path) -> None:
    points = [
        (item["base_silence_ms"], item["summary"]["macro_cer"] * 100)
        for item in data["results"]
    ]
    width, height = 760, 430
    left, right, top, bottom = 80, 30, 40, 70
    plot_w, plot_h = width - left - right, height - top - bottom
    low = min(value for _, value in points)
    high = max(value for _, value in points)
    padding = max((high - low) * 0.2, 0.05)
    low, high = max(0.0, low - padding), high + padding

    def xy(x: int, y: float) -> tuple[float, float]:
        x_min, x_max = min(value for value, _ in points), max(value for value, _ in points)
        px = left + (x - x_min) / max(x_max - x_min, 1) * plot_w
        py = top + (high - y) / max(high - low, 0.01) * plot_h
        return px, py

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(*point) for point in points))
    grid = []
    for tick in range(5):
        value = low + (high - low) * tick / 4
        y = top + plot_h * (1 - tick / 4)
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#d8dee8"/>'
            f'<text x="{left - 12}" y="{y + 5:.1f}" text-anchor="end" font-size="13">{value:.2f}%</text>'
        )
    marks = []
    for x_value, y_value in points:
        x, y = xy(x_value, y_value)
        marks.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#1665d8"/>'
            f'<text x="{x:.1f}" y="{y - 10:.1f}" text-anchor="middle" font-size="12">{y_value:.2f}%</text>'
            f'<text x="{x:.1f}" y="{top + plot_h + 25}" text-anchor="middle" font-size="13">{x_value}</text>'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="white"/>'
        f'<text x="{width / 2}" y="24" text-anchor="middle" font-size="17">ASR CER vs. base VAD silence threshold</text>'
        + "".join(grid)
        + f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#5b6575"/>'
        + f'<polyline points="{polyline}" fill="none" stroke="#1665d8" stroke-width="2.5"/>'
        + "".join(marks)
        + f'<text x="{left + plot_w / 2}" y="{height - 18}" text-anchor="middle" font-size="14">base_silence_ms</text>'
        + f'<text transform="translate(20 {top + plot_h / 2}) rotate(-90)" text-anchor="middle" font-size="14">CER (macro)</text>'
        + "</svg>"
    )
    path.write_text(svg, encoding="utf-8")


def write_report(data: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    best = data["best"]
    fixed = data["fixed_parameters"]
    baseline = data.get("phase6_fast_baseline")
    lines = [
        "# 阶段 8：VAD 静音阈值网格搜索",
        "",
        f"- 样本数：{data['dataset_count']} 条，本地两遍 ASR（Sherpa partial + SenseVoice final）",
        f"- 回放模式：{'实时' if data['realtime'] else '快速离线；CER 可比较，延迟不作为实机指标'}",
        f"- 固定阈值：fast={fixed['fast_silence_ms']} ms，incomplete={fixed['incomplete_silence_ms']} ms",
        f"- 固定缓冲：pre-roll={fixed['pre_roll_ms']} ms，post-roll={fixed['post_roll_ms']} ms",
        *([f"- 阶段 6 快速基线：CER={baseline['macro_cer']:.2%}，术语召回={baseline['hotword_recall']:.2%}，问题准确率={baseline['question_accuracy']:.2%}。"] if baseline else []),
        "- 搜索变量：`base_silence_ms`；其余 VAD、ASR、后处理参数保持当前配置。",
        "",
        "## 本阶段实现",
        "",
        "- 静音阈值保持自适应三档：快语速 300 ms、常规语速使用搜索参数、未完结句尾 700 ms；未完结标记覆盖“的、和、是、那个、就是”等。明确问号/感叹号结尾优先按完整句处理。",
        "- `pre_roll_ms` 至少 200 ms，`post_roll_ms` 至少 150 ms；配置校验同时约束 fast ≤ base ≤ incomplete 且各阈值在 300–700 ms。",
        "- 问题检测前合并间隔小于 1.5 秒、声道相同、说话人一致（若可识别）且前句没有句末语气的 final。完整句直接进入检测，避免额外等待。",
        "",
        "## CER 曲线",
        "",
        "![base VAD 静音阈值与 CER 曲线](phase8_vad_grid.svg)",
        "",
        "| base silence (ms) | CER | 术语召回率 | 问题级准确率 | final 延迟 P50 (ms) |",
        "|---:|---:|---:|---:|---:|",
    ]
    for item in data["results"]:
        summary = item["summary"]
        final_p50 = summary["final_p50_ms"]
        lines.append(
            f"| {item['base_silence_ms']} | {summary['macro_cer']:.2%} | "
            f"{summary['hotword_recall']:.2%} | {summary['question_accuracy']:.2%} | "
            f"{final_p50:.0f} |" if final_p50 is not None else
            f"| {item['base_silence_ms']} | {summary['macro_cer']:.2%} | "
            f"{summary['hotword_recall']:.2%} | {summary['question_accuracy']:.2%} | N/A |"
        )
    if baseline:
        lines.extend([
            "",
            "### 阶段 6 基线对比",
            "",
            "| 版本 | CER | 术语召回率 | 问题级准确率 |",
            "|---|---:|---:|---:|",
            f"| 阶段 6 快速基线 | {baseline['macro_cer']:.2%} | {baseline['hotword_recall']:.2%} | {baseline['question_accuracy']:.2%} |",
            f"| 阶段 8 最优（base={best['base_silence_ms']} ms） | {best['summary']['macro_cer']:.2%} | {best['summary']['hotword_recall']:.2%} | {best['summary']['question_accuracy']:.2%} |",
        ])
    lines.extend([
        "",
        "## 选择结果",
        "",
        f"CER 优先、术语召回和问题级准确率次序判定后，选出的 `base_silence_ms={best['base_silence_ms']}` ms，CER={best['summary']['macro_cer']:.2%}。",
        "若前几项完全相同，选择较短的 base 静音等待以减小响应延迟。该参数只更新 `vad.base_silence_ms`；fast/incomplete 继续采用配置值，未改变语速快句与未完结句的自适应分支。",
        "",
        "## 限制与复现",
        "",
        "本评测集现有 33 条样本全部为 TTS/加噪 TTS，没有人工校对的真人或会议环回音频。网格搜索结果是可复现工程对比，不代表真实会议环境的 CER 保证。",
        "配置默认仍保留原值；网格搜索不会自动改写 `config/config.yaml`。确认参数后可手动把选择值写入 `vad.base_silence_ms`。",
        "",
        "运行命令：",
        "",
        "```powershell",
        r".\.venv-pack\Scripts\python.exe -X utf8 eval\asr\tune_vad.py --values 300 400 500 600 700",
        "```",
        "",
    ])
    output.write_text("\n".join(lines), encoding="utf-8")
    output.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_svg(data, output.with_name("phase8_vad_grid.svg"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--values", nargs="+", type=int, default=[300, 400, 500, 600, 700])
    parser.add_argument("--realtime", action="store_true", help="按实时节奏回放；更准确测量延迟但运行更久")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    data = asyncio.run(run_grid(args.values, realtime=args.realtime))
    write_report(data, args.report.resolve())
    print(f"最佳 base_silence_ms={data['best']['base_silence_ms']} ms")
    print(f"报告: {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
