"""TTS 引擎注册表。新增引擎：继承 TTSEngine，实现 synthesize()，再在这里 TTS_ENGINES.register 一行。

会话和离线任务不直接用这里的引擎，而是用 live/tts/service.py 的 TTSService（回退、缓存、用量保护、热切换）。
"""
from __future__ import annotations

from live.core.registry import Registry
from live.tts.base import TTSEngine

TTS_ENGINES = Registry("TTS 引擎")


@TTS_ENGINES.register("edge")
def _edge(cfg, env=None):
    from live.tts.edge import EdgeTTS
    return EdgeTTS(cfg)


@TTS_ENGINES.register("http")
def _http(cfg, env=None):
    from live.tts.http_tts import HttpTTS
    return HttpTTS(cfg)


@TTS_ENGINES.register("mock")
def _mock(cfg, env=None):
    from live.tts.mock import MockTTS
    return MockTTS(cfg)


@TTS_ENGINES.register("volc")
def _volc(cfg, env=None):
    from live.tts.volc import VolcTTS
    return VolcTTS(cfg, env)


def create_tts(cfg: dict, env=None, engine: str | None = None) -> TTSEngine:
    return TTS_ENGINES.create(engine or cfg["tts"]["engine"], cfg, env)


__all__ = ["TTSEngine", "TTS_ENGINES", "create_tts"]
