"""控制台推送（EventFeed）：订阅事件总线上给 Web 看的主题，环形缓冲最近 N 条，扇出给 /ws/console 的每个客户端。

推给客户端的消息：{"topic": "settings.changed", "ts": 1730000000.0, "data": {...}}
新连接的客户端先收到一条 {"topic": "hello", "data": {"recent": {topic: [...]}}}，用来补齐历史。

live.event 量很大（进场、点赞），推给 Web 前节流（LiveEventThrottle）：
  - 点赞：窗口内合并成一条 {"type": "like_batch", "count", "events", "users": [{nickname, count}]}；
  - 进场：普通进场合并成 {"type": "member_batch", "count", "users": [ViewerProfile…]}；
    财富等级 ≥ 15 或有粉丝团的单独推（值得看）；
  - 在线人数 room_stat：最多每 2 秒推一次最新值；
  - 连击中的礼物包（is_final=False）不推，只推结算后的那条。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time
from collections import deque

from live.core.bus import EventBus

FEED_TOPICS = ("settings.changed", "env.changed", "danmaku.status", "live.event", "interaction.cue",
               "host.action", "kb.ingest", "scene.changed", "tts.status")


class LiveEventThrottle:
    def __init__(self, window_s: float = 1.5, stat_interval_s: float = 2.0, notable_wealth: int = 15):
        self.window_s = window_s
        self.stat_interval_s = stat_interval_s
        self.notable_wealth = notable_wealth
        self._lock = threading.Lock()
        self._likes: dict | None = None
        self._members: dict | None = None
        self._stat = None
        self._stat_last = 0.0

    def offer(self, ev) -> dict | None:
        """返回立即推送的 data，或 None（攒着等 flush）。"""
        t = ev.type
        now = time.time()
        with self._lock:
            if t == "gift" and ev.data.get("is_final") is False:
                return None
            if t == "like":
                b = self._likes or {"type": "like_batch", "count": 0, "events": 0, "users": {}, "ts": now,
                                    "since": now, "room_id": ev.room_id, "platform": ev.platform}
                b["count"] += int(ev.data.get("count") or 1)
                b["events"] += 1
                name = ev.nickname or "某位观众"
                b["users"][name] = b["users"].get(name, 0) + int(ev.data.get("count") or 1)
                b["total"] = ev.data.get("total")
                b["ts"] = now
                self._likes = b
                return None
            if t == "member":
                u = ev.user
                if u and (u.wealth_level >= self.notable_wealth or u.fans_club):
                    return ev.to_dict()
                b = self._members or {"type": "member_batch", "count": 0, "users": [], "ts": now, "since": now,
                                      "room_id": ev.room_id, "platform": ev.platform}
                b["count"] += 1
                if u and len(b["users"]) < 12:
                    b["users"].append(u.to_dict())
                b["ts"] = now
                self._members = b
                return None
            if t == "room_stat":
                if now - self._stat_last >= self.stat_interval_s:
                    self._stat_last, self._stat = now, None
                    return ev.to_dict()
                self._stat = ev.to_dict()
                return None
        return ev.to_dict()

    def flush(self, force: bool = False) -> list:
        out = []
        now = time.time()
        with self._lock:
            for attr in ("_likes", "_members"):
                b = getattr(self, attr)
                if b and (force or now - b["since"] >= self.window_s):
                    if attr == "_likes":
                        b["users"] = [{"nickname": k, "count": v}
                                      for k, v in sorted(b["users"].items(), key=lambda kv: -kv[1])[:10]]
                    out.append(b)
                    setattr(self, attr, None)
            if self._stat and (force or now - self._stat_last >= self.stat_interval_s):
                out.append(self._stat)
                self._stat, self._stat_last = None, now
        return out


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
        self.throttle = LiveEventThrottle()
        self._stop = threading.Event()
        self._subs = [bus.subscribe(t, self._on_bus, queue_size=2000, name=f"feed:{t}") for t in FEED_TOPICS]
        threading.Thread(target=self._flush_loop, name="feed-flush", daemon=True).start()

    # ---- 总线工作线程 ----
    def _on_bus(self, topic: str, payload) -> None:
        msg = self.make_message(topic, payload)
        if msg is None:
            return
        self._push(topic, msg)

    def _push(self, topic: str, msg: dict) -> None:
        self.buffers.setdefault(topic, deque(maxlen=self.max_items)).append(msg)
        self.post(msg)

    def make_message(self, topic: str, payload) -> dict | None:
        """返回 None 表示不立即推送（live.event 的点赞/进场会攒起来由 _flush_loop 合并推送）。"""
        if topic == "live.event" and hasattr(payload, "type"):
            data = self.throttle.offer(payload)
            if data is None:
                return None
            return {"topic": topic, "ts": time.time(), "data": data}
        return {"topic": topic, "ts": time.time(), "data": to_jsonable(payload)}

    def _flush_loop(self) -> None:
        while not self._stop.wait(0.5):
            for data in self.throttle.flush():
                self._push("live.event", {"topic": "live.event", "ts": time.time(), "data": data})

    def flush_now(self) -> None:
        """测试用：立即推出攒着的合并消息。"""
        for data in self.throttle.flush(force=True):
            self._push("live.event", {"topic": "live.event", "ts": time.time(), "data": data})

    def close(self) -> None:
        self._stop.set()

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
