# -*- coding: utf-8 -*-
"""依据 cases.yaml 生成可重复的 Windows SAPI TTS 评测音频。"""
from __future__ import annotations

import math
import base64
import subprocess
import sys
import wave
from pathlib import Path


HERE = Path(__file__).resolve().parent
CASES_PATH = HERE / "cases.yaml"
SAMPLES_DIR = HERE / "samples"


def load_cases() -> list[dict]:
    import yaml

    data = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8")) or {}
    return list(data.get("cases", []))


def _pick_chinese_voice(voice):
    for token in voice.GetVoices():
        name = token.GetAttribute("Name") or ""
        try:
            language = token.GetAttribute("Language")
        except Exception:
            language = ""
        if any(key in name for key in (
            "Huihui", "Kangkang", "Yaoyao", "Yingtao", "Xiaoxiao",
            "Chinese", "zh-", "zh_",
        )) or language in {"804", "0804", "zh-CN"}:
            return token, name
    return None, None


def _read_pcm(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    if sample_width != 2:
        raise RuntimeError(f"仅支持 PCM16 TTS 输出: {path}")
    if channels > 1:
        usable = len(pcm) // channels * channels
        pcm = pcm[:usable].reshape(-1, channels).mean(axis=1).astype(np.int16)
    return pcm, sample_rate


def _to_16k(pcm, sample_rate: int):
    import numpy as np

    if sample_rate == 16_000:
        return np.asarray(pcm, dtype=np.int16)
    from scipy.signal import resample_poly

    divisor = math.gcd(sample_rate, 16_000)
    output = resample_poly(
        pcm.astype(np.float64), 16_000 // divisor, sample_rate // divisor,
    )
    return np.clip(output, -32768, 32767).astype(np.int16)


def _add_padding_and_noise(pcm, case: dict):
    import numpy as np

    leading = np.zeros(int(0.25 * 16_000), dtype=np.int16)
    trailing = np.zeros(int(0.95 * 16_000), dtype=np.int16)
    body = np.asarray(pcm, dtype=np.float64)
    snr_db = case.get("noise_snr_db")
    if snr_db is not None and len(body):
        # 固定随机种子，确保每次回归得到相同的背景声。
        seed = int(case["id"].removeprefix("asr") or "0")
        rng = np.random.default_rng(seed)
        signal_rms = math.sqrt(float(np.mean(body * body))) or 1.0
        noise_rms = signal_rms / (10 ** (float(snr_db) / 20.0))
        white = rng.normal(0.0, noise_rms * 0.75, len(body))
        t = np.arange(len(body), dtype=np.float64) / 16_000.0
        hum = np.sin(2 * math.pi * 60.0 * t) * noise_rms * 0.25
        body = body + white + hum
    body = np.clip(body, -32768, 32767).astype(np.int16)
    return np.concatenate([leading, body, trailing])


def _write_pcm(path: Path, pcm) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(pcm.tobytes())


def _generate_with_powershell(items: list[tuple[Path, str]]) -> str:
    """在精简 Python 环境没有 pywin32 时，调用 Windows 自带 System.Speech。"""
    rows = []
    for path, text in items:
        path_b64 = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
        text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        rows.append(f"@{{Path='{path_b64}';Text='{text_b64}'}}")
    script = "\n".join([
        "Add-Type -AssemblyName System.Speech",
        "$utf8=[System.Text.Encoding]::UTF8",
        "$speaker=New-Object System.Speech.Synthesis.SpeechSynthesizer",
        "$installed=$speaker.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'zh-CN' } | Select-Object -First 1",
        "if ($null -eq $installed) { throw '未找到 zh-CN System.Speech 语音包' }",
        "$speaker.SelectVoice($installed.VoiceInfo.Name)",
        f"$items=@({','.join(rows)})",
        "foreach($item in $items){",
        "  $path=$utf8.GetString([Convert]::FromBase64String($item.Path))",
        "  $text=$utf8.GetString([Convert]::FromBase64String($item.Text))",
        "  $speaker.SetOutputToWaveFile($path)",
        "  $speaker.Speak($text)",
        "  $speaker.SetOutputToNull()",
        "}",
        "$name=$speaker.Voice.Name",
        "$speaker.Dispose()",
        "Write-Output $name",
    ])
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip().splitlines()[-1]


def generate(force: bool = False) -> int:
    if sys.platform != "win32":
        raise RuntimeError("TTS 样本生成目前使用 Windows SAPI，只能在 Windows 运行")
    cases = load_cases()
    if len(cases) < 30:
        raise RuntimeError(f"评测条目不足 30 条，当前为 {len(cases)} 条")
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    pending = [
        case for case in cases
        if force or not (SAMPLES_DIR / f"{case['id']}.wav").is_file()
    ]
    if not pending:
        print(f"[TTS] existing={len(cases)} generated=0")
        return 0

    temporary_items = [
        (SAMPLES_DIR / f".{case['id']}.sapi.wav", str(case["reference"]))
        for case in pending
    ]
    try:
        from win32com.client import gencache

        voice = gencache.EnsureDispatch("SAPI.SpVoice")
        token, voice_name = _pick_chinese_voice(voice)
        if token is None:
            names = [item.GetAttribute("Name") for item in voice.GetVoices()]
            raise RuntimeError(f"未找到中文 TTS 语音包，可用语音: {names}")
        voice.Voice = token
        voice.Rate = 0
        for temporary, text in temporary_items:
            stream = gencache.EnsureDispatch("SAPI.SpFileStream")
            stream.Format.Type = 34  # SAFT16kHz16BitMono
            stream.Open(str(temporary), 3)  # SSFMCreateForWrite
            voice.AudioOutputStream = stream
            voice.Speak(text)
            stream.Close()
            voice.AudioOutputStream = None
    except ModuleNotFoundError:
        voice_name = _generate_with_powershell(temporary_items)

    for case, (temporary, _text) in zip(pending, temporary_items):
        output = SAMPLES_DIR / f"{case['id']}.wav"
        pcm, sample_rate = _read_pcm(temporary)
        pcm = _to_16k(pcm, sample_rate)
        pcm = _add_padding_and_noise(pcm, case)
        _write_pcm(output, pcm)
        temporary.unlink(missing_ok=True)
    print(f"[TTS] voice={voice_name} generated={len(pending)} total={len(cases)}")
    return len(pending)


if __name__ == "__main__":
    generate(force="--force" in sys.argv)
