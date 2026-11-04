"""假 TTS：不联网、不依赖 ffmpeg，生成类似说话节奏的合成音（按音节调幅的谐波）。

用来离线联调整条链路；接真模型时它也能驱动嘴型（会像在"咿咿呀呀"）。
"""
from __future__ import annotations

import numpy as np

from live.tts.base import TTSEngine


class MockTTS(TTSEngine):
    name = "mock"

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0,
                   emotion: str | None = None) -> np.ndarray:
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        cjk = sum(1 for c in text if "一" <= c <= "鿿")
        dur = max(0.4, cjk * 0.22 + (len(text) - cjk) * 0.07) / max(0.5, float(speed or 1.0))
        sr = self.sr
        t = np.arange(int(dur * sr)) / sr
        seed = sum(map(ord, text)) % 1000
        f0 = 150 + 60 * np.sin(2 * np.pi * 0.7 * t + seed)   # 语调起伏
        phase = 2 * np.pi * np.cumsum(f0) / sr
        voiced = sum(np.sin(k * phase) / k for k in range(1, 6))
        syll = np.clip(np.sin(2 * np.pi * 4.0 * t + seed) * 0.8 + 0.4, 0, 1) ** 1.5  # 约每秒 4 个音节
        pcm = voiced * syll * 0.3 + np.random.default_rng(seed).standard_normal(t.size) * 0.01
        return pcm.astype(np.float32)

    def voices(self) -> list:
        return [{"id": "mock", "name": "mock（合成音）"}]
