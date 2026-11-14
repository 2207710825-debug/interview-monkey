"""config.yaml + 环境变量配置加载。密钥只从环境变量读取。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AudioConfig:
    sample_rate: int = 16_000
    frame_ms: int = 20
    ring_frames: int = 250
    mic_agc_enabled: bool = True
    loopback_agc_enabled: bool = False
    target_rms_dbfs: float = -20.0
    limiter_dbfs: float = -1.0
    max_gain_db: float = 12.0


@dataclass(frozen=True, slots=True)
class VadConfig:
    base_silence_ms: int = 500
    fast_silence_ms: int = 300
    incomplete_silence_ms: int = 700
    pre_roll_ms: int = 200
    post_roll_ms: int = 150
    max_utterance_ms: int = 15_000
    energy_threshold: int = 450


@dataclass(frozen=True, slots=True)
class TwoPassConfig:
    enabled: bool = True
    engine: str = "sense_voice"
    timeout_ms: int = 2500


@dataclass(frozen=True, slots=True)
class AsrConfig:
    provider: str = "local_sherpa"
    engine: str = "sherpa_transducer"
    websocket_url: str = ""
    language: str = "zh-CN"
    hotwords_path: str = "config/glossary.yaml"
    hotword_boost: float = 1.5
    two_pass: TwoPassConfig = field(default_factory=TwoPassConfig)
    reconnect_attempts: int = 5
    heartbeat_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class RagConfig:
    knowledge_path: str = "engine/knowledge"
    top_k: int = 4
    min_score: float = 0.35
    generic_product_skip_score: float = 0.75
    chunk_tokens: int = 400
    overlap_ratio: float = 0.15
    token_budget: int = 6000


@dataclass(frozen=True, slots=True)
class LlmConfig:
    provider: str = "mock"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen2.5:7b"
    api_key_env: str = "MH_LLM_API_KEY"
    temperature: float = 0.2
    timeout_seconds: float = 45.0


@dataclass(frozen=True, slots=True)
class LlmCorrectionConfig:
    enabled: bool = True
    timeout_ms: int = 300
    confidence_threshold: float = 0.7


@dataclass(frozen=True, slots=True)
class PostprocessConfig:
    fuzzy_threshold: float = 0.84
    suspicious_threshold: float = 0.68
    fragment_min_chars: int = 4
    llm_correction: LlmCorrectionConfig = field(default_factory=LlmCorrectionConfig)


@dataclass(frozen=True, slots=True)
class PrivacyConfig:
    local_mode: bool = True
    third_party_consent: bool = False


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    record_audio_for_replay: bool = False


@dataclass(frozen=True, slots=True)
class RealtimeConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    @property
    def llm_api_key(self) -> str:
        return os.environ.get(self.llm.api_key_env, "")

    def assert_cloud_allowed(self) -> None:
        cloud = self.asr.provider.startswith("ws_") or self.llm.provider not in {"mock", "ollama", "local"}
        if cloud and (self.privacy.local_mode or not self.privacy.third_party_consent):
            raise PermissionError("第三方服务未获用户确认；请关闭仅本地模式并明确同意后再启用")


def _section(cls: type, raw: Mapping[str, Any] | None):
    raw = dict(raw or {})
    allowed = cls.__dataclass_fields__
    return cls(**{key: value for key, value in raw.items() if key in allowed})


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: str | os.PathLike[str], environ: Mapping[str, str] | None = None) -> RealtimeConfig:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("缺少 PyYAML，请执行 pip install -r requirements-realtime.txt") from exc

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 顶层必须是对象")

    env = dict(os.environ if environ is None else environ)
    privacy = dict(raw.get("privacy") or {})
    llm = dict(raw.get("llm") or {})
    asr = dict(raw.get("asr") or {})
    postprocess = dict(raw.get("postprocess") or {})
    if "MH_LOCAL_MODE" in env:
        privacy["local_mode"] = _env_bool(env["MH_LOCAL_MODE"])
    if "MH_THIRD_PARTY_CONSENT" in env:
        privacy["third_party_consent"] = _env_bool(env["MH_THIRD_PARTY_CONSENT"])
    if env.get("MH_LLM_BASE_URL"):
        llm["base_url"] = env["MH_LLM_BASE_URL"]
    if env.get("MH_LLM_MODEL"):
        llm["model"] = env["MH_LLM_MODEL"]
    if env.get("MH_ASR_WEBSOCKET_URL"):
        asr["websocket_url"] = env["MH_ASR_WEBSOCKET_URL"]

    asr["two_pass"] = _section(TwoPassConfig, asr.get("two_pass"))
    postprocess["llm_correction"] = _section(
        LlmCorrectionConfig, postprocess.get("llm_correction"),
    )
    cfg = RealtimeConfig(
        audio=_section(AudioConfig, raw.get("audio")),
        vad=_section(VadConfig, raw.get("vad")),
        asr=_section(AsrConfig, asr),
        rag=_section(RagConfig, raw.get("rag")),
        llm=_section(LlmConfig, llm),
        postprocess=_section(PostprocessConfig, postprocess),
        privacy=_section(PrivacyConfig, privacy),
        observability=_section(ObservabilityConfig, raw.get("observability")),
    )
    if cfg.audio.frame_ms not in {20, 30, 40}:
        raise ValueError("audio.frame_ms 必须为 20、30 或 40")
    if not -30.0 <= cfg.audio.target_rms_dbfs <= -12.0:
        raise ValueError("audio.target_rms_dbfs 必须在 -30~-12 dBFS")
    if not -6.0 <= cfg.audio.limiter_dbfs <= -0.1:
        raise ValueError("audio.limiter_dbfs 必须在 -6~-0.1 dBFS")
    if not 0.0 <= cfg.audio.max_gain_db <= 24.0:
        raise ValueError("audio.max_gain_db 必须在 0~24 dB")
    silence_values = (
        cfg.vad.fast_silence_ms,
        cfg.vad.base_silence_ms,
        cfg.vad.incomplete_silence_ms,
    )
    if any(not 300 <= value <= 700 for value in silence_values):
        raise ValueError("vad 静音阈值必须在 300~700ms")
    if not cfg.vad.fast_silence_ms <= cfg.vad.base_silence_ms <= cfg.vad.incomplete_silence_ms:
        raise ValueError("vad 静音阈值必须满足 fast <= base <= incomplete")
    if cfg.vad.pre_roll_ms < 200:
        raise ValueError("vad.pre_roll_ms 必须至少为 200ms")
    if not 150 <= cfg.vad.post_roll_ms <= 500:
        raise ValueError("vad.post_roll_ms 必须在 150~500ms")
    if not 0.0 <= cfg.llm.temperature <= 0.3:
        raise ValueError("llm.temperature 必须在 0~0.3")
    if cfg.asr.engine != "sherpa_transducer":
        raise ValueError("当前本地 asr.engine 仅支持 sherpa_transducer")
    if cfg.asr.two_pass.engine != "sense_voice":
        raise ValueError("当前 asr.two_pass.engine 仅支持 sense_voice")
    if not 500 <= cfg.asr.two_pass.timeout_ms <= 10_000:
        raise ValueError("asr.two_pass.timeout_ms 必须在 500~10000ms")
    if not 0.0 <= cfg.asr.hotword_boost <= 10.0:
        raise ValueError("asr.hotword_boost 必须在 0~10")
    if not 0.0 <= cfg.rag.generic_product_skip_score <= 1.0:
        raise ValueError("rag.generic_product_skip_score 必须在 0~1")
    if not 0.5 <= cfg.postprocess.fuzzy_threshold <= 1.0:
        raise ValueError("postprocess.fuzzy_threshold 必须在 0.5~1.0")
    if not 0.0 <= cfg.postprocess.suspicious_threshold <= cfg.postprocess.fuzzy_threshold:
        raise ValueError("postprocess.suspicious_threshold 必须不高于 fuzzy_threshold")
    if not 1 <= cfg.postprocess.fragment_min_chars <= 32:
        raise ValueError("postprocess.fragment_min_chars 必须在 1~32")
    if not 50 <= cfg.postprocess.llm_correction.timeout_ms <= 1000:
        raise ValueError("postprocess.llm_correction.timeout_ms 必须在 50~1000ms")
    if not 0.0 <= cfg.postprocess.llm_correction.confidence_threshold <= 1.0:
        raise ValueError("postprocess.llm_correction.confidence_threshold 必须在 0~1")
    return cfg
