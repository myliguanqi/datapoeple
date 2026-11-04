"""CueQueue（移植参考项目 live_main.CueQueue）：发声优先级队列。

- push(cue)：按 priority 入队，同级先到先说（FIFO）；coalesce 里的事件（欢迎、冷场、点赞……）同名只留最新一条，防堆积；
- get()：取最高优先级的一条；等待超过 max_stale_s（cue.max_stale_s 优先，否则 interaction.max_stale_s）的直接丢弃（别说 N 秒前的旧事）；
- peek_priority()：主持人判断要不要打断正在讲的讲品段；
- 从不打断由调用方保证（取出才说，说完再取下一条）。线程安全；时钟可注入（测试用假时钟）。
"""
from __future__ import annotations

import heapq
import itertools
import threading

from live.core.clock import SYSTEM_CLOCK, Clock
from live.interaction.cue import Cue

COALESCE_EVENTS = {"welcome_member", "welcome_back", "welcome_regular", "idle", "idle_interact", "viewer_surge",
                   "viewer_drain", "chat_reply", "like"}


class CueQueue:
    def __init__(self, cfg: dict, clock: Clock = SYSTEM_CLOCK, coalesce=COALESCE_EVENTS):
        self.cfg = cfg
        self.clock = clock
        self.coalesce = set(coalesce)
        self._heap: list = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._stopped = False
        self.on_drop = None                        # callback(cue, reason)：决策日志记录"过期丢弃 / 被合并"

    def stale_s(self, cue: Cue) -> float:
        return float(cue.max_stale_s if cue.max_stale_s else self.cfg["interaction"]["max_stale_s"])

    def push(self, cue: Cue) -> None:
        dropped = []
        with self._cv:
            if cue.event in self.coalesce and self._heap:
                keep = []
                for it in self._heap:
                    (dropped if it[3].event == cue.event else keep).append(it)
                self._heap = keep
                heapq.heapify(self._heap)
            heapq.heappush(self._heap, (-int(cue.priority), next(self._seq), self.clock.now(), cue))
            self._cv.notify_all()
        for it in dropped:
            self._notify_drop(it[3], "被同类新事件合并")

    def _purge_locked(self) -> list:
        if not self._heap:
            return []
        now = self.clock.now()
        dropped = [it[3] for it in self._heap if now - it[2] > self.stale_s(it[3])]
        if dropped:
            self._heap = [it for it in self._heap if now - it[2] <= self.stale_s(it[3])]
            heapq.heapify(self._heap)
        return dropped

    def get(self, timeout: float = 0.5) -> Cue | None:
        with self._cv:
            dropped = self._purge_locked()
            if not self._heap and not self._stopped and timeout > 0:
                self._cv.wait(timeout=timeout)
                dropped += self._purge_locked()
            cue = heapq.heappop(self._heap)[3] if (self._heap and not self._stopped) else None
        for c in dropped:
            self._notify_drop(c, "排队过期")
        return cue

    def peek_priority(self) -> int | None:
        with self._cv:
            dropped = self._purge_locked()
            top = -self._heap[0][0] if self._heap else None
        for c in dropped:
            self._notify_drop(c, "排队过期")
        return top

    def clear(self, reason: str = "清空") -> int:
        with self._cv:
            items, self._heap = self._heap, []
        for it in items:
            self._notify_drop(it[3], reason)
        return len(items)

    def snapshot(self) -> list:
        now = self.clock.now()
        with self._cv:
            items = sorted(self._heap)
        return [{"id": c.id, "event": c.event, "priority": c.priority, "kind": c.kind, "waited_s": round(now - ts, 1),
                 "stale_s": self.stale_s(c), "text": c.fallback_text, "reason": c.reason} for _, _, ts, c in items]

    def __len__(self) -> int:
        with self._cv:
            return len(self._heap)

    def wake(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify_all()

    def _notify_drop(self, cue: Cue, reason: str) -> None:
        if self.on_drop:
            try:
                self.on_drop(cue, reason)
            except Exception:
                pass
