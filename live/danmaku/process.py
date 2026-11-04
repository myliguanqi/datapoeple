"""弹幕子进程入口（multiprocessing spawn）。每个直播间一个子进程。

为什么单独一个进程：vendor 的 AGPL 代码、对 websocket 库的全局 monkeypatch、JS 引擎（mini_racer / Node）都隔离在这里，
崩溃或卡死不影响直播主进程；换房间号 = 重启子进程。

  主 → 子  ctrl_q: ("stop",) | ("set_sniff", bool)
  子 → 主  out_q : ("event", LiveEvent.to_dict()) | ("status", {...增量状态...}) | ("exit", {"reason"})
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time


def child_main(source_name: str, opts: dict, out_q, ctrl_q) -> None:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level=opts.get("log_level", "INFO"),
               format=f"<green>{{time:HH:mm:ss}}</green> | {{level:<7}} | danmaku/{source_name} | {{message}}")

    from live.danmaku.base import SOURCES

    counter = {"events": 0}

    def emit(ev) -> None:
        counter["events"] += 1
        out_q.put(("event", ev.to_dict()))

    def status(d: dict) -> None:
        out_q.put(("status", dict(d)))

    try:
        src = SOURCES.create(source_name, opts, emit, status)
    except Exception as e:  # noqa: BLE001  创建失败（缺依赖等）把原因带回主进程
        logger.exception("弹幕源创建失败")
        out_q.put(("status", {"state": "error", "error": f"弹幕源创建失败: {e}"[:300]}))
        out_q.put(("exit", {"reason": "error"}))
        return

    def ctrl_loop():
        while True:
            try:
                msg = ctrl_q.get(timeout=0.5)
            except queue.Empty:
                if src.stopped:
                    return
                continue
            except (EOFError, OSError):
                src.stop()
                return
            if msg[0] == "stop":
                src.stop()
                return
            if msg[0] == "set_sniff":
                src.set_sniff(bool(msg[1]))

    def tick_loop():
        last_hb = 0.0
        while not src.stopped:
            try:
                src.tick()
            except Exception:
                logger.exception("tick 失败")
            now = time.monotonic()
            if now - last_hb >= 2.0:
                last_hb = now
                out_q.put(("status", {"heartbeat": time.time(), "events": counter["events"]}))
            time.sleep(0.5)

    threading.Thread(target=ctrl_loop, name="danmaku-ctrl", daemon=True).start()
    threading.Thread(target=tick_loop, name="danmaku-tick", daemon=True).start()
    reason = "ended"
    try:
        src.run()
    except Exception as e:  # noqa: BLE001
        logger.exception("弹幕源异常退出")
        out_q.put(("status", {"state": "error", "error": f"{type(e).__name__}: {e}"[:300]}))
        reason = "error"
    try:
        src.tick()                                   # 退出前把没结算的连击礼物结算掉
    except Exception:
        pass
    out_q.put(("status", {"events": counter["events"]}))
    out_q.put(("exit", {"reason": "stopped" if src.stopped else reason}))
    time.sleep(0.2)                                  # 让队列把最后几条送出去
