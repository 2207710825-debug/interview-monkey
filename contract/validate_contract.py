#!/usr/bin/env python3
"""Validate committed protocol examples and optionally inspect live legacy SSE."""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import zlib
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, ValidationError


ROOT = Path(__file__).resolve().parent
SCHEMAS = ROOT / "schemas"
EXAMPLES = ROOT / "examples"
SCHEMA_BY_PREFIX = (
    ("handshake", "handshake.schema.json"),
    ("control_", "control.schema.json"),
    ("audio_frame", "audio_frame.schema.json"),
    ("event_", "server_event.schema.json"),
    ("legacy_sse_", "legacy_sse_event.schema.json"),
)


def load_validator(filename: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_for_example(path: Path) -> str:
    for prefix, schema_name in SCHEMA_BY_PREFIX:
        if path.name.startswith(prefix):
            return schema_name
    raise ValueError(f"No schema mapping for example: {path.name}")


def validate_decoded_audio_frame(frame: dict) -> None:
    payload = base64.b64decode(frame["payload_base64"], validate=True)
    if len(payload) != frame["payload_length"]:
        raise ValueError("decoded audio payload length differs from payload_length")
    flags = 1 if frame["eof"] else 0
    channel = 0 if frame["channel"] == "mic" else 1
    header_without_crc = struct.pack(
        "<4sBBHIQIIB3s",
        b"IMON",
        frame["version"],
        1,  # AUDIO frame type
        flags,
        frame["seq"],
        frame["timestamp_ms"],
        frame["sample_rate"],
        frame["payload_length"],
        channel,
        b"\0\0\0",
    )
    expected_crc = zlib.crc32(header_without_crc + payload) & 0xFFFFFFFF
    if frame["crc32"] != f"{expected_crc:08x}":
        raise ValueError("decoded audio frame CRC32 does not match")
    wire = header_without_crc + expected_crc.to_bytes(4, "little") + payload
    if len(wire) != 36 + frame["payload_length"]:
        raise ValueError("encoded binary frame does not match the 36-byte header contract")


def validate_examples() -> int:
    validators = {path.name: load_validator(path.name) for path in SCHEMAS.glob("*.schema.json")}
    count = 0
    for path in sorted(EXAMPLES.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        validator = validators[schema_for_example(path)]
        try:
            validator.validate(document)
            if path.name.startswith("audio_frame"):
                validate_decoded_audio_frame(document)
        except (ValidationError, ValueError, struct.error) as exc:
            location = ".".join(str(item) for item in getattr(exc, "absolute_path", ())) or "<root>"
            raise RuntimeError(f"{path.name}: validation failed at {location} ({type(exc).__name__})") from None
        count += 1
    if count == 0:
        raise RuntimeError("No contract examples found")
    print(f"EXAMPLES_PASS count={count} schemas={len(validators)}")
    return count


def iter_sse_events(response: httpx.Response):
    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line:
            if event_name is not None:
                yield {"event": event_name, "data": json.loads("\n".join(data_lines))}
            event_name, data_lines = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if event_name is not None:
        yield {"event": event_name, "data": json.loads("\n".join(data_lines))}


def validate_live(base_url: str, question: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("--live only permits an http://127.0.0.1 base URL")
    endpoint = base_url.rstrip("/") + "/api/answer/stream"
    validator = load_validator("legacy_sse_event.schema.json")
    observed: list[str] = []
    counts: Counter[str] = Counter()
    payload = {"question": question, "session_id": f"contract-check-{uuid4().hex}"}
    try:
        with httpx.Client(timeout=httpx.Timeout(90.0, connect=5.0)) as client:
            with client.stream("POST", endpoint, json=payload) as response:
                response.raise_for_status()
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise RuntimeError("live endpoint did not return text/event-stream")
                for item in iter_sse_events(response):
                    try:
                        validator.validate(item)
                    except ValidationError as exc:
                        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
                        raise RuntimeError(
                            f"live SSE event {item.get('event', '<unknown>')} violates legacy schema at {location}"
                        ) from None
                    name = item["event"]
                    observed.append(name)
                    counts[name] += 1
    except httpx.HTTPError as exc:
        raise RuntimeError(f"live request failed ({type(exc).__name__})") from None

    if not observed or observed[0] != "meta" or observed[-1] not in {"done", "error"}:
        raise RuntimeError(f"unexpected live SSE event order: {observed[:1]} ... {observed[-1:]}")
    if counts["token"] == 0:
        raise RuntimeError("live SSE emitted no token events")
    if counts["error"]:
        raise RuntimeError("live SSE conforms to Schema but ended with an error event")
    print(
        "LIVE_SCHEMA_PASS "
        f"events={len(observed)} meta={counts['meta']} token={counts['token']} done={counts['done']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="validate output from a running localhost legacy SSE service")
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--question", default="请简要说明 Kafka 如何保障消息可靠性？")
    args = parser.parse_args()
    validate_examples()
    if args.live:
        validate_live(args.base_url, args.question)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"CONTRACT_VALIDATION_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
