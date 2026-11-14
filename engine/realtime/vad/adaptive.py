"""20~40ms 帧级动态 VAD，含 pre-roll 与最大句长保护。"""
from __future__ import annotations

import math
from array import array
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterable, AsyncIterator, Callable

from ..contracts import AudioFrame
from ..utterance import is_incomplete_ending


@dataclass(frozen=True, slots=True)
class VadBoundary:
    kind: str
    timestamp_ms: int
    reason: str = ""


def _rms(pcm: bytes) -> float:
    values = array("h")
    values.frombytes(pcm)
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


class AdaptiveVadSegmenter:
    def __init__(
        self,
        *,
        energy_threshold: int = 450,
        base_silence_ms: int = 500,
        fast_silence_ms: int = 300,
        incomplete_silence_ms: int = 700,
        pre_roll_ms: int = 200,
        post_roll_ms: int = 150,
        max_utterance_ms: int = 15_000,
        on_boundary: Callable[[VadBoundary], None] | None = None,
        use_webrtc: bool = True,
        webrtc_aggressiveness: int = 2,
    ):
        self.energy_threshold = energy_threshold
        self.base_silence_ms = base_silence_ms
        self.fast_silence_ms = fast_silence_ms
        self.incomplete_silence_ms = incomplete_silence_ms
        self.pre_roll_ms = pre_roll_ms
        self.post_roll_ms = post_roll_ms
        self.max_utterance_ms = max_utterance_ms
        self.on_boundary = on_boundary
        self.partial_text = ""
        self._speech_rate = 0.0
        self._webrtc = None
        if use_webrtc:
            try:
                import webrtcvad
                self._webrtc = webrtcvad.Vad(webrtc_aggressiveness)
            except ImportError:
                self._webrtc = None

    def _is_voiced(self, frame: AudioFrame) -> bool:
        if self._webrtc is None:
            return _rms(frame.pcm) >= self.energy_threshold
        # WebRTC VAD 只接受 10/20/30ms。较大的输入切成 20ms 子帧，任一命中即视为语音。
        bytes_per_20ms = int(frame.sample_rate * 0.02) * 2
        if len(frame.pcm) in {
            int(frame.sample_rate * 0.01) * 2,
            bytes_per_20ms,
            int(frame.sample_rate * 0.03) * 2,
        }:
            return bool(self._webrtc.is_speech(frame.pcm, frame.sample_rate))
        chunks = [
            frame.pcm[start:start + bytes_per_20ms]
            for start in range(0, len(frame.pcm) - bytes_per_20ms + 1, bytes_per_20ms)
        ]
        if not chunks:
            return _rms(frame.pcm) >= self.energy_threshold
        return any(self._webrtc.is_speech(chunk, frame.sample_rate) for chunk in chunks)

    def observe_partial(self, text: str, utterance_ms: int) -> None:
        self.partial_text = (text or "").strip()
        self._speech_rate = len(self.partial_text) / max(utterance_ms / 1000.0, 0.1)

    @property
    def silence_threshold_ms(self) -> int:
        if is_incomplete_ending(self.partial_text):
            return self.incomplete_silence_ms
        if self._speech_rate >= 6.0:
            return self.fast_silence_ms
        return self.base_silence_ms

    def _emit(self, kind: str, timestamp_ms: int, reason: str = "") -> None:
        if self.on_boundary:
            self.on_boundary(VadBoundary(kind, timestamp_ms, reason))

    async def segment(self, frames: AsyncIterable[AudioFrame]) -> AsyncIterator[AudioFrame]:
        pre_roll: deque[AudioFrame] = deque()
        pre_roll_duration = 0.0
        speaking = False
        speech_start_ms = 0
        silence_ms = 0.0
        last_frame: AudioFrame | None = None

        async for frame in frames:
            last_frame = frame
            voiced = self._is_voiced(frame)
            if not speaking:
                pre_roll.append(frame)
                pre_roll_duration += frame.duration_ms
                while pre_roll and pre_roll_duration > self.pre_roll_ms:
                    pre_roll_duration -= pre_roll.popleft().duration_ms
                if not voiced:
                    continue
                speaking = True
                speech_start_ms = frame.timestamp_ms
                silence_ms = 0.0
                self._emit("speech_start", frame.timestamp_ms)
                while pre_roll:
                    yield pre_roll.popleft()
                pre_roll_duration = 0.0
                continue

            yield frame
            silence_ms = 0.0 if voiced else silence_ms + frame.duration_ms
            utterance_ms = frame.timestamp_ms + frame.duration_ms - speech_start_ms
            reason = ""
            if utterance_ms >= self.max_utterance_ms:
                reason = "max_utterance"
            elif silence_ms >= max(self.silence_threshold_ms, self.post_roll_ms):
                reason = "silence"
            if reason:
                end_ms = int(frame.timestamp_ms + frame.duration_ms)
                self._emit("speech_end", end_ms, reason)
                yield AudioFrame(
                    b"", end_ms, frame.channel, frame.sample_rate, utterance_end=True,
                )
                speaking = False
                silence_ms = 0.0
                self.partial_text = ""
                self._speech_rate = 0.0

        if speaking and last_frame is not None:
            end_ms = int(last_frame.timestamp_ms + last_frame.duration_ms)
            self._emit("speech_end", end_ms, "source_closed")
            yield AudioFrame(
                b"", end_ms, last_frame.channel, last_frame.sample_rate, utterance_end=True,
            )
