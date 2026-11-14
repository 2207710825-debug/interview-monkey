# -*- coding: utf-8 -*-
"""第 2 段：语音转文本（ASR Engine）

本地 ASR 引擎（无网络、数据不出本机）：
  - sherpa-onnx：轻量、CPU 可跑，支持中英文流式识别
  - faster-whisper：Whisper 的 CTranslate2 加速实现

未安装/未下载模型时返回 None，由上层降级（Web Speech 或手动输入）。
"""
import os


class _SherpaOnnx:
    def __init__(self, model_dir=None):
        import sherpa_onnx
        if not model_dir:
            # 需自行下载模型，例如：
            #   https://github.com/k2-fsa/sherpa-onnx/releases 的
            #   sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23 之类
            raise RuntimeError("未提供 sherpa-onnx 模型目录（--asr-model）")
        self.rec = sherpa_onnx.OnlineRecognizer.from_zipformer(
            encoder=os.path.join(model_dir, "encoder.onnx"),
            decoder=os.path.join(model_dir, "decoder.onnx"),
            joiner=os.path.join(model_dir, "joiner.onnx"),
            tokens=os.path.join(model_dir, "tokens.txt"),
            num_threads=2,
            sample_rate=16000,
        )

    def transcribe(self, pcm, sample_rate=16000):
        import sherpa_onnx
        self.rec.reset()
        stream = self.rec.create_stream()
        import numpy as np
        samples = np.asarray(pcm)
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        chunk = 3200
        for i in range(0, len(samples), chunk):
            stream.accept_waveform(sample_rate, samples[i:i + chunk])
            if self.rec.is_ready(stream):
                self.rec.decode(stream)
        self.rec.decode(stream)
        return self.rec.get_result(stream)


class _FasterWhisper:
    def __init__(self, model_size="small"):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def transcribe(self, pcm, sample_rate=16000):
        import numpy as np
        samples = np.asarray(pcm)
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        segs, _ = self.model.transcribe(samples, language="zh")
        return "".join(s.text for s in segs)


def build_asr(engine="sherpa", model_dir=None, model_size="small"):
    """返回一个带 transcribe(pcm, sample_rate) 的对象；不可用返回 None。"""
    try:
        if engine == "sherpa":
            return _SherpaOnnx(model_dir)
        if engine == "whisper":
            return _FasterWhisper(model_size)
    except Exception:
        return None
    return None


def available(engine="sherpa"):
    try:
        if engine == "sherpa":
            import sherpa_onnx  # noqa: F401
        elif engine == "whisper":
            import faster_whisper  # noqa: F401
        else:
            return False
        return True
    except Exception:
        return False
