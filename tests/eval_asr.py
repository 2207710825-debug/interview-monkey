# -*- coding: utf-8 -*-
"""ASR 对比评测：旧流式模型 vs SenseVoice + 术语纠错。

用法（在 面试猴-app 目录下）:
    python tests/eval_asr.py

做法：
  1. 遍历 samples/*.wav，找同名 .txt 作为标注；没有标注的样本跳过
     （不打印 CER，只提示跳过）。
  2. 「旧流式模型」：engine/asr.py 的 transducer（asr_models_zh_en）整段识别，
     不做纠错——它就是现在麦克风实时链路用的引擎。
  3. 「SenseVoice + 纠错」：engine/asr2.py 的 TwoPassASR（silero VAD 切句 +
     SenseVoice 离线解码），拼接各段结果后走 engine/pipeline.apply_corrections
     （corrections.json 字面映射 + terms.txt 拼音近音术语替换）。
  4. 打印每条的 CER（字错误率，编辑距离/标注长度，先去标点空白、英文转小写）
     与实时率 RTF（识别耗时 / 音频时长），最后打印平均 CER、平均 RTF。

验收口径：若 SenseVoice 平均 CER 不低于旧模型，按用户要求停下汇报，不自行换模型。
"""
import os
import re
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SAMPLES = os.path.join(ROOT, "samples")

_PUNCT_RE = re.compile(
    r"[\s，。！？、；：·“”‘’「」『』（）〈〉《》…—,,\.!\?;:\"'\(\)\[\]\{\}]")


def normalize(text):
    """去空白与标点、英文转小写，只留可比对的内容。"""
    return _PUNCT_RE.sub("", (text or "")).lower()


def edit_distance(a, b):
    """经典 DP 编辑距离（O(len_a * len_b)，评测文本都很短）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def cer(ref, hyp):
    r, h = normalize(ref), normalize(hyp)
    if not r:
        return 0.0 if not h else 1.0
    return edit_distance(r, h) / len(r)


def read_wav_16k_mono(path):
    """读 wav，返回 (int16 numpy 一维数组, 实际采样率)；重采样交给引擎。"""
    import numpy as np
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise RuntimeError("仅支持 16bit wav: %s" % path)
    pcm = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return pcm, sr


def resample_to_16k(pcm, sr):
    """使用与生产链路一致的抗混叠多相滤波重采样到 16k。"""
    import numpy as np
    if sr == 16000 or len(pcm) == 0:
        return pcm
    from engine.capture import resample_pcm16
    output = resample_pcm16(pcm, sr, 16000)
    return np.clip(output, -32768, 32767).astype(np.int16)


def eval_old_model(pcm16):
    """旧流式模型（transducer 整段识别），返回 (text, rtf, seconds)。"""
    from engine import asr as asr1
    global _OLD_ENGINE
    if _OLD_ENGINE is None:
        _OLD_ENGINE = asr1.build_asr("sherpa")
        if _OLD_ENGINE is None:
            raise RuntimeError("旧流式模型不可用（asr_models_zh_en 缺失）")
    dur = len(pcm16) / 16000.0
    t0 = time.perf_counter()
    text = _OLD_ENGINE.transcribe(pcm16, 16000)
    elapsed = time.perf_counter() - t0
    return (text or "").strip(), elapsed / max(dur, 1e-6), dur


_OLD_ENGINE = None


_OLD_ENGINE = None
_SV_SESSION = None
_SV_FINALS = []


def eval_sense_voice(pcm16):
    """SenseVoice 两遍识别 + 纠错，返回 (text, rtf, seconds)。

    会话跨样本复用（模型加载 + 239MB ASCII 路径拷贝只做一次）；
    每个样本开始前清掉上一样本残留的输入缓冲和定稿队列。
    """
    from engine import asr2
    from engine import pipeline
    global _SV_SESSION, _SV_FINALS
    if _SV_SESSION is None:
        _SV_SESSION = asr2.build(_SV_FINALS.append)
    if hasattr(_SV_SESSION, "buf"):
        _SV_SESSION.buf = _SV_SESSION.buf[:0]
    del _SV_FINALS[:]
    dur = len(pcm16) / 16000.0
    t0 = time.perf_counter()
    _SV_SESSION.feed(pcm16)
    _SV_SESSION.flush()
    elapsed = time.perf_counter() - t0
    raw = " ".join(_SV_FINALS).strip()
    corrected = pipeline.apply_corrections(raw)
    return corrected, elapsed / max(dur, 1e-6), dur


def main():
    import numpy as np  # noqa: F401  提前暴露缺依赖问题

    wav_files = sorted(f for f in os.listdir(SAMPLES)
                       if f.lower().endswith(".wav"))
    items = []
    for f in wav_files:
        txt = os.path.join(SAMPLES, os.path.splitext(f)[0] + ".txt")
        if os.path.isfile(txt):
            with open(txt, encoding="utf-8") as fh:
                ref = fh.read().strip()
            if ref:
                items.append((os.path.join(SAMPLES, f), ref))
    print("=" * 78)
    print("eval_asr: 旧流式模型 vs SenseVoice + 纠错")
    print("样本目录:", SAMPLES)
    if not items:
        print("[FAIL] samples/ 下没有「wav + 同名 txt」的样本。")
        print("       可先运行 tests/make_tts_samples.py 生成带标注的测试语音。")
        return 1
    print("带标注样本数: %d / %d" % (len(items), len(wav_files)))
    from engine import asr2
    st = asr2.status()
    print("SenseVoice 状态: mode=%s sherpa_onnx=%s pypinyin=%s"
          % (st.get("mode"), st.get("sherpa_onnx"), st.get("pypinyin")))
    if st.get("mode") != "sense_voice":
        print("[WARN] SenseVoice 模型未就绪，实际走的是兜底识别，结果不具对比意义！")
    print("=" * 78)

    rows = []
    for wav_path, ref in items:
        pcm, sr = read_wav_16k_mono(wav_path)
        pcm16 = resample_to_16k(pcm, sr)
        name = os.path.basename(wav_path)

        old_text, old_rtf, dur = eval_old_model(pcm16)
        sv_text, sv_rtf, _ = eval_sense_voice(pcm16)
        old_c, sv_c = cer(ref, old_text), cer(ref, sv_text)
        rows.append((name, dur, old_c, old_rtf, sv_c, sv_rtf))
        print("\n[%s]  时长 %.2fs" % (name, dur))
        print("  标注       : %s" % ref)
        print("  旧流式     : %s" % (old_text or "(空)"))
        print("  SenseVoice : %s" % (sv_text or "(空)"))
        print("  CER  旧=%.3f  SenseVoice=%.3f   |  RTF 旧=%.3f  SenseVoice=%.3f"
              % (old_c, sv_c, old_rtf, sv_rtf))

    n = len(rows)
    avg = lambda idx: sum(r[idx] for r in rows) / n if n else float("nan")
    print("\n" + "=" * 78)
    print("汇总（%d 条）" % n)
    print("  平均 CER : 旧流式模型=%.4f   SenseVoice+纠错=%.4f"
          % (avg(2), avg(4)))
    print("  平均 RTF : 旧流式模型=%.4f   SenseVoice+纠错=%.4f（越小越实时）"
          % (avg(3), avg(5)))
    winner = "SenseVoice+纠错" if avg(4) < avg(2) else "旧流式模型"
    print("  本轮 CER 更低: %s" % winner)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
