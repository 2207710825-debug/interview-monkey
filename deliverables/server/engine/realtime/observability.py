"""本机问答 JSONL 日志与可选的 VAD 片段归档。"""
from __future__ import annotations

import json
import os
import threading
import wave
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def default_qa_log_path() -> Path:
    """返回当前用户数据目录，避免把运行日志写入安装目录。"""
    override = os.environ.get("MH_QA_LOG_PATH")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "InterviewMonkey" / "logs" / "qa.jsonl"


class QALogWriter:
    """以单行 JSON 追加记录；音频只在配置明确启用时写入本机。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        record_audio: bool = False,
        audio_dir: str | Path | None = None,
    ):
        self.path = Path(path) if path is not None else default_qa_log_path()
        self.audio_dir = Path(audio_dir) if audio_dir is not None else self.path.parent / "audio"
        self.record_audio = record_audio
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        row = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **record,
        }
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")

    def save_audio(
        self,
        pcm: bytes,
        sample_rate: int,
        channel: str,
        start_ms: int,
        end_ms: int,
    ) -> Path | None:
        if not self.record_audio or not pcm:
            return None
        if sample_rate <= 0 or len(pcm) % 2:
            raise ValueError("音频归档要求有效采样率和 PCM16 数据")
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{channel}-{start_ms}-{end_ms}-{uuid4().hex[:10]}"
        target = self.audio_dir / f"{stem}.wav"
        temporary = self.audio_dir / f".{stem}.tmp.wav"
        try:
            with wave.open(str(temporary), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target.resolve()
