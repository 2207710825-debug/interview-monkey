# -*- coding: utf-8 -*-
"""回环采集：抓系统正在播放的声音（会议里对方的声音），不含麦克风。

音频回调只把原始帧放进有界队列；转单声道、重采样和识别均由工作线程完成。

依赖：pyaudiowpatch（WASAPI loopback）、numpy、scipy。
"""
from math import gcd
import logging
import queue
import threading
import time

try:
    import numpy as np
except Exception:                 # 缺 numpy 时模块仍可导入，便于查询设备/状态
    np = None


log = logging.getLogger("interview_monkey.capture")


def available():
    """pyaudiowpatch 是否可用（Windows WASAPI 回环设备）。"""
    try:
        import pyaudiowpatch  # noqa: F401
        return True
    except Exception:
        return False


def list_loopback_devices():
    """枚举 WASAPI 回环设备，返回 [{index, name, sample_rate, channels, is_default}]。

    供 Api.list_sources() 与设备热插拔监听使用；拿不到就返回空列表，不抛异常。
    """
    if not available():
        return []
    try:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
    except Exception:
        return []
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        default_idx = int(wasapi["defaultOutputDevice"])
        default_name = str(p.get_device_info_by_index(default_idx).get("name", ""))
        out = []
        for lb in p.get_loopback_device_info_generator():
            name = str(lb.get("name", ""))
            out.append({
                "index": int(lb["index"]),
                "name": name,
                "sample_rate": int(lb.get("defaultSampleRate", 16000)),
                "channels": int(lb.get("maxInputChannels", 1)),
                "is_default": bool(default_name) and default_name in name,
            })
        return out
    except Exception:
        return []
    finally:
        try:
            p.terminate()
        except Exception:
            pass


def resample_pcm16(samples, sample_rate, target_rate=16000):
    """使用抗混叠多相滤波重采样；生产链路不再退化到线性插值。"""
    if np is None:
        raise RuntimeError("缺少 numpy，无法做 16k 重采样")
    values = np.asarray(samples)
    if sample_rate == target_rate or len(values) == 0:
        return values.astype(np.float64, copy=False)
    try:
        from scipy.signal import resample_poly
    except Exception as exc:
        raise RuntimeError("高质量重采样需要 scipy.signal.resample_poly") from exc
    divisor = gcd(int(sample_rate), int(target_rate))
    return resample_poly(
        values.astype(np.float64), target_rate // divisor, sample_rate // divisor,
    )


def pcm16_to_mono_16k(data, sample_rate, channels):
    """交错 PCM16 → 双声道平均/多声道平均 → 16 kHz 单声道 PCM16。"""
    if np is None:
        raise RuntimeError("缺少 numpy，无法转换 PCM")
    values = np.frombuffer(data, dtype=np.int16) if isinstance(data, (bytes, bytearray)) else np.asarray(data)
    usable = len(values) // channels * channels
    if usable <= 0:
        return np.zeros(0, dtype=np.int16)
    mono = values[:usable].reshape(-1, channels).astype(np.float64).mean(axis=1)
    mono = resample_pcm16(mono, sample_rate, 16000)
    return np.clip(mono, -32768, 32767).astype(np.int16)


class AudioConditioner:
    """轻量电平调节：平滑 RMS 增益 + 峰值限幅，不包含强降噪。"""

    def __init__(
        self,
        enabled=True,
        target_rms_dbfs=-20.0,
        limiter_dbfs=-1.0,
        max_gain_db=12.0,
    ):
        self.enabled = bool(enabled)
        self.target_rms = 10.0 ** (float(target_rms_dbfs) / 20.0)
        self.peak_limit = 32768.0 * 10.0 ** (float(limiter_dbfs) / 20.0)
        self.max_gain = 10.0 ** (float(max_gain_db) / 20.0)
        self.min_gain = 1.0 / self.max_gain
        self.gain = 1.0

    def process(self, pcm16):
        if np is None:
            raise RuntimeError("缺少 numpy，无法调节音频电平")
        values = np.asarray(pcm16, dtype=np.int16)
        if not self.enabled or len(values) == 0:
            return values
        normalized = values.astype(np.float64) / 32768.0
        rms = float(np.sqrt(np.mean(normalized * normalized)))
        if rms > 1e-5:
            desired = min(self.max_gain, max(self.min_gain, self.target_rms / rms))
            # 降增益用较快系数防止削波；升增益较慢，避免短时静音造成泵动。
            smoothing = 0.25 if desired < self.gain else 0.06
            self.gain += (desired - self.gain) * smoothing
        output = values.astype(np.float64) * self.gain
        peak = float(np.max(np.abs(output))) if len(output) else 0.0
        if peak > self.peak_limit:
            output *= self.peak_limit / peak
        return np.clip(output, -32768, 32767).astype(np.int16)


class LoopbackCapture:
    """抓取系统声音；PyAudio 回调仅入队，转换工作在专用线程执行。"""

    def __init__(self, on_pcm16k, device_index=None, raw_queue_frames=100):
        self.on_pcm16k, self.device_index = on_pcm16k, device_index
        self.pa = self.stream = None
        self.sample_rate = None
        self.device_name = None
        self.device_index_used = None
        self.raw_queue_frames = raw_queue_frames
        self._raw_queue = None
        self._worker = None
        self._last_callback_at = None
        self._expected_callback_ms = 20.0
        self._stats = {}

    def _reset_stats(self):
        self._stats = {
            "raw_frames": 0,
            "processed_frames": 0,
            "dropped_frames": 0,
            "callback_status_count": 0,
            "callback_error_count": 0,
            "worker_error_count": 0,
            "callback_late_count": 0,
            "max_callback_gap_ms": 0.0,
            "max_queue_depth": 0,
        }

    def stats(self):
        return dict(self._stats)

    def _process_raw(self):
        while True:
            item = self._raw_queue.get()
            if item is None:
                return
            try:
                data, sample_rate, channels = item
                pcm = pcm16_to_mono_16k(data, sample_rate, channels)
                if len(pcm):
                    self.on_pcm16k(pcm)
                    self._stats["processed_frames"] += 1
            except Exception:
                self._stats["worker_error_count"] += 1
                log.exception("audio_processing_error")

    def _default_loopback(self, pyaudio):
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        spk = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if spk.get("isLoopbackDevice"):
            return spk
        for lb in self.pa.get_loopback_device_info_generator():
            if spk["name"] in lb["name"]:
                return lb
        raise RuntimeError("没找到默认扬声器的回环设备")

    def start(self):
        if np is None:
            raise RuntimeError("缺少 numpy，无法采集系统声音")
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            raise RuntimeError("缺少 pyaudiowpatch，无法采集系统声音：%s" % e)

        self.pa = pyaudio.PyAudio()
        dev = (self.pa.get_device_info_by_index(self.device_index)
               if self.device_index is not None else self._default_loopback(pyaudio))
        sr, ch = int(dev["defaultSampleRate"]), int(dev["maxInputChannels"])
        if ch <= 0:
            raise RuntimeError("回环设备没有可用的输入通道")
        self.sample_rate = sr
        self.device_name = str(dev.get("name", ""))
        self.device_index_used = int(dev["index"])
        # 首次导入 scipy.signal 可能耗时数百毫秒；必须在音频流启动前完成，
        # 否则首批回调会在原始队列中形成明显积压。
        if sr != 16000:
            resample_pcm16(np.zeros(max(1, int(sr * 0.001)), dtype=np.int16), sr, 16000)
        self._expected_callback_ms = 20.0
        self._last_callback_at = None
        self._reset_stats()
        self._raw_queue = queue.Queue(maxsize=self.raw_queue_frames)

        def cb(data, frames, t, status):
            try:
                now = time.perf_counter()
                if self._last_callback_at is not None:
                    gap_ms = (now - self._last_callback_at) * 1000.0
                    self._stats["max_callback_gap_ms"] = max(
                        self._stats["max_callback_gap_ms"], gap_ms,
                    )
                    if gap_ms > self._expected_callback_ms * 1.5:
                        self._stats["callback_late_count"] += 1
                        count = self._stats["callback_late_count"]
                        if count == 1 or count % 50 == 0:
                            log.warning("audio_callback_late gap_ms=%.1f count=%d", gap_ms, count)
                self._last_callback_at = now
                if status:
                    self._stats["callback_status_count"] += 1
                    log.warning("audio_callback_status status=%s", status)
                if self._raw_queue.full():
                    self._raw_queue.get_nowait()
                    self._stats["dropped_frames"] += 1
                    count = self._stats["dropped_frames"]
                    if count == 1 or count % 50 == 0:
                        log.warning("audio_raw_queue_overflow dropped=%d", count)
                self._raw_queue.put_nowait((bytes(data), sr, ch))
                self._stats["raw_frames"] += 1
                self._stats["max_queue_depth"] = max(
                    self._stats["max_queue_depth"], self._raw_queue.qsize(),
                )
            except Exception:
                self._stats["callback_error_count"] += 1
                log.exception("audio_callback_error")
            return (None, pyaudio.paContinue)

        self.stream = self.pa.open(
            format=pyaudio.paInt16, channels=ch, rate=sr,
            input=True, input_device_index=dev["index"],
            frames_per_buffer=max(1, int(sr * 0.02)), stream_callback=cb,
            start=False)
        self._worker = threading.Thread(target=self._process_raw, daemon=True)
        self._worker.start()
        self.stream.start_stream()

    def stop(self):
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:
            pass
        if self._raw_queue is not None:
            try:
                self._raw_queue.put_nowait(None)
            except queue.Full:
                try:
                    self._raw_queue.get_nowait()
                except queue.Empty:
                    pass
                self._raw_queue.put_nowait(None)
        if self._worker is not None:
            self._worker.join(timeout=3.0)
        try:
            if self.pa:
                self.pa.terminate()
        except Exception:
            pass
        self.stream = self.pa = None
        self._raw_queue = self._worker = None
