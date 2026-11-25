"""把真实音频源接入异步闭环，供 FastAPI 控制与推送状态。"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from ..asr.hotwords import load_hotwords
from ..asr.postprocess import PostprocessingASRProvider, TranscriptPostprocessor
from ..asr.providers import JsonWebSocketASRProvider, LocalSherpaASRProvider
from ..asr.termbank import merge_hotwords
from ..capture.sources import PortAudioInputSource, WindowsLoopbackAudioSource
from ..config import RealtimeConfig
from ..contracts import Channel
from ..events import AsyncEventBus
from ..orchestrator import RealtimePipeline
from ..qa.detector import ChannelSpeakerGate, RuleQuestionDetector
from ..vad.adaptive import AdaptiveVadSegmenter


class CaptureRuntime:
    def __init__(
        self,
        cfg: RealtimeConfig,
        answer_service,
        project_root: str | Path,
        qa_logger=None,
        transcript_logger=None,
        extra_hotwords: tuple = (),
    ):
        self.cfg = cfg
        self.answer_service = answer_service
        self.qa_logger = qa_logger
        self.transcript_logger = transcript_logger
        self.extra_hotwords = tuple(extra_hotwords)
        self.project_root = Path(project_root)
        self.bus = AsyncEventBus(queue_size=256)
        self.tasks: list[asyncio.Task] = []
        self.sources: list[object] = []
        self.last_audio_stats: list[dict[str, object]] = []
        self._asr_cache: dict[Channel, object] = {}
        self.state = "stopped"
        self.last_error = ""
        self.session_id = ""
        self.started_at = ""

    def status(self) -> dict[str, object]:
        audio_stats = []
        for source in self.sources:
            read_stats = getattr(source, "stats", None)
            if read_stats:
                audio_stats.append(read_stats())
        if not audio_stats:
            audio_stats = list(self.last_audio_stats)
        return {
            "capture": self.state,
            "asr": (
                "connected" if self.state == "capturing"
                else "loading" if self.state == "starting"
                else "idle"
            ),
            "active_sources": len(self.sources),
            "audio_stats": audio_stats,
            "error": self.last_error,
            "session_id": self.session_id,
            "capture_started_at": self.started_at,
        }

    def _hotwords(self):
        path = Path(self.cfg.asr.hotwords_path)
        if not path.is_absolute():
            path = self.project_root / path
        manual = load_hotwords(path) if path.is_file() else []
        return list(merge_hotwords(manual, self.extra_hotwords))

    def reload_hotwords(self) -> int:
        """人工确认 alias 后立即刷新已加载 provider 的文本纠偏表。"""
        hotwords = self._hotwords()
        for provider in self._asr_cache.values():
            update = getattr(provider, "update_hotwords", None)
            if update:
                update(hotwords)
        return len(hotwords)

    def _pipeline(self, source, question_channel: Channel = Channel.LOOPBACK):
        vad_cfg = self.cfg.vad
        vad = AdaptiveVadSegmenter(
            energy_threshold=vad_cfg.energy_threshold,
            base_silence_ms=vad_cfg.base_silence_ms,
            fast_silence_ms=vad_cfg.fast_silence_ms,
            incomplete_silence_ms=vad_cfg.incomplete_silence_ms,
            pre_roll_ms=vad_cfg.pre_roll_ms,
            post_roll_ms=vad_cfg.post_roll_ms,
            max_utterance_ms=vad_cfg.max_utterance_ms,
        )
        hotwords = self._hotwords()
        if self.cfg.asr.provider == "local_sherpa":
            channel = getattr(source, "channel", Channel.LOOPBACK)
            asr = self._asr_cache.get(channel)
            if asr is None:
                provider = LocalSherpaASRProvider(
                    hotwords=hotwords,
                    hotword_boost=self.cfg.asr.hotword_boost,
                    two_pass_enabled=self.cfg.asr.two_pass.enabled,
                    two_pass_engine=self.cfg.asr.two_pass.engine,
                    second_pass_timeout_ms=self.cfg.asr.two_pass.timeout_ms,
                )
                processor = TranscriptPostprocessor(
                    hotwords,
                    fuzzy_threshold=self.cfg.postprocess.fuzzy_threshold,
                    suspicious_threshold=self.cfg.postprocess.suspicious_threshold,
                    fragment_min_chars=self.cfg.postprocess.fragment_min_chars,
                )
                asr = PostprocessingASRProvider(provider, processor)
                self._asr_cache[channel] = asr
        elif self.cfg.asr.provider.startswith("ws_"):
            token = os.environ.get("MH_ASR_TOKEN", "")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            asr = JsonWebSocketASRProvider(
                self.cfg.asr.websocket_url,
                language=self.cfg.asr.language,
                hotwords=hotwords,
                headers=headers,
                reconnect_attempts=self.cfg.asr.reconnect_attempts,
                heartbeat_seconds=self.cfg.asr.heartbeat_seconds,
            )
            asr = PostprocessingASRProvider(asr, TranscriptPostprocessor(
                hotwords,
                fuzzy_threshold=self.cfg.postprocess.fuzzy_threshold,
                suspicious_threshold=self.cfg.postprocess.suspicious_threshold,
                fragment_min_chars=self.cfg.postprocess.fragment_min_chars,
            ))
        else:
            raise ValueError(f"未知 ASR provider: {self.cfg.asr.provider}")
        detector = RuleQuestionDetector(ChannelSpeakerGate(question_channel=question_channel))
        return RealtimePipeline(
            vad, asr, detector, self.answer_service, self.bus,
            qa_logger=self.qa_logger,
            transcript_logger=self.transcript_logger,
        )

    async def start(self, mode: str = "loopback", device=None, session_id: str = "default") -> dict[str, object]:
        if self.tasks:
            return {"ok": True, **self.status()}
        if mode not in {"loopback", "mic", "mic_question", "both"}:
            raise ValueError("mode 必须是 loopback、mic、mic_question 或 both")
        self.cfg.assert_cloud_allowed()
        self.state = "starting"
        self.last_error = ""
        self.last_audio_stats = []
        self.session_id = str(session_id or "default")
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            if mode in {"loopback", "both"}:
                if os.name != "nt":
                    raise RuntimeError("系统环回在非 Windows 上请改选 monitor/虚拟声卡输入")
                self.sources.append(WindowsLoopbackAudioSource(
                    device_index=int(device) if device not in {None, ""} else None,
                    queue_frames=self.cfg.audio.ring_frames,
                    agc_enabled=self.cfg.audio.loopback_agc_enabled,
                    target_rms_dbfs=self.cfg.audio.target_rms_dbfs,
                    limiter_dbfs=self.cfg.audio.limiter_dbfs,
                    max_gain_db=self.cfg.audio.max_gain_db,
                ))
            if mode in {"mic", "mic_question", "both"}:
                self.sources.append(PortAudioInputSource(
                    Channel.MIC,
                    device=device if mode in {"mic", "mic_question"} and device not in {None, ""} else None,
                    frame_ms=self.cfg.audio.frame_ms,
                    queue_frames=self.cfg.audio.ring_frames,
                    agc_enabled=self.cfg.audio.mic_agc_enabled,
                    target_rms_dbfs=self.cfg.audio.target_rms_dbfs,
                    limiter_dbfs=self.cfg.audio.limiter_dbfs,
                    max_gain_db=self.cfg.audio.max_gain_db,
                ))
            question_channel = Channel.MIC if mode == "mic_question" else Channel.LOOPBACK
            pipelines = [self._pipeline(source, question_channel) for source in self.sources]
            await self.bus.publish("ui", {"type": "status", "payload": self.status()})
            for pipeline in pipelines:
                warm = getattr(pipeline.asr, "_ensure_models", None)
                if warm:
                    await warm()
            self.tasks = [
                asyncio.create_task(pipeline.run(source, session_id), name=f"capture:{index}")
                for index, (pipeline, source) in enumerate(zip(pipelines, self.sources))
            ]
            self.state = "capturing"
            await self.bus.publish("ui", {"type": "status", "payload": self.status()})
            for task in self.tasks:
                task.add_done_callback(self._task_done)
            return {"ok": True, **self.status()}
        except Exception as exc:
            self.state = "error"
            self.last_error = str(exc)
            await self.stop()
            return {"ok": False, **self.status(), "error": str(exc)}

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error:
            self.state = "error"
            self.last_error = str(error)
            asyncio.create_task(self.bus.publish("ui", {"type": "status", "payload": self.status()}))

    async def stop(self) -> dict[str, object]:
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        sources, self.sources = self.sources, []
        self.last_audio_stats = [
            read_stats()
            for source in sources
            if (read_stats := getattr(source, "stats", None))
        ]
        for source in sources:
            close = getattr(source, "close", None)
            if close:
                await close()
        if self.state != "error":
            self.state = "stopped"
        await self.bus.publish("ui", {"type": "status", "payload": self.status()})
        return {"ok": True, **self.status()}
