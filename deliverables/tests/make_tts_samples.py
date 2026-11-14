# -*- coding: utf-8 -*-
"""用 Windows 自带 SAPI TTS 生成 samples/ 下的评测样本。

每个句子生成两个文件：
  samples/ttsNN.wav   —— 16kHz / 16bit / 单声道（ASR 直接可用）
  samples/ttsNN.txt   —— 同名标注（ground truth，eval_asr.py 算 CER 用）

用途：手头没有真实录音时，用合成语音先跑通「旧流式模型 vs SenseVoice + 纠错」
的对比评测；之后把真实录音（面试/会议回环录的）配同名 .txt 放进 samples/ 即可
自动纳入评测。运行环境：需要 pywin32（managed python 已装）。
"""
import os
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SENTENCES = [
    "请讲一下交并比的定义和计算方法。",
    "非极大值抑制在目标检测里起什么作用？",
    "自注意力机制的计算复杂度是多少？",
    "简述端到端自动驾驶方案的优缺点。",
    "预训练和微调有什么区别？",
    "说说你对感受野的理解。",
    "多尺度特征融合是怎么实现的？",
    "上采样和下采样分别用在哪里？",
]


def pick_chinese_voice(voice):
    """挑一个中文语音包（Huihui / Kangkang / Yaoyao 等）。"""
    for tok in voice.GetVoices():
        name = tok.GetAttribute("Name") or ""
        lang = ""
        try:
            lang = tok.GetAttribute("Language")
        except Exception:
            pass
        if any(k in name for k in ("Huihui", "Kangkang", "Yaoyao", "Yingtao",
                                   "Xiaoxiao", "Chinese", "zh-", "zh_")):
            return tok, name
        if lang in ("804", "0804", "zh-CN"):
            return tok, name
    return None, None


def _force_16k(wav_path):
    """SAPI 在部分 COM 包装下不认 Format.Type 设置，落到默认 44.1kHz；
    生成后读回重采样为 16kHz/16bit/单声道覆盖，保证 ASR 输入规范。"""
    import wave
    import numpy as np
    with wave.open(wav_path, "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if sr == 16000:
        return False
    from engine.capture import resample_pcm16
    out = resample_pcm16(pcm, sr, 16000)
    out = np.clip(out, -32768, 32767).astype(np.int16)
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(out.tobytes())
    return True


def main():
    # EnsureDispatch 生成 makepy 包装后 Format.Type 赋值才真正生效，
    # 普通动态 Dispatch 会静默回落到默认 44.1kHz
    from win32com.client import gencache

    voice = gencache.EnsureDispatch("SAPI.SpVoice")
    tok, tok_name = pick_chinese_voice(voice)
    if tok is None:
        names = [t.GetAttribute("Name") for t in voice.GetVoices()]
        print("[FAIL] 未找到中文 TTS 语音包，可用：", names)
        return 1
    voice.Voice = tok
    voice.Rate = 0
    print("[TTS] 使用语音包:", tok_name)

    os.makedirs(SAMPLES, exist_ok=True)
    for i, sent in enumerate(SENTENCES, 1):
        wav_path = os.path.join(SAMPLES, "tts%02d.wav" % i)
        txt_path = os.path.join(SAMPLES, "tts%02d.txt" % i)
        stream = gencache.EnsureDispatch("SAPI.SpFileStream")
        stream.Format.Type = 34          # SAFT16kHz16BitMono
        stream.Open(wav_path, 3)         # SSFMCreateForWrite
        voice.AudioOutputStream = stream
        voice.Speak(sent)
        stream.Close()
        voice.AudioOutputStream = None
        _force_16k(wav_path)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(sent + "\n")
        with wave.open(wav_path, "rb") as w:
            info = "%dHz %ds" % (w.getframerate(),
                                 round(w.getnframes() / w.getframerate(), 2))
        print("  %s (%s) <- %s" % (wav_path, info, sent))
    print("[OK] 共生成 %d 组样本（wav + 同名 txt）" % len(SENTENCES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
