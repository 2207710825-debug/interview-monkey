# -*- coding: utf-8 -*-
"""A/B 测试：14M 纯中文模型 vs 中英双语模型（同音频、同流式参数）"""
import os, sys, wave, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import asr as A

DIR_SMALL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine", "asr_models")
DIR_BILING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine", "asr_models_zh_en")
WAVS = [os.path.join(r"E:\面试猴\.workbuddy", f"bi{i}.wav") for i in range(4)]


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() == 2:
            pcm = pcm.reshape(-1, 2).mean(axis=1).astype(np.int16)
    if sr != 16000:
        from engine.capture import resample_pcm16
        pcm = np.clip(resample_pcm16(pcm, sr, 16000), -32768, 32767).astype(np.int16)
    return pcm


def run(model_dir, pcm):
    t0 = time.time()
    sess = A.StreamingSession(model_dir=model_dir)
    for i in range(0, len(pcm), 4800):     # 0.3s 一块，模拟实时
        sess.feed(pcm[i:i + 4800])
    txt = sess.finish()
    return txt, time.time() - t0


def main():
    print("小模型目录:", os.path.basename(DIR_SMALL), "| 双语目录:", os.path.basename(DIR_BILING))
    for p in WAVS:
        if not os.path.isfile(p):
            continue
        pcm = read_wav(p)
        dur = len(pcm) / 16000
        print("\n===== %s  %.1fs =====" % (os.path.basename(p), dur))
        try:
            t1, c1 = run(DIR_SMALL, pcm)
        except Exception as e:
            t1, c1 = "ERR:%s" % e, 0
        try:
            t2, c2 = run(DIR_BILING, pcm)
        except Exception as e:
            t2, c2 = "ERR:%s" % e, 0
        print("[14M 中文] %.2fs  %s" % (c1, t1))
        print("[双语大模型] %.2fs  %s" % (c2, t2))


if __name__ == "__main__":
    main()
