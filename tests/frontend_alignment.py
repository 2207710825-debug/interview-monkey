"""验证实时问答前端所用的 /api/events SSE 与 --mock fixture 对齐。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def collect_alignment(base_url: str) -> list[dict]:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("前端 Mock 联调仅允许 http://127.0.0.1")
    records: list[dict] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(15, connect=3)) as client:
        await client.post("/api/capture/stop")

        async def consume_events():
            async with client.stream("GET", "/api/events") as response:
                response.raise_for_status()
                name = None
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if not line:
                        if name:
                            payload = json.loads("\n".join(data_lines) or "{}")
                            record = {"source": "legacy_sse", "event": name}
                            if name == "answer":
                                record["answer_event"] = payload.get("event")
                                record["trace_id"] = payload.get("trace_id")
                            elif name == "transcript":
                                record["kind"] = payload.get("kind")
                                record["text_chars"] = len(payload.get("text", ""))
                            elif name == "question_decision":
                                record["is_question"] = payload.get("is_question")
                            records.append(record)
                            if name == "answer" and payload.get("event") == "done":
                                return
                        name, data_lines = None, []
                    elif line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())

        consumer = asyncio.create_task(consume_events())
        await asyncio.sleep(0.15)
        response = await client.post("/api/capture/start", json={
            "mode": "loopback", "session_id": "frontend-alignment-mock",
        })
        response.raise_for_status()
        await asyncio.wait_for(consumer, timeout=10)

    ordered = [item.get("event") for item in records]
    if "transcript" not in ordered or "question_decision" not in ordered or "answer" not in ordered:
        raise RuntimeError(f"缺少旧前端 SSE 事件：{ordered}")
    answer_names = [item.get("answer_event") for item in records if item.get("event") == "answer"]
    if not answer_names or answer_names[0] != "meta" or answer_names[-1] != "done" or "token" not in answer_names:
        raise RuntimeError(f"Mock SSE 回答事件顺序无效：{answer_names}")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--log", default=str(PROJECT_ROOT / "deliverables" / "logs" / "frontend_mock_alignment.jsonl"))
    args = parser.parse_args()
    records = asyncio.run(collect_alignment(args.base_url))
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    summary = {
        "result": "ALIGNMENT_PASS",
        "events": len(records),
        "transcript_events": sum(item["event"] == "transcript" for item in records),
        "question_events": sum(item["event"] == "question_decision" for item in records),
        "answer_event_order": [item.get("answer_event") for item in records if item["event"] == "answer"],
        "log": str(log_path),
    }
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
