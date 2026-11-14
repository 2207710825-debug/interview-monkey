# -*- coding: utf-8 -*-
"""两遍识别：VAD 切句 + 离线模型整段解码。

首选路径照规格实现：silero VAD（sherpa-onnx）+ SenseVoice 离线识别。
**feed 必须在工作线程里调用，不要在音频回调里调**——解码会阻塞几十到几百毫秒。

模型缺失时的离线兜底：不用 silero / SenseVoice，改用纯 numpy 能量 VAD 切句 +
项目内置的 transducer 模型（engine/asr.py）逐段识别。这样即使没下载新模型，
回环采集这条链路也能跑通；把 silero_vad.onnx 和 SenseVoice 目录放进 engine/
即自动切回首选路径。
"""
import os
import re
import threading

try:
    import numpy as np
except Exception:                 # 缺 numpy 时模块仍可导入，便于查询 status
    np = None

try:
    import sherpa_onnx
except Exception:
    sherpa_onnx = None

_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
# 规格指定位置：silero VAD 放 asr_models/，SenseVoice 放 asr_models_sense/
DEFAULT_VAD = os.path.join(_ENGINE_DIR, "asr_models", "silero_vad.onnx")
DEFAULT_SENSE_DIR = os.path.join(_ENGINE_DIR, "asr_models_sense")
# 兼容旧的放置方式（engine 根目录），放齐也认
LEGACY_VAD = os.path.join(_ENGINE_DIR, "silero_vad.onnx")
LEGACY_SENSE_DIR = os.path.join(_ENGINE_DIR, "sense_voice")


def _resolve_vad(vad_model=None):
    if vad_model:
        return vad_model
    if os.path.isfile(DEFAULT_VAD):
        return DEFAULT_VAD
    return LEGACY_VAD


def _resolve_sense_dir(sense_dir=None):
    if sense_dir:
        return sense_dir
    if os.path.isfile(os.path.join(DEFAULT_SENSE_DIR, "model.int8.onnx")):
        return DEFAULT_SENSE_DIR
    return LEGACY_SENSE_DIR


def _ascii_path(path):
    """复用 asr.py 的 _ascii_path：sherpa-onnx 读文件走 C++ 窄字符 ifstream，
    路径含中文（如 E:\\面试猴\\...）会打开失败；非 ASCII 路径复制到 ASCII
    临时目录再返回新路径。"""
    from .asr import _ascii_path as _fix
    return _fix(path)

_TAG_RE = re.compile(r"<\|.*?\|>")


def models_available(vad_model=None, sense_dir=None):
    """首选路径所需的文件是否齐全。"""
    vad_model = _resolve_vad(vad_model)
    sense_dir = _resolve_sense_dir(sense_dir)
    return (sherpa_onnx is not None
            and os.path.isfile(vad_model)
            and os.path.isfile(os.path.join(sense_dir, "model.int8.onnx"))
            and os.path.isfile(os.path.join(sense_dir, "tokens.txt")))


def _clean(text):
    return _TAG_RE.sub("", text or "").strip()


class SenseVoiceRecognizer:
    """可复用的 SenseVoice 整句识别器；调用方负责提供已经切好的 16k PCM。"""

    def __init__(self, sense_dir=None, num_threads=4):
        if sherpa_onnx is None:
            raise RuntimeError("sherpa_onnx 不可用")
        if np is None:
            raise RuntimeError("缺少 numpy，无法运行 SenseVoice")
        sense_dir = _resolve_sense_dir(sense_dir)
        if not models_available(sense_dir=sense_dir):
            raise RuntimeError("SenseVoice 模型文件不完整")
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=_ascii_path(os.path.join(sense_dir, "model.int8.onnx")),
            tokens=_ascii_path(os.path.join(sense_dir, "tokens.txt")),
            use_itn=True,
            language="auto",
            num_threads=num_threads,
        )
        self.lock = threading.Lock()

    def transcribe(self, pcm16, sample_rate=16000):
        samples = np.asarray(pcm16)
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        else:
            samples = samples.astype(np.float32, copy=False)
        if not len(samples):
            return ""
        with self.lock:
            stream = self.rec.create_stream()
            stream.accept_waveform(sample_rate, samples)
            self.rec.decode_stream(stream)
            return _clean(stream.result.text)


class TwoPassASR:
    """silero VAD 切句 + SenseVoice 整段解码（规格实现）。"""

    def __init__(self, vad_model, sense_dir, on_final):
        if sherpa_onnx is None:
            raise RuntimeError("sherpa_onnx 不可用")
        if np is None:
            raise RuntimeError("缺少 numpy，无法做 VAD 分段")
        vc = sherpa_onnx.VadModelConfig()
        # 路径含中文时复制到 ASCII 临时目录，sherpa-onnx 才能打开
        vc.silero_vad.model = _ascii_path(vad_model)
        vc.silero_vad.min_silence_duration = 0.5
        vc.silero_vad.min_speech_duration = 0.25
        try:
            vc.silero_vad.max_speech_duration = 15   # 超长自动切，避免延迟累积
        except Exception:
            pass
        vc.sample_rate = 16000
        self.vad = sherpa_onnx.VoiceActivityDetector(vc, buffer_size_in_seconds=30)
        self.recognizer = SenseVoiceRecognizer(sense_dir)
        self.on_final = on_final
        self.buf = np.zeros(0, np.float32)
        self.lock = threading.Lock()

    def feed(self, pcm16):
        """喂 16k int16 PCM；只由工作线程调用。"""
        with self.lock:
            self.buf = np.concatenate([self.buf, pcm16.astype(np.float32) / 32768])
            while len(self.buf) >= 512:
                self.vad.accept_waveform(self.buf[:512])
                self.buf = self.buf[512:]
            while not self.vad.empty():
                seg = self.vad.front.samples
                self.vad.pop()
                text = self.recognizer.transcribe(seg, 16000)
                if text:
                    self.on_final(text)

    def flush(self):
        """停止采集时把最后一段未达静音阈值的语音强制吐出。"""
        with self.lock:
            try:
                self.vad.flush()
            except Exception:
                pass
            while not self.vad.empty():
                seg = self.vad.front.samples
                self.vad.pop()
                text = self.recognizer.transcribe(seg, 16000)
                if text:
                    self.on_final(text)


class EnergyVadFallback:
    """无 silero/SenseVoice 时的兜底：能量 VAD + 内置 transducer 分段识别。"""

    FRAME = 512            # 32ms @ 16k
    MIN_SILENCE = 0.5      # 静音多久断句
    MAX_SPEECH = 15.0      # 单段最长秒数
    RMS_THRESHOLD = 0.012

    def __init__(self, on_final, asr_engine=None):
        if np is None:
            raise RuntimeError("缺少 numpy，无法做能量 VAD 分段")
        from . import asr as asr_mod
        self.engine = asr_engine if asr_engine is not None else asr_mod.build_asr("sherpa")
        self.on_final = on_final
        self.buf = np.zeros(0, np.float32)
        self.speech = []
        self.silence = 0.0
        self.lock = threading.Lock()

    def feed(self, pcm16):
        with self.lock:
            x = np.asarray(pcm16, dtype=np.float32) / 32768
            self.buf = np.concatenate([self.buf, x])
            while len(self.buf) >= self.FRAME:
                frame = self.buf[:self.FRAME]
                self.buf = self.buf[self.FRAME:]
                rms = float(np.sqrt(np.mean(frame * frame)))
                if rms > self.RMS_THRESHOLD:
                    self.speech.append(frame)
                    self.silence = 0.0
                else:
                    self.silence += self.FRAME / 16000
                    if self.speech:
                        self.speech.append(frame)
                dur = sum(len(f) for f in self.speech) / 16000
                if self.speech and (self.silence >= self.MIN_SILENCE
                                    or dur >= self.MAX_SPEECH):
                    self._emit()

    def _emit(self):
        seg = np.concatenate(self.speech) if self.speech else np.zeros(0, np.float32)
        self.speech, self.silence = [], 0.0
        if len(seg) < 1600 or self.engine is None:   # 短于 0.1 秒的噪声丢弃
            return
        pcm = np.clip(seg * 32768, -32768, 32767).astype(np.int16)
        try:
            text = _clean(self.engine.transcribe(pcm, 16000))
        except Exception:
            return
        if text:
            self.on_final(text)

    def flush(self):
        with self.lock:
            self._emit()


def build(on_final, vad_model=None, sense_dir=None):
    """工厂：优先 SenseVoice 两遍识别，模型不全则退到能量 VAD 兜底。"""
    if models_available(vad_model, sense_dir):
        return TwoPassASR(_resolve_vad(vad_model),
                          _resolve_sense_dir(sense_dir), on_final)
    return EnergyVadFallback(on_final)


def status():
    """给前端/自检看的可用性说明。"""
    from . import capture as capture_mod
    from . import correct as correct_mod
    return {
        "mode": "sense_voice" if models_available() else "fallback_transducer",
        "sense_voice_ready": models_available(),
        "vad_model": _resolve_vad(),
        "sense_dir": _resolve_sense_dir(),
        "sherpa_onnx": sherpa_onnx is not None,
        "pypinyin": correct_mod.pypinyin_available(),
        "loopback": capture_mod.available(),
    }
