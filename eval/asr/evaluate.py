# -*- coding: utf-8 -*-
"""在项目当前生产链路上执行 ASR 基线评测并生成 Markdown 报告。"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import re
import statistics
import sys
import time
import unicodedata
import wave
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CASES_PATH = HERE / "cases.yaml"
SAMPLES_DIR = HERE / "samples"
DEFAULT_REPORT = HERE / "reports" / "baseline.md"
_DROP = re.compile(r"[\s，。！？、；：·“”‘’「」『』（）〈〉《》…—,.!?;:'\"()\[\]{}]+")


def normalize_text(text: str) -> str:
    return _DROP.sub("", unicodedata.normalize("NFKC", text or "")).casefold()


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, char_left in enumerate(left, 1):
        current = [row]
        for column, char_right in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (char_left != char_right),
            ))
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float:
    reference_norm = normalize_text(reference)
    if not reference_norm:
        return 0.0 if not normalize_text(hypothesis) else 1.0
    return edit_distance(reference_norm, normalize_text(hypothesis)) / len(reference_norm)


def canonicalize_colloquial(text: str) -> str:
    """只用于评测：把标准文本和识别文本做相同的口癖/ITN 归一化。"""
    from engine.realtime.asr.postprocess import clean_colloquial, normalize_itn

    cleaned, _ = clean_colloquial(text)
    normalized, _ = normalize_itn(cleaned)
    return normalized


def canonical_cer(reference: str, hypothesis: str) -> float:
    return cer(canonicalize_colloquial(reference), canonicalize_colloquial(hypothesis))


def term_present(term: str, text: str) -> bool:
    return normalize_text(term) in normalize_text(text)


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


@dataclass(slots=True)
class Recognition:
    text: str
    first_partial_ms: float | None
    final_latency_ms: float | None
    processing_ms: float
    pass1_ms: float | None = None
    pass2_ms: float | None = None
    asr_engine: str | None = None
    raw_text: str | None = None
    preprocess_text: str | None = None


def load_cases() -> list[dict]:
    import yaml

    data = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8")) or {}
    cases = list(data.get("cases", []))
    ids = [str(item.get("id", "")) for item in cases]
    if len(cases) < 30:
        raise RuntimeError(f"评测集至少需要 30 条，当前只有 {len(cases)} 条")
    if len(ids) != len(set(ids)):
        raise RuntimeError("cases.yaml 中存在重复 id")
    return cases


def read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    if sample_width != 2 or channels != 1 or sample_rate != 16_000:
        raise RuntimeError(
            f"{path.name} 必须为 16kHz/mono/PCM16，实际为 "
            f"{sample_rate}Hz/{channels}ch/{sample_width * 8}bit"
        )
    return pcm


def condition_pcm(pcm, cfg):
    """按生产 20ms 分帧方式应用轻量 AGC，用于离线 A/B。"""
    import numpy as np
    from engine.capture import AudioConditioner

    conditioner = AudioConditioner(
        True,
        cfg.audio.target_rms_dbfs,
        cfg.audio.limiter_dbfs,
        cfg.audio.max_gain_db,
    )
    frame_samples = int(cfg.audio.sample_rate * cfg.audio.frame_ms / 1000)
    return np.concatenate([
        conditioner.process(pcm[offset:offset + frame_samples])
        for offset in range(0, len(pcm), frame_samples)
    ])


def _load_runtime_config():
    from engine.realtime.config import load_config

    return load_config(ROOT / "config" / "config.yaml")


def _load_hotwords(cfg):
    from engine.realtime.asr.hotwords import load_hotwords

    path = Path(cfg.asr.hotwords_path)
    if not path.is_absolute():
        path = ROOT / path
    return load_hotwords(path)


async def recognize_current(pcm, engine, cfg, corrector, realtime: bool) -> Recognition:
    """复用当前 StreamingSession + AdaptiveVadSegmenter，不修改生产实现。"""
    from engine.asr import StreamingSession
    from engine.realtime.contracts import AudioFrame, Channel
    from engine.realtime.vad.adaptive import AdaptiveVadSegmenter

    session = StreamingSession(engine_obj=engine)
    speech_started_at: float | None = None
    speech_ended_at: float | None = None
    first_partial_ms: float | None = None
    finals: list[str] = []
    processing_ms = 0.0

    def boundary(event) -> None:
        nonlocal speech_started_at, speech_ended_at
        now = time.perf_counter()
        if event.kind == "speech_start" and speech_started_at is None:
            speech_started_at = now
        elif event.kind == "speech_end":
            speech_ended_at = now

    vad_cfg = cfg.vad
    vad = AdaptiveVadSegmenter(
        energy_threshold=vad_cfg.energy_threshold,
        base_silence_ms=vad_cfg.base_silence_ms,
        fast_silence_ms=vad_cfg.fast_silence_ms,
        incomplete_silence_ms=vad_cfg.incomplete_silence_ms,
        pre_roll_ms=vad_cfg.pre_roll_ms,
        post_roll_ms=vad_cfg.post_roll_ms,
        max_utterance_ms=vad_cfg.max_utterance_ms,
        on_boundary=boundary,
    )

    async def frames():
        frame_samples = int(cfg.audio.sample_rate * cfg.audio.frame_ms / 1000)
        started = time.perf_counter()
        for offset in range(0, len(pcm), frame_samples):
            chunk = pcm[offset:offset + frame_samples]
            if len(chunk) < frame_samples:
                import numpy as np
                chunk = np.pad(chunk, (0, frame_samples - len(chunk)))
            if realtime:
                due = started + offset / cfg.audio.sample_rate
                delay = due - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            yield AudioFrame(
                chunk.astype("int16", copy=False).tobytes(),
                int(offset / cfg.audio.sample_rate * 1000),
                Channel.LOOPBACK,
                cfg.audio.sample_rate,
            )

    async for frame in vad.segment(frames()):
        before = time.perf_counter()
        text = await asyncio.to_thread(session.feed, frame.pcm, frame.sample_rate)
        processing_ms += (time.perf_counter() - before) * 1000
        text = corrector.correct(text)
        if text:
            vad.observe_partial(text, max(1, int(frame.timestamp_ms + frame.duration_ms)))
        pending = getattr(session, "_pending", None)
        if pending:
            session._pending = None
            finals.append(corrector.correct(pending))
        elif text and first_partial_ms is None and speech_started_at is not None:
            first_partial_ms = (time.perf_counter() - speech_started_at) * 1000

    before = time.perf_counter()
    tail = corrector.correct(await asyncio.to_thread(session.finish))
    processing_ms += (time.perf_counter() - before) * 1000
    pending = getattr(session, "_pending", None)
    if pending:
        finals.append(corrector.correct(pending))
    elif tail and not finals:
        finals.append(tail)
    final_at = time.perf_counter()
    final_latency_ms = None
    if speech_ended_at is not None:
        final_latency_ms = max(0.0, (final_at - speech_ended_at) * 1000)
    return Recognition(
        "".join(finals).strip(), first_partial_ms, final_latency_ms,
        processing_ms, pass1_ms=processing_ms, asr_engine="sherpa_transducer",
    )


async def recognize_two_pass(pcm, provider, cfg, realtime: bool) -> Recognition:
    from engine.realtime.contracts import AudioFrame, Channel
    from engine.realtime.vad.adaptive import AdaptiveVadSegmenter

    speech_started_at: float | None = None
    speech_ended_at: float | None = None
    first_partial_ms: float | None = None
    final_latency_ms: float | None = None
    final_texts: list[str] = []
    raw_final_texts: list[str] = []
    preprocess_texts: list[str] = []
    pass1_ms = 0.0
    pass2_ms = 0.0
    final_engine: str | None = None

    def boundary(event) -> None:
        nonlocal speech_started_at, speech_ended_at
        now = time.perf_counter()
        if event.kind == "speech_start" and speech_started_at is None:
            speech_started_at = now
        elif event.kind == "speech_end":
            speech_ended_at = now

    vad_cfg = cfg.vad
    vad = AdaptiveVadSegmenter(
        energy_threshold=vad_cfg.energy_threshold,
        base_silence_ms=vad_cfg.base_silence_ms,
        fast_silence_ms=vad_cfg.fast_silence_ms,
        incomplete_silence_ms=vad_cfg.incomplete_silence_ms,
        pre_roll_ms=vad_cfg.pre_roll_ms,
        post_roll_ms=vad_cfg.post_roll_ms,
        max_utterance_ms=vad_cfg.max_utterance_ms,
        on_boundary=boundary,
    )

    async def frames():
        import numpy as np

        frame_samples = int(cfg.audio.sample_rate * cfg.audio.frame_ms / 1000)
        started = time.perf_counter()
        for offset in range(0, len(pcm), frame_samples):
            chunk = pcm[offset:offset + frame_samples]
            if len(chunk) < frame_samples:
                chunk = np.pad(chunk, (0, frame_samples - len(chunk)))
            if realtime:
                due = started + offset / cfg.audio.sample_rate
                delay = due - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            yield AudioFrame(
                chunk.astype("int16", copy=False).tobytes(),
                int(offset / cfg.audio.sample_rate * 1000),
                Channel.LOOPBACK,
                cfg.audio.sample_rate,
            )

    async for transcript in provider.transcribe(vad.segment(frames())):
        if transcript.kind.value == "partial":
            if first_partial_ms is None and speech_started_at is not None:
                first_partial_ms = (time.perf_counter() - speech_started_at) * 1000.0
            vad.observe_partial(
                transcript.text,
                max(1, transcript.end_ms - transcript.start_ms),
            )
            continue
        final_texts.append(transcript.text)
        raw_final_texts.append(transcript.raw_text or transcript.text)
        preprocess_texts.append(transcript.preprocess_text or transcript.text)
        pass1_ms += transcript.pass1_ms or 0.0
        pass2_ms += transcript.pass2_ms or 0.0
        final_engine = transcript.asr_engine
        if speech_ended_at is not None:
            final_latency_ms = max(0.0, (time.perf_counter() - speech_ended_at) * 1000.0)

    return Recognition(
        "".join(final_texts).strip(),
        first_partial_ms,
        final_latency_ms,
        pass1_ms + pass2_ms,
        pass1_ms=pass1_ms,
        pass2_ms=pass2_ms or None,
        asr_engine=final_engine,
        raw_text="".join(raw_final_texts).strip(),
        preprocess_text="".join(preprocess_texts).strip(),
    )


async def evaluate(
    realtime: bool,
    agc: bool = False,
    two_pass: bool = False,
    vad_overrides: dict[str, int] | None = None,
) -> tuple[list[dict], dict]:
    from engine import asr
    from engine.realtime.asr.hotwords import HotwordCorrector
    from engine.realtime.contracts import Channel, Transcript, TranscriptKind
    from engine.realtime.qa.detector import ChannelSpeakerGate, RuleQuestionDetector

    cases = load_cases()
    cfg = _load_runtime_config()
    if vad_overrides:
        cfg = replace(cfg, vad=replace(cfg.vad, **vad_overrides))
    hotwords = _load_hotwords(cfg)
    corrector = HotwordCorrector(hotwords)
    engine = None
    provider = None
    if two_pass:
        from engine.realtime.asr.providers import LocalSherpaASRProvider
        from engine.realtime.asr.postprocess import (
            PostprocessingASRProvider,
            TranscriptPostprocessor,
        )
        provider = LocalSherpaASRProvider(
            hotwords=hotwords,
            hotword_boost=cfg.asr.hotword_boost,
            two_pass_enabled=True,
            two_pass_engine=cfg.asr.two_pass.engine,
            second_pass_timeout_ms=cfg.asr.two_pass.timeout_ms,
        )
        provider = PostprocessingASRProvider(
            provider,
            TranscriptPostprocessor(
                hotwords,
                fuzzy_threshold=cfg.postprocess.fuzzy_threshold,
                suspicious_threshold=cfg.postprocess.suspicious_threshold,
                fragment_min_chars=cfg.postprocess.fragment_min_chars,
            ),
        )
    else:
        engine = await asyncio.to_thread(asr._SherpaOnnx)
    detector = RuleQuestionDetector(ChannelSpeakerGate())
    rows: list[dict] = []
    for index, case in enumerate(cases, 1):
        audio = SAMPLES_DIR / f"{case['id']}.wav"
        if not audio.is_file():
            raise FileNotFoundError(f"缺少评测音频: {audio}")
        pcm = read_wav(audio)
        if agc:
            pcm = condition_pcm(pcm, cfg)
        if two_pass:
            recognition = await recognize_two_pass(pcm, provider, cfg, realtime)
        else:
            recognition = await recognize_current(pcm, engine, cfg, corrector, realtime)
        end_ms = int(len(pcm) / 16_000 * 1000)
        transcript = Transcript(
            TranscriptKind.FINAL, recognition.text, Channel.LOOPBACK, 0, end_ms,
        )
        decision = await detector.evaluate(transcript, [])
        expected = str(case["expected_question"])
        similarity = 1.0 - cer(expected, decision.normalized_query)
        terms = [str(term) for term in case.get("terms", [])]
        terms_hit = sum(term_present(term, recognition.text) for term in terms)
        question_ok = bool(decision.is_question and similarity >= 0.70 and terms_hit == len(terms))
        row = {
            "id": case["id"],
            "category": case["category"],
            "source": case["source"],
            "audio_path": str(audio.relative_to(ROOT)).replace("\\", "/"),
            "reference": case["reference"],
            "expected_question": expected,
            "terms": terms,
            "hypothesis": recognition.text,
            "raw_hypothesis": recognition.raw_text or recognition.text,
            "preprocess_hypothesis": recognition.preprocess_text or recognition.text,
            "raw_cer": cer(str(case["reference"]), recognition.raw_text or recognition.text),
            "preprocess_cer": cer(
                str(case["reference"]), recognition.preprocess_text or recognition.text,
            ),
            "preprocess_canonical_cer": canonical_cer(
                str(case["reference"]), recognition.preprocess_text or recognition.text,
            ),
            "canonical_cer": canonical_cer(str(case["reference"]), recognition.text),
            "cer": cer(str(case["reference"]), recognition.text),
            "terms_total": len(terms),
            "terms_hit": terms_hit,
            "question_detected": decision.is_question,
            "question_similarity": similarity,
            "question_ok": question_ok,
            "first_partial_ms": recognition.first_partial_ms,
            "final_latency_ms": recognition.final_latency_ms,
            "processing_ms": recognition.processing_ms,
            "pass1_ms": recognition.pass1_ms,
            "pass2_ms": recognition.pass2_ms,
            "asr_engine": recognition.asr_engine,
        }
        rows.append(row)
        print(
            f"[{index:02d}/{len(cases)}] {case['id']} "
            f"CER={row['cer']:.3f} terms={terms_hit}/{len(terms)} "
            f"question={'OK' if question_ok else 'FAIL'}"
        )

    terms_total = sum(row["terms_total"] for row in rows)
    terms_hit = sum(row["terms_hit"] for row in rows)
    partials = [row["first_partial_ms"] for row in rows if row["first_partial_ms"] is not None]
    finals = [row["final_latency_ms"] for row in rows if row["final_latency_ms"] is not None]
    pass2_values = [row["pass2_ms"] for row in rows if row["pass2_ms"] is not None]
    sources = Counter(row["source"] for row in rows)
    summary = {
        "count": len(rows),
        "macro_cer": statistics.fmean(row["cer"] for row in rows),
        "raw_macro_cer": statistics.fmean(row["raw_cer"] for row in rows),
        "preprocess_macro_cer": statistics.fmean(row["preprocess_cer"] for row in rows),
        "preprocess_canonical_cer": statistics.fmean(row["preprocess_canonical_cer"] for row in rows),
        "canonical_macro_cer": statistics.fmean(row["canonical_cer"] for row in rows),
        "terms_total": terms_total,
        "terms_hit": terms_hit,
        "hotword_recall": terms_hit / terms_total if terms_total else 1.0,
        "term_error_rate": (terms_total - terms_hit) / terms_total if terms_total else 0.0,
        "question_accuracy": sum(row["question_ok"] for row in rows) / len(rows),
        "partial_p50_ms": percentile(partials, 0.50),
        "partial_p95_ms": percentile(partials, 0.95),
        "final_p50_ms": percentile(finals, 0.50),
        "final_p95_ms": percentile(finals, 0.95),
        "pass2_p50_ms": percentile(pass2_values, 0.50),
        "pass2_p95_ms": percentile(pass2_values, 0.95),
        "sources": sources,
        "realtime": realtime,
        "agc": agc,
        "two_pass": two_pass,
    }
    return rows, summary


def _ms(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f}"


def write_report(rows: list[dict], summary: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = summary["sources"]
    human_count = sources.get("human", 0) + sources.get("loopback_real", 0)
    lines = [
        "# ASR 两遍识别评测报告" if summary.get("two_pass") else "# ASR 阶段 1 基线报告",
        "",
        f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- Python：{platform.python_version()}",
        (
            "- 生产链路：`VAD → sherpa partial → SenseVoice final + explicit glossary aliases → deterministic postprocess → question detection`"
            if summary.get("two_pass")
            else "- 生产链路：`AdaptiveVadSegmenter → StreamingSession(zh_en) → HotwordCorrector`"
        ),
        f"- 评测模式：{'实时节奏回放' if summary['realtime'] else '快速离线回放（延迟仅供调试）'}",
        f"- 音频电平调节：{'AGC 开启' if summary.get('agc') else 'AGC 关闭'}",
        f"- 样本数：{summary['count']}",
        "",
        "## 汇总指标",
        "",
        "| 指标 | 指标值 |",
        "|---|---:|",
        f"| CER（macro） | {summary['macro_cer']:.2%} |",
        *(
            [
                f"| 原始解码 CER（macro） | {summary['raw_macro_cer']:.2%} |",
                f"| 后处理器输入 CER（macro） | {summary['preprocess_macro_cer']:.2%} |",
                f"| CER 变化（输出减后处理器输入） | {summary['macro_cer'] - summary['preprocess_macro_cer']:+.2%} |",
                f"| 对称清理后的 CER（macro） | {summary['canonical_macro_cer']:.2%} |",
                f"| 清理后后处理器输入 CER | {summary['preprocess_canonical_cer']:.2%} |",
            ]
            if summary.get("two_pass") else []
        ),
        f"| 热词召回率 | {summary['hotword_recall']:.2%} ({summary['terms_hit']}/{summary['terms_total']}) |",
        f"| 术语误识率 | {summary['term_error_rate']:.2%} |",
        f"| 问题级准确率 | {summary['question_accuracy']:.2%} |",
        f"| 首个 partial 延迟 P50 | {_ms(summary['partial_p50_ms'])} ms |",
        f"| 首个 partial 延迟 P95 | {_ms(summary['partial_p95_ms'])} ms |",
        f"| final 延迟 P50 | {_ms(summary['final_p50_ms'])} ms |",
        f"| final 延迟 P95 | {_ms(summary['final_p95_ms'])} ms |",
        f"| 第二遍解码 P50 | {_ms(summary.get('pass2_p50_ms'))} ms |",
        f"| 第二遍解码 P95 | {_ms(summary.get('pass2_p95_ms'))} ms |",
        "",
        "术语误识率定义为“标注中的目标术语未在最终文本中以规范形式出现”的比例；",
        "对称清理 CER 在参考文本和识别文本两侧均应用相同的口癖清理与 ITN，单列展示以避免因规范化导致的表面删除错误被隐藏。",
        "问题级准确率使用可重复代理判定：问题检测通过、与标准问题相似度不低于 0.70，且目标术语全部保留。",
        (
            "本离线评测只运行确定性文本后处理；可选本地 LLM 纠错需要实时问答请求，故不计入 CER 对比。"
            if summary.get("two_pass") else ""
        ),
        (
            "final 延迟从显式 utterance_end 边界开始计算，包含第二遍整句解码。"
            if summary.get("two_pass")
            else "final 延迟包含当前实现等待文件尾 flush 的时间；两遍模式使用显式 utterance_end 重新测量。"
        ),
        "",
        "## 数据覆盖",
        "",
        "| 来源 | 数量 |",
        "|---|---:|",
    ]
    for source, count in sorted(sources.items()):
        lines.append(f"| {source} | {count} |")
    command_flags = []
    if not summary.get("realtime"):
        command_flags.append("--fast")
    if summary.get("agc"):
        command_flags.append("--agc")
    if summary.get("two_pass"):
        command_flags.append("--two-pass")
    command = r".\.venv-pack\Scripts\python.exe -X utf8 eval\asr\run.py"
    if command_flags:
        command += " " + " ".join(command_flags)
    lines.extend([
        f"| human / loopback_real | {human_count} |",
        "",
    ])
    if human_count == 0:
        lines.extend([
            "> **覆盖缺口：当前没有人工校对的真人或真实会议环回样本。** "
            "本报告可以作为可重复的 TTS 工程基线，但不能替代真实场景验收。",
            "",
        ])
    lines.extend([
        "## 明细",
        "",
        "| ID | 类别 | 标准文本 | 后处理器输入 | 当前转写 | CER | 术语 | 问题 | partial(ms) | final(ms) | pass2(ms) |",
        "|---|---|---|---|---|---:|---:|---|---:|---:|---:|",
    ])
    for row in rows:
        reference = str(row["reference"]).replace("|", "\\|")
        preprocess_hypothesis = str(row.get("preprocess_hypothesis") or row["hypothesis"] or "（空）").replace("|", "\\|")
        hypothesis = str(row["hypothesis"] or "（空）").replace("|", "\\|")
        lines.append(
            f"| {row['id']} | {row['category']} | {reference} | {preprocess_hypothesis} | {hypothesis} | "
            f"{row['cer']:.3f} | {row['terms_hit']}/{row['terms_total']} | "
            f"{'通过' if row['question_ok'] else '失败'} | {_ms(row['first_partial_ms'])} | "
            f"{_ms(row['final_latency_ms'])} | {_ms(row.get('pass2_ms'))} |"
        )
    lines.extend([
        "",
        "## 复现命令",
        "",
        "```powershell",
        r"cd E:\面试猴\面试猴-app",
        command,
        "```",
        "",
    ])
    output.write_text("\n".join(lines), encoding="utf-8")
    machine_output = output.with_suffix(".json")
    serializable_summary = {**summary, "sources": dict(summary["sources"])}
    machine_output.write_text(
        json.dumps({"summary": serializable_summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行当前 ASR 生产链路基线评测")
    parser.add_argument("--fast", action="store_true", help="不按实时节奏等待，仅用于快速调试")
    parser.add_argument("--agc", action="store_true", help="按生产分帧方式开启轻量 AGC")
    parser.add_argument("--two-pass", action="store_true", help="评测 sherpa partial + SenseVoice final")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    rows, summary = asyncio.run(evaluate(
        realtime=not args.fast,
        agc=args.agc,
        two_pass=args.two_pass,
    ))
    write_report(rows, summary, args.report)
    print("=" * 72)
    print(f"CER={summary['macro_cer']:.4f}")
    print(f"Hotword Recall={summary['hotword_recall']:.4f}")
    print(f"Term Error Rate={summary['term_error_rate']:.4f}")
    print(f"Question Accuracy={summary['question_accuracy']:.4f}")
    print(
        f"Partial P50/P95={_ms(summary['partial_p50_ms'])}/{_ms(summary['partial_p95_ms'])} ms"
    )
    print(f"Final P50/P95={_ms(summary['final_p50_ms'])}/{_ms(summary['final_p95_ms'])} ms")
    print(f"Pass2 P50/P95={_ms(summary['pass2_p50_ms'])}/{_ms(summary['pass2_p95_ms'])} ms")
    print(f"Report={args.report.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
