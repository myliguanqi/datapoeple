from __future__ import annotations

import numpy as np


class TTSEngine:
    """TTS 接口：一次合成一句，返回 float32 单声道、采样率 = cfg.live.sample_rate（16k）。

    synthesize 在会话的 TTS 线程里同步调用，可以阻塞；长文本的分句由上层完成，
    所以这里只需要处理一句话。
    """

    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sr = cfg["live"]["sample_rate"]
        self.ffmpeg = cfg["server"]["ffmpeg"]

    @property
    def default_voice(self) -> str:
        return self.cfg["tts"]["voice"]          # 每次读 cfg：Web 改音色立即生效

    def synthesize(self, text: str, voice: str | None = None) -> np.ndarray:
        raise NotImplementedError

    def voices(self) -> list:
        """返回 [{"id": ..., "name": ...}]，给前端下拉框用。"""
        return [{"id": self.default_voice, "name": self.default_voice}]
