# -*- coding: utf-8 -*-
"""离线流式 ASR 自测：用真实中文音频验证「边说边出字」"""
import os, sys, wave, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import asr as A

WAVS = [r"E:\面试猴\.workbuddy\test0.wav", r"E:\面试猴\.workbuddy\test1.wav"]


def read_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1, "需要单声道"
        sr = w.getframerate()
        data = w.readframes(w.getnframes())
    pcm = np.frombuffer(data, dtype=np.int16)
    if sr != 16000:
        from engine.capture import resample_pcm16
        pcm = np.clip(resample_pcm16(pcm, sr, 16000), -32768, 32767).astype(np.int16)
        sr = 16000
    return pcm, sr


def main():
    print("sherpa-onnx 可用:", A.available())
    for path in WAVS:
        if not os.path.isfile(path):
            print("跳过（文件不存在）:", path)
            continue
        pcm, sr = read_wav(path)
        dur = len(pcm) / sr
        print("\n=== %s  时长 %.1fs ===" % (os.path.basename(path), dur))

        # 流式：按 0.3 秒一块灌入，模拟边说边识别
        t0 = time.time()
        sess = A.StreamingSession()
        chunk = int(sr * 0.3)
        snapshots = []
        for i in range(0, len(pcm), chunk):
            txt = sess.feed(pcm[i:i + chunk], sr)
            if txt and (not snapshots or txt != snapshots[-1]):
                snapshots.append(txt)
        final = sess.finish()
        cost = time.time() - t0
        print("增量出字过程:")
        for s in snapshots[-4:]:
            print("   ", s)
        print("最终文本:", final)
        print("耗时 %.2fs（音频 %.1fs，实时率 %.2fx）" % (cost, dur, dur / max(cost, 1e-6)))

    # 整段一次性识别（非流式接口）
    be = A.build_asr("sherpa")
    print("\nbuild_asr 结果:", type(be).__name__ if be else None)
    if be:
        pcm, sr = read_wav(WAVS[0])
        t0 = time.time()
        print("整段识别:", be.transcribe(pcm, sr), "| %.2fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
