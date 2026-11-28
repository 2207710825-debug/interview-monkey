"""FastAPI SSE 网关。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import load_config
from ..contracts import Chunk
from ..history import RealtimeHistoryStore, valid_session_id
from ..asr.glossary import append_alias, record_correction, suggest_alias
from ..asr.hotwords import load_hotwords
from ..asr.postprocess import LocalLLMTranscriptCorrector
from ..asr.termbank import build_termbank
from ..llm.providers import OpenAICompatLLMProvider, UnavailableLLMProvider
from ..llm.service import RealtimeAnswerService
from ..latency import LATENCY_METRICS
from ..mocks import MockLLMProvider
from ..observability import QALogWriter
from ..rag.hybrid import HybridRetriever
from ..rag.ingest import document_category, ingest_directory
from ..rag.vault import VaultRetriever
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


def _app_config_path() -> Path:
    """桌面设置页写入的 config.json 路径。"""
    appdata = os.environ.get("APPDATA")
    return (
        Path(appdata) / "InterviewMonkey" / "config.json"
        if appdata
        else Path.home() / ".config" / "InterviewMonkey" / "config.json"
    )


def _app_llm_config() -> dict | None:
    """读取桌面 App 设置页选择的模型。

    返回 None 表示没有可用的 App 配置，继续使用 config/config.yaml。
    用户在设置页明确选择云端 provider（如 deepseek）视为同意把“问题文本”发送到
    云端；音频仍只在本机 ASR，不会上传。

    配置不完整时（例如选了 DeepSeek 却没保存 Key）**不再静默回落到
    config.yaml 的 Ollama**。否则状态栏会显示 ollama，真正的错误则被伪装成
    “Ollama 连不上”，用户完全无从排查。这里保留用户选择的 provider，并用
    ``configured=False`` 明确告诉界面缺什么。
    """
    path = _app_config_path()
    if not path.is_file():
        return None
    try:
        from engine.config_store import ConfigStore
        from engine import llm as legacy_llm

        view = ConfigStore(str(path)).as_dict()
    except Exception as exc:
        log.warning("app_llm_config_load_failed error=%s", type(exc).__name__)
        return None

    provider = str(view.get("provider") or "auto").strip().lower()
    key = str(view.get("api_key") or "").strip()
    if provider == "auto":
        provider = legacy_llm.detect_provider_by_key(key) or "ollama"
    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        return {
            "provider": "ollama",
            "base_url": base_url,
            "model": str(view.get("ollama_model") or "qwen2.5:3b"),
            "api_key": "",
            "trust_env": False,
            "cloud_text": False,
            "configured": True,
            "error": "",
            "source": "app",
        }

    preset = legacy_llm.PROVIDERS.get(provider)
    if preset is None:
        return {
            "provider": provider,
            "base_url": str(view.get("base_url") or "").strip(),
            "model": str(view.get("model") or "").strip(),
            "api_key": "",
            "trust_env": True,
            "cloud_text": True,
            "configured": False,
            "error": f"无法识别模型服务商 {provider!r}，请在设置中重新选择",
            "source": "app",
        }
    base_url = str(view.get("base_url") or "").strip() or preset["base_url"]
    model = str(view.get("model") or "").strip() or preset["model"]
    if not key:
        return {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key": "",
            "trust_env": True,
            "cloud_text": True,
            "configured": False,
            "error": (
                f"已选择 {preset['name']}（{model}），但还没有保存 API Key。"
                "请到「模型设置」填写 Key 并点保存。"
            ),
            "source": "app",
        }
    return {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key": key,
        "trust_env": True,
        "cloud_text": True,
        "configured": True,
        "error": "",
        "source": "app",
    }


def _notes_index_paths() -> list[Path]:
    """本机知识库索引文件：个人笔记 + 导入题库 + 微信/PDF/XMind 资料。

    这些文件使用相同的 JSONL schema；分开保存可以随时删除导入的资料，
    不会改动用户自己的 notes.jsonl。
    """
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    directory = base / "InterviewMonkey" / "knowledge"
    return [
        path for path in (
            directory / "notes.jsonl",
            directory / "nowcoder.jsonl",
            directory / "wechat_notes.jsonl",
            directory / "study_notes.jsonl",
        )
        if path.is_file()
    ]


def _load_notes_chunks() -> list[Chunk] | None:
    paths = _notes_index_paths()
    if not paths:
        return None
    chunks: list[Chunk] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    text = str(row.get("text") or "").strip()
                    if not text:
                        continue
                    chunks.append(Chunk(
                        id=str(row.get("id") or len(chunks)),
                        text=text,
                        source=str(row.get("source") or ""),
                        score=0.0,
                        section=row.get("section") or None,
                        metadata=row.get("metadata") or {},
                    ))
        except Exception as exc:
            log.warning(
                "notes_index_load_failed file=%s error=%s",
                path.name, type(exc).__name__,
            )
    return chunks or None


def _llm_metadata(cfg, app_llm: dict | None) -> dict:
    """状态栏/SSE 使用的模型元数据，明确标注配置是否完整。"""
    if app_llm is not None:
        return {
            "provider": str(app_llm.get("provider") or ""),
            "base_url": str(app_llm.get("base_url") or ""),
            "model": str(app_llm.get("model") or ""),
            "cloud_text": bool(app_llm.get("cloud_text", False)),
            "configured": bool(app_llm.get("configured", True)),
            "error": str(app_llm.get("error") or ""),
            "source": "app",
        }
    return {
        "provider": cfg.llm.provider,
        "base_url": cfg.llm.base_url,
        "model": cfg.llm.model,
        "cloud_text": cfg.llm.provider not in {"mock", "ollama", "local"},
        "configured": True,
        "error": "",
        "source": "yaml",
    }


def _build_llm_runtime(cfg, app_llm: dict | None, hotwords):
    """按 App 设置或 config.yaml 构建 LLM 与本地纠错器。"""
    transcript_corrector = None
    if app_llm is not None:
        if not app_llm.get("configured", True):
            message = str(app_llm.get("error") or "模型配置不完整，请到「模型设置」检查")
            return UnavailableLLMProvider(
                message,
                provider=str(app_llm.get("provider") or ""),
                model=str(app_llm.get("model") or ""),
            ), None
        llm = OpenAICompatLLMProvider(
            app_llm["base_url"],
            app_llm["model"],
            app_llm["api_key"],
            temperature=cfg.llm.temperature,
            timeout_seconds=cfg.llm.timeout_seconds,
            trust_env=bool(app_llm.get("trust_env", True)),
        )
    elif cfg.llm.provider == "mock":
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

    metadata = _llm_metadata(cfg, app_llm)
    endpoint_host = urlparse(metadata["base_url"]).hostname
    if (
        cfg.postprocess.llm_correction.enabled
        and metadata["provider"] in {"ollama", "local"}
        and endpoint_host in {"localhost", "127.0.0.1", "::1"}
    ):
        transcript_corrector = LocalLLMTranscriptCorrector(
            llm,
            hotwords,
            confidence_threshold=cfg.postprocess.llm_correction.confidence_threshold,
        )
    return llm, transcript_corrector


def _schedule_provider_close(app: FastAPI, provider) -> None:
    """替换 provider 后延迟释放旧连接，避免打断正在生成的回答。"""
    close = getattr(provider, "close", None)
    if not callable(close):
        return

    async def close_later():
        await asyncio.sleep(30)
        try:
            await close()
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = getattr(app.state, "event_loop", None)
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(close_later(), loop)
        return
    loop.create_task(close_later())


class LLMConfigManager:
    """在不重启应用的情况下切换实时问答使用的模型。

    背景：桌面主进程在 import ``realtime_server`` 时就创建了 FastAPI app。
    用户随后在「模型设置」保存 DeepSeek Key 时，旧实现只在下次启动读取配置，
    于是实时页继续显示并请求 config.yaml 里的本地 Ollama，报
    “All connection attempts failed”。这里同时做两件事：

    1. 监测 config.json / config.yaml 的指纹，在 HTTP 请求前自动重新加载；
    2. 提供 ``reload()`` 给主进程的保存按钮立即调用。

    只替换 LLM provider 和纠错器，保留检索器和会话上下文；正在播放的音频
    管线持有同一个 service 对象，因此也会立即用上新模型。
    """

    def __init__(self, app: FastAPI, service, cfg, config_path: str | Path | None = None):
        self.app = app
        self.service = service
        self._cfg = cfg
        self._config_path = Path(config_path or PROJECT_ROOT / "config" / "config.yaml")
        self._lock = threading.RLock()
        self.last_error = ""
        self._fingerprint = self._fingerprint_now()

    def _fingerprint_now(self) -> tuple:
        parts = []
        for path in (_app_config_path(), self._config_path):
            try:
                stat = path.stat()
                parts.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                parts.append((str(path), None, None))
        return tuple(parts)

    def snapshot(self) -> dict:
        meta = getattr(self.service, "app_llm", {}) or {}
        return {
            "provider": meta.get("provider", ""),
            "model": meta.get("model", ""),
            "configured": bool(meta.get("configured", True)),
            "error": meta.get("error") or self.last_error,
            "source": meta.get("source", ""),
        }

    def reload(self, *, force: bool = False) -> dict:
        with self._lock:
            fingerprint = self._fingerprint_now()
            if not force and fingerprint == self._fingerprint:
                return {"ok": True, "changed": False, **self.snapshot()}
            self._fingerprint = fingerprint
            try:
                cfg = load_config(self._config_path)
                app_llm = _app_llm_config()
                # 与启动路径一致：明确选择 App 内云端模型时视为用户同意文本出网。
                if app_llm is None:
                    cfg.assert_cloud_allowed()
                llm, corrector = _build_llm_runtime(
                    cfg, app_llm, getattr(self.service, "hotwords", ()),
                )
                metadata = _llm_metadata(cfg, app_llm)
                old_provider = getattr(self.service, "llm_provider", None)
                replace = getattr(self.service, "replace_llm", None)
                if callable(replace):
                    replace(llm, transcript_corrector=corrector, app_llm=metadata)
                else:  # 兼容第三方/测试用的轻量 service
                    self.service.llm_provider = llm
                    self.service.transcript_corrector = corrector
                    self.service.app_llm = metadata
                self._cfg = cfg
                self.last_error = ""
                if old_provider is not llm:
                    _schedule_provider_close(self.app, old_provider)
                log.info(
                    "llm_config_reloaded provider=%s model=%s configured=%s",
                    metadata["provider"], metadata["model"], metadata["configured"],
                )
                return {"ok": True, "changed": True, **self.snapshot()}
            except Exception as exc:
                self.last_error = f"模型配置加载失败：{exc}"
                log.warning(
                    "llm_config_reload_failed error=%s detail=%s",
                    type(exc).__name__, exc,
                )
                return {
                    "ok": False,
                    "changed": False,
                    **self.snapshot(),
                    "error": self.last_error,
                }


def build_default_service(
    config_path: str | Path | None = None,
    *,
    use_app_config: bool = True,
) -> RealtimeAnswerService:
    cfg_path = Path(config_path or PROJECT_ROOT / "config" / "config.yaml")
    cfg = load_config(cfg_path)
    app_llm = _app_llm_config() if use_app_config else None
    # App 设置页明确选择云端 provider 时，允许文本出网；音频仍只在本机 ASR。
    if app_llm is None:
        cfg.assert_cloud_allowed()
    notes_chunks = _load_notes_chunks() if use_app_config else None
    if notes_chunks:
        # 个人笔记库优先；音频仍只在本机 ASR，问题文本才可能发给云端 LLM。
        retriever = VaultRetriever(notes_chunks)
        source_chunks = notes_chunks
    else:
        knowledge = Path(cfg.rag.knowledge_path)
        if not knowledge.is_absolute():
            knowledge = PROJECT_ROOT / knowledge
        chunks = ingest_directory(knowledge, cfg.rag.chunk_tokens, cfg.rag.overlap_ratio)
        retriever = HybridRetriever(chunks)
        source_chunks = chunks
    glossary = Path(cfg.asr.hotwords_path)
    if not glossary.is_absolute():
        glossary = PROJECT_ROOT / glossary
    manual_hotwords = load_hotwords(glossary)
    termbank = None
    try:
        # 从个人笔记/知识库抽取英文技术术语，与人工词库合并后交给
        # 流式解码热词、文本纠错和问答检索共同使用。
        termbank = build_termbank(source_chunks, manual_hotwords)
        hotwords = list(termbank.hotwords)
    except Exception as exc:
        log.warning("termbank_build_failed error=%s", type(exc).__name__)
        hotwords = list(manual_hotwords)
    llm, transcript_corrector = _build_llm_runtime(cfg, app_llm, hotwords)
    service = RealtimeAnswerService(
        retriever,
        llm,
        PROJECT_ROOT / "prompts" / "answer_system.md",
        top_k=6 if notes_chunks else cfg.rag.top_k,
        min_score=cfg.rag.min_score,
        token_budget=cfg.rag.token_budget,
        transcript_corrector=transcript_corrector,
        correction_timeout_ms=cfg.postprocess.llm_correction.timeout_ms,
        hotwords=hotwords,
        generic_product_skip_score=cfg.rag.generic_product_skip_score,
    )
    service.app_llm = _llm_metadata(cfg, app_llm)
    service.notes_chunks = len(notes_chunks or [])
    service.extra_hotwords = tuple(termbank.hotwords) if termbank else ()
    service.asr_terms = termbank.decoder_count if termbank else 0
    service.termbank_unique = termbank.unique_terms if termbank else 0
    return service


def create_app(
    service: RealtimeAnswerService | None = None,
    runtime: CaptureRuntime | None = None,
    glossary_path: str | Path | None = None,
    *,
    mock: bool = False,
    mock_settings: MockSettings | None = None,
    use_app_config: bool = True,
    history_store: RealtimeHistoryStore | None = None,
) -> FastAPI:
    app = FastAPI(title="面试猴实时语音问答核心", version="2.0.0")
    cfg = load_config(PROJECT_ROOT / "config" / "config.yaml")
    app.state.mock_mode = mock
    app.state.history_store = history_store
    app.state.event_loop = None
    app.state.llm_config_manager = None
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
        owns_service = service is None and runtime is None and use_app_config
        app.state.answer_service = service or build_default_service(
            use_app_config=use_app_config,
        )
        app.state.qa_logger = QALogWriter(record_audio=cfg.observability.record_audio_for_replay)
        app.state.history_store = history_store or RealtimeHistoryStore()
        app.state.capture_runtime = runtime or CaptureRuntime(
            cfg,
            app.state.answer_service,
            PROJECT_ROOT,
            qa_logger=app.state.qa_logger,
            transcript_logger=app.state.history_store,
            extra_hotwords=getattr(app.state.answer_service, "extra_hotwords", ()),
        )
        if owns_service:
            app.state.llm_config_manager = LLMConfigManager(
                app, app.state.answer_service, cfg,
            )
    configured_glossary = Path(glossary_path or cfg.asr.hotwords_path)
    if not configured_glossary.is_absolute():
        configured_glossary = PROJECT_ROOT / configured_glossary
    app.state.glossary_path = configured_glossary
    app.state.glossary_corrections_path = configured_glossary.with_name("glossary.corrections.jsonl")

    def reload_llm_config(*, force: bool = True) -> dict:
        """供桌面主进程在设置保存后立即调用（可跨线程）。"""
        manager = app.state.llm_config_manager
        if manager is None:
            return {
                "ok": False,
                "changed": False,
                "error": "当前实时服务使用外部注入的模型实例，未启用动态配置",
            }
        result = manager.reload(force=force)
        # 采集运行时持有同一个 service；显式赋值避免自定义运行时遗漏。
        if getattr(app.state, "capture_runtime", None) is not None:
            app.state.capture_runtime.answer_service = app.state.answer_service
        return result

    app.state.reload_llm_config = reload_llm_config

    @app.middleware("http")
    async def _reload_llm_config_before_request(request: Request, call_next):
        # 保存按钮走 js_api 线程时需要一个事件循环来关闭旧 provider；这里记录
        # 首个 HTTP 请求所在循环。没有请求时旧 provider 尚未创建连接，无需关闭。
        app.state.event_loop = asyncio.get_running_loop()
        manager = app.state.llm_config_manager
        if manager is not None:
            result = manager.reload()
            if not result.get("ok"):
                log.warning(
                    "llm_config_reload_failed_on_request error=%s",
                    result.get("error", ""),
                )
        return await call_next(request)

    @app.get("/")
    async def index():
        return FileResponse(PROJECT_ROOT / "web" / "realtime.html")

    @app.get("/api/status")
    async def status():
        llm_meta = getattr(app.state.answer_service, "app_llm", {}) or {}
        manager = app.state.llm_config_manager
        notes_count = int(getattr(app.state.answer_service, "notes_chunks", 0) or 0)
        return {
            "ok": True,
            "transport": "sse",
            "local_default": cfg.privacy.local_mode,
            "llm_provider": llm_meta.get("provider", cfg.llm.provider),
            "llm_model": llm_meta.get("model", cfg.llm.model),
            "cloud_text": bool(llm_meta.get("cloud_text", False)),
            "llm_configured": bool(llm_meta.get("configured", True)),
            "llm_config_error": str(
                llm_meta.get("error") or (getattr(manager, "last_error", "") if manager else "")
            ),
            "llm_config_source": llm_meta.get("source", ""),
            "notes_mode": notes_count > 0,
            "notes_chunks": notes_count,
            "asr_terms": int(getattr(app.state.answer_service, "asr_terms", 0) or 0),
            "latency_histograms": LATENCY_METRICS.snapshot(),
            **app.state.capture_runtime.status(),
        }

    @app.get("/api/sessions")
    async def sessions(limit: int = 50):
        store = app.state.history_store
        if store is None:
            return {"items": []}
        return {"items": await asyncio.to_thread(store.list_sessions, limit)}

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str):
        store = app.state.history_store
        if store is None:
            raise HTTPException(404, "当前模式未启用本机会话历史")
        if not valid_session_id(session_id):
            raise HTTPException(400, "session_id 格式不合法")
        detail = await asyncio.to_thread(store.get_session, session_id)
        if detail is None:
            raise HTTPException(404, "会话不存在")
        return detail

    @app.delete("/api/sessions/{session_id}")
    async def session_delete(session_id: str):
        store = app.state.history_store
        if store is None:
            raise HTTPException(404, "当前模式未启用本机会话历史")
        try:
            removed = await asyncio.to_thread(store.delete_session, session_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "removed": removed}

    @app.post("/api/answer/cancel")
    async def answer_cancel(body: ContextRequest):
        await app.state.answer_service.cancel(body.session_id)
        return {"ok": True}

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
                    payload = dict(event.data)
                    payload.setdefault("trace_id", event.trace_id)
                    yield _sse(event.event, payload)
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
                        "utterance_id": None,
                        "question_start_ms": None,
                        "question_end_ms": None,
                        "raw_text": body.question,
                        "corrected_text": body.question,
                        "corrections": [],
                        "asr_engine": None,
                        "pass1_text": None,
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
