# -*- coding: utf-8 -*-
"""第 1 段：音频采集（Audio Capture）

从麦克风采集 PCM 音频流，输出 16kHz / 单声道 / int16 的 numpy 数组（ASR 通用规格）。

依赖：sounddevice（可选）。未安装时 record() 返回 None，由上层降级到 Web 端语音或手动输入。
"""


def _np_or_bytes():
    try:
        import numpy as _np
        return _np
    except Exception:
        return None


def record_seconds(duration=5.0, sample_rate=16000):
    """录 duration 秒，返回 (pcm_int16_array_or_bytes, sample_rate)；不可用返回 (None, sample_rate)。"""
    try:
        import sounddevice as sd
    except Exception:
        return None, sample_rate

    np = _np_or_bytes()
    try:
        audio = sd.rec(int(duration * sample_rate), samplerate=sample_rate,
                       channels=1, dtype="int16")
        sd.wait()
        if np is not None:
            return np.asarray(audio).reshape(-1), sample_rate
        return audio.tobytes() if hasattr(audio, "tobytes") else bytes(audio), sample_rate
    except Exception:
        return None, sample_rate


def available():
    try:
        import sounddevice  # noqa: F401
        return True
    except Exception:
        return False
