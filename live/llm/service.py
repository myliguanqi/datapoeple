"""LLMService：主进程持有的 LLM 入口（统计、每日上限、防复读记忆、热重建）。

- llm.provider 变了、.env 重新加载（LLM_* 变化）→ 重建客户端；
- chat() 失败抛 LLMError / LLMUnavailable，调用方（主持人写词）回退模板话术；
- 统计：调用次数、失败次数、最近耗时（流式记首个片段耗时 ttft 与总耗时）、最近错误，Web「数字人与系统」显示；
- llm.daily_max_calls > 0 时超过当天上限直接 LLMUnavailable；
- note_spoken()/recent_lines()：最近实际说出去的台词（不论来自 LLM 还是模板），写词时告诉 LLM 别复读。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Iterator

from loguru import logger

from live.llm import LLM_PROVIDERS
from live.llm.base import LLMClient, LLMError, LLMUnavailable


class LLMService:
    def __init__(self, cfg: dict, env, usage=None, client: LLMClient | None = None):
        self.cfg = cfg
        self.env = env
        self.usage = usage
        self._lock = threading.Lock()
        self._fixed = client is not None             # 测试注入的客户端：不随设置重建
        self.client: LLMClient = client or self._build()
        self.history: deque = deque(maxlen=max(1, int(cfg["llm"]["history_size"]) or 1))
        self.stats = {"calls": 0, "failures": 0, "last_ms": None, "last_ttft_ms": None, "last_error": None,
                      "last_at": None}

    def _build(self) -> LLMClient:
        try:
            return LLM_PROVIDERS.create(self.cfg["llm"]["provider"], self.cfg, self.env)
        except Exception as e:
            logger.error(f"创建 LLM 客户端失败: {e}")
            from live.llm.mock import MockLLM
            return MockLLM()

    def rebuild(self) -> None:
        if self._fixed:
            return
        with self._lock:
            self.client = self._build()
        logger.info(f"LLM: {self.client.name} 模型={self.client.model or '—'} "
                    f"{'可用' if self.available else self.reason}")

    # ------------------------------------------------------------------ 状态
    @property
    def available(self) -> bool:
        return self.reason is None

    @property
    def reason(self) -> str | None:
        r = self.client.unavailable_reason
        if r:
            return r
        limit = int(self.cfg["llm"].get("daily_max_calls") or 0)
        if limit and self.usage is not None and self.usage.today("llm_calls") >= limit:
            return f"今日 LLM 调用已达上限 {limit} 次"
        return None

    def status(self) -> dict:
        return {"provider": self.client.name, "model": self.client.model, "available": self.available,
                "reason": self.reason, "today_calls": self.usage.today("llm_calls") if self.usage else None,
                "daily_max_calls": self.cfg["llm"].get("daily_max_calls", 0), **self.stats}

    # ------------------------------------------------------------------ 调用
    def chat(self, messages: list, *, stream: bool | None = None, **params) -> str | Iterator[str]:
        reason = self.reason
        if reason:
            raise LLMUnavailable(reason)
        stream = bool(self.cfg["llm"]["stream"]) if stream is None else stream
        self.stats["calls"] += 1
        self.stats["last_at"] = time.time()
        if self.usage is not None:
            self.usage.add("llm_calls")
        t0 = time.perf_counter()
        try:
            out = self.client.chat(messages, stream=stream, **params)
        except Exception as e:
            self._fail(e)
            raise LLMError(str(e)) from None
        if not stream:
            self.stats["last_ms"] = round((time.perf_counter() - t0) * 1000)
            return out
        return self._wrap_stream(out, t0)

    def _wrap_stream(self, it, t0: float):
        first = True
        try:
            for piece in it:
                if first:
                    self.stats["last_ttft_ms"] = round((time.perf_counter() - t0) * 1000)
                    first = False
                yield piece
        except Exception as e:
            self._fail(e)
            raise LLMError(str(e)) from None
        self.stats["last_ms"] = round((time.perf_counter() - t0) * 1000)

    def _fail(self, e: Exception) -> None:
        self.stats["failures"] += 1
        self.stats["last_error"] = (str(e).splitlines()[0] if str(e) else type(e).__name__)[:200]
        logger.warning(f"LLM 调用失败，本句走兜底: {self.stats['last_error']}")

    # ------------------------------------------------------------------ 防复读
    def note_spoken(self, text: str) -> None:
        text = (text or "").strip()
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)

    def recent_lines(self) -> list:
        n = int(self.cfg["llm"]["history_size"])
        return list(self.history)[-n:] if n > 0 else []

    # ------------------------------------------------------------------ 热更新
    def apply_settings(self, settings, changed: set) -> None:
        if "llm.provider" in changed:
            self.rebuild()
        if "llm.history_size" in changed:
            self.history = deque(self.history, maxlen=max(1, int(self.cfg["llm"]["history_size"]) or 1))

    def on_env_changed(self, _topic=None, payload=None) -> None:
        keys = (payload or {}).get("keys") or []
        if not keys or any(k.startswith("LLM_") for k in keys):
            self.rebuild()
