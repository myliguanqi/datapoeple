"""TTSService：会话和离线任务持有的 TTS 入口，内部引擎可热切换。

Web 改 tts.engine / tts.edge.* 等 → settings.changed → apply_settings 重建内部引擎，
正在用它的会话无感（下一句起用新引擎）。新引擎创建失败时保留旧引擎并在 status() 里报错。
"""
from __future__ import annotations

import threading
import time

import numpy as np
from loguru import logger

from live.tts import create_tts
from live.tts.base import TTSEngine


class TTSService(TTSEngine):
    def __init__(self, cfg: dict, factory=create_tts):
        super().__init__(cfg)
        self._factory = factory
        self._lock = threading.Lock()
        self.engine: TTSEngine | None = None
        self.error: str | None = None
        self.stats = {"calls": 0, "failures": 0, "last_ms": None, "last_error": None}
        self.rebuild()

    @property
    def name(self) -> str:   # type: ignore[override]
        return self.engine.name if self.engine else "none"

    def rebuild(self) -> None:
        requested = self.cfg["tts"]["engine"]
        try:
            eng = self._factory(self.cfg)
            err = None
        except Exception as e:
            eng, err = None, f"创建 TTS 引擎 {requested} 失败: {e}"
            logger.error(err)
        with self._lock:
            if eng is not None:
                self.engine = eng
            elif self.engine is None:          # 第一次就失败：退到 mock，保证不断流
                from live.tts.mock import MockTTS
                self.engine = MockTTS(self.cfg)
            self.error = err
        logger.info(f"TTS 引擎: {self.name}" + (f"（{err}）" if err else ""))

    def synthesize(self, text: str, voice: str | None = None) -> np.ndarray:
        eng = self.engine
        t0 = time.perf_counter()
        self.stats["calls"] += 1
        try:
            pcm = eng.synthesize(text, voice)
        except Exception as e:
            self.stats["failures"] += 1
            self.stats["last_error"] = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            raise
        self.stats["last_ms"] = round((time.perf_counter() - t0) * 1000)
        return pcm

    def voices(self) -> list:
        return self.engine.voices()

    def status(self) -> dict:
        return {"requested": self.cfg["tts"]["engine"], "engine": self.name, "error": self.error,
                "available": self.engine is not None and self.error is None, **self.stats}

    def apply_settings(self, settings, changed: set) -> None:
        if any(k == "tts.engine" or k.startswith(("tts.edge.",)) for k in changed):
            self.rebuild()
