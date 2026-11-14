"""仅供本机联调使用的 fixture Mock；不调用 ASR、RAG 或大模型。"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .contracts import StreamEvent
from .events import AsyncEventBus
from .protocol import AudioFrameError, DecodedAudioFrame, decode_audio_frame


PROTOCOL_SUBPROTOCOL = "interview-monkey.v1"
RESUME_TTL_SECONDS = 600
ERROR_MESSAGES = {
    "AUTH_REQUIRED": "缺少本地开发令牌",
    "AUTH_INVALID": "本地开发令牌无效",
    "INVALID_CONTROL": "控制帧无效或状态不允许此操作",
    "INVALID_FRAME": "音频帧格式无效",
    "FRAME_TOO_LARGE": "音频帧超过最大允许长度",
    "CHECKSUM_MISMATCH": "音频帧 CRC32 校验失败",
    "SEQ_GAP": "音频帧序号不连续",
    "SESSION_NOT_FOUND": "会话不存在",
    "RESUME_EXPIRED": "恢复令牌已过期或不匹配",
    "HEARTBEAT_TIMEOUT": "会话心跳超时",
    "RATE_LIMITED": "本机 Mock 服务暂时繁忙",
    "INTERNAL": "本机 Mock 服务发生内部错误",
}
RETRYABLE = {"CHECKSUM_MISMATCH", "SEQ_GAP", "HEARTBEAT_TIMEOUT", "RATE_LIMITED", "INTERNAL"}
VALID_ERROR_CODES = frozenset(ERROR_MESSAGES)
log = logging.getLogger("interview_monkey.mock_protocol")


@dataclass(frozen=True, slots=True)
class MockSettings:
    token: str
    token_delay_ms: int = 0
    drop_seq: int | None = None
    disconnect_after_frames: int | None = None
    ignore_ping: bool = False
    error_code: str | None = None
    heartbeat_timeout_seconds: float = 45.0

    @classmethod
    def from_env(cls) -> "MockSettings":
        delay = max(0, min(int(os.getenv("MH_MOCK_DELAY_MS", "0")), 60_000))
        drop_raw = os.getenv("MH_MOCK_DROP_SEQ", "").strip()
        disconnect_raw = os.getenv("MH_MOCK_DISCONNECT_AFTER_FRAMES", "").strip()
        error_code = os.getenv("MH_MOCK_ERROR_CODE", "").strip().upper() or None
        if error_code not in VALID_ERROR_CODES:
            error_code = None
        timeout = float(os.getenv("MH_MOCK_HEARTBEAT_TIMEOUT_SECONDS", "45"))
        return cls(
            token=os.getenv("DEV_TOKEN", ""),
            token_delay_ms=delay,
            drop_seq=int(drop_raw) if drop_raw else None,
            disconnect_after_frames=int(disconnect_raw) if disconnect_raw else None,
            ignore_ping=os.getenv("MH_MOCK_IGNORE_PING", "").lower() in {"1", "true", "yes"},
            error_code=error_code,
            heartbeat_timeout_seconds=max(0.05, min(timeout, 3600.0)),
        )


@dataclass(slots=True)
class MockSession:
    session_id: str
    resume_token: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    expires_at: float = field(default_factory=lambda: time.monotonic() + RESUME_TTL_SECONDS)
    state: str = "streaming"
    event_index: int = -1
    expected_seq: int = 0
    last_audio_seq: int = -1
    last_timestamp_ms: int = -1
    last_ack_event_index: int = -1
    received_bytes: int = 0
    frames_received: int = 0
    seen_crc: dict[int, int] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    eof_received: bool = False
    fixture_token_position: int = 0
    finished: bool = False
    injected_error_sent: bool = False
    injected_disconnect_sent: bool = False


class JsonlAudit:
    """安全日志只记录协议元数据，不保存 token、PCM 或回答正文。"""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = asyncio.Lock()

    async def write(self, **fields: Any) -> None:
        record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        log.info(line)
        if self.path is None:
            return
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")


class FixtureAnswerService:
    """实现旧 SSE service 接口，但所有回答均来自静态 fixtures。"""

    def __init__(self, fixtures: dict[str, Any], delay_seconds: float = 0):
        self.fixtures = fixtures
        self.delay_seconds = delay_seconds
        self.retriever = None

    def clear_context(self, _session_id: str) -> None:
        return None

    async def cancel(self, _session_id: str) -> None:
        return None

    async def stream_answer(self, question: str, *, session_id: str = "default", recent=(), **_kwargs):
        trace_id = uuid.uuid4().hex
        sources = self.fixtures.get("sources", [])
        meta = {
            "trace_id": trace_id,
            "question": self.fixtures["question"],
            "raw_question": question,
            "llm_correction": None,
            "sources": sources,
            "retrieval_queries": [],
            "product_filter_applied": False,
            "low_confidence": False,
            "rag_ms": 0,
        }
        yield StreamEvent("meta", trace_id, meta)
        parts = self.fixtures["answer_tokens"]
        start = time.perf_counter()
        for piece in parts:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            yield StreamEvent("token", trace_id, {"delta": piece})
        elapsed = (time.perf_counter() - start) * 1000
        yield StreamEvent("done", trace_id, {
            "status": "done",
            "pieces": len(parts),
            "metrics": {"ttft_ms": round(elapsed, 3), "end_to_end_ms": round(elapsed, 3)},
        })


class FixtureCaptureRuntime:
    """发布预制转写和问答事件，保持旧 SSE 前端的事件名和字段。"""

    def __init__(self, fixtures: dict[str, Any], answer_service: FixtureAnswerService):
        self.fixtures = fixtures
        self.answer_service = answer_service
        self.bus = AsyncEventBus(queue_size=256)
        self.state = "stopped"
        self.last_error = ""
        self._task: asyncio.Task | None = None

    def status(self) -> dict[str, Any]:
        return {
            "capture": self.state,
            "asr": "connected" if self.state == "capturing" else "idle",
            "active_sources": 0,
            "audio_stats": [],
            "error": self.last_error,
            "mock": True,
        }

    async def _publish(self, kind: str, payload: dict[str, Any]) -> None:
        await self.bus.publish("ui", {"type": kind, "payload": payload})

    async def start(self, mode: str = "loopback", device=None, session_id: str = "default") -> dict[str, Any]:
        del device
        if mode not in {"loopback", "mic", "mic_question", "both"}:
            return {"ok": False, **self.status(), "error": "mode 无效"}
        if self.state == "capturing":
            return {"ok": True, **self.status()}
        self.state = "capturing"
        self.last_error = ""
        await self._publish("status", self.status())
        self._task = asyncio.create_task(self._publish_fixtures(session_id))
        return {"ok": True, **self.status()}

    async def _publish_fixtures(self, session_id: str) -> None:
        channel = "mic" if session_id and self.fixtures.get("channel") == "mic" else "loopback"
        for index, text in enumerate(self.fixtures["transcript"]):
            await self._publish("transcript", {
                "kind": "final",
                "text": text,
                "channel": channel,
                "speaker_id": "Mock",
                "utterance_id": f"mock-{session_id}-{index}",
                "start_ms": index * 1000,
                "end_ms": index * 1000 + 900,
                "raw_text": text,
                "corrected": False,
            })
        await self._publish("question_decision", {
            "is_question": True,
            "confidence": 1.0,
            "normalized_query": self.fixtures["question"],
            "reason": "fixture",
        })
        async for event in self.answer_service.stream_answer(
            self.fixtures["question"], session_id=session_id,
        ):
            await self._publish("answer", {
                "event": event.event,
                "trace_id": event.trace_id,
                "data": dict(event.data),
            })

    async def stop(self) -> dict[str, Any]:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self.state = "stopped"
        await self._publish("status", self.status())
        return {"ok": True, **self.status()}

    def reload_hotwords(self) -> int:
        return 0


class MockProtocolServer:
    def __init__(self, fixtures: dict[str, Any], settings: MockSettings, log_path: Path | None = None):
        self.fixtures = fixtures
        self.settings = settings
        self.audit = JsonlAudit(log_path)
        self.sessions: dict[str, MockSession] = {}
        self._sessions_lock = asyncio.Lock()

    @staticmethod
    def _loopback(websocket: WebSocket) -> bool:
        client = websocket.client
        if client is None:
            return False
        try:
            return ipaddress.ip_address(client.host.split("%", 1)[0]).is_loopback
        except ValueError:
            return False

    async def _deny(self, websocket: WebSocket, status: int, code: str) -> None:
        await self.audit.write(session_id="", event="handshake_rejected", state="rejected", reason=code)
        body = {"code": code, "message": ERROR_MESSAGES[code]}
        response = JSONResponse(body, status_code=status)
        try:
            await websocket.send_denial_response(response)
        except (RuntimeError, AttributeError):
            await websocket.close(code=4401 if status == 401 else 4403, reason=code)

    def _event(self, session: MockSession, event_type: str, **payload: Any) -> dict[str, Any]:
        session.event_index += 1
        event = {
            "protocol_version": 1,
            "session_id": session.session_id,
            "event_index": session.event_index,
            "type": event_type,
            "trace_id": session.trace_id,
            **payload,
        }
        session.events.append(event)
        return event

    async def _send(self, websocket: WebSocket, event: dict[str, Any], *, replay: bool = False) -> None:
        await websocket.send_json(event)
        if not replay and event["type"] != "ack" and event["type"] != "pong":
            await self.audit.write(
                session_id=event["session_id"],
                event="server_event",
                state="connected",
                frame_type=event["type"],
                size=len(json.dumps(event, ensure_ascii=False)),
                event_index=event["event_index"],
            )

    async def _emit(self, websocket: WebSocket, session: MockSession, event_type: str, **payload: Any) -> dict[str, Any]:
        event = self._event(session, event_type, **payload)
        await self._send(websocket, event)
        return event

    async def _emit_error(
        self,
        websocket: WebSocket,
        session: MockSession | None,
        session_id: str,
        code: str,
        *,
        close_code: int | None = None,
    ) -> None:
        if session:
            event = self._event(
                session, "error", code=code, message=ERROR_MESSAGES[code], retryable=code in RETRYABLE,
            )
            await self._send(websocket, event)
        else:
            await websocket.send_json({
                "protocol_version": 1, "session_id": session_id or "unstarted",
                "event_index": 0, "type": "error", "trace_id": uuid.uuid4().hex,
                "code": code, "message": ERROR_MESSAGES[code], "retryable": code in RETRYABLE,
            })
        if close_code is not None:
            await websocket.close(code=close_code, reason=code)

    def _meta(self, session: MockSession) -> dict[str, Any]:
        duration_ms = session.received_bytes / 32.0
        return self._event(
            session,
            "meta",
            resume_token=session.resume_token,
            resume_ttl_seconds=RESUME_TTL_SECONDS,
            received_bytes=session.received_bytes,
            audio_duration_ms=duration_ms,
            audio_duration_seconds=session.received_bytes / 32_000.0,
            audio={"sample_rate": 16000, "channels": 1, "sample_format": "pcm_s16le", "frame_ms": 100},
        )

    async def _send_meta(self, websocket: WebSocket, session: MockSession) -> None:
        await self._send(websocket, self._meta(session))

    async def _stream_fixtures(self, websocket: WebSocket, session: MockSession) -> None:
        streams = (
            ("transcript", "transcript", self.fixtures["transcript_tokens"]),
            ("question", "current_question", self.fixtures["question_tokens"]),
            ("answer", "answer", self.fixtures["answer_tokens"]),
        )
        flat = [(stream, column, token) for stream, column, tokens in streams for token in tokens]
        while session.fixture_token_position < len(flat):
            if self.settings.token_delay_ms:
                await asyncio.sleep(self.settings.token_delay_ms / 1000)
            stream, column, item = flat[session.fixture_token_position]
            token = item if isinstance(item, str) else item["delta"]
            is_partial = bool(item.get("is_partial", False)) if isinstance(item, dict) else False
            index = sum(1 for old in flat[:session.fixture_token_position] if old[0] == stream)
            session.fixture_token_position += 1
            event = self._event(
                session, "token", stream=stream, column=column, index=index,
                delta=token, is_partial=is_partial,
                utterance_id=f"fixture-{session.session_id}" if stream == "transcript" else None,
            )
            if event.get("utterance_id") is None:
                event.pop("utterance_id", None)
            await self._send(websocket, event)
        if not session.finished:
            stats = {
                "received_bytes": session.received_bytes,
                "audio_duration_ms": session.received_bytes / 32.0,
                "audio_duration_seconds": session.received_bytes / 32_000.0,
                "frames_received": session.frames_received,
                "last_audio_seq": session.last_audio_seq,
                "elapsed_ms": round((time.monotonic() - session.created_at) * 1000, 3),
            }
            await self._emit(websocket, session, "done", stats=stats)
            session.finished = True
            session.state = "finished"
            await self.audit.write(session_id=session.session_id, event="state_transition", state="finished", reason="done")
            await websocket.close(code=1000, reason="done")

    async def _start(self, websocket: WebSocket, control: dict[str, Any]) -> MockSession | None:
        session_id = str(control.get("session_id", ""))
        audio = control.get("audio")
        expected_audio = {"sample_rate": 16000, "channels": 1, "sample_format": "pcm_s16le", "frame_ms": 100}
        if not session_id or len(session_id) > 128 or audio != expected_audio:
            await self._emit_error(websocket, None, session_id, "INVALID_CONTROL", close_code=1008)
            return None
        async with self._sessions_lock:
            old = self.sessions.get(session_id)
            if old is not None and old.expires_at > time.monotonic():
                await self._emit_error(websocket, None, session_id, "INVALID_CONTROL", close_code=1008)
                return None
            session = MockSession(session_id=session_id, resume_token=secrets.token_urlsafe(32))
            self.sessions[session_id] = session
        await self.audit.write(session_id=session_id, event="state_transition", state="streaming", reason="start")
        await self._send_meta(websocket, session)
        return session

    async def _resume(self, websocket: WebSocket, control: dict[str, Any]) -> MockSession | None:
        session_id = str(control.get("session_id", ""))
        resume_token = control.get("resume_token")
        last_audio_seq = control.get("last_audio_seq")
        last_event_index = control.get("last_event_index")
        if (
            not isinstance(resume_token, str) or not 16 <= len(resume_token) <= 512
            or isinstance(last_audio_seq, bool) or not isinstance(last_audio_seq, int) or last_audio_seq < 0
            or isinstance(last_event_index, bool) or not isinstance(last_event_index, int) or last_event_index < 0
        ):
            await self._emit_error(websocket, None, session_id, "INVALID_CONTROL", close_code=1008)
            return None
        async with self._sessions_lock:
            session = self.sessions.get(session_id)
        if session is not None and session.state not in {"disconnected", "finished"}:
            await self._emit_error(websocket, None, session_id, "INVALID_CONTROL", close_code=1008)
            return None
        if (
            session is None or session.expires_at <= time.monotonic()
            or not hmac.compare_digest(session.resume_token, resume_token)
            or last_audio_seq > session.last_audio_seq
            or last_event_index > session.event_index
        ):
            await self._emit_error(websocket, None, session_id, "RESUME_EXPIRED", close_code=4400)
            return None
        session.state = "streaming"
        session.last_activity = time.monotonic()
        await self.audit.write(session_id=session_id, event="state_transition", state="streaming", reason="resume")
        last_event = last_event_index
        for event in session.events:
            if event["event_index"] > last_event:
                await self._send(websocket, event, replay=True)
        if session.last_audio_seq >= 0:
            await self._emit(
                websocket, session, "ack", seq=session.last_audio_seq,
                received_bytes=session.received_bytes,
            )
        if session.eof_received and not session.finished:
            await self._stream_fixtures(websocket, session)
        elif session.finished:
            await websocket.close(code=1000, reason="already_done")
        return session

    async def handle(self, websocket: WebSocket) -> None:
        if not self._loopback(websocket):
            await self._deny(websocket, 403, "AUTH_INVALID")
            return
        protocols = websocket.scope.get("subprotocols", [])
        if PROTOCOL_SUBPROTOCOL not in protocols:
            await self._deny(websocket, 400, "INVALID_CONTROL")
            return
        token = self.settings.token
        if not token:
            await self._deny(websocket, 401, "AUTH_REQUIRED")
            return
        supplied = websocket.headers.get("authorization", "")
        scheme, _, supplied_token = supplied.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, supplied_token):
            await self._deny(websocket, 403, "AUTH_INVALID")
            return

        await websocket.accept(subprotocol=PROTOCOL_SUBPROTOCOL)
        session: MockSession | None = None
        active_id = "unstarted"
        try:
            while True:
                idle_for = time.monotonic() - (session.last_activity if session else time.monotonic())
                timeout = max(0.01, self.settings.heartbeat_timeout_seconds - idle_for)
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._emit_error(websocket, session, active_id, "HEARTBEAT_TIMEOUT", close_code=4408)
                    return
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    if session is None:
                        await self._emit_error(websocket, None, active_id, "INVALID_CONTROL", close_code=1008)
                        return
                    wire = message["bytes"]
                    seq = int.from_bytes(wire[8:12], "little") if len(wire) >= 12 else None
                    if self.settings.error_code and not session.injected_error_sent:
                        session.injected_error_sent = True
                        await self._emit_error(websocket, session, session.session_id, self.settings.error_code, close_code=1011)
                        return
                    if self.settings.drop_seq is not None and seq == self.settings.drop_seq:
                        await self.audit.write(session_id=session.session_id, event="frame_drop", frame_type="audio", size=len(wire), seq=seq, state=session.state, reason="fault_injection")
                        await self._emit_error(websocket, session, session.session_id, "SEQ_GAP")
                        continue
                    try:
                        frame = decode_audio_frame(wire, expected_seq=session.expected_seq, seen_crc=session.seen_crc)
                    except AudioFrameError as exc:
                        await self.audit.write(session_id=session.session_id, event="validation_error", frame_type="audio", size=len(wire), seq=seq, state=session.state, reason=exc.code)
                        await self._emit_error(websocket, session, session.session_id, exc.code)
                        continue
                    if not frame.duplicate and frame.timestamp_ms <= session.last_timestamp_ms:
                        await self.audit.write(session_id=session.session_id, event="validation_error", frame_type="audio", size=len(wire), seq=frame.seq, state=session.state, reason="INVALID_FRAME_TIMESTAMP")
                        await self._emit_error(websocket, session, session.session_id, "INVALID_FRAME")
                        continue
                    session.last_activity = time.monotonic()
                    if session.state == "paused":
                        await self._emit_error(websocket, session, session.session_id, "INVALID_CONTROL")
                        continue
                    if not frame.duplicate:
                        session.seen_crc[frame.seq] = frame.crc32
                        session.expected_seq += 1
                        session.last_audio_seq = frame.seq
                        session.last_timestamp_ms = frame.timestamp_ms
                        if not frame.eof:
                            session.received_bytes += len(frame.payload)
                            session.frames_received += 1
                        else:
                            session.eof_received = True
                    await self.audit.write(session_id=session.session_id, event="audio_frame", frame_type="audio", size=len(wire), seq=frame.seq, state=session.state, eof=frame.eof, duplicate=frame.duplicate)
                    ack = await self._emit(
                        websocket, session, "ack", seq=frame.seq,
                        received_bytes=session.received_bytes,
                    )
                    session.last_ack_event_index = ack["event_index"]
                    if (
                        self.settings.disconnect_after_frames is not None
                        and session.frames_received >= self.settings.disconnect_after_frames
                        and not frame.eof
                        and not session.injected_disconnect_sent
                    ):
                        session.injected_disconnect_sent = True
                        await self.audit.write(session_id=session.session_id, event="state_transition", state="disconnected", reason="fault_injection")
                        await websocket.close(code=1012, reason="mock_disconnect")
                        return
                    if frame.eof:
                        await self._send_meta(websocket, session)
                        await self._stream_fixtures(websocket, session)
                        return
                    continue

                text = message.get("text")
                if not text:
                    await self._emit_error(websocket, session, active_id, "INVALID_CONTROL", close_code=1008)
                    return
                try:
                    control = json.loads(text)
                except json.JSONDecodeError:
                    await self._emit_error(websocket, session, active_id, "INVALID_CONTROL", close_code=1008)
                    return
                if not isinstance(control, dict):
                    await self._emit_error(websocket, session, active_id, "INVALID_CONTROL", close_code=1008)
                    return
                kind = control.get("type")
                active_id = str(control.get("session_id", active_id))
                if kind == "start" and session is None:
                    session = await self._start(websocket, control)
                    if session is None:
                        return
                    continue
                if kind == "resume" and session is None:
                    session = await self._resume(websocket, control)
                    if session is None:
                        return
                    continue
                if session is None or control.get("session_id") != session.session_id:
                    await self._emit_error(websocket, None, active_id, "SESSION_NOT_FOUND", close_code=4400)
                    return
                if kind == "ping":
                    nonce = control.get("nonce")
                    if not isinstance(nonce, str) or not 1 <= len(nonce) <= 128:
                        await self._emit_error(websocket, session, session.session_id, "INVALID_CONTROL", close_code=1008)
                        return
                    if self.settings.ignore_ping:
                        await self.audit.write(session_id=session.session_id, event="heartbeat_ignored", state=session.state, reason="fault_injection")
                        continue
                    session.last_activity = time.monotonic()
                    await self._emit(websocket, session, "pong", nonce=nonce)
                elif kind == "ack":
                    event_index = control.get("event_index")
                    if isinstance(event_index, bool) or not isinstance(event_index, int) or event_index < 0:
                        await self._emit_error(websocket, session, session.session_id, "INVALID_CONTROL", close_code=1008)
                        return
                    session.last_activity = time.monotonic()
                    session.last_ack_event_index = max(session.last_ack_event_index, event_index)
                elif kind == "pause" and session.state == "streaming":
                    session.state = "paused"
                    session.last_activity = time.monotonic()
                    await self.audit.write(session_id=session.session_id, event="state_transition", state="paused", reason="pause")
                elif kind == "resume" and session.state == "paused":
                    session.state = "streaming"
                    session.last_activity = time.monotonic()
                    await self.audit.write(session_id=session.session_id, event="state_transition", state="streaming", reason="resume")
                elif kind == "stop":
                    session.eof_received = True
                    session.last_activity = time.monotonic()
                    await self._send_meta(websocket, session)
                    await self._stream_fixtures(websocket, session)
                    return
                else:
                    await self._emit_error(websocket, session, session.session_id, "INVALID_CONTROL", close_code=1008)
                    return
        except WebSocketDisconnect:
            return
        finally:
            if session and not session.finished:
                session.state = "disconnected"
                session.expires_at = time.monotonic() + RESUME_TTL_SECONDS
                await self.audit.write(session_id=session.session_id, event="state_transition", state="disconnected", reason="socket_closed")


def load_fixtures(project_root: Path) -> dict[str, Any]:
    path = project_root / "fixtures" / "realtime_mock.json"
    return json.loads(path.read_text(encoding="utf-8"))
