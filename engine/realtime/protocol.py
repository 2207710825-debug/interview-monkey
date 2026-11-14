"""面试猴 v1 客户端音频二进制帧编解码。"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


MAGIC = b"IMON"
VERSION = 1
FRAME_AUDIO = 1
HEADER_PREFIX = struct.Struct("<4sBBHIQIIB3s")
HEADER_SIZE = 36
PCM_BYTES_PER_FRAME = 3200
SAMPLE_RATE = 16_000


class AudioFrameError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DecodedAudioFrame:
    seq: int
    timestamp_ms: int
    sample_rate: int
    channel: str
    payload: bytes
    eof: bool
    crc32: int
    duplicate: bool = False


def encode_audio_frame(
    seq: int,
    timestamp_ms: int,
    payload: bytes,
    *,
    channel: str = "mic",
    eof: bool = False,
) -> bytes:
    """编码一帧 v1 PCM；该函数同时供联调模拟器使用。"""
    if channel not in {"mic", "loopback"}:
        raise ValueError("channel 必须是 mic 或 loopback")
    if eof and payload:
        raise ValueError("EOF 帧不得包含 PCM payload")
    if not eof and len(payload) != PCM_BYTES_PER_FRAME:
        raise ValueError(f"普通音频帧 payload 必须为 {PCM_BYTES_PER_FRAME} 字节")
    flags = 1 if eof else 0
    channel_id = 0 if channel == "mic" else 1
    prefix = HEADER_PREFIX.pack(
        MAGIC, VERSION, FRAME_AUDIO, flags, seq, timestamp_ms, SAMPLE_RATE,
        len(payload), channel_id, b"\0\0\0",
    )
    crc32 = zlib.crc32(prefix + payload) & 0xFFFFFFFF
    return prefix + struct.pack("<I", crc32) + payload


def decode_audio_frame(
    wire: bytes,
    *,
    expected_seq: int,
    seen_crc: dict[int, int] | None = None,
) -> DecodedAudioFrame:
    """检查头部、payload、CRC 与序号；相同旧帧可幂等确认。"""
    if len(wire) > HEADER_SIZE + PCM_BYTES_PER_FRAME:
        raise AudioFrameError("FRAME_TOO_LARGE", "音频帧超过最大允许长度")
    if len(wire) < HEADER_SIZE:
        raise AudioFrameError("INVALID_FRAME", "音频帧头不完整")

    (magic, version, frame_type, flags, seq, timestamp_ms, sample_rate,
     payload_length, channel_id, reserved) = HEADER_PREFIX.unpack(wire[:32])
    crc32 = struct.unpack("<I", wire[32:36])[0]
    payload = wire[HEADER_SIZE:]
    eof = bool(flags & 1)

    if magic != MAGIC or version != VERSION or frame_type != FRAME_AUDIO:
        raise AudioFrameError("INVALID_FRAME", "音频帧 magic、版本或类型无效")
    if flags & ~1 or reserved != b"\0\0\0" or channel_id not in {0, 1}:
        raise AudioFrameError("INVALID_FRAME", "音频帧 flags、保留位或 channel 无效")
    if sample_rate != SAMPLE_RATE:
        raise AudioFrameError("INVALID_FRAME", "音频采样率必须为 16000 Hz")
    if payload_length != len(payload):
        raise AudioFrameError("INVALID_FRAME", "payload_length 与实际长度不一致")
    if (eof and payload_length != 0) or (not eof and payload_length != PCM_BYTES_PER_FRAME):
        raise AudioFrameError("INVALID_FRAME", "普通帧必须为 3200 字节，EOF 帧必须为空")

    actual_crc = zlib.crc32(wire[:32] + payload) & 0xFFFFFFFF
    if actual_crc != crc32:
        raise AudioFrameError("CHECKSUM_MISMATCH", "音频帧 CRC32 校验失败", retryable=True)

    duplicate = False
    seen_crc = seen_crc if seen_crc is not None else {}
    if seq < expected_seq:
        if seen_crc.get(seq) != crc32:
            raise AudioFrameError("INVALID_FRAME", "重复序号对应的帧内容不一致")
        duplicate = True
    elif seq > expected_seq:
        raise AudioFrameError("SEQ_GAP", f"音频帧序号跳号，期望 {expected_seq}", retryable=True)

    return DecodedAudioFrame(
        seq=seq,
        timestamp_ms=timestamp_ms,
        sample_rate=sample_rate,
        channel="mic" if channel_id == 0 else "loopback",
        payload=payload,
        eof=eof,
        crc32=crc32,
        duplicate=duplicate,
    )
