"""每日用量计数（计费保护）：火山 TTS 字数、LLM 调用次数……按自然日累计，落盘 live_data/usage.json，重启不清零。

    usage.add("tts_chars:volc", len(text));  usage.today("llm_calls")
"""
from __future__ import annotations

import json
import os
import threading
import time


class UsageMeter:
    def __init__(self, path: str | None, keep_days: int = 31, flush_interval_s: float = 5.0):
        self.path = path
        self.keep_days = keep_days
        self.flush_interval_s = flush_interval_s
        self._lock = threading.Lock()
        self._data: dict = {}
        self._dirty = False
        self._last_flush = 0.0
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._data = json.load(f) or {}
            except (OSError, ValueError):
                self._data = {}

    @staticmethod
    def day() -> str:
        return time.strftime("%Y-%m-%d")

    def add(self, key: str, n: int = 1) -> int:
        with self._lock:
            d = self._data.setdefault(self.day(), {})
            d[key] = int(d.get(key, 0)) + int(n)
            self._dirty = True
            v = d[key]
        self._maybe_flush()
        return v

    def today(self, key: str) -> int:
        with self._lock:
            return int(self._data.get(self.day(), {}).get(key, 0))

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data.get(self.day(), {}))

    def _maybe_flush(self) -> None:
        if time.time() - self._last_flush >= self.flush_interval_s:
            self.flush()

    def flush(self) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            days = sorted(self._data)[-self.keep_days:]
            data = {k: self._data[k] for k in days}
            self._dirty = False
            self._last_flush = time.time()
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass
