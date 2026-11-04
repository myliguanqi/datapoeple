"""控制台推送（EventFeed）：订阅事件总线上给 Web 看的主题，环形缓冲最近 N 条，扇出给 /ws/console 的每个客户端。

推给客户端的消息：{"topic": "settings.changed", "ts": 1730000000.0, "data": {...}}
新连接的客户端先收到一条 {"topic": "hello", "data": {"recent": {topic: [...]}}}，用来补齐历史。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections import deque

from live.core.bus import EventBus

FEED_TOPICS = ("settings.changed", "env.changed", "danmaku.status", "live.event", "interaction.cue",
               "host.action", "kb.ingest", "scene.changed", "tts.status")


def to_jsonable(payload):
    if hasattr(payload, "to_dict"):
        return payload.to_dict()
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        return dataclasses.asdict(payload)
    return payload


class EventFeed:
    def __init__(self, bus: EventBus, loop: asyncio.AbstractEventLoop, max_items: int = 500,
                 client_queue: int = 1000):
        self.loop = loop
        self.max_items = max_items
        self.client_queue = client_queue
        self.buffers: dict = {t: deque(maxlen=max_items) for t in FEED_TOPICS}
        self.clients: set = set()            # 只在事件循环线程里增删
        self.dropped = 0
        self._subs = [bus.subscribe(t, self._on_bus, queue_size=2000, name=f"feed:{t}") for t in FEED_TOPICS]

    # ---- 总线工作线程 ----
    def _on_bus(self, topic: str, payload) -> None:
        msg = self.make_message(topic, payload)
        if msg is None:
            return
        self.buffers.setdefault(topic, deque(maxlen=self.max_items)).append(msg)
        self.post(msg)

    def make_message(self, topic: str, payload) -> dict | None:
        """子类/后续阶段可在这里做节流合并（点赞合并、进场折叠）。返回 None 表示不推送。"""
        return {"topic": topic, "ts": time.time(), "data": to_jsonable(payload)}

    def post(self, msg: dict) -> None:
        text = json.dumps(msg, ensure_ascii=False, default=str)
        try:
            self.loop.call_soon_threadsafe(self._fanout, text)
        except RuntimeError:                  # 事件循环已关闭
            pass

    def _fanout(self, text: str) -> None:
        for q in list(self.clients):
            if q.qsize() >= self.client_queue:
                try:
                    q.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(text)

    # ---- 事件循环线程 ----
    def add_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.clients.add(q)
        return q

    def remove_client(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def recent(self, topic: str, limit: int = 100) -> list:
        items = list(self.buffers.get(topic, ()))
        return items[-limit:] if limit else items

    def snapshot(self, limit: int = 50) -> dict:
        return {t: self.recent(t, limit) for t in self.buffers}

    def set_max_items(self, n: int) -> None:
        n = max(10, int(n))
        if n == self.max_items:
            return
        self.max_items = n
        self.buffers = {t: deque(b, maxlen=n) for t, b in self.buffers.items()}
