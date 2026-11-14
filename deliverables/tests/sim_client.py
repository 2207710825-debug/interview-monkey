"""本机 v1 Mock 协议模拟客户端；生成 16 kHz PCM16 单声道音频。"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import struct
import sys
import time
import uuid
from pathlib import Path

from websockets.asyncio.client import connect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.realtime.mock_backend import PROTOCOL_SUBPROTOCOL
from engine.realtime.protocol import encode_audio_frame


TOKEN = os.getenv("DEV_TOKEN", "local-mock-token")
SAMPLES_PER_FRAME = 1600


def make_pcm_frame(frame_index: int) -> bytes:
    """生成 100 ms、440 Hz 正弦波，输出固定 3200 字节。"""
    samples = [
        int(4000 * math.sin(2 * math.pi * 440 * ((frame_index * SAMPLES_PER_FRAME + i) / 16000)))
        for i in range(SAMPLES_PER_FRAME)
    ]
    return struct.pack("<1600h", *samples)


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * ratio))], 3) if ordered else 0.0


async def receive_json(websocket, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout))


async def send_control(websocket, payload: dict) -> None:
    await websocket.send(json.dumps(payload, separators=(",", ":")))


async def run_session(url: str, token: str, session_id: str, duration_seconds: float, *, pace: bool = True) -> dict:
    frames = max(1, math.ceil(duration_seconds * 10))
    latencies: list[float] = []
    uri = url.rstrip("/")
    async with connect(
        uri,
        additional_headers={"Authorization": f"Bearer {token}"},
        subprotocols=[PROTOCOL_SUBPROTOCOL],
        ping_interval=None,
        max_size=2_000_000,
    ) as websocket:
        await send_control(websocket, {
            "type": "start",
            "session_id": session_id,
            "audio": {"sample_rate": 16000, "channels": 1, "sample_format": "pcm_s16le", "frame_ms": 100},
        })
        meta = await receive_json(websocket)
        await send_control(websocket, {"type": "ack", "session_id": session_id, "event_index": meta["event_index"]})
        resume_token = meta["resume_token"]
        last_event_index = meta["event_index"]
        received_bytes = 0
        for seq in range(frames):
            started = time.perf_counter()
            payload = make_pcm_frame(seq)
            await websocket.send(encode_audio_frame(seq, seq * 100, payload))
            reply = await receive_json(websocket)
            if reply.get("type") != "ack" or reply.get("seq") != seq:
                raise RuntimeError(f"音频帧未确认：type={reply.get('type')} code={reply.get('code')}")
            latencies.append((time.perf_counter() - started) * 1000)
            received_bytes = reply["received_bytes"]
            last_event_index = reply["event_index"]
            await send_control(websocket, {"type": "ack", "session_id": session_id, "event_index": last_event_index})
            if pace:
                await asyncio.sleep(0.1)

        eof_seq = frames
        await websocket.send(encode_audio_frame(eof_seq, eof_seq * 100, b"", eof=True))
        events = []
        while True:
            event = await receive_json(websocket, timeout=15)
            events.append(event)
            last_event_index = event["event_index"]
            if event["type"] == "done":
                break
            if event["type"] != "ack":
                await send_control(websocket, {
                    "type": "ack", "session_id": session_id,
                    "event_index": last_event_index,
                })
        done = events[-1]
        if done["stats"]["received_bytes"] != received_bytes:
            raise RuntimeError("done 统计值和最后音频 ack 不一致")
        return {
            "session_id": session_id,
            "frames": frames,
            "received_bytes": received_bytes,
            "audio_duration_seconds": done["stats"]["audio_duration_seconds"],
            "last_audio_seq": done["stats"]["last_audio_seq"],
            "event_types": [event["type"] for event in events],
            "latencies_ms": latencies,
            "resume_token_received": bool(resume_token),
        }


async def run_many(url: str, token: str, concurrency: int, duration_seconds: float) -> dict:
    started = time.perf_counter()
    results = await asyncio.gather(*(
        run_session(url, token, f"sim-{index}-{uuid.uuid4().hex[:8]}", duration_seconds)
        for index in range(concurrency)
    ))
    latencies = [value for result in results for value in result["latencies_ms"]]
    elapsed = time.perf_counter() - started
    return {
        "concurrency": concurrency,
        "requested_duration_seconds": duration_seconds,
        "sessions_completed": len(results),
        "total_audio_frames": sum(item["frames"] for item in results),
        "total_received_bytes": sum(item["received_bytes"] for item in results),
        "p50_ack_ms": percentile(latencies, 0.50),
        "p95_ack_ms": percentile(latencies, 0.95),
        "wall_seconds": round(elapsed, 3),
        "all_sessions_isolated": len({item["session_id"] for item in results}) == concurrency
            and all(item["received_bytes"] == item["frames"] * 3200 for item in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:18765/v1/realtime")
    parser.add_argument("--token", default=TOKEN, help="本机 DEV_TOKEN；默认读取环境变量 DEV_TOKEN")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--duration", type=float, default=1.0, help="每路推流时长；100ms 分块")
    args = parser.parse_args()
    if not args.url.startswith("ws://127.0.0.1:"):
        parser.error("模拟器仅允许连接 ws://127.0.0.1")
    if args.concurrency < 1 or args.concurrency > 50 or args.duration < 0:
        parser.error("concurrency 范围为 1~50，duration 必须非负")
    result = asyncio.run(run_many(args.url, args.token, args.concurrency, args.duration))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["sessions_completed"] == args.concurrency and result["all_sessions_isolated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
