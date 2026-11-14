"""用问答日志或 ASR 评测 JSON 重放本地音频链路。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_records(path: str | Path) -> list[dict]:
    source = Path(path)
    content = source.read_text(encoding="utf-8-sig")
    if source.suffix.casefold() == ".jsonl":
        return [json.loads(line) for line in content.splitlines() if line.strip()]
    data = json.loads(content)
    if isinstance(data, dict):
        return list(data.get("rows", []))
    if isinstance(data, list):
        return data
    raise ValueError("日志必须是 JSONL、rows JSON 报告或 JSON 数组")


def _audio_paths(record: dict, log_path: Path) -> list[dict]:
    refs = record.get("audio_paths") or []
    if not refs and record.get("audio_path"):
        refs = [record["audio_path"]]
    if not refs and record.get("id"):
        sample = ROOT / "eval" / "asr" / "samples" / f"{record['id']}.wav"
        if sample.is_file():
            refs = [sample]
    result = []
    for item in refs:
        value = item.get("path") if isinstance(item, dict) else item
        channel = str(item.get("channel", "loopback")) if isinstance(item, dict) else "loopback"
        if not value:
            continue
        audio = Path(value).expanduser()
        if not audio.is_absolute():
            candidates = (log_path.parent / audio, ROOT / audio)
            audio = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
        if not audio.is_file():
            raise FileNotFoundError(f"日志引用的音频不存在: {audio}")
        result.append({"path": audio.resolve(), "channel": channel})
    return result


class WaveFileAudioSource:
    """将本机 16 kHz / mono / PCM16 WAV 转为实时链路使用的音频帧。"""

    def __init__(self, paths: list[Path], frame_ms: int = 20, channels: list[str] | None = None):
        self.audio_paths = [str(path) for path in paths]
        self.channels = channels or ["loopback"] * len(paths)
        self.frame_ms = frame_ms

    async def frames(self):
        from engine.realtime.contracts import AudioFrame, Channel

        timestamp_ms = 0
        for path, channel_name in zip(self.audio_paths, self.channels):
            channel = Channel(channel_name)
            with wave.open(path, "rb") as wav:
                if (
                    wav.getnchannels() != 1
                    or wav.getsampwidth() != 2
                    or wav.getframerate() != 16_000
                    or wav.getcomptype() != "NONE"
                ):
                    raise ValueError(f"回放音频必须是 16 kHz mono PCM16 WAV: {path}")
                pcm = wav.readframes(wav.getnframes())
            frame_bytes = int(16_000 * self.frame_ms / 1000) * 2
            for offset in range(0, len(pcm), frame_bytes):
                frame = pcm[offset:offset + frame_bytes]
                if len(frame) < frame_bytes:
                    frame += bytes(frame_bytes - len(frame))
                yield AudioFrame(frame, timestamp_ms, channel, 16_000)
                timestamp_ms += self.frame_ms


async def replay_record(record: dict, log_path: Path, config_path: Path, *, use_local_llm: bool) -> dict:
    from engine.realtime.api.app import build_default_service
    from engine.realtime.api.runtime import CaptureRuntime
    from engine.realtime.config import load_config
    from engine.realtime.mocks import MockLLMProvider
    from engine.realtime.contracts import TranscriptKind

    cfg = load_config(config_path)
    if not cfg.privacy.local_mode:
        raise PermissionError("回放仅允许在 privacy.local_mode=true 时运行")
    service = build_default_service(config_path)
    if not use_local_llm:
        service.llm_provider = MockLLMProvider(["本地回放完成。"])
    runtime = CaptureRuntime(cfg, service, ROOT)
    audio_refs = _audio_paths(record, log_path)
    if not audio_refs:
        raise ValueError(f"记录 {record.get('id') or record.get('trace_id')} 没有 audio_path")
    paths = [item["path"] for item in audio_refs]
    source = WaveFileAudioSource(
        paths, cfg.audio.frame_ms, [item["channel"] for item in audio_refs],
    )
    pipeline = runtime._pipeline(source)
    stream = pipeline.bus.subscribe("ui")
    observed = {"transcripts": [], "question_detected": False, "question": "", "answer": []}

    async def collect():
        async for envelope in stream:
            event_type = envelope.get("type")
            payload = envelope.get("payload")
            if event_type == "transcript" and payload.kind is TranscriptKind.FINAL:
                observed["transcripts"].append(payload.text)
            elif event_type == "question_decision":
                observed["question_detected"] = bool(payload.is_question)
            elif event_type == "answer":
                if payload.event == "meta":
                    observed["question"] = str(payload.data.get("question", ""))
                elif payload.event == "token":
                    observed["answer"].append(str(payload.data.get("delta", "")))

    collector = asyncio.create_task(collect())
    try:
        await pipeline.run(source, f"replay-{record.get('id') or record.get('trace_id') or 'sample'}")
    finally:
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)

    from eval.asr.evaluate import cer, term_present

    expected = str(
        record.get("reference")
        or record.get("corrected_text")
        or record.get("hypothesis")
        or ""
    )
    hypothesis = "".join(observed["transcripts"])
    terms = [str(term) for term in record.get("terms", [])]
    terms_hit = sum(term_present(term, hypothesis) for term in terms)
    return {
        "id": record.get("id") or record.get("trace_id"),
        "audio_paths": [str(path) for path in paths],
        "expected_text": expected,
        "hypothesis": hypothesis,
        "cer": cer(expected, hypothesis) if expected else None,
        "terms_total": len(terms),
        "terms_hit": terms_hit,
        "question_detected": observed["question_detected"],
        "question_sent_to_llm": observed["question"],
        "answer": "".join(observed["answer"]),
    }


async def _run(args) -> list[dict]:
    log_path = args.log.resolve()
    config_path = args.config.resolve()
    records = load_records(log_path)
    if args.limit:
        records = records[:args.limit]
    results = []
    for index, record in enumerate(records, 1):
        result = await replay_record(
            record, log_path, config_path, use_local_llm=args.use_local_llm,
        )
        results.append(result)
        cer_value = "N/A" if result["cer"] is None else f"{result['cer']:.3%}"
        print(
            f"[{index}/{len(records)}] {result['id']} CER={cer_value} "
            f"question={'yes' if result['question_detected'] else 'no'}"
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重放问答 JSONL 日志中的本地音频")
    parser.add_argument("--log", type=Path, required=True, help="qa.jsonl 或 ASR 评测 JSON 报告")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config.yaml")
    parser.add_argument("--limit", type=int, default=0, help="最多重放条数；0 表示全部")
    parser.add_argument("--output", type=Path, help="可选的 JSON 回放结果路径")
    parser.add_argument(
        "--use-local-llm", action="store_true",
        help="使用 config.yaml 中已配置的本地 LLM；默认用 Mock 避免模型差异",
    )
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit 不能为负数")
    results = asyncio.run(_run(args))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果：{args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
