"""TTS 引擎工厂。新增引擎：继承 TTSEngine，实现 synthesize()，再在 create_tts 里注册。"""
from __future__ import annotations

from live.tts.base import TTSEngine


def create_tts(cfg: dict) -> TTSEngine:
    engine = cfg["tts"]["engine"]
    if engine == "edge":
        from live.tts.edge import EdgeTTS
        return EdgeTTS(cfg)
    if engine == "http":
        from live.tts.http_tts import HttpTTS
        return HttpTTS(cfg)
    if engine == "mock":
        from live.tts.mock import MockTTS
        return MockTTS(cfg)
    raise ValueError(f"未知 tts.engine: {engine}")


__all__ = ["TTSEngine", "create_tts"]
