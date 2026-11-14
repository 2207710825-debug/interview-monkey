"""v1 WebSocket Mock 协议与旧 SSE 前端契约的本机联调测试。"""
from __future__ import annotations

import asyncio
import json
import socket
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from jsonschema import Draft202012Validator
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from engine.realtime.api.app import create_app
from engine.realtime.mock_backend import MockSettings, PROTOCOL_SUBPROTOCOL
from engine.realtime.protocol import encode_audio_frame
from tests.sim_client import receive_json, run_session, send_control


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Draft202012Validator(json.loads(
    (ROOT / "contract" / "schemas" / "server_event.schema.json").read_text(encoding="utf-8")
))
TOKEN = "local-test-token-only"
AUDIO = {"sample_rate": 16000, "channels": 1, "sample_format": "pcm_s16le", "frame_ms": 100}


@asynccontextmanager
async def mock_server(**settings_values):
    settings = MockSettings(token=TOKEN, **settings_values)
    app = create_app(mock=True, mock_settings=settings)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="critical",
        access_log=False,
        lifespan="off",
        ws="websockets",
    ))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        if not server.started:
            raise RuntimeError("本机 Uvicorn Mock 测试服务启动超时")
        yield f"ws://127.0.0.1:{port}/v1/realtime", f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


def open_ws(url: str, token: str = TOKEN):
    return connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"} if token is not None else {},
        subprotocols=[PROTOCOL_SUBPROTOCOL],
        ping_interval=None,
        max_size=2_000_000,
    )


async def start_session(websocket, session_id: str) -> dict:
    await send_control(websocket, {"type": "start", "session_id": session_id, "audio": AUDIO})
    event = await receive_json(websocket)
    SCHEMA.validate(event)
    return event


async def collect_to_done(websocket) -> list[dict]:
    events = []
    while True:
        event = await receive_json(websocket)
        SCHEMA.validate(event)
        events.append(event)
        if event["type"] == "done":
            return events


class TestMockProtocol(unittest.IsolatedAsyncioTestCase):
    async def test_full_audio_fixture_stream_and_legacy_sse_frontend(self):
        async with mock_server() as (ws_url, http_url, app):
            async with open_ws(ws_url) as ws:
                session_id = f"full-{uuid.uuid4().hex[:8]}"
                meta = await start_session(ws, session_id)
                self.assertEqual(meta["received_bytes"], 0)
                ack_event = {"type": "ack", "session_id": session_id, "event_index": meta["event_index"]}
                await send_control(ws, ack_event)
                for seq in range(2):
                    await ws.send(encode_audio_frame(seq, seq * 100, bytes([seq + 1]) * 3200))
                    ack = await receive_json(ws)
                    SCHEMA.validate(ack)
                    self.assertEqual((ack["type"], ack["seq"]), ("ack", seq))
                    await send_control(ws, {"type": "ack", "session_id": session_id, "event_index": ack["event_index"]})
                await ws.send(encode_audio_frame(2, 200, b"", eof=True))
                events = await collect_to_done(ws)
                self.assertEqual(events[0]["type"], "ack")
                final_meta = next(event for event in events if event["type"] == "meta")
                done = events[-1]
                self.assertEqual(final_meta["received_bytes"], 6400)
                self.assertEqual(final_meta["audio_duration_seconds"], 0.2)
                self.assertEqual(done["stats"]["received_bytes"], 6400)
                self.assertEqual(done["stats"]["frames_received"], 2)
                self.assertEqual(done["stats"]["last_audio_seq"], 2)
                tokens = [event for event in events if event["type"] == "token"]
                self.assertEqual([event["stream"] for event in tokens], [
                    "transcript", "transcript", "question", "question", "answer", "answer", "answer",
                ])
                self.assertEqual([event["index"] for event in tokens if event["stream"] == "answer"], [0, 1, 2])
                self.assertEqual([event["event_index"] for event in events], sorted(event["event_index"] for event in events))

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=http_url) as client:
                page = await client.get("/")
                status = await client.get("/api/status")
                answer = await client.post("/api/answer/stream", json={
                    "question": "任意输入不影响 fixture 的固定回答", "session_id": "legacy-mock",
                })
            self.assertEqual(page.status_code, 200)
            self.assertIn("EventSource('/api/events')", page.text)
            self.assertTrue(status.json()["mock"] if "mock" in status.json() else app.state.mock_mode)
            self.assertEqual(answer.status_code, 200)
            legacy_names = [line[7:] for line in answer.text.splitlines() if line.startswith("event: ")]
            self.assertEqual(legacy_names[0], "meta")
            self.assertIn("token", legacy_names)
            self.assertEqual(legacy_names[-1], "done")
            self.assertIn("fixture-kafka-reliability", answer.text)

    async def test_crc_and_sequence_errors_are_recoverable(self):
        async with mock_server() as (url, _, _):
            async with open_ws(url) as ws:
                sid = f"check-{uuid.uuid4().hex[:8]}"
                await start_session(ws, sid)
                bad_crc = bytearray(encode_audio_frame(0, 0, b"a" * 3200))
                bad_crc[32] ^= 0x01
                await ws.send(bytes(bad_crc))
                error = await receive_json(ws)
                SCHEMA.validate(error)
                self.assertEqual(error["code"], "CHECKSUM_MISMATCH")
                await ws.send(encode_audio_frame(2, 200, b"b" * 3200))
                error = await receive_json(ws)
                self.assertEqual(error["code"], "SEQ_GAP")
                await ws.send(encode_audio_frame(0, 0, b"a" * 3200))
                self.assertEqual((await receive_json(ws))["seq"], 0)
                await ws.send(encode_audio_frame(1, 100, b"b" * 3200))
                self.assertEqual((await receive_json(ws))["seq"], 1)
                await ws.send(encode_audio_frame(2, 200, b"", eof=True))
                events = await collect_to_done(ws)
                self.assertEqual(events[-1]["stats"]["received_bytes"], 6400)

    async def test_invalid_header_length_and_oversize_errors(self):
        async with mock_server() as (url, _, _):
            async with open_ws(url) as ws:
                sid = f"header-{uuid.uuid4().hex[:8]}"
                await start_session(ws, sid)
                bad_magic = bytearray(encode_audio_frame(0, 0, b"h" * 3200))
                bad_magic[0] = ord("X")
                await ws.send(bytes(bad_magic))
                self.assertEqual((await receive_json(ws))["code"], "INVALID_FRAME")
                bad_size = bytearray(encode_audio_frame(0, 0, b"h" * 3200))
                bad_size[24:28] = (3199).to_bytes(4, "little")
                await ws.send(bytes(bad_size))
                self.assertEqual((await receive_json(ws))["code"], "INVALID_FRAME")
                await ws.send(b"x" * 3237)
                self.assertEqual((await receive_json(ws))["code"], "FRAME_TOO_LARGE")
                await ws.send(encode_audio_frame(0, 0, b"h" * 3200))
                self.assertEqual((await receive_json(ws))["seq"], 0)
                await ws.send(encode_audio_frame(1, 0, b"i" * 3200))
                self.assertEqual((await receive_json(ws))["code"], "INVALID_FRAME")
                await ws.send(encode_audio_frame(1, 100, b"", eof=True))
                self.assertEqual((await collect_to_done(ws))[-1]["stats"]["received_bytes"], 3200)

    async def test_fixture_delay_fault_injection(self):
        async with mock_server(token_delay_ms=40) as (url, _, _):
            async with open_ws(url) as ws:
                sid = f"delay-{uuid.uuid4().hex[:8]}"
                await start_session(ws, sid)
                started = asyncio.get_running_loop().time()
                await ws.send(encode_audio_frame(0, 0, b"t" * 3200))
                await receive_json(ws)  # ack
                await ws.send(encode_audio_frame(1, 100, b"", eof=True))
                await receive_json(ws)  # EOF ack
                await receive_json(ws)  # final meta
                events = await collect_to_done(ws)
                elapsed = asyncio.get_running_loop().time() - started
                self.assertEqual(sum(event["type"] == "token" for event in events), 7)
                self.assertGreaterEqual(elapsed, 0.24)

    async def test_pause_resume_ping_and_stop_controls(self):
        async with mock_server() as (url, _, _):
            async with open_ws(url) as ws:
                sid = f"control-{uuid.uuid4().hex[:8]}"
                await start_session(ws, sid)
                await send_control(ws, {"type": "pause", "session_id": sid})
                await ws.send(encode_audio_frame(0, 0, b"x" * 3200))
                self.assertEqual((await receive_json(ws))["code"], "INVALID_CONTROL")
                await send_control(ws, {"type": "resume", "session_id": sid})
                await send_control(ws, {"type": "ping", "session_id": sid, "nonce": "n-1"})
                pong = await receive_json(ws)
                self.assertEqual((pong["type"], pong["nonce"]), ("pong", "n-1"))
                await ws.send(encode_audio_frame(0, 0, b"x" * 3200))
                self.assertEqual((await receive_json(ws))["seq"], 0)
                await send_control(ws, {"type": "stop", "session_id": sid})
                self.assertEqual((await collect_to_done(ws))[-1]["stats"]["received_bytes"], 3200)

    async def test_resume_preserves_seq_and_does_not_double_count(self):
        async with mock_server() as (url, _, _):
            sid = f"resume-{uuid.uuid4().hex[:8]}"
            async with open_ws(url) as first:
                meta = await start_session(first, sid)
                await first.send(encode_audio_frame(0, 0, b"r" * 3200))
                ack = await receive_json(first)
                self.assertEqual(ack["received_bytes"], 3200)
                await first.close()
            await asyncio.sleep(0.03)
            async with open_ws(url) as second:
                await send_control(second, {
                    "type": "resume", "session_id": sid,
                    "resume_token": meta["resume_token"],
                    "last_audio_seq": 0,
                    "last_event_index": ack["event_index"],
                })
                resumed_ack = await receive_json(second)
                SCHEMA.validate(resumed_ack)
                self.assertEqual(resumed_ack["seq"], 0)
                await second.send(encode_audio_frame(0, 0, b"r" * 3200))
                duplicate_ack = await receive_json(second)
                self.assertEqual(duplicate_ack["received_bytes"], 3200)
                await second.send(encode_audio_frame(1, 100, b"s" * 3200))
                self.assertEqual((await receive_json(second))["seq"], 1)
                await second.send(encode_audio_frame(2, 200, b"", eof=True))
                events = await collect_to_done(second)
                self.assertEqual(events[-1]["stats"]["received_bytes"], 6400)
                self.assertEqual(events[-1]["stats"]["frames_received"], 2)

    async def test_session_rejects_a_second_live_resume_without_corrupting_event_order(self):
        async with mock_server() as (url, _, _):
            sid = f"single-attach-{uuid.uuid4().hex[:8]}"
            async with open_ws(url) as first:
                meta = await start_session(first, sid)
                async with open_ws(url) as second:
                    await send_control(second, {
                        "type": "resume", "session_id": sid,
                        "resume_token": meta["resume_token"],
                        "last_audio_seq": 0,
                        "last_event_index": meta["event_index"],
                    })
                    rejected = await receive_json(second)
                    SCHEMA.validate(rejected)
                    self.assertEqual(rejected["code"], "INVALID_CONTROL")
                await first.send(encode_audio_frame(0, 0, b"a" * 3200))
                ack = await receive_json(first)
                self.assertEqual(ack["event_index"], meta["event_index"] + 1)
                await first.send(encode_audio_frame(1, 100, b"", eof=True))
                self.assertEqual((await collect_to_done(first))[-1]["stats"]["received_bytes"], 3200)

    async def test_auth_rejection_status(self):
        async with mock_server() as (url, _, _):
            for headers in [{}, {"Authorization": "Bearer wrong-token"}]:
                with self.assertRaises(InvalidStatus) as caught:
                    await connect(url, additional_headers=headers, subprotocols=[PROTOCOL_SUBPROTOCOL], ping_interval=None)
                self.assertEqual(caught.exception.response.status_code, 403)

    async def test_heartbeat_timeout_fault_injection(self):
        async with mock_server(ignore_ping=True, heartbeat_timeout_seconds=0.3) as (url, _, _):
            async with open_ws(url) as ws:
                sid = f"heartbeat-{uuid.uuid4().hex[:8]}"
                await start_session(ws, sid)
                await send_control(ws, {"type": "ping", "session_id": sid, "nonce": "ignored"})
                event = await receive_json(ws, timeout=2)
                SCHEMA.validate(event)
                self.assertEqual(event["code"], "HEARTBEAT_TIMEOUT")

    async def test_fault_injection_drop_disconnect_and_error(self):
        async with mock_server(drop_seq=0) as (url, _, _):
            async with open_ws(url) as ws:
                sid = f"drop-{uuid.uuid4().hex[:8]}"
                await start_session(ws, sid)
                await ws.send(encode_audio_frame(0, 0, b"d" * 3200))
                self.assertEqual((await receive_json(ws))["code"], "SEQ_GAP")

        async with mock_server(disconnect_after_frames=1) as (url, _, _):
            sid = f"disconnect-{uuid.uuid4().hex[:8]}"
            async with open_ws(url) as ws:
                meta = await start_session(ws, sid)
                await ws.send(encode_audio_frame(0, 0, b"d" * 3200))
                ack = await receive_json(ws)
                self.assertEqual(ack["seq"], 0)
                await ws.wait_closed()
            async with open_ws(url) as ws:
                await send_control(ws, {
                    "type": "resume", "session_id": sid,
                    "resume_token": meta["resume_token"], "last_audio_seq": 0,
                    "last_event_index": ack["event_index"],
                })
                self.assertEqual((await receive_json(ws))["seq"], 0)
                await ws.send(encode_audio_frame(1, 100, b"e" * 3200))
                self.assertEqual((await receive_json(ws))["seq"], 1)
                await ws.send(encode_audio_frame(2, 200, b"", eof=True))
                self.assertEqual((await collect_to_done(ws))[-1]["stats"]["received_bytes"], 6400)

        async with mock_server(error_code="INTERNAL") as (url, _, _):
            async with open_ws(url) as ws:
                sid = f"error-{uuid.uuid4().hex[:8]}"
                await start_session(ws, sid)
                await ws.send(encode_audio_frame(0, 0, b"e" * 3200))
                event = await receive_json(ws)
                SCHEMA.validate(event)
                self.assertEqual(event["code"], "INTERNAL")

    async def test_fifty_parallel_sessions_do_not_cross_talk(self):
        async with mock_server() as (url, _, _):
            results = await asyncio.gather(*(
                run_session(url, TOKEN, f"parallel-{index}-{uuid.uuid4().hex[:4]}", 0, pace=False)
                for index in range(50)
            ))
            self.assertEqual(len({result["session_id"] for result in results}), 50)
            self.assertTrue(all(result["received_bytes"] == 3200 for result in results))
            self.assertTrue(all(result["last_audio_seq"] == 1 for result in results))


if __name__ == "__main__":
    unittest.main()
