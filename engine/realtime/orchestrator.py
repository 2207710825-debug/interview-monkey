"""Audio -> VAD -> ASR -> Question -> RAG/LLM 的异步闭环编排。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from .contracts import QuestionDecision
from .events import AsyncEventBus
from .latency import LatencyTracker
from .qa.detector import AdjacentFinalMerger
from .rag.context import ConversationContext

log = logging.getLogger("interview_monkey.realtime.qa")


class RealtimePipeline:
    def __init__(
        self,
        vad,
        asr,
        detector,
        answer_service,
        bus: AsyncEventBus | None = None,
        qa_logger=None,
        transcript_logger=None,
    ):
        self.vad = vad
        self.asr = asr
        self.detector = detector
        self.answer_service = answer_service
        self.bus = bus or AsyncEventBus()
        self.qa_logger = qa_logger
        self.transcript_logger = transcript_logger
        self.context = ConversationContext()
        self.final_merger = AdjacentFinalMerger(window_ms=1500)
        self._answer_task: asyncio.Task | None = None
        self._tracker: LatencyTracker | None = None
        self._trackers: dict[str, LatencyTracker] = {}
        self._audio_segments: list[dict] = []
        self._source_audio_refs: list[dict] = []
        self.vad.on_boundary = self._on_vad_boundary

    def _on_vad_boundary(self, boundary) -> None:
        if boundary.kind == "speech_start":
            self._tracker = LatencyTracker()
            self._tracker.mark("audio_first")
            self._tracker.mark("speech_start")
        elif boundary.kind == "speech_end" and self._tracker:
            self._tracker.mark("speech_end")

    async def _publish(self, topic: str, event) -> None:
        await self.bus.publish(topic, event)
        await self.bus.publish("ui", {"type": topic, "payload": event})

    async def _emit_answer(
        self,
        question: str,
        session_id: str,
        tracker: LatencyTracker | None,
        *,
        suspicious_candidates=(),
        raw_question: str | None = None,
        asr_alternatives=(),
        recent=(),
        transcript=None,
        audio_refs=(),
    ) -> None:
        metadata = {}
        latency_metrics = {}
        answer_parts = []
        status = "error"
        error = None
        try:
            async for event in self.answer_service.stream_answer(
                question,
                session_id=session_id,
                recent=recent,
                trace_id=tracker.trace_id if tracker else None,
                tracker=tracker,
                suspicious_candidates=suspicious_candidates,
                raw_question=raw_question,
                asr_alternatives=asr_alternatives,
            ):
                if event.event == "meta":
                    metadata = dict(event.data)
                elif event.event == "token":
                    answer_parts.append(str(event.data.get("delta", "")))
                elif event.event == "done":
                    status = str(event.data.get("status", "done"))
                    latency_metrics = dict(event.data.get("metrics", {}))
                elif event.event == "error":
                    status = "error"
                    error = str(event.data.get("message", ""))
                    latency_metrics = dict(event.data.get("metrics", {}))
                await self._publish("answer", event)
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception as exc:
            status = "error"
            error = str(exc)
            raise
        finally:
            if self.qa_logger is not None and transcript is not None:
                record = {
                    "trace_id": tracker.trace_id if tracker else transcript.trace_id,
                    "session_id": session_id,
                    "source": "speech",
                    "utterance_id": transcript.utterance_id,
                    "question_start_ms": transcript.start_ms,
                    "question_end_ms": transcript.end_ms,
                    "raw_text": transcript.raw_text or raw_question or transcript.text,
                    "corrected_text": transcript.text,
                    "corrections": [dict(item) for item in transcript.corrections],
                    "asr_engine": transcript.asr_engine,
                    "pass1_text": transcript.pass1_text,
                    "pass1_ms": transcript.pass1_ms,
                    "pass2_ms": transcript.pass2_ms,
                    "retrieval_top_k": metadata.get("sources", []),
                    "retrieval_queries": metadata.get("retrieval_queries", []),
                    "retrieval_low_confidence": metadata.get("low_confidence"),
                    "product_filter_applied": metadata.get("product_filter_applied"),
                    "question": metadata.get("question", question),
                    "llm_correction": metadata.get("llm_correction"),
                    "answer": "".join(answer_parts),
                    "status": status,
                    "error": error,
                    "latency_metrics": latency_metrics,
                    "audio_paths": list(audio_refs),
                }
                try:
                    self.qa_logger.append(record)
                except Exception as exc:
                    log.warning("qa_log_write_failed error=%s", type(exc).__name__)

    async def _start_answer(
        self,
        question: str,
        session_id: str,
        tracker: LatencyTracker | None = None,
        *,
        suspicious_candidates=(),
        raw_question: str | None = None,
        asr_alternatives=(),
        transcript=None,
        audio_refs=(),
    ) -> None:
        await self.answer_service.cancel(session_id)
        if self._answer_task and not self._answer_task.done():
            self._answer_task.cancel()
            await asyncio.gather(self._answer_task, return_exceptions=True)
            await self._publish("answer", {"event": "interrupted", "session_id": session_id})
        recent = [item.text for item in self.context.recent(6) if item.text.strip()][-3:]
        self._answer_task = asyncio.create_task(self._emit_answer(
            question,
            session_id,
            tracker,
            suspicious_candidates=suspicious_candidates,
            raw_question=raw_question,
            asr_alternatives=asr_alternatives,
            recent=recent,
            transcript=transcript,
            audio_refs=audio_refs,
        ))

    def _audio_references(self, transcript) -> list[dict]:
        if self._source_audio_refs:
            return list(self._source_audio_refs)
        matches = [
            item
            for item in self._audio_segments
            if item["channel"] == transcript.channel.value
            and item["start_ms"] <= transcript.end_ms
            and item["end_ms"] >= transcript.start_ms
        ]
        refs = []
        for item in matches:
            path = item.get("path")
            if path is None:
                try:
                    path = self.qa_logger.save_audio(
                        item["pcm"], item["sample_rate"], item["channel"],
                        item["start_ms"], item["end_ms"],
                    )
                    item["path"] = str(path) if path is not None else None
                except Exception as exc:
                    log.warning("qa_audio_archive_failed error=%s", type(exc).__name__)
            if path:
                refs.append({
                    "path": path,
                    "channel": item["channel"],
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                })
        return refs

    async def _start_ready(self, ready, session_id: str) -> None:
        trace_id = ready[0].trace_id
        tracker = self._trackers.pop(trace_id, None) if trace_id else None
        transcript = ready[0]
        candidates = list(transcript.suspicious)
        candidates.extend(
            item for item in transcript.corrections
            if item.get("reason") == "pinyin_fuzzy"
        )
        unique_candidates = {}
        for item in candidates:
            key = (str(item.get("raw", "")), str(item.get("corrected", "")))
            if all(key):
                unique_candidates[key] = item
        top_candidates = sorted(
            unique_candidates.values(),
            key=lambda item: float(item.get("score", 0.0) or 0.0),
            reverse=True,
        )[:3]
        await self._start_answer(
            ready[1].normalized_query,
            session_id,
            tracker,
            suspicious_candidates=top_candidates,
            raw_question=transcript.raw_text or transcript.text,
            asr_alternatives=(transcript.pass1_text,) if transcript.pass1_text else (),
            transcript=transcript,
            audio_refs=self._audio_references(transcript),
        )

    def _log_transcript(self, session_id: str, transcript) -> None:
        """把定稿转写写入本机会话历史；写失败不影响实时链路。"""
        if self.transcript_logger is None:
            return
        try:
            self.transcript_logger.append_transcript({
                "session_id": session_id,
                "utterance_id": transcript.utterance_id,
                "trace_id": transcript.trace_id,
                "channel": transcript.channel.value,
                "speaker_id": transcript.speaker_id,
                "text": transcript.text,
                "raw_text": transcript.raw_text,
                "pass1_text": transcript.pass1_text,
                "corrections": [dict(item) for item in transcript.corrections],
                "asr_engine": transcript.asr_engine,
                "start_ms": transcript.start_ms,
                "end_ms": transcript.end_ms,
                "is_fragment": transcript.is_fragment,
            })
        except Exception as exc:
            log.warning("transcript_log_write_failed error=%s", type(exc).__name__)

    async def _process_final(self, transcript, session_id: str) -> None:
        self._log_transcript(session_id, transcript)
        if transcript.is_fragment:
            decision = QuestionDecision(
                False, 0.0, transcript.text, "fragment",
                transcript.utterance_id, transcript.start_ms, transcript.end_ms, transcript.text,
            )
        else:
            decision = replace(
                await self.detector.evaluate(transcript, self.context.recent()),
                utterance_id=transcript.utterance_id,
                start_ms=transcript.start_ms,
                end_ms=transcript.end_ms,
                text=transcript.text,
            )
        self.context.append(transcript)
        await self._publish("question_decision", decision)
        if decision.is_question:
            await self._start_ready((transcript, decision), session_id)
        self._audio_segments = [item for item in self._audio_segments if item["end_ms"] > transcript.end_ms]

    async def run(self, source, session_id: str = "default") -> None:
        self._audio_segments = []
        source_paths = getattr(source, "audio_paths", None)
        if source_paths is None:
            source_path = getattr(source, "audio_path", None) or getattr(source, "path", None)
            source_paths = [source_path] if source_path else []
        self._source_audio_refs = [
            {"path": str(Path(path).resolve())}
            for path in source_paths
            if path and Path(path).is_file()
        ]
        speech_frames = self.vad.segment(source.frames())
        if self.qa_logger is not None and self.qa_logger.record_audio:
            speech_frames = self._record_audio_frames(speech_frames)
        transcript_stream = self.asr.transcribe(speech_frames).__aiter__()
        next_transcript = asyncio.create_task(anext(transcript_stream))
        deadline: float | None = None
        loop = asyncio.get_running_loop()
        try:
            while True:
                timeout = None if deadline is None else max(0.0, deadline - loop.time())
                completed, _ = await asyncio.wait({next_transcript}, timeout=timeout)
                if not completed:
                    pending = self.final_merger.flush()
                    deadline = None
                    if pending:
                        await self._process_final(pending, session_id)
                    continue

                try:
                    transcript = next_transcript.result()
                except StopAsyncIteration:
                    break
                next_transcript = asyncio.create_task(anext(transcript_stream))
                await self._publish("transcript", transcript)
                if transcript.kind.value == "partial":
                    if self._tracker:
                        self._tracker.mark("asr_partial")
                    observe = getattr(self.vad, "observe_partial", None)
                    if observe:
                        observe(transcript.text, max(1, transcript.end_ms - transcript.start_ms))
                    continue

                if self._tracker:
                    self._tracker.mark("asr_final")
                    transcript = replace(transcript, trace_id=self._tracker.trace_id)
                    self._trackers[self._tracker.trace_id] = self._tracker
                for ready_transcript in self.final_merger.push(transcript):
                    await self._process_final(ready_transcript, session_id)
                deadline = (
                    loop.time() + self.final_merger.window_ms / 1000.0
                    if self.final_merger.has_pending else None
                )
        finally:
            if not next_transcript.done():
                next_transcript.cancel()
                await asyncio.gather(next_transcript, return_exceptions=True)
            pending = self.final_merger.flush()
            if pending:
                await self._process_final(pending, session_id)
            if self._answer_task:
                await self._answer_task

    async def _record_audio_frames(self, frames):
        pcm = bytearray()
        start_ms: int | None = None
        channel = None
        sample_rate = 16_000
        async for frame in frames:
            if frame.pcm:
                if start_ms is None:
                    start_ms = frame.timestamp_ms
                    channel = frame.channel.value
                    sample_rate = frame.sample_rate
                pcm.extend(frame.pcm)
            if frame.utterance_end and pcm and start_ms is not None:
                end_ms = frame.timestamp_ms
                self._audio_segments.append({
                    "pcm": bytes(pcm),
                    "sample_rate": sample_rate,
                    "channel": channel,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                })
                pcm.clear()
                start_ms = None
                channel = None
            yield frame
