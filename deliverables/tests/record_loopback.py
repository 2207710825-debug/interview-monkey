# -*- coding: utf-8 -*-
"""录「系统正在播放的声音」为 16k / 单声道 / int16 wav（不含麦克风）。

用法（要用带 pyaudiowpatch 的解释器，例如打包环境）：
    E:/面试猴/.workbuddy/gui-build/venv/Scripts/python.exe \
        tests/record_loopback.py --seconds 20 --out samples/loop1.wav

验收要点：录的同时对人声说话，文件里只会有系统播放的内容，不会有人声——
因为打开的是 WASAPI 回环（渲染端点）设备，物理上不接麦克风输入流。
"""
import argparse
import os
import sys
import time
import wave

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from engine import capture  # noqa: E402

SAMPLE_RATE = 16000


def main():
    ap = argparse.ArgumentParser(description="录系统声音为 16k 单声道 wav")
    ap.add_argument("--seconds", type=float, default=20.0, help="录制秒数")
    ap.add_argument("--out", default="samples/loop1.wav", help="输出 wav 路径")
    ap.add_argument("--device", type=int, default=None,
                    help="回环设备 index；不传用默认扬声器")
    args = ap.parse_args()

    if not capture.available():
        print("缺少 pyaudiowpatch。请用打包环境运行：")
        print("  E:/面试猴/.workbuddy/gui-build/venv/Scripts/python.exe "
              "tests/record_loopback.py --seconds 20 --out samples/loop1.wav")
        return 2
    try:
        import numpy as np
    except Exception:
        print("缺少 numpy，无法写 wav")
        return 2

    sources = capture.list_loopback_devices()
    default = next((d for d in sources if d.get("is_default")), None)
    print("回环设备数：%d" % len(sources))
    if default:
        print("默认回环设备：#%d %s (%d Hz, %d ch)"
              % (default["index"], default["name"],
                 default["sample_rate"], default["channels"]))
    if not sources and args.device is None:
        print("警告：没枚举到回环设备，仍会尝试打开默认扬声器回环")

    chunks = []
    cap = capture.LoopbackCapture(lambda pcm: chunks.append(pcm), args.device)
    try:
        cap.start()
    except Exception as e:
        print("打开回环设备失败：%s" % e)
        return 3
    print("打开的设备：%s（原始采样率 %s Hz）"
          % (cap.device_name or "未知", cap.sample_rate))
    print("录制 %.1f 秒… 请同时外放一段音频并对着麦克风说话" % args.seconds)

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(0.1)
    finally:
        cap.stop()

    pcm = (np.concatenate(chunks).astype(np.int16) if chunks
           else np.zeros(0, dtype=np.int16))
    want = int(round(args.seconds * SAMPLE_RATE))
    if len(pcm) < want:                       # 不足补静音，保证时长精确
        pcm = np.pad(pcm, (0, want - len(pcm)))
    else:
        pcm = pcm[:want]

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())

    # 读回校验，打印验收要的三个数
    with wave.open(args.out, "rb") as w:
        ch, width, rate, frames = (w.getnchannels(), w.getsampwidth(),
                                   w.getframerate(), w.getnframes())
    peak = int(np.max(np.abs(pcm.astype(np.int32)))) if len(pcm) else 0
    rms = (float(np.sqrt(np.mean((pcm.astype(np.float64) / 32768) ** 2)))
           if len(pcm) else 0.0)
    print("")
    print("文件：%s" % os.path.abspath(args.out))
    print("时长：%.3f 秒 (%d 帧)" % (frames / rate, frames))
    print("采样率：%d Hz" % rate)
    print("通道数：%d（位深 %d bit）" % (ch, width * 8))
    print("峰值：%d / 32768，RMS：%.4f" % (peak, rms))
    print("采集统计：%s" % cap.stats())
    print("提示：对着麦克风说的话不会出现在该文件里；"
          "若要自证，可分别录「只说话不外放」与「只外放不说话」两段做波形对比。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
