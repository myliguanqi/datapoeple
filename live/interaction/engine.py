"""InteractionEngine：订阅 live.event → 长期记忆 + RoomState → Director（每 interaction.tick_s 一拍，关键事件立即一拍）
→ 发布 interaction.cue → CueQueue（主持人从这里取）。

一拍最多出 3 条 cue（只有紧急优先级的会连着出：礼物 + 提问 + 关注同时到时不用等下一拍）。
决策日志：每条 cue 一行（事件 → 触发器 → 原因 → 台词来源 → 最终台词 → 合规命中 → 耗时 → 状态），主持人说的过程中
通过 update() 补全，每次变化发布 host.action 给 Web。
互动总开关 interaction.enabled / 控制台「暂停互动」host.pause_interaction：关掉时照常记状态和长期记忆，但不出 cue。
没接弹幕时不出 cue（否则"冷场话术"会让数字人自言自语）：is_live() 为真（弹幕已连接）或最近 LIVE_GRACE_S 秒内收到过事件（含模拟注入）才算在播。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from loguru import logger

from live.core.clock import SYSTEM_CLOCK, Clock
from live.core.events import LiveEvent
from live.interaction.cue import Cue
from live.interaction.director import Director
from live.interaction.intent import IntentClassifier
from live.interaction.queue import CueQueue
from live.interaction.state import RoomState


class InteractionEngine:
    LOG_MAX = 300
    LIVE_GRACE_S = 120.0

    def __init__(self, cfg: dict, bus, queue: CueQueue, memory=None, llm=None, clock: Clock = SYSTEM_CLOCK,
                 context=None, director: Director | None = None, start_thread: bool = True, is_live=None):
        self.cfg = cfg
        self.bus = bus
        self.queue = queue
        self.memory = memory
        self.clock = clock
        self.intent = IntentClassifier(cfg, llm)
        self.state = RoomState(cfg, self.intent, memory, clock)
        self.director = director or Director(cfg, clock, context=context)
        self.is_live = is_live
        self._last_event_ts = -1e9
        self.log: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self.queue.on_drop = lambda cue, reason: self.update(cue.id, status="dropped", note=reason)
        self.sub = bus.subscribe("live.event", self._on_event, queue_size=5000, name="interaction") if bus else None
        self._thread = None
        if start_thread:
            self._thread = threading.Thread(target=self._loop, name="interaction-tick", daemon=True)
            self._thread.start()

    @property
    def active(self) -> bool:
        return bool(self.cfg["interaction"]["enabled"]) and not bool(self.cfg["host"]["pause_interaction"])

    @property
    def on_air(self) -> bool:
        if self.is_live is None or self.clock.now() - self._last_event_ts <= self.LIVE_GRACE_S:
            return True
        try:
            return bool(self.is_live())
        except Exception:
            return False

    # ------------------------------------------------------------------ 事件
    def _on_event(self, _topic, ev: LiveEvent) -> None:
        self.on_event(ev)

    def on_event(self, ev: LiveEvent) -> None:
        if self.memory is not None:
            self.memory.record(ev)
        self._last_event_ts = self.clock.now()
        self.state.on_event(ev)
        if ev.type in RoomState.INSTANT_TYPES and self.active:
            self.tick()

    def _loop(self) -> None:
        while not self._stop.wait(max(0.2, float(self.cfg["interaction"]["tick_s"]))):
            try:
                self.tick()
            except Exception:
                logger.exception("互动引擎 tick 失败")
            if self.memory is not None:
                self.memory._maybe_flush()

    def tick(self) -> list:
        if not self.active or not self.on_air:
            return []
        out = []
        with self._tick_lock:
            snap = self.state.snapshot()
            urgent = int(self.cfg["interaction"]["urgent_priority"])
            for i in range(3):
                cue = self.director.tick(snap, decay=(i == 0))
                if cue is None:
                    break
                self._emit(cue)
                out.append(cue)
                if cue.priority < urgent:
                    break
        return out

    def _emit(self, cue: Cue) -> None:
        # 先来先答的意图：回应完从队列移除，下一条接着答；普通意图靠导演的消费标记防重复。
        if cue.intent and cue.first_come and cue.source and cue.source.get("msg_id"):
            self.state.consume_intent(cue.intent, cue.source["msg_id"])
        if cue.intent and cue.qa != "off" and self.memory is not None and cue.user and cue.user.get("user_id"):
            self.memory.note_question(cue.user["user_id"], cue.question)
        self.add_log(cue)
        self.bus.publish("interaction.cue", cue.to_dict())
        self.queue.push(cue)

    # ------------------------------------------------------------------ 决策日志
    def add_log(self, cue: Cue, status: str = "queued") -> dict:
        entry = {"id": cue.id, "ts": cue.created_at, "event": cue.event, "kind": cue.kind, "priority": cue.priority,
                 "reason": cue.reason, "source": cue.source, "status": status, "text": None, "text_source": None,
                 "fallback_text": cue.fallback_text, "compliance_hits": [], "compose_ms": None, "latency_ms": None,
                 "note": None}
        with self._lock:
            self.log[cue.id] = entry
            while len(self.log) > self.LOG_MAX:
                self.log.popitem(last=False)
        return entry

    def update(self, cue_id: str, **fields) -> dict | None:
        with self._lock:
            entry = self.log.get(cue_id)
            if entry is None:
                return None
            entry.update(fields)
            snap = dict(entry)
        if self.bus is not None:
            self.bus.publish("host.action", snap)
        return snap

    def recent_log(self, limit: int = 100) -> list:
        with self._lock:
            items = list(self.log.values())[-limit:]
        return [dict(x) for x in reversed(items)]

    def apply_settings(self, settings, changed: set) -> None:
        self.director.apply_settings(settings, changed)
        if "host.pause_interaction" in changed and self.cfg["host"]["pause_interaction"]:
            n = self.queue.clear("互动已暂停")
            if n:
                logger.info(f"互动已暂停，清掉队列里 {n} 条")

    def reset(self, reason: str = "切换人设") -> None:
        """切换人设：重建意图队列、清空旧弹幕/队列/决策日志，并重置导演的冷却和消费标记。"""
        with self._tick_lock:
            self.state.configure()
            self.state.reset()
            self.director.reset()
            self.queue.clear(reason)
            with self._lock:
                self.log.clear()
            self._last_event_ts = -1e9

    def reload_persona(self, reason: str = "人设已修改") -> None:
        """编辑当前人设：立即重读意图/触发器，丢掉已排队但按旧规则产出的 cue。"""
        with self._tick_lock:
            self.state.configure()
            self.director.reload_triggers()
            self.queue.clear(reason)

    def stop(self) -> None:
        self._stop.set()
        if self.sub is not None and self.bus is not None:
            self.bus.unsubscribe(self.sub)
        if self.memory is not None:
            self.memory.flush()

    def stats(self) -> dict:
        return {"active": self.active, "queue": len(self.queue), "counters": dict(self.state.counters),
                "memory": self.memory.stats() if self.memory is not None else None, "time": time.time()}
