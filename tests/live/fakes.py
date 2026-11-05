"""测试替身：假会话（记录 begin/append/end/interrupt，由测试决定何时"说完"）等，P4/P5 的主持人测试共用。"""
import threading
import time


class FakeSession:
    state = "running"

    def __init__(self, bus):
        self.bus = bus
        self.reqs = {}
        self.order = []
        self.lock = threading.Lock()
        self.n = 0

    def begin_request(self, request_id=None, interruptible=False):
        with self.lock:
            self.n += 1
            rid = f"r{self.n}"
            self.reqs[rid] = {"texts": [], "open": True, "interrupted": False, "interruptible": interruptible, "done": False}
            self.order.append(rid)
        return rid

    def append(self, rid, text, speed=None, emotion=None):
        with self.lock:
            st = self.reqs[rid]
            if st["interrupted"] or not st["open"]:
                raise ValueError("已结束")
            st["texts"].append(text)

    def end_request(self, rid):
        with self.lock:
            self.reqs[rid]["open"] = False

    def request_active(self, rid):
        st = self.reqs.get(rid)
        return bool(st) and not st["done"] and not st["interrupted"]

    def interrupt(self):
        with self.lock:
            rids = [r for r, st in self.reqs.items() if not st["done"] and not st["interrupted"]]
            for r in rids:
                self.reqs[r]["interrupted"] = True
        self.bus.publish("session.event", {"type": "interrupted", "session_id": "default", "request_ids": rids})
        return {"request_ids": rids, "dropped_segments": [], "dropped_frames": 0}

    def finish(self, rid):
        self.reqs[rid]["done"] = True
        self.bus.publish("session.event", {"type": "speak_done", "session_id": "default", "request_id": rid})

    def text(self, rid):
        return "".join(self.reqs[rid]["texts"])

    def closed(self, rid):
        return not self.reqs[rid]["open"]


class Sessions:
    def __init__(self, s):
        self.s = s

    def get(self, sid):
        return self.s if sid == "default" else None


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False
