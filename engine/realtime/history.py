"""本机会话历史：问答记录 + 实时转写。

问答记录沿用 ``QALogWriter`` 写的 ``qa.jsonl``；本模块只读不写。
实时转写单独追加到同目录 ``transcripts.jsonl``，供“上下文模式”和
历史会话回看使用。全部数据留在本机，不上传。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .observability import default_qa_log_path


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def valid_session_id(session_id: str | None) -> bool:
    return bool(session_id and SESSION_ID_RE.fullmatch(session_id))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _row_session_id(row: dict[str, Any]) -> str:
    session_id = str(row.get("session_id") or "default").strip() or "default"
    return session_id


def _row_time(row: dict[str, Any]) -> str:
    value = row.get("recorded_at")
    if isinstance(value, str) and value:
        return value
    return ""


def _first_text(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for row in rows:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


class RealtimeHistoryStore:
    """读写本机实时会话历史。

    ``qa_path`` 读取 ``QALogWriter`` 的记录；``transcript_path`` 由
    ``RealtimePipeline`` 追加。两个路径都可由测试注入临时目录。
    """

    def __init__(
        self,
        qa_path: str | Path | None = None,
        transcript_path: str | Path | None = None,
    ):
        self.qa_path = Path(qa_path) if qa_path is not None else default_qa_log_path()
        self.transcript_path = (
            Path(transcript_path)
            if transcript_path is not None
            else self.qa_path.with_name("transcripts.jsonl")
        )
        self._lock = threading.Lock()

    # ---------- 写 ----------
    def append_transcript(self, record: dict[str, Any]) -> None:
        row = {
            "schema_version": 1,
            "recorded_at": _now_iso(),
            **record,
        }
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.transcript_path.open(
            "a", encoding="utf-8", newline="\n",
        ) as stream:
            stream.write(line + "\n")

    def delete_session(self, session_id: str) -> int:
        if not valid_session_id(session_id):
            raise ValueError("session_id 格式不合法")
        removed = 0
        with self._lock:
            for path in (self.qa_path, self.transcript_path):
                rows = _read_jsonl(path)
                if not rows:
                    continue
                kept = [row for row in rows if _row_session_id(row) != session_id]
                removed_now = len(rows) - len(kept)
                if removed_now:
                    removed += removed_now
                    _atomic_write_jsonl(path, kept)
        return removed

    # ---------- 读 ----------
    def _load(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return _read_jsonl(self.qa_path), _read_jsonl(self.transcript_path)

    @staticmethod
    def _dedupe_transcripts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            key = (
                row.get("utterance_id"),
                row.get("start_ms"),
                row.get("end_ms"),
                row.get("text"),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        qa_rows, transcript_rows = self._load()
        groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in qa_rows:
            groups.setdefault(_row_session_id(row), {"qa": [], "transcript": []})["qa"].append(row)
        for row in transcript_rows:
            groups.setdefault(_row_session_id(row), {"qa": [], "transcript": []})["transcript"].append(row)

        items: list[dict[str, Any]] = []
        for session_id, group in groups.items():
            qa = sorted(group["qa"], key=_row_time)
            transcript = self._dedupe_transcripts(group["transcript"])
            transcript.sort(key=lambda row: (row.get("start_ms") or 0, _row_time(row)))
            times = [value for value in (
                _row_time(row) for row in (qa + transcript)
            ) if value]
            title = _first_text(qa, ("question", "corrected_text", "raw_text"))
            if not title:
                title = _first_text(transcript, ("text",))
            if not title:
                title = "未命名会话"
            items.append({
                "session_id": session_id,
                "title": title[:100],
                "started_at": min(times) if times else "",
                "last_at": max(times) if times else "",
                "qa_count": len(qa),
                "transcript_count": len(transcript),
            })
        items.sort(key=lambda item: item.get("last_at") or "", reverse=True)
        return items[: max(1, min(int(limit), 200))]

    def get_session(
        self,
        session_id: str,
        *,
        limit_qa: int = 200,
        limit_transcript: int = 2000,
    ) -> dict[str, Any] | None:
        if not valid_session_id(session_id):
            raise ValueError("session_id 格式不合法")
        qa_rows, transcript_rows = self._load()
        qa = [row for row in qa_rows if _row_session_id(row) == session_id]
        transcript = [
            row for row in transcript_rows if _row_session_id(row) == session_id
        ]
        if not qa and not transcript:
            return None
        qa.sort(key=_row_time)
        transcript = self._dedupe_transcripts(transcript)
        transcript.sort(key=lambda row: (row.get("start_ms") or 0, _row_time(row)))
        if limit_qa > 0:
            qa = qa[-limit_qa:]
        if limit_transcript > 0:
            transcript = transcript[-limit_transcript:]
        times = [value for value in (
            _row_time(row) for row in (qa + transcript)
        ) if value]
        title = _first_text(qa, ("question", "corrected_text", "raw_text"))
        if not title:
            title = _first_text(transcript, ("text",)) or "未命名会话"
        return {
            "session_id": session_id,
            "title": title[:100],
            "started_at": min(times) if times else "",
            "last_at": max(times) if times else "",
            "qa_count": len(qa),
            "transcript_count": len(transcript),
            "qa": qa,
            "transcript": transcript,
        }
