# -*- coding: utf-8 -*-
"""第 2 段：语音转文本（ASR Engine）

本地 ASR 引擎（无网络、数据不出本机）：
  - sherpa-onnx：轻量、CPU 可跑，支持中英文流式识别（默认，随包内置中文模型）
  - faster-whisper：Whisper 的 CTranslate2 加速实现（可选）

说明：WebView2 里的 Web Speech API 属于 Google 在线识别，在桌面 App 内
必然返回 network 错误，因此 App 使用本模块做真正的离线流式识别。
"""
import os
import threading

_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))


def _pick_model_dir():
    """优先用中英双语大模型（能识别 CIoU / Focal Loss 这类术语），退回小模型。"""
    for name in ("asr_models_zh_en", "asr_models"):
        p = os.path.join(_ENGINE_DIR, name)
        if os.path.isdir(p):
            return p
    return os.path.join(_ENGINE_DIR, "asr_models")


DEFAULT_MODEL_DIR = _pick_model_dir()
HOTWORDS_FILE = os.path.join(_ENGINE_DIR, "hotwords.txt")


def _ascii_path(path):
    """sherpa-onnx 的热词/词表加载走 C++ 窄字符 ifstream，
    路径含中文（如 E:\\面试猴\\...）会打开失败。

    处理：路径纯 ASCII 原样返回；否则把文件复制到 ASCII 临时目录再返回新路径。
    只用于几 KB 的小文件（hotwords.txt / tokens.txt），开销可忽略。
    """
    try:
        full = os.path.abspath(path)
        if all(ord(c) < 128 for c in full):
            return full
        import tempfile
        out_dir = os.path.join(tempfile.gettempdir(), "interviewmonkey_asr")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, os.path.basename(path))
        with open(path, "rb") as f, open(out, "wb") as g:
            g.write(f.read())
        return out
    except Exception:
        return path


class _SherpaOnnx:
    def __init__(self, model_dir=None, hotwords_score=1.5, hotwords_file=None):
        import sherpa_onnx
        model_dir = model_dir or DEFAULT_MODEL_DIR
        if not os.path.isdir(model_dir):
            raise RuntimeError("未找到 sherpa-onnx 模型目录: %s" % model_dir)

        def pick(*names):
            for n in names:
                p = os.path.join(model_dir, n)
                if os.path.isfile(p):
                    return p
            return None

        encoder = pick("encoder-epoch-99-avg-1.int8.onnx", "encoder.onnx",
                       "encoder-epoch-99-avg-1.onnx")
        decoder = pick("decoder-epoch-99-avg-1.onnx", "decoder.onnx")
        joiner = pick("joiner-epoch-99-avg-1.int8.onnx", "joiner.onnx",
                      "joiner-epoch-99-avg-1.onnx")
        tokens = pick("tokens.txt")
        if not all([encoder, decoder, joiner, tokens]):
            raise RuntimeError("模型文件不完整，缺少 encoder/decoder/joiner/tokens")

        self.model_dir = model_dir
        # 34MB 小模型 -> 用 2 线程；190MB 双语模型 -> 多给线程，保证不落后于说话速度
        big = "zh_en" in os.path.basename(model_dir)
        # VAD 停顿阈值：说完静音多久算一句。0.8s 是「实时问答」的经验甜点——
        # 再短会把一句完整的话切断（触发多次大请求），再长用户就等不及了。
        # 可用环境变量 VAD_SILENCE 微调（秒）。
        try:
            vad_silence = float(os.environ.get("VAD_SILENCE", "0.8"))
        except ValueError:
            vad_silence = 0.8
        kwargs = dict(
            tokens=tokens, encoder=encoder, decoder=decoder, joiner=joiner,
            num_threads=4 if big else 2, sample_rate=16000,
            feature_dim=80,
            decoding_method="modified_beam_search" if big else "greedy_search",
            enable_endpoint_detection=True,
            # 断句时机：说完停 0.8 秒就断句（默认 2.4 秒太慢，实时问答等不起），
            # 前两条规则管尾部静音，第三条兜底防止一句话被无限拖长。
            rule1_min_trailing_silence=2.0,
            rule2_min_trailing_silence=vad_silence,
            rule3_min_utterance_length=15,
        )
        if big:
            kwargs["max_active_paths"] = 4
        # 热词增强：把视觉/3D 方向的专有名词喂进去，显著减少同音错字
        # （sherpa-onnx 要求热词必须配 modified_beam_search 解码）
        selected_hotwords = hotwords_file or HOTWORDS_FILE
        if big and os.path.isfile(selected_hotwords):
            kwargs.update(hotwords_file=_ascii_path(selected_hotwords),
                          hotwords_score=float(hotwords_score),
                          modeling_unit="cjkchar+bpe",
                          bpe_vocab=_ascii_path(tokens))
        try:
            self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(**kwargs)
            self.hotwords = bool(big and os.path.isfile(selected_hotwords))
        except Exception:
            # 热词配置不被该模型接受时，退回不带热词的版本，保证可用
            for k in ("hotwords_file", "hotwords_score", "modeling_unit", "bpe_vocab"):
                kwargs.pop(k, None)
            kwargs["decoding_method"] = "greedy_search"
            self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(**kwargs)
            self.hotwords = False


    def transcribe(self, pcm, sample_rate=16000):
        import numpy as np
        self.rec.reset(self.stream) if hasattr(self, "stream") else None
        stream = self.rec.create_stream()
        samples = np.asarray(pcm)
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        for i in range(0, len(samples), 3200):
            stream.accept_waveform(sample_rate, samples[i:i + 3200])
            while self.rec.is_ready(stream):
                self.rec.decode_stream(stream)
        stream.accept_waveform(sample_rate, np.zeros(2400, dtype=np.float32))
        while self.rec.is_ready(stream):
            self.rec.decode_stream(stream)
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
            return os.path.isdir(DEFAULT_MODEL_DIR)
        elif engine == "whisper":
            import faster_whisper  # noqa: F401
        else:
            return False
        return True
    except Exception:
        return False


class StreamingSession:
    """一次录音会话：不断灌入 16k/单声道/int16 PCM，随时取当前识别文字。

    GUI 侧（WebView2）用 getUserMedia 采集音频，按 0.3 秒一块把 PCM 送进来，
    这里的识别结果是"边说边出字"的增量文本，不依赖任何云服务。
    """

    def __init__(self, engine_obj=None, model_dir=None, hotwords_score=1.5):
        self.engine = engine_obj or _SherpaOnnx(model_dir, hotwords_score)
        self.stream = self.engine.rec.create_stream()
        self.text = ""
        self.sentences = []      # 已定稿的句子
        self._pending = None     # 本次 feed 新定稿、还没被取走的句子
        self.lock = threading.Lock()
        self.finished = False

    def feed(self, pcm, sample_rate=16000):
        """灌入一块 PCM（int16 数组或 bytes），返回当前累计文本。"""
        import numpy as np
        if isinstance(pcm, (bytes, bytearray)):
            pcm = np.frombuffer(bytes(pcm), dtype=np.int16)
        samples = np.asarray(pcm)
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        with self.lock:
            if self.finished:
                return self.text
            self.stream.accept_waveform(sample_rate, samples)
            rec = self.engine.rec
            while rec.is_ready(self.stream):
                rec.decode_stream(self.stream)
            piece = rec.get_result(self.stream)
            if rec.is_endpoint(self.stream):
                # 一句话说完：把这句话并入累计文本，重置以识别下一句
                if piece:
                    piece = piece.strip()
                    self.text = (self.text + piece).strip()
                    self.sentences.append(piece)
                    # 一块音频里可能断出多句，拼起来交给上层，避免丢句
                    self._pending = ((self._pending or "") + piece).strip()
                rec.reset(self.stream)
                piece = ""
            return (self.text + (piece or "")).strip()

    def finish(self):
        """结束录音，返回最终文本。"""
        import numpy as np
        with self.lock:
            if self.finished:
                return self.text
            rec = self.engine.rec
            self.stream.accept_waveform(16000, np.zeros(3200, dtype=np.float32))
            while rec.is_ready(self.stream):
                rec.decode_stream(self.stream)
            tail = rec.get_result(self.stream) or ""
            if tail:
                tail = tail.strip()
                # 断句已经把这句定稿过时，finish 再取到同样内容是重复，丢弃
                dup = bool(tail) and (self.text.endswith(tail) or
                                      (self.sentences and self.sentences[-1] == tail))
                if tail and not dup:
                    self.text = (self.text + tail).strip()
                    # 停止监听时最后一句往往还没触发断句，也要作为定稿句子交出去
                    self.sentences.append(tail)
                    self._pending = ((self._pending or "") + tail).strip()
            self.finished = True
            return self.text
