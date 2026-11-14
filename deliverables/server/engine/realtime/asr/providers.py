"""本地 sherpa-onnx 与通用 JSON WebSocket 流式 ASR 提供器。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
from collections import deque
from dataclasses import replace
from typing import AsyncIterable, AsyncIterator, Callable, Iterable
from uuid import uuid4

from ..contracts import AudioFrame, Channel, Hotword, Transcript, TranscriptKind
from ..events import DropOldestQueue
from .hotwords import HotwordCorrector, materialize_decoder_hotwords


log = logging.getLogger("interview_monkey.realtime.asr")
_COMPARE_DROP = re.compile(r"[\s，。！？、；：,.!?;:]+")


def _comparable(text: str) -> str:
    return _COMPARE_DROP.sub("", text or "").casefold()


class LocalSherpaASRProvider:
    def __init__(
        self,
        model_dir: str | None = None,
        hotwords: Iterable[Hotword] = (),
        *,
        hotword_boost: float = 1.5,
        two_pass_enabled: bool = False,
        two_pass_engine: str = "sense_voice",
        second_pass_timeout_ms: int = 2500,
    ):
        self.model_dir = model_dir
        self.hotwords = tuple(hotwords)
        self.corrector = HotwordCorrector(self.hotwords)
        self.hotword_boost = hotword_boost
        self.two_pass_enabled = two_pass_enabled
        self.two_pass_engine = two_pass_engine
        self.second_pass_timeout_ms = second_pass_timeout_ms
        self._streaming_engine = None
        self._second_pass = None
        self._models_loaded = False
        self._model_lock = asyncio.Lock()

    async def _ensure_models(self) -> None:
        if self._models_loaded:
            return
        async with self._model_lock:
            if self._models_loaded:
                return
            from engine.asr import _SherpaOnnx

            decoder_hotwords = await asyncio.to_thread(materialize_decoder_hotwords, self.hotwords)
            self._streaming_engine = await asyncio.to_thread(
                _SherpaOnnx, self.model_dir, self.hotword_boost,
                str(decoder_hotwords) if decoder_hotwords else None,
            )
            if self.two_pass_enabled:
                if self.two_pass_engine != "sense_voice":
                    raise ValueError(f"不支持的第二遍引擎: {self.two_pass_engine}")
                try:
                    from engine.asr2 import SenseVoiceRecognizer
                    self._second_pass = await asyncio.to_thread(SenseVoiceRecognizer)
                except Exception as exc:
                    self._second_pass = None
                    log.warning("second_pass_unavailable error=%s", type(exc).__name__)
            self._models_loaded = True

    def update_hotwords(self, hotwords: Iterable[Hotword]) -> None:
        """刷新文本级别名；模型级术语在下次进程启动时重建。"""
        self.hotwords = tuple(hotwords)
        self.corrector = HotwordCorrector(self.hotwords)

    async def transcribe(self, frames: AsyncIterable[AudioFrame]) -> AsyncIterator[Transcript]:
        from engine.asr import StreamingSession
        import numpy as np

        await self._ensure_models()
        session = StreamingSession(self._streaming_engine)
        last_partial = ""
        last_raw_partial = ""
        start_ms: int | None = None
        channel = Channel.LOOPBACK
        utterance_id: str | None = None
        pcm_buffer = bytearray()
        streaming_finals: list[str] = []
        pass1_ms = 0.0

        def current_utterance_id() -> str:
            nonlocal utterance_id
            if utterance_id is None:
                utterance_id = uuid4().hex
            return utterance_id

        async def finalize(end_ms: int) -> Transcript | None:
            nonlocal pass1_ms
            if start_ms is None or not pcm_buffer:
                return None
            confirmed_before_finish = getattr(session, "text", "") or ""
            started = time.perf_counter()
            cumulative_tail = await asyncio.to_thread(session.finish)
            pass1_ms += (time.perf_counter() - started) * 1000.0
            pending = getattr(session, "_pending", None)
            if pending:
                session._pending = None
                streaming_finals.append(str(pending))
            elif cumulative_tail.startswith(confirmed_before_finish):
                tail = cumulative_tail[len(confirmed_before_finish):]
                if tail:
                    streaming_finals.append(tail)

            raw_pass1 = "".join(streaming_finals).strip() or last_raw_partial.strip()
            fallback = self.corrector.correct(raw_pass1)
            final_text = fallback
            raw_final = raw_pass1
            engine_name = "sherpa_transducer"
            pass2_ms: float | None = None

            if self.two_pass_enabled and self._second_pass is not None:
                samples = np.frombuffer(bytes(pcm_buffer), dtype=np.int16)
                second_started = time.perf_counter()
                try:
                    decoded = await asyncio.wait_for(
                        asyncio.to_thread(self._second_pass.transcribe, samples, 16000),
                        timeout=self.second_pass_timeout_ms / 1000.0,
                    )
                    pass2_ms = (time.perf_counter() - second_started) * 1000.0
                    raw_decoded = str(decoded or "").strip()
                    decoded = self.corrector.correct(raw_decoded)
                    if decoded:
                        raw_final = raw_decoded
                        final_text = decoded
                        engine_name = "sense_voice"
                except asyncio.TimeoutError:
                    pass2_ms = (time.perf_counter() - second_started) * 1000.0
                    log.warning(
                        "second_pass_timeout utterance_id=%s timeout_ms=%d",
                        current_utterance_id(), self.second_pass_timeout_ms,
                    )
                except Exception as exc:
                    pass2_ms = (time.perf_counter() - second_started) * 1000.0
                    log.warning(
                        "second_pass_failed utterance_id=%s error=%s",
                        current_utterance_id(), type(exc).__name__,
                    )

            if not final_text:
                return None
            return Transcript(
                TranscriptKind.FINAL,
                final_text,
                channel,
                start_ms,
                end_ms,
                confidence=None,
                utterance_id=current_utterance_id(),
                raw_text=raw_final or None,
                corrected=bool(
                    (fallback and _comparable(final_text) != _comparable(fallback))
                    or (raw_final and _comparable(final_text) != _comparable(raw_final))
                ),
                asr_engine=engine_name,
                pass1_ms=round(pass1_ms, 3),
                pass2_ms=round(pass2_ms, 3) if pass2_ms is not None else None,
                pass1_text=raw_pass1 or None,
            )

        def reset() -> None:
            nonlocal session, last_partial, last_raw_partial, start_ms
            nonlocal utterance_id, pcm_buffer, streaming_finals, pass1_ms
            session = StreamingSession(self._streaming_engine)
            last_partial = ""
            last_raw_partial = ""
            start_ms = None
            utterance_id = None
            pcm_buffer = bytearray()
            streaming_finals = []
            pass1_ms = 0.0

        async for frame in frames:
            channel = frame.channel
            if frame.utterance_end:
                final = await finalize(frame.timestamp_ms)
                if final:
                    yield final
                reset()
                continue
            if start_ms is None:
                start_ms = frame.timestamp_ms
            pcm_buffer.extend(frame.pcm)
            started = time.perf_counter()
            cumulative = await asyncio.to_thread(session.feed, frame.pcm, frame.sample_rate)
            pass1_ms += (time.perf_counter() - started) * 1000.0
            # StreamingSession 为兼容旧会议界面返回“历史 final + 当前 partial”。
            # 实时工作台只发布当前 utterance，避免下一句 partial 携带所有历史文本。
            confirmed = getattr(session, "text", "") or ""
            current = cumulative[len(confirmed):] if cumulative.startswith(confirmed) else cumulative
            last_raw_partial = current or last_raw_partial
            text = self.corrector.correct(current)
            pending = getattr(session, "_pending", None)
            if pending:
                session._pending = None
                if self.two_pass_enabled:
                    streaming_finals.append(str(pending))
                else:
                    final_text = self.corrector.correct(pending)
                    yield Transcript(
                        TranscriptKind.FINAL, final_text, channel,
                        start_ms, int(frame.timestamp_ms + frame.duration_ms), confidence=1.0,
                        utterance_id=current_utterance_id(),
                        raw_text=final_text,
                        asr_engine="sherpa_transducer",
                        pass1_ms=round(pass1_ms, 3),
                    )
                    start_ms = int(frame.timestamp_ms + frame.duration_ms)
                    last_partial = ""
                    last_raw_partial = ""
                    utterance_id = None
                    pcm_buffer = bytearray()
                    streaming_finals = []
                    pass1_ms = 0.0
            elif text and text != last_partial:
                last_partial = text
                yield Transcript(
                    TranscriptKind.PARTIAL, text, channel,
                    start_ms, int(frame.timestamp_ms + frame.duration_ms),
                    utterance_id=current_utterance_id(),
                    asr_engine="sherpa_transducer",
                )
        end_ms = start_ms if start_ms is not None else 0
        final = await finalize(end_ms)
        if final:
            yield final


class JsonWebSocketASRProvider:
    """通用 WebSocket 适配器。

    默认协议发送 ``audio/start/stop/ping`` JSON；接收
    ``{"type":"partial|final","text":"..."}``。服务商协议不同，可通过
    message_builder/parser 注入转换逻辑，无需改重连与缓冲代码。
    """

    def __init__(
        self,
        url: str,
        *,
        language: str = "zh-CN",
        hotwords: Iterable[Hotword] = (),
        headers: dict[str, str] | None = None,
        reconnect_attempts: int = 5,
        heartbeat_seconds: float = 15.0,
        buffered_frames: int = 500,
        message_builder: Callable[[AudioFrame], str] | None = None,
        parser: Callable[[str], Transcript | None] | None = None,
    ):
        self.url = url
        self.language = language
        self.hotwords = tuple(hotwords)
        self.headers = headers or {}
        self.reconnect_attempts = reconnect_attempts
        self.heartbeat_seconds = heartbeat_seconds
        self.queue = DropOldestQueue(buffered_frames)
        self.message_builder = message_builder or self._build_audio
        self.parser = parser or self._parse

    @staticmethod
    def _build_audio(frame: AudioFrame) -> str:
        if frame.utterance_end:
            return json.dumps({
                "type": "utterance_end",
                "timestamp_ms": frame.timestamp_ms,
                "channel": frame.channel.value,
            }, separators=(",", ":"))
        return json.dumps({
            "type": "audio",
            "pcm": base64.b64encode(frame.pcm).decode("ascii"),
            "timestamp_ms": frame.timestamp_ms,
            "sample_rate": frame.sample_rate,
            "channel": frame.channel.value,
        }, separators=(",", ":"))

    @staticmethod
    def _parse(message: str) -> Transcript | None:
        data = json.loads(message)
        if data.get("type") not in {"partial", "final"}:
            return None
        return Transcript(
            TranscriptKind(data["type"]), str(data.get("text", "")),
            Channel(data.get("channel", "loopback")),
            int(data.get("start_ms", 0)), int(data.get("end_ms", 0)),
            data.get("speaker_id"), data.get("confidence"), data.get("trace_id"),
            data.get("utterance_id"),
        )

    async def _produce(self, frames: AsyncIterable[AudioFrame]) -> None:
        async for frame in frames:
            self.queue.put_latest_nowait(frame)
        self.queue.put_latest_nowait(None)

    async def transcribe(self, frames: AsyncIterable[AudioFrame]) -> AsyncIterator[Transcript]:
        from websockets.asyncio.client import connect

        producer = asyncio.create_task(self._produce(frames))
        attempt = 0
        ended = False
        utterance_id: str | None = None

        def ensure_utterance_id(transcript: Transcript) -> Transcript:
            nonlocal utterance_id
            if transcript.utterance_id:
                current = transcript
            else:
                if utterance_id is None:
                    utterance_id = uuid4().hex
                current = replace(transcript, utterance_id=utterance_id)
            if current.kind is TranscriptKind.FINAL:
                utterance_id = None
            return current

        try:
            while not ended and attempt < self.reconnect_attempts:
                try:
                    async with connect(self.url, additional_headers=self.headers, ping_interval=None) as ws:
                        await ws.send(json.dumps({
                            "type": "start",
                            "language": self.language,
                            "hotwords": [
                                {"term": word.term, "weight": word.weight, "aliases": list(word.aliases)}
                                for word in self.hotwords
                            ],
                        }, ensure_ascii=False))
                        while True:
                            recv_task = asyncio.create_task(ws.recv())
                            audio_task = asyncio.create_task(self.queue.get())
                            done, pending = await asyncio.wait(
                                {recv_task, audio_task},
                                timeout=self.heartbeat_seconds,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for task in pending:
                                task.cancel()
                            if pending:
                                await asyncio.gather(*pending, return_exceptions=True)
                            if not done:
                                await ws.send('{"type":"ping"}')
                                continue
                            if audio_task in done:
                                frame = audio_task.result()
                                self.queue.task_done()
                                if frame is None:
                                    await ws.send('{"type":"stop"}')
                                    ended = True
                                else:
                                    await ws.send(self.message_builder(frame))
                            if recv_task in done:
                                transcript = self.parser(recv_task.result())
                                if transcript:
                                    yield ensure_utterance_id(transcript)
                            if ended:
                                # 给服务端最后一次返回 final 的机会。
                                try:
                                    message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                                    transcript = self.parser(message)
                                    if transcript:
                                        yield ensure_utterance_id(transcript)
                                except Exception:
                                    pass
                                break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    attempt += 1
                    if attempt >= self.reconnect_attempts:
                        raise
                    delay = min(8.0, 0.5 * (2 ** (attempt - 1)))
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
