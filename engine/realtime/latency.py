"""全链路延迟打点与 JSON 结构化日志。"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


_PAIRS = {
    "vad_ms": ("audio_first", "speech_start"),
    "asr_first_partial_ms": ("speech_start", "asr_partial"),
    "rag_ms": ("rag_start", "rag_end"),
    "ttft_ms": ("asr_final", "llm_first_token"),
    "render_ms": ("llm_first_token", "render_first"),
    "server_e2e_ms": ("speech_end", "llm_first_token"),
    "end_to_end_ms": ("speech_end", "render_first"),
    "total_ms": ("audio_first", "done"),
}


class LatencyHistogramRegistry:
    """进程内阶段直方图摘要；生产环境可把 snapshot 接到 Prometheus。"""

    def __init__(self, max_samples: int = 2000):
        self.max_samples = max_samples
        self._samples: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def observe(self, report: dict[str, object]) -> None:
        metrics = report.get("metrics", {})
        if not isinstance(metrics, dict):
            return
        with self._lock:
            for name, value in metrics.items():
                if value is None:
                    continue
                values = self._samples.setdefault(name, [])
                values.append(float(value))
                if len(values) > self.max_samples:
                    del values[:len(values) - self.max_samples]

    @staticmethod
    def _percentile(values: list[float], ratio: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
        return round(ordered[index], 3)

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        with self._lock:
            return {
                name: {
                    "count": len(values),
                    "p50_ms": self._percentile(values, 0.50),
                    "p95_ms": self._percentile(values, 0.95),
                    "max_ms": round(max(values), 3) if values else None,
                }
                for name, values in self._samples.items()
            }


LATENCY_METRICS = LatencyHistogramRegistry()


@dataclass(slots=True)
class LatencyTracker:
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    clock: Callable[[], float] = time.perf_counter
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("interview_monkey.realtime"))
    timestamps: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    registry: LatencyHistogramRegistry = field(default_factory=lambda: LATENCY_METRICS)

    def mark(self, stage: str) -> float:
        now = self.clock()
        self.timestamps.setdefault(stage, now)
        return now

    def increment(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    def elapsed_ms(self, start: str, end: str) -> float | None:
        if start not in self.timestamps or end not in self.timestamps:
            return None
        return round((self.timestamps[end] - self.timestamps[start]) * 1000.0, 3)

    def report(self, status: str = "done", **extra: object) -> dict[str, object]:
        metrics = {
            name: self.elapsed_ms(start, end)
            for name, (start, end) in _PAIRS.items()
        }
        return {
            "event": "qa_latency",
            "trace_id": self.trace_id,
            "status": status,
            "metrics": metrics,
            "counters": dict(self.counters),
            **extra,
        }

    def finish(self, status: str = "done", **extra: object) -> dict[str, object]:
        self.mark("done")
        report = self.report(status, **extra)
        self.registry.observe(report)
        self.logger.info(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return report
