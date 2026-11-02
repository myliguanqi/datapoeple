"""空闲音频源：没有 TTS 时喂给模型的"空音频"。

FlashHead 由音频驱动，空闲时也必须持续喂音频，模型才会持续生成帧（眨眼、微动），
画面不会停住。纯数字静音（全 0）在训练数据里几乎不存在，容易让动作发僵，
所以默认用极低电平的底噪，也可以换成真人录的呼吸声/环境声循环。
"""
from __future__ import annotations

import numpy as np

from live.audio_utils import db_to_amp, load_audio_file


class IdleSource:
    def __init__(self, cfg: dict):
        idle = cfg["idle"]
        self.sr = cfg["live"]["sample_rate"]
        self.mode = idle["mode"]
        self.rng = np.random.default_rng(1234)
        self.amp = db_to_amp(idle["noise_db"])
        self.loop = None
        self.pos = 0
        if self.mode == "file":
            if not idle.get("file"):
                raise ValueError("idle.mode=file 时必须配置 idle.file")
            pcm = load_audio_file(idle["file"], sr=self.sr, ffmpeg=cfg["server"]["ffmpeg"])
            if pcm.size == 0:
                raise ValueError(f"空闲音频为空: {idle['file']}")
            self.loop = (pcm * db_to_amp(idle.get("file_gain_db", 0.0))).astype(np.float32)
        elif self.mode not in ("noise", "zeros"):
            raise ValueError(f"未知 idle.mode: {self.mode}")

    def read(self, n: int) -> np.ndarray:
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        if self.mode == "zeros":
            return np.zeros(n, dtype=np.float32)
        if self.mode == "file":
            idx = (self.pos + np.arange(n)) % self.loop.size
            self.pos = int((self.pos + n) % self.loop.size)
            return self.loop[idx]
        # 轻微低通的白噪声，听感接近房间底噪；RMS 固定为 noise_db
        white = self.rng.standard_normal(n + 4)
        noise = np.convolve(white, np.ones(5) / 5.0, mode="valid")[:n]
        noise /= max(float(np.sqrt(np.mean(noise ** 2))), 1e-9)
        return (noise * self.amp).astype(np.float32)
