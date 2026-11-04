"""TTSService：会话和离线任务持有的 TTS 入口。

- 首选引擎 tts.engine，不可用（没配凭证 / 创建失败）时按 tts.fallback 顺序选第一个可用的（Web 顶栏显示"回退"）；
- 单句合成失败：tts.runtime_fallback 打开时用下一个可用引擎补上这一句，否则抛出（会话跳过这一句）；
- 计费引擎（火山）受 tts.daily_max_chars 保护，超过当天改用回退引擎并告警；
- 合成缓存（live/tts/cache.py）对所有引擎生效；
- Web 改 tts.engine / fallback / edge.* / volc.* / http.* 或重新加载 .env → rebuild()，正在用的会话下一句起生效。
"""
from __future__ import annotations

import threading
import time

import numpy as np
from loguru import logger

from live.tts import create_tts
from live.tts.base import TTSEngine
from live.tts.cache import TTSCache

_REBUILD_PREFIXES = ("tts.engine", "tts.fallback", "tts.edge.", "tts.volc.", "tts.http.")


class TTSAllFailed(RuntimeError):
    pass


class TTSService(TTSEngine):
    def __init__(self, cfg: dict, env=None, usage=None, factory=create_tts):
        super().__init__(cfg)
        self.env = env
        self.usage = usage
        self._factory = factory
        self._lock = threading.Lock()
        self.cache = TTSCache(cfg)
        self._engines: dict = {}
        self._errors: dict = {}
        self.active: TTSEngine | None = None
        self.warning: str | None = None
        self.stats = {"calls": 0, "failures": 0, "fallbacks": 0, "last_ms": None, "last_error": None,
                      "last_engine": None}
        self.rebuild()

    # ------------------------------------------------------------------ 引擎链
    def chain(self) -> list:
        primary = self.cfg["tts"]["engine"]
        return [primary] + [n for n in (self.cfg["tts"].get("fallback") or []) if n != primary]

    def _build(self, name: str) -> TTSEngine | None:
        try:
            return self._factory(self.cfg, self.env, engine=name)
        except Exception as e:
            self._errors[name] = f"创建 TTS 引擎 {name} 失败: {e}"
            logger.error(self._errors[name])
            return None

    def rebuild(self) -> None:
        engines, errors = {}, {}
        self._errors = errors
        for name in self.chain():
            eng = self._build(name)
            if eng is not None:
                engines[name] = eng
        active = next((engines[n] for n in self.chain() if n in engines and engines[n].available), None)
        if active is None:                      # 全都不可用：退到 mock，保证不断流
            from live.tts.mock import MockTTS
            active = MockTTS(self.cfg)
            engines.setdefault("mock", active)
        with self._lock:
            self._engines, self.active = engines, active
        requested = self.cfg["tts"]["engine"]
        if active.name != requested:
            logger.warning(f"TTS 首选 {requested} 不可用（{self.primary_reason() or '未知原因'}），回退到 {active.name}")
        else:
            logger.info(f"TTS 引擎: {active.name}")

    def primary_reason(self) -> str | None:
        requested = self.cfg["tts"]["engine"]
        if requested in self._errors:
            return self._errors[requested]
        eng = self._engines.get(requested)
        return eng.unavailable_reason if eng is not None else None

    @property
    def engine(self) -> TTSEngine:
        return self.active

    @property
    def name(self) -> str:   # type: ignore[override]
        return self.active.name if self.active else "none"

    @property
    def default_voice(self) -> str:   # type: ignore[override]
        return self.active.default_voice if self.active else self.cfg["tts"]["voice"]

    def _candidates(self) -> list:
        with self._lock:
            engines, active = dict(self._engines), self.active
        out = [active]
        for n in self.chain():
            e = engines.get(n)
            if e is not None and e is not active and e.available:
                out.append(e)
        if not any(e.name == "mock" for e in out):
            from live.tts.mock import MockTTS
            out.append(engines.get("mock") or MockTTS(self.cfg))
        return out

    def _over_limit(self, eng: TTSEngine, text: str) -> bool:
        limit = int(self.cfg["tts"].get("daily_max_chars") or 0)
        if not eng.billable or limit <= 0 or self.usage is None:
            return False
        return self.usage.today(f"tts_chars:{eng.name}") + len(text) > limit

    # ------------------------------------------------------------------ 合成
    def synthesize(self, text: str, voice: str | None = None, speed: float | None = None,
                   emotion: str | None = None) -> np.ndarray:
        speed = float(self.cfg["tts"]["speed"] if speed is None else speed)
        self.stats["calls"] += 1
        errors = []
        for i, eng in enumerate(self._candidates()):
            if i > 0 and not self.cfg["tts"].get("runtime_fallback", True):
                break
            if self._over_limit(eng, text):
                self.warning = f"{eng.name} 今日字数已达上限 {self.cfg['tts']['daily_max_chars']}，改用回退引擎"
                errors.append(self.warning)
                continue
            v = voice if i == 0 else None          # 指定的音色只属于当前引擎；回退引擎用它自己的默认音色
            key = self.cache.key(eng, text, v, speed, emotion) if self.cache.enabled else None
            if key:
                hit = self.cache.get(key)
                if hit is not None:
                    self._ok(eng, 0.0, i)
                    return hit
            t0 = time.perf_counter()
            try:
                pcm = eng.synthesize(text, v, speed=speed, emotion=emotion)
                if pcm is None or pcm.size == 0:
                    raise RuntimeError("没有返回音频")
            except Exception as e:
                msg = f"{eng.name}: {str(e).splitlines()[0][:200] if str(e) else type(e).__name__}"
                errors.append(msg)
                self.stats["failures"] += 1
                self.stats["last_error"] = msg
                logger.warning(f"TTS 合成失败 {msg}")
                continue
            if eng.billable and self.usage is not None:
                self.usage.add(f"tts_chars:{eng.name}", len(text))
            if key:
                self.cache.put(key, pcm, self.sr)
            self._ok(eng, time.perf_counter() - t0, i)
            return pcm
        raise TTSAllFailed("；".join(errors) or "没有可用的 TTS 引擎")

    def _ok(self, eng, seconds: float, index: int) -> None:
        if index > 0:
            self.stats["fallbacks"] += 1
        if seconds:
            self.stats["last_ms"] = round(seconds * 1000)
        self.stats["last_engine"] = eng.name

    def voices(self) -> list:
        return self.active.voices()

    def status(self) -> dict:
        requested = self.cfg["tts"]["engine"]
        usage = {}
        if self.usage is not None:
            usage = {k.split(":", 1)[1]: v for k, v in self.usage.snapshot().items() if k.startswith("tts_chars:")}
        return {"requested": requested, "engine": self.name, "available": self.active is not None,
                "error": self.primary_reason() if self.name != requested else None,
                "warning": self.warning, "chain": self.chain(), "cache": self.cache.stats(),
                "today_chars": usage, "daily_max_chars": self.cfg["tts"].get("daily_max_chars", 0), **self.stats}

    def apply_settings(self, settings, changed: set) -> None:
        if any(k.startswith(_REBUILD_PREFIXES) for k in changed):
            self.rebuild()
        if "tts.daily_max_chars" in changed:
            self.warning = None

    def on_env_changed(self, _topic=None, payload=None) -> None:
        keys = (payload or {}).get("keys") or []
        if not keys or any(k.startswith("VOLC_") for k in keys):
            self.rebuild()
