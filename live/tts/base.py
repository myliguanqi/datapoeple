from __future__ import annotations

import numpy as np


class TTSEngine:
    """TTS 接口：一次合成一句，返回 float32 单声道、采样率 = cfg.live.sample_rate（16k）。

    synthesize 在会话的 TTS 线程里同步调用，可以阻塞；长文本的分句由上层完成，
    所以这里只需要处理一句话。speed 是语速比（1.0 = 正常），emotion 是中间情绪标签（引擎不支持就忽略）。
    合成失败请抛异常（上层按 tts.fallback 回退或跳过这一句）。
    """

    name = "base"
    billable = False           # 按调用计费的引擎设 True（受 tts.daily_max_chars 保护）

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sr = cfg["live"]["sample_rate"]
        self.ffmpeg = cfg["server"]["ffmpeg"]

    @property
    def default_voice(self) -> str:
        return self.cfg["tts"]["voice"]          # 每次读 cfg：Web 改音色立即生效

    @property
    def available(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str | None:
        return None

    @property
    def cache_signature(self) -> str:
        """进缓存键的引擎签名：换引擎 / 关键参数时旧缓存自动失效。"""
        return self.name

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0,
                   emotion: str | None = None) -> np.ndarray:
        raise NotImplementedError

    def voices(self) -> list:
        """返回 [{"id": ..., "name": ...}]，给前端下拉框用。"""
        return [{"id": self.default_voice, "name": self.default_voice}]
