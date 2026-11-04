"""DanmakuManager（主进程）：管理弹幕子进程，把子进程产出的事件发布到事件总线。

- danmaku.enabled 打开 → 按 danmaku.source 起子进程（live/danmaku/process.py）；
- 子进程退出 / 崩溃 / 被杀 → 指数退避重启（danmaku.reconnect_delay_s 起步，翻倍，上限 60s；连上后清零）；
- danmaku.live_id / source / 连击超时 / 性别映射 / mock、replay 参数变了 → 立即重启子进程（热切换房间）；
- danmaku.sniff 变了 → 发控制消息给子进程，不断线；
- 发布：live.event（LiveEvent 对象）、danmaku.status（状态变化时 + 每 5 秒）。
模拟注入（Web「模拟」面板）不经过子进程，直接 inject() 发布。
"""
from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time
from collections import deque

from loguru import logger

from live.core.events import LiveEvent
from live.danmaku.cookie import cookie_path, cookie_status
from live.danmaku.process import child_main

_RESTART_KEYS = {"danmaku.enabled", "danmaku.source", "danmaku.live_id", "danmaku.combo_timeout_s",
                 "danmaku.gender_map"}
_RESTART_PREFIXES = ("danmaku.mock.", "danmaku.replay.")
MAX_BACKOFF_S = 60.0


class DanmakuManager:
    def __init__(self, cfg: dict, bus, env):
        self.cfg = cfg
        self.bus = bus
        self.env = env
        self.mp = mp.get_context("spawn")
        self._lock = threading.RLock()
        self.proc = None
        self.out_q = None
        self.ctrl_q = None
        self._want = False
        self._fail = 0
        self._next_spawn = 0.0
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self.recent: deque = deque(maxlen=int(cfg["danmaku"]["feed"]["max_items"]))
        self.counters: dict = {}
        self.last_online: int | None = None
        self._st = {"state": "stopped", "source": None, "live_id": None, "room_id": None, "logged_in": False,
                    "error": None, "pid": None, "events": 0, "restarts": 0, "last_event_ts": None,
                    "started_at": None, "heartbeat": None}
        self._last_pub = 0.0
        self._last_pub_state = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._want = bool(self.cfg["danmaku"]["enabled"])
        self._thread = threading.Thread(target=self._loop, name="danmaku-manager", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._want = False
        self._stop_evt.set()
        self._kill_child()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def restart(self) -> None:
        """立即重启子进程（改房间号、重新连接、更新 cookie 后）。"""
        with self._lock:
            self._kill_child()
            self._fail = 0
            self._next_spawn = 0.0
            self._want = bool(self.cfg["danmaku"]["enabled"])
            if not self._want:
                self._set({"state": "stopped", "pid": None})

    def apply_settings(self, settings, changed: set) -> None:
        if any(k in _RESTART_KEYS or k.startswith(_RESTART_PREFIXES) for k in changed):
            logger.info(f"弹幕设置变化 {sorted(changed)} → 重新连接")
            self.restart()
        if "danmaku.sniff" in changed:
            self._ctrl(("set_sniff", bool(self.cfg["danmaku"]["sniff"])))
        if "danmaku.feed.max_items" in changed:
            self.recent = deque(self.recent, maxlen=int(self.cfg["danmaku"]["feed"]["max_items"]))

    # ------------------------------------------------------------------ 子进程
    def _child_opts(self, source: str) -> dict:
        d = self.cfg["danmaku"]
        return {
            "live_id": d["live_id"], "gender_map": d["gender_map"], "combo_timeout_s": d["combo_timeout_s"],
            "sniff": d["sniff"], "cookie_file": cookie_path(self.env),
            "rate_per_min": d["mock"]["rate_per_min"], "mix": d["mock"]["mix"],
            "question_ratio": d["mock"]["question_ratio"], "female_ratio": d["mock"]["female_ratio"],
            "wealth_dist": d["mock"]["wealth_dist"], "seed": d["mock"].get("seed"),
            "replay_file": d["replay"]["file"], "replay_speed": d["replay"]["speed"], "replay_loop": d["replay"]["loop"],
        }

    def _spawn(self) -> None:
        source = self.cfg["danmaku"]["source"]
        out_q, ctrl_q = self.mp.Queue(), self.mp.Queue()
        proc = self.mp.Process(target=child_main, args=(source, self._child_opts(source), out_q, ctrl_q),
                               name=f"danmaku-{source}", daemon=True)
        proc.start()
        with self._lock:
            self.proc, self.out_q, self.ctrl_q = proc, out_q, ctrl_q
        live_id = self.cfg["danmaku"]["live_id"] if source == "douyin" else source
        self._set({"state": "starting", "source": source, "live_id": live_id, "room_id": None, "pid": proc.pid,
                   "error": None, "started_at": time.time(), "logged_in": False, "next_retry_at": None})
        logger.info(f"弹幕子进程启动 source={source} live_id={live_id} pid={proc.pid}")

    def _kill_child(self) -> None:
        with self._lock:
            proc, ctrl_q = self.proc, self.ctrl_q
            self.proc = self.out_q = self.ctrl_q = None
        if proc is None:
            return
        try:
            ctrl_q.put(("stop",))
        except Exception:
            pass
        proc.join(timeout=3)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2)
        logger.info(f"弹幕子进程已停止 pid={proc.pid}")

    def _ctrl(self, msg) -> None:
        with self._lock:
            q = self.ctrl_q
        if q is not None:
            try:
                q.put(msg)
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            with self._lock:
                proc, out_q = self.proc, self.out_q
                if self._want and proc is None and time.time() >= self._next_spawn:
                    try:
                        self._spawn()
                    except Exception as e:
                        logger.exception("弹幕子进程启动失败")
                        self._set({"state": "error", "error": f"子进程启动失败: {e}"})
                        self._fail += 1
                        self._next_spawn = time.time() + self._backoff()
                    continue
            if proc is None:
                self._maybe_publish_status()
                time.sleep(0.2)
                continue
            self._drain(out_q, block=0.2)
            if not proc.is_alive():
                self._drain(out_q, block=0)
                self._on_child_exit(proc)
            self._maybe_publish_status()

    def _drain(self, out_q, block: float) -> None:
        for i in range(500):
            try:
                kind, payload = out_q.get(timeout=block) if (i == 0 and block) else out_q.get_nowait()
            except (queue.Empty, EOFError, OSError, ValueError):
                return
            if out_q is not self.out_q:      # 已经换了新子进程：旧房间的消息丢掉
                return
            if kind == "event":
                self._publish_event(LiveEvent.from_dict(payload))
            elif kind == "status":
                self._set(payload)
                if payload.get("state") == "connected":
                    self._fail = 0
            elif kind == "exit":
                self._st["exit_reason"] = payload.get("reason")

    def _backoff(self) -> float:
        base = float(self.cfg["danmaku"]["reconnect_delay_s"])
        return min(MAX_BACKOFF_S, base * (2 ** max(0, self._fail - 1)))

    def _on_child_exit(self, proc) -> None:
        with self._lock:
            if self.proc is not proc:
                return
            self.proc = self.out_q = self.ctrl_q = None
            if not self._want:
                self._set({"state": "stopped", "pid": None})
                return
            self._fail += 1
            delay = self._backoff()
            self._next_spawn = time.time() + delay
            self._st["restarts"] = self._st.get("restarts", 0) + 1
        err = self._st.get("error")
        logger.warning(f"弹幕子进程退出（exitcode={proc.exitcode}），{delay:.0f}s 后重连" + (f"：{err}" if err else ""))
        self._set({"state": "backoff", "pid": None, "next_retry_at": self._next_spawn})

    # ------------------------------------------------------------------ 事件 / 状态
    def _publish_event(self, ev: LiveEvent) -> None:
        self.recent.append(ev)
        self.counters[ev.type] = self.counters.get(ev.type, 0) + 1
        self._st["events"] = self._st.get("events", 0) + 1 if ev.platform != "simulate" else self._st.get("events", 0)
        self._st["last_event_ts"] = ev.ts
        if ev.type == "room_stat" and ev.data.get("online") is not None:
            self.last_online = int(ev.data["online"])
        self.bus.publish("live.event", ev)

    def inject(self, ev: LiveEvent) -> LiveEvent:
        """模拟注入：不经过子进程，直接当作一条弹幕事件发布。"""
        self._publish_event(ev)
        return ev

    def _set(self, d: dict) -> None:
        events = d.pop("events", None)        # 子进程计数只作参考，主进程自己数
        if events is not None:
            self._st["child_events"] = events
        self._st.update(d)

    def _maybe_publish_status(self) -> None:
        now = time.time()
        state = (self._st.get("state"), self._st.get("error"), self._st.get("room_id"), self._st.get("pid"))
        if state != self._last_pub_state or now - self._last_pub >= 5:
            self._last_pub, self._last_pub_state = now, state
            self.bus.publish("danmaku.status", self.status())

    def status(self) -> dict:
        st = dict(self._st)
        st["enabled"] = bool(self.cfg["danmaku"]["enabled"])
        st["cookie"] = cookie_status(cookie_path(self.env))
        if st.get("next_retry_at"):
            st["next_retry_in"] = max(0.0, round(st["next_retry_at"] - time.time(), 1))
        st["counters"] = dict(self.counters)
        st["last_online"] = self.last_online
        return st

    def recent_events(self, limit: int = 100, types: set | None = None) -> list:
        items = [e for e in self.recent if not types or e.type in types]
        return [e.to_dict() for e in items[-limit:]]
