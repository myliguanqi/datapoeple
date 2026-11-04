"""弹幕录制：danmaku.record 打开时，把 live.event 逐行写进 live_data/events/YYYYMMDD_<房间>.jsonl（可被 replay 回放）。

录制内容含观众昵称等信息，默认关闭；测试夹具提交前要去标识化。回放产生的事件不再录制。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from loguru import logger

from live.core.events import LiveEvent


class Recorder:
    def __init__(self, cfg: dict, bus):
        self.cfg = cfg
        self.dir = os.path.join(cfg["server"]["data_dir"], "events")
        self._files: dict = {}
        self._lock = threading.Lock()
        self.written = 0
        self.sub = bus.subscribe("live.event", self._on_event, queue_size=5000, name="recorder")

    def _path(self, ev: LiveEvent) -> str:
        room = re.sub(r"[^A-Za-z0-9_\-]", "_", ev.room_id or ev.platform or "room")[:40]
        return os.path.join(self.dir, f"{time.strftime('%Y%m%d', time.localtime(ev.ts))}_{room}.jsonl")

    def _on_event(self, _topic, ev: LiveEvent) -> None:
        if not self.cfg["danmaku"]["record"] or ev.platform in ("replay",):
            return
        path = self._path(ev)
        with self._lock:
            f = self._files.get(path)
            if f is None:
                os.makedirs(self.dir, exist_ok=True)
                f = self._files[path] = open(path, "a", encoding="utf-8")
                logger.info(f"弹幕录制 → {path}")
            f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
            self.written += 1

    def apply_settings(self, settings, changed: set) -> None:
        if "danmaku.record" in changed and not self.cfg["danmaku"]["record"]:
            self.close()

    def close(self) -> None:
        with self._lock:
            for f in self._files.values():
                try:
                    f.close()
                except OSError:
                    pass
            self._files.clear()

    def list_files(self) -> list:
        try:
            names = sorted((n for n in os.listdir(self.dir) if n.endswith(".jsonl")), reverse=True)
        except FileNotFoundError:
            return []
        out = []
        for n in names:
            p = os.path.join(self.dir, n)
            out.append({"name": n, "path": p, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        return out
