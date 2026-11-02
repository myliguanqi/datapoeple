"""音频工具：ffmpeg 解码、响度归一化、淡入淡出、wav 读写。全部是 float32 单声道 [-1, 1]。"""
from __future__ import annotations

import subprocess
import wave

import numpy as np


def decode_audio_bytes(data: bytes, sr: int = 16000, ffmpeg: str = "ffmpeg") -> np.ndarray:
    """任意格式（wav/mp3/ogg/...）字节 -> float32 单声道 sr。"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
           "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(sr), "pipe:1"]
    proc = subprocess.run(cmd, input=data, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码失败: {proc.stderr.decode('utf-8', 'ignore')[-500:]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def load_audio_file(path: str, sr: int = 16000, ffmpeg: str = "ffmpeg") -> np.ndarray:
    with open(path, "rb") as f:
        return decode_audio_bytes(f.read(), sr=sr, ffmpeg=ffmpeg)


def write_wav(path: str, pcm: np.ndarray, sr: int = 16000) -> str:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(float_to_int16(pcm).tobytes())
    return path


def float_to_int16(pcm: np.ndarray) -> np.ndarray:
    return (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)


def db_to_amp(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def rms_db(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))
    return 20.0 * np.log10(max(rms, 1e-6))


def normalize_rms(pcm: np.ndarray, target_db: float | None, max_gain_db: float = 20.0,
                  peak: float = 0.98) -> np.ndarray:
    """把语音 RMS 拉到 target_db，增益上限 max_gain_db，最后做峰值限制。

    嘴型张合幅度跟输入音量强相关：TTS 太小声会显得嘴不动、太大声会夸张。
    """
    if target_db is None or pcm.size == 0:
        return pcm
    # 只用有声部分算 RMS，避免句首句尾静音拉低估计
    frame = 320
    n = pcm.size // frame * frame
    if n > 0:
        frames = pcm[:n].reshape(-1, frame)
        e = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
        voiced = frames[e > max(e.max() * 0.1, 1e-4)]
        ref = voiced.reshape(-1) if voiced.size else pcm
    else:
        ref = pcm
    gain_db = min(target_db - rms_db(ref), max_gain_db)
    out = pcm * db_to_amp(gain_db)
    m = float(np.max(np.abs(out))) if out.size else 0.0
    if m > peak:
        out = out * (peak / m)
    return out.astype(np.float32)


def apply_fade(pcm: np.ndarray, sr: int, fade_ms: float) -> np.ndarray:
    n = int(sr * fade_ms / 1000)
    if n <= 0 or pcm.size < 2 * n:
        return pcm
    out = pcm.copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out[:n] *= ramp
    out[-n:] *= ramp[::-1]
    return out
