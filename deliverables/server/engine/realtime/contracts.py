"""实时闭环的稳定数据契约。

接口采用异步迭代器而不是同步回调：生产者可以自然等待有界队列，从而把
背压一路传回音频入口；需要实时性的音频队列则显式采用“丢最旧帧”策略。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterable, AsyncIterator, Mapping, Protocol, Sequence


class Channel(str, Enum):
    LOOPBACK = "loopback"
    MIC = "mic"


class TranscriptKind(str, Enum):
    PARTIAL = "partial"
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """一帧 16-bit little-endian PCM；默认 16 kHz、单声道。"""

    pcm: bytes
    timestamp_ms: int
    channel: Channel
    sample_rate: int = 16_000
    utterance_end: bool = False

    def __post_init__(self) -> None:
        if len(self.pcm) % 2:
            raise ValueError("PCM16 字节数必须是偶数")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate 必须为正数")

    @property
    def duration_ms(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate * 1000.0


@dataclass(frozen=True, slots=True)
class Transcript:
    kind: TranscriptKind
    text: str
    channel: Channel
    start_ms: int
    end_ms: int
    speaker_id: str | None = None
    confidence: float | None = None
    trace_id: str | None = None
    utterance_id: str | None = None
    raw_text: str | None = None
    corrected: bool = False
    asr_engine: str | None = None
    pass1_ms: float | None = None
    pass2_ms: float | None = None
    pass1_text: str | None = None
    corrections: tuple[Mapping[str, Any], ...] = ()
    suspicious: tuple[Mapping[str, Any], ...] = ()
    is_fragment: bool = False
    preprocess_text: str | None = None


@dataclass(frozen=True, slots=True)
class Hotword:
    term: str
    weight: int = 5
    aliases: tuple[str, ...] = ()
    category: str = ""

    def __post_init__(self) -> None:
        if not self.term.strip():
            raise ValueError("热词不能为空")
        if not 1 <= self.weight <= 10:
            raise ValueError("热词权重必须在 1~10")


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str
    text: str
    source: str
    score: float
    section: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QuestionDecision:
    is_question: bool
    confidence: float
    normalized_query: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StreamEvent:
    event: str
    trace_id: str
    data: Mapping[str, Any]


class AudioSource(Protocol):
    def frames(self) -> AsyncIterator[AudioFrame]:
        """持续产出 PCM 帧；迭代结束代表采集停止。"""


class VadSegmenter(Protocol):
    async def segment(self, frames: AsyncIterable[AudioFrame]) -> AsyncIterator[AudioFrame]:
        """只产出属于语音段的帧，并自行发出边界事件。"""


class ASRProvider(Protocol):
    def transcribe(self, frames: AsyncIterable[AudioFrame]) -> AsyncIterator[Transcript]:
        """消费音频并产出 partial/final。"""


class QuestionDetector(Protocol):
    async def evaluate(self, transcript: Transcript, context: Sequence[Transcript]) -> QuestionDecision:
        """判断 final 是否需要回答，并返回独立查询。"""


class Retriever(Protocol):
    async def retrieve(self, query: str, *, top_k: int = 4, min_score: float = 0.35) -> list[Chunk]:
        """返回已按相关性排序的知识片段。"""


class LLMProvider(Protocol):
    def stream(self, messages: Sequence[Mapping[str, str]], cancel: Any = None) -> AsyncIterator[str]:
        """逐段返回模型文本；cancel 被触发后必须尽快退出。"""
