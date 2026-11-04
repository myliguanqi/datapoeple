"""回放录制的 JSONL（live/danmaku/recorder.py 写的格式：每行一个 LiveEvent.to_dict()）。

按原始时间间隔（除以 speed）重放，事件 ts 改成当前墙钟（保鲜窗口按墙钟算），原始 ts 放 data["_orig_ts"]。
"""
from __future__ import annotations

import json
import time

from loguru import logger

from live.core.events import LiveEvent
from live.danmaku.base import DanmakuSource


def load_jsonl(path: str) -> list:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(LiveEvent.from_dict(json.loads(line)))
            except Exception as e:
                logger.warning(f"回放文件第 {i} 行解析失败，跳过: {e}")
    return out


class ReplaySource(DanmakuSource):
    name = "replay"

    def __init__(self, opts, emit, status):
        super().__init__(opts, emit, status)
        self.path = opts.get("replay_file") or ""
        self.speed = max(0.01, float(opts.get("replay_speed", 1.0)))
        self.loop = bool(opts.get("replay_loop"))

    def run(self) -> None:
        if not self.path:
            self.status({"state": "error", "error": "没有设置回放文件（danmaku.replay.file）"})
            return
        try:
            events = load_jsonl(self.path)
        except OSError as e:
            self.status({"state": "error", "error": f"读不了回放文件: {e}"})
            return
        if not events:
            self.status({"state": "error", "error": "回放文件里没有事件"})
            return
        self.status({"state": "connected", "room_id": events[0].room_id, "live_id": "replay", "error": None,
                     "replay_total": len(events)})
        while not self.stopped:
            prev = events[0].ts
            for i, ev in enumerate(events):
                gap = max(0.0, (ev.ts - prev) / self.speed)
                prev = ev.ts
                if self._stop.wait(min(gap, 60.0)):
                    return
                out = LiveEvent.from_dict(ev.to_dict())
                out.data["_orig_ts"] = ev.ts
                out.ts = time.time()
                out.platform = "replay"
                self.emit(out)
                if i % 50 == 0:
                    self.status({"replay_pos": i + 1})
            if not self.loop:
                break
        self.status({"state": "finished", "replay_pos": len(events)})
        self._stop.wait()
