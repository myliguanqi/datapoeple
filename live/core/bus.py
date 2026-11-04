"""进程内事件总线（线程安全 pub/sub）。

    sub = bus.subscribe("live.*", handler, queue_size=1000, name="feed")   # handler(topic, payload)
    bus.publish("live.event", event)

- 每个订阅者一个有界队列 + 一个工作线程：发布永不阻塞；慢订阅者队列满了丢最旧的并计数。
- handler 抛异常只记日志和计数，不影响其他订阅者，也不影响后续消息。
- 主题匹配用 fnmatch：精确名、"live.*"、"*"。
- 测试里用 bus.flush() 等所有订阅者处理完。
"""
from __future__ import annotations

import fnmatch
import itertools
import threading
import time
from collections import deque
from typing import Any, Callable

from loguru import logger

Handler = Callable[[str, Any], None]


class Subscription:
    def __init__(self, bus: "EventBus", pattern: str, handler: Handler, queue_size: int, name: str):
        self.bus = bus
        self.pattern = pattern
        self.handler = handler
        self.name = name
        self.queue: deque = deque(maxlen=max(1, int(queue_size)))
        self.delivered = 0
        self.dropped = 0
        self.errors = 0
        self._busy = False
        self._closed = False
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._run, name=f"bus-{name}", daemon=True)
        self._thread.start()

    def matches(self, topic: str) -> bool:
        return self.pattern == topic or fnmatch.fnmatchcase(topic, self.pattern)

    def offer(self, topic: str, payload: Any) -> None:
        with self._cv:
            if self._closed:
                return
            if len(self.queue) == self.queue.maxlen:
                self.dropped += 1                    # deque(maxlen) 会自动挤掉最旧的一条
            self.queue.append((topic, payload))
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self.queue and not self._closed:
                    self._cv.wait()
                if self._closed and not self.queue:
                    return
                topic, payload = self.queue.popleft()
                self._busy = True
            try:
                self.handler(topic, payload)
                self.delivered += 1
            except Exception:
                self.errors += 1
                logger.exception(f"[bus] 订阅者 {self.name} 处理 {topic} 失败")
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def idle(self) -> bool:
        with self._cv:
            return not self.queue and not self._busy

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self.queue.clear()
            self._cv.notify_all()

    def stats(self) -> dict:
        return {"name": self.name, "pattern": self.pattern, "queued": len(self.queue),
                "delivered": self.delivered, "dropped": self.dropped, "errors": self.errors}


class EventBus:
    def __init__(self):
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.published = 0

    def subscribe(self, pattern: str, handler: Handler, *, queue_size: int = 1000,
                  name: str | None = None) -> Subscription:
        sub = Subscription(self, pattern, handler, queue_size, name or f"sub{next(self._ids)}")
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        sub.close()

    def publish(self, topic: str, payload: Any = None) -> int:
        """非阻塞广播，返回匹配到的订阅者数。"""
        with self._lock:
            subs = [s for s in self._subs if s.matches(topic)]
        self.published += 1
        for s in subs:
            s.offer(topic, payload)
        return len(subs)

    def flush(self, timeout: float = 2.0) -> bool:
        """等所有订阅者把已收到的消息处理完（测试 / 关停用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                subs = list(self._subs)
            if all(s.idle() for s in subs):
                return True
            time.sleep(0.005)
        return False

    def stats(self) -> list:
        with self._lock:
            return [s.stats() for s in self._subs]

    def close(self) -> None:
        with self._lock:
            subs, self._subs = self._subs, []
        for s in subs:
            s.close()
