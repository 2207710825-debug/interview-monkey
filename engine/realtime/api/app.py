"""FastAPI SSE 网关。"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import load_config
from ..asr.glossary import append_alias, record_correction, suggest_alias
from ..asr.hotwords import load_hotwords
from ..asr.postprocess import LocalLLMTranscriptCorrector
from ..llm.providers import OpenAICompatLLMProvider
from ..llm.service import RealtimeAnswerService
from ..latency import LATENCY_METRICS
from ..mocks import MockLLMProvider
from ..observability import QALogWriter
from ..rag.hybrid import HybridRetriever
from ..rag.ingest import document_category, ingest_directory
from ..mock_backend import FixtureAnswerService, FixtureCaptureRuntime, MockProtocolServer, MockSettings, load_fixtures
from .runtime import CaptureRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[3]
log = logging.getLogger("interview_monkey.realtime.qa")


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="default", min_length=1, max_length=128)
    recent: list[str] = Field(default_factory=list, max_length=20)


class ContextRequest(BaseModel):
    session_id: str = Field(default="default", min_length=1, max_length=128)


class CaptureRequest(BaseModel):
    mode: str = Field(default="loopback", pattern="^(loopback|mic|mic_question|both)$")
    device: int | str | None = None
    session_id: str = Field(default="default", min_length=1, max_length=128)


class KnowledgeRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class TranscriptCorrectionRequest(BaseModel):
    raw: str = Field(min_length=1, max_length=4000)
    corrected: str = Field(min_length=1, max_length=4000)


class GlossaryAliasRequest(BaseModel):
    term: str = Field(min_length=1, max_length=100)
    alias: str = Field(min_length=1, max_length=100)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _plain(value):
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def build_default_service(config_path: str | Path | None = None) -> RealtimeAnswerService:
    cfg_path = Path(config_path or PROJECT_ROOT / "config" / "config.yaml")
    cfg = load_config(cfg_path)
    cfg.assert_cloud_allowed()
    knowledge = Path(cfg.rag.knowledge_path)
    if not knowledge.is_absolute():
        knowledge = PROJECT_ROOT / knowledge
    chunks = ingest_directory(knowledge, cfg.rag.chunk_tokens, cfg.rag.overlap_ratio)
    retriever = HybridRetriever(chunks)
    glossary = Path(cfg.asr.hotwords_path)
    if not glossary.is_absolute():
        glossary = PROJECT_ROOT / glossary
    hotwords = load_hotwords(glossary)
    if cfg.llm.provider == "mock":
        llm = MockLLMProvider([
            "**结论**：离线 Mock 闭环已运行。\n",
            "**要点**：\n- 使用 **异步队列** 解耦\n- 通过 **RRF** 融合检索\n",
            "- 使用 **SSE** 流式输出\n**关键词**：**异步队列** / **RRF** / **SSE**",
        ])
    else:
        llm = OpenAICompatLLMProvider(
            cfg.llm.base_url,
            cfg.llm.model,
            cfg.llm_api_key,
            temperature=cfg.llm.temperature,
            timeout_seconds=cfg.llm.timeout_seconds,
        )
    transcript_corrector = None
    endpoint_host = urlparse(cfg.llm.base_url).hostname
    if (
        cfg.postprocess.llm_correction.enabled
        and cfg.llm.provider in {"ollama", "local"}
        and endpoint_host in {"localhost", "127.0.0.1", "::1"}
    ):
        transcript_corrector = LocalLLMTranscriptCorrector(
            llm,
            hotwords,
            confidence_threshold=cfg.postprocess.llm_correction.confidence_threshold,
        )
    return RealtimeAnswerService(
        retriever,
        llm,
        PROJECT_ROOT / "prompts" / "answer_system.md",
        top_k=cfg.rag.top_k,
        min_score=cfg.rag.min_score,
        token_budget=cfg.rag.token_budget,
        transcript_corrector=transcript_corrector,
        correction_timeout_ms=cfg.postprocess.llm_correction.timeout_ms,
        hotwords=hotwords,
        generic_product_skip_score=cfg.rag.generic_product_skip_score,
    )


def create_app(
    service: RealtimeAnswerService | None = None,
    runtime: CaptureRuntime | None = None,
    glossary_path: str | Path | None = None,
    *,
    mock: bool = False,
    mock_settings: MockSettings | None = None,
) -> FastAPI:
    app = FastAPI(title="面试猴实时语音问答核心", version="2.0.0")
    cfg = load_config(PROJECT_ROOT / "config" / "config.yaml")
    app.state.mock_mode = mock
    if mock:
        fixtures = load_fixtures(PROJECT_ROOT)
        settings = mock_settings or MockSettings.from_env()
        app.state.answer_service = FixtureAnswerService(fixtures, settings.token_delay_ms / 1000)
        app.state.qa_logger = QALogWriter(record_audio=False)
        app.state.capture_runtime = runtime or FixtureCaptureRuntime(fixtures, app.state.answer_service)
        app.state.mock_protocol_server = MockProtocolServer(
            fixtures, settings, PROJECT_ROOT / "logs" / "mock_protocol.jsonl",
        )
    else:
        app.state.answer_service = service or build_default_service()
        app.state.qa_logger = QALogWriter(record_audio=cfg.observability.record_audio_for_replay)
        app.state.capture_runtime = runtime or CaptureRuntime(
            cfg,
            app.state.answer_service,
            PROJECT_ROOT,
            qa_logger=app.state.qa_logger,
        )
    configured_glossary = Path(glossary_path or cfg.asr.hotwords_path)
    if not configured_glossary.is_absolute():
        configured_glossary = PROJECT_ROOT / configured_glossary
    app.state.glossary_path = configured_glossary
    app.state.glossary_corrections_path = configured_glossary.with_name("glossary.corrections.jsonl")

    @app.get("/")
    async def index():
        return FileResponse(PROJECT_ROOT / "web" / "realtime.html")

    @app.get("/api/status")
    async def status():
        return {
            "ok": True,
            "transport": "sse",
            "local_default": cfg.privacy.local_mode,
            "latency_histograms": LATENCY_METRICS.snapshot(),
            **app.state.capture_runtime.status(),
        }

    @app.post("/api/capture/start")
    async def capture_start(body: CaptureRequest):
        return await app.state.capture_runtime.start(body.mode, body.device, body.session_id)

    @app.post("/api/capture/stop")
    async def capture_stop():
        return await app.state.capture_runtime.stop()

    @app.get("/api/events")
    async def events(request: Request):
        async def generate() -> AsyncIterator[str]:
            async for item in app.state.capture_runtime.bus.subscribe("ui"):
                if await request.is_disconnected():
                    return
                payload = _plain(item)
                yield _sse(payload.get("type", "message"), payload.get("payload", {}))

        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/context/clear")
    async def clear_context(body: ContextRequest):
        app.state.answer_service.clear_context(body.session_id)
        return {"ok": True}

    @app.post("/api/glossary/corrections")
    async def glossary_correction(body: TranscriptCorrectionRequest):
        try:
            item = await asyncio.to_thread(
                record_correction,
                app.state.glossary_corrections_path,
                body.raw,
                body.corrected,
            )
            hotwords = await asyncio.to_thread(load_hotwords, app.state.glossary_path)
            suggestion = suggest_alias(body.raw, body.corrected, hotwords)
            return {"ok": True, "id": item["id"], "alias_suggestion": suggestion}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/glossary/aliases")
    async def glossary_alias(body: GlossaryAliasRequest):
        try:
            added = await asyncio.to_thread(
                append_alias, app.state.glossary_path, body.term, body.alias,
            )
            count = app.state.capture_runtime.reload_hotwords()
            return {"ok": True, "added": added, "term": body.term, "entries": count}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/knowledge")
    async def knowledge_list():
        if mock:
            return {"items": [{"name": "本地 Mock 知识样例", "path": "fixture:mock", "category": "mock"}]}
        root = (PROJECT_ROOT / cfg.rag.knowledge_path).resolve()
        files = [
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".pdf"}
        ] if root.is_dir() else []
        categories = sorted({document_category(path, root) for path in files})
        return {
            "items": [
                {
                    "name": "全部知识库",
                    "path": str(root.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                    "category": None,
                },
                *[
                    {"name": category, "path": f"category:{category}", "category": category}
                    for category in categories
                ],
            ] if files else []
        }

    @app.post("/api/knowledge/switch")
    async def knowledge_switch(body: KnowledgeRequest):
        if mock:
            if body.path != "fixture:mock":
                raise HTTPException(404, "Mock 知识样例不存在")
            return {"ok": True, "path": body.path, "category": "mock", "chunks": 1}
        knowledge_root = (PROJECT_ROOT / cfg.rag.knowledge_path).resolve()
        selected_category = None
        if body.path.startswith("category:"):
            selected_category = body.path.removeprefix("category:").strip()
            available = {
                document_category(path, knowledge_root)
                for path in knowledge_root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".md", ".txt", ".pdf"}
            }
            if not selected_category or selected_category not in available:
                raise HTTPException(404, "知识库类别不存在")
            candidate = knowledge_root
            chunks = ingest_directory(
                candidate,
                cfg.rag.chunk_tokens,
                cfg.rag.overlap_ratio,
                category=selected_category,
            )
        else:
            candidate = Path(body.path)
            if not candidate.is_absolute():
                candidate = PROJECT_ROOT / candidate
            candidate = candidate.resolve()
            try:
                candidate.relative_to(PROJECT_ROOT)
            except ValueError as exc:
                raise HTTPException(400, "知识库路径必须位于项目目录内") from exc
            if not candidate.is_dir():
                raise HTTPException(404, "知识库目录不存在")
            chunks = ingest_directory(candidate, cfg.rag.chunk_tokens, cfg.rag.overlap_ratio)
        app.state.answer_service.retriever = HybridRetriever(chunks)
        return {
            "ok": True,
            "path": body.path,
            "category": selected_category,
            "chunks": len(chunks),
        }

    @app.post("/api/answer/stream")
    async def answer_stream(body: AnswerRequest, request: Request):
        async def generate() -> AsyncIterator[str]:
            metadata = {}
            latency_metrics = {}
            answer_parts = []
            status = "error"
            error = None
            try:
                async for event in app.state.answer_service.stream_answer(
                    body.question,
                    session_id=body.session_id,
                    recent=body.recent,
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
                    if await request.is_disconnected():
                        status = "interrupted"
                        await app.state.answer_service.cancel(body.session_id)
                        return
                    yield _sse(event.event, dict(event.data))
            except Exception as exc:
                status = "error"
                error = str(exc)
                raise
            finally:
                try:
                    app.state.qa_logger.append({
                        "trace_id": metadata.get("trace_id"),
                        "session_id": body.session_id,
                        "source": "text",
                        "raw_text": body.question,
                        "corrected_text": body.question,
                        "corrections": [],
                        "asr_engine": None,
                        "pass1_ms": None,
                        "pass2_ms": None,
                        "retrieval_top_k": metadata.get("sources", []),
                        "retrieval_queries": metadata.get("retrieval_queries", []),
                        "retrieval_low_confidence": metadata.get("low_confidence"),
                        "product_filter_applied": metadata.get("product_filter_applied"),
                        "question": metadata.get("question", body.question),
                        "llm_correction": metadata.get("llm_correction"),
                        "answer": "".join(answer_parts),
                        "status": status,
                        "error": error,
                        "latency_metrics": latency_metrics,
                        "audio_paths": [],
                    })
                except Exception as exc:
                    log.warning("qa_log_write_failed error=%s", type(exc).__name__)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if mock:
        @app.websocket("/v1/realtime")
        async def realtime_v1(websocket: WebSocket):
            await app.state.mock_protocol_server.handle(websocket)

    return app
