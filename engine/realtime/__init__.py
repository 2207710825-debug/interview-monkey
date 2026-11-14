"""低延迟实时语音问答核心。

该包与旧版 ``engine`` 模块并存，先提供可独立测试的异步闭环，后续由
pywebview 入口按需接入，避免一次性重写现有桌面应用。
"""

from .contracts import (
    AudioFrame,
    Channel,
    Chunk,
    Hotword,
    QuestionDecision,
    StreamEvent,
    Transcript,
    TranscriptKind,
)
from .latency import LatencyTracker

__all__ = [
    "AudioFrame",
    "Channel",
    "Chunk",
    "Hotword",
    "LatencyTracker",
    "QuestionDecision",
    "StreamEvent",
    "Transcript",
    "TranscriptKind",
]
