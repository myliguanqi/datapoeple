"""可注入时钟：保鲜窗口、冷却、连击超时、讲品时长等时间逻辑都从这里取时间，测试换成 FakeClock，不靠 sleep。"""
from __future__ import annotations

import threading
import time


class Clock:
    def now(self) -> float:
        """墙钟秒（与弹幕事件 ts 同源）。"""
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def __call__(self) -> float:
        return self.now()


SYSTEM_CLOCK = Clock()


class FakeClock(Clock):
    """测试用：时间只在 advance()/sleep() 时前进。"""

    def __init__(self, start: float = 1_000_000.0):
        self.t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self.t

    def monotonic(self) -> float:
        return self.now()

    def advance(self, seconds: float) -> float:
        with self._lock:
            self.t += float(seconds)
            return self.t

    def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))
