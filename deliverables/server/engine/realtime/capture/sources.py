"""音频源适配器。

Windows 系统声使用项目已有的 WASAPI Loopback；麦克风、Linux monitor source
与 macOS 虚拟声卡都通过 PortAudio 设备选择接入。所有回调只写有界环形队列，
重采样和声道转换在 asyncio 消费侧完成。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Sequence

from ..contracts import AudioFrame, Channel
from ..events import DropOldestQueue


log = logging.getLogger("interview_monkey.realtime.capture")


@dataclass(frozen=True, slots=True)
class _RawFrame:
    pcm: bytes
    timestamp_ms: int
    sample_rate: int
    channels: int


def _to_mono_16k(raw: _RawFrame, conditioner=None) -> bytes:
    from engine.capture import pcm16_to_mono_16k

    mono = pcm16_to_mono_16k(raw.pcm, raw.sample_rate, raw.channels)
    if conditioner is not None:
        mono = conditioner.process(mono)
    return mono.tobytes()


class WindowsLoopbackAudioSource:
    """真实 WASAPI Loopback；内部复用已经过设备测试的 LoopbackCapture。"""

    channel = Channel.LOOPBACK

    def __init__(
        self,
        device_index: int | None = None,
        queue_frames: int = 250,
        *,
        agc_enabled: bool = False,
        target_rms_dbfs: float = -20.0,
        limiter_dbfs: float = -1.0,
        max_gain_db: float = 12.0,
    ):
        from engine.capture import AudioConditioner

        self.device_index = device_index
        self.queue = DropOldestQueue(queue_frames, self._warn_drop)
        self.conditioner = AudioConditioner(
            agc_enabled, target_rms_dbfs, limiter_dbfs, max_gain_db,
        )
        self._capture = None
        self._closed = False

    @staticmethod
    def _warn_drop(_frame) -> None:
        log.warning("audio_buffer_overflow", extra={"channel": Channel.LOOPBACK.value})

    async def frames(self) -> AsyncIterator[AudioFrame]:
        from engine.capture import LoopbackCapture

        loop = asyncio.get_running_loop()

        def on_pcm(pcm) -> None:
            pcm = self.conditioner.process(pcm)
            data = pcm.tobytes() if hasattr(pcm, "tobytes") else bytes(pcm)
            frame = AudioFrame(data, int(time.time() * 1000), Channel.LOOPBACK)
            loop.call_soon_threadsafe(self.queue.put_latest_nowait, frame)

        self._capture = LoopbackCapture(on_pcm, self.device_index)
        await asyncio.to_thread(self._capture.start)
        try:
            while not self._closed:
                yield await self.queue.get()
                self.queue.task_done()
        finally:
            await self.close()

    def stats(self) -> dict[str, object]:
        output = {
            "channel": Channel.LOOPBACK.value,
            "queue_published": self.queue.stats.published,
            "queue_dropped": self.queue.stats.dropped,
        }
        if self._capture is not None:
            output.update(self._capture.stats())
        return output

    async def close(self) -> None:
        self._closed = True
        if self._capture is not None:
            capture, self._capture = self._capture, None
            await asyncio.to_thread(capture.stop)


class PortAudioInputSource:
    """麦克风/虚拟声卡/monitor source 统一适配器。

    macOS 可选择 BlackHole 等虚拟声卡，Linux 可选择 PulseAudio/PipeWire 的
    ``*.monitor`` 输入。设备原始采样率会在消费线程转为 16 kHz 单声道 PCM16。
    """

    def __init__(
        self,
        channel: Channel = Channel.MIC,
        device: int | str | None = None,
        sample_rate: int | None = None,
        channels: int = 1,
        frame_ms: int = 20,
        queue_frames: int = 250,
        agc_enabled: bool = True,
        target_rms_dbfs: float = -20.0,
        limiter_dbfs: float = -1.0,
        max_gain_db: float = 12.0,
    ):
        from engine.capture import AudioConditioner

        self.channel = channel
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.queue = DropOldestQueue(queue_frames, self._warn_drop)
        self.conditioner = AudioConditioner(
            agc_enabled, target_rms_dbfs, limiter_dbfs, max_gain_db,
        )
        self._stream = None
        self._closed = False
        self._last_callback_at = None
        self._callback_late_count = 0
        self._callback_status_count = 0
        self._max_callback_gap_ms = 0.0

    def _warn_drop(self, _frame) -> None:
        log.warning("audio_buffer_overflow", extra={"channel": self.channel.value})

    async def frames(self) -> AsyncIterator[AudioFrame]:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("PortAudio 输入需要 sounddevice，请安装 requirements-realtime.txt") from exc

        loop = asyncio.get_running_loop()
        info = sd.query_devices(self.device, "input")
        sample_rate = int(self.sample_rate or info["default_samplerate"])
        channels = min(self.channels, int(info["max_input_channels"]))
        blocksize = max(1, int(sample_rate * self.frame_ms / 1000))

        def callback(indata, frames, timestamp, status) -> None:
            now = time.perf_counter()
            if self._last_callback_at is not None:
                gap_ms = (now - self._last_callback_at) * 1000.0
                self._max_callback_gap_ms = max(self._max_callback_gap_ms, gap_ms)
                if gap_ms > self.frame_ms * 1.5:
                    self._callback_late_count += 1
                    count = self._callback_late_count
                    if count == 1 or count % 50 == 0:
                        log.warning("audio_callback_late channel=%s gap_ms=%.1f count=%d", self.channel.value, gap_ms, count)
            self._last_callback_at = now
            if status:
                self._callback_status_count += 1
                log.warning("audio_capture_status", extra={"status": str(status)})
            raw = _RawFrame(bytes(indata), int(time.time() * 1000), sample_rate, channels)
            loop.call_soon_threadsafe(self.queue.put_latest_nowait, raw)

        self._stream = sd.RawInputStream(
            samplerate=sample_rate,
            blocksize=blocksize,
            device=self.device,
            channels=channels,
            dtype="int16",
            callback=callback,
        )
        self._stream.start()
        try:
            while not self._closed:
                raw = await self.queue.get()
                self.queue.task_done()
                pcm = await asyncio.to_thread(_to_mono_16k, raw, self.conditioner)
                if pcm:
                    yield AudioFrame(pcm, raw.timestamp_ms, self.channel)
        finally:
            await self.close()

    def stats(self) -> dict[str, object]:
        return {
            "channel": self.channel.value,
            "queue_published": self.queue.stats.published,
            "queue_dropped": self.queue.stats.dropped,
            "callback_late_count": self._callback_late_count,
            "callback_status_count": self._callback_status_count,
            "max_callback_gap_ms": round(self._max_callback_gap_ms, 3),
        }

    async def close(self) -> None:
        self._closed = True
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await asyncio.to_thread(stream.stop)
            await asyncio.to_thread(stream.close)


class CombinedAudioSource:
    """并行合并多个音频源，保留每帧的 channel。"""

    def __init__(self, sources: Sequence[object], queue_frames: int = 500):
        self.sources = tuple(sources)
        self.queue = DropOldestQueue(queue_frames)
        self._tasks: list[asyncio.Task] = []

    async def _pump(self, source) -> None:
        async for frame in source.frames():
            self.queue.put_latest_nowait(frame)

    async def frames(self) -> AsyncIterator[AudioFrame]:
        self._tasks = [asyncio.create_task(self._pump(source)) for source in self.sources]
        try:
            while True:
                if self._tasks and all(task.done() for task in self._tasks) and self.queue.empty():
                    return
                try:
                    frame = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                self.queue.task_done()
                yield frame
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            for source in self.sources:
                close = getattr(source, "close", None)
                if close:
                    await close()
