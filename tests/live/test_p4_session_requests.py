"""P4：LiveSession 追加式请求（begin/append/end）与 speak_done / 打断语义。用假 worker（不起子进程、不要显卡），真实播放时钟。"""
import threading
import time
from concurrent.futures import Future

import numpy as np
import pytest

from live.session import LiveSession
from live.sinks import FrameSink
from live.tts.mock import MockTTS


class FakeWorker:
    index = 0
    gpu_id = 0

    def __init__(self):
        self.live_sessions = set()

    def call(self, method, priority=0, **kw):
        f = Future()
        if method == "open_session":
            f.set_result({"chunk_frames": 24, "chunk_samples": 15360, "chunk_seconds": 0.96, "motion_frames": 9,
                          "fps": 25, "sample_rate": 16000, "height": 16, "width": 16})
        elif method == "generate":
            time.sleep(0.02)
            f.set_result({"frames": np.zeros((24, 16, 16, 3), np.uint8)})
        else:
            f.set_result(None)
        return f


class Events(FrameSink):
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()

    def on_frame(self, frame):
        pass

    def on_event(self, ev):
        with self.lock:
            self.items.append((time.monotonic(), dict(ev)))

    def of(self, typ, rid=None):
        with self.lock:
            return [(t, e) for t, e in self.items if e["type"] == typ and (rid is None or e.get("request_id") == rid)]


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def sess(mock_cfg):
    s = LiveSession("t", mock_cfg, FakeWorker(), MockTTS(mock_cfg), model="lite", avatar="", seed=1, use_face_crop=False)
    ev = Events()
    s.add_sink(ev)
    s.start()
    yield s, ev
    s.close()


def test_plain_speak_done_once_after_last_segment(sess):
    s, ev = sess
    r = s.speak("第一句话。第二句话。")
    assert wait_for(lambda: ev.of("speak_done", r["request_id"]))
    time.sleep(0.3)
    done = ev.of("speak_done", r["request_id"])
    ends = [t for t, e in ev.of("playback") if e["state"] == "end" and e["request_id"] == r["request_id"]]
    assert len(done) == 1 and len(ends) == 2 and done[0][0] >= max(ends)


def test_append_request_done_only_after_end(sess):
    s, ev = sess
    rid = s.begin_request()
    s.append(rid, "先说一句。")
    # 第一句播完时请求还没 end，不能发 speak_done
    assert wait_for(lambda: any(e["state"] == "end" and e["request_id"] == rid for _, e in ev.of("playback")))
    time.sleep(0.2)
    assert ev.of("speak_done", rid) == [] and s.request_active(rid)
    s.append(rid, "再补一句。")
    s.end_request(rid)
    assert wait_for(lambda: ev.of("speak_done", rid))
    texts = [e["text"] for _, e in ev.of("playback") if e["state"] == "start" and e["request_id"] == rid]
    assert texts == ["先说一句。", "再补一句。"] and len(ev.of("speak_done", rid)) == 1
    assert not s.request_active(rid)


def test_end_after_everything_played_emits_immediately(sess):
    s, ev = sess
    rid = s.begin_request()
    s.append(rid, "短句。")
    assert wait_for(lambda: any(e["state"] == "end" and e["request_id"] == rid for _, e in ev.of("playback")))
    s.end_request(rid)
    assert wait_for(lambda: ev.of("speak_done", rid), 2)


def test_empty_request_done(sess):
    s, ev = sess
    rid = s.begin_request()
    s.end_request(rid)
    assert wait_for(lambda: ev.of("speak_done", rid), 2)


def test_interrupt_returns_request_and_no_done(sess):
    s, ev = sess
    rid = s.begin_request(interruptible=True)
    s.append(rid, "这是一段很长很长的讲解，" * 6)
    assert wait_for(lambda: ev.of("playback"))
    r = s.interrupt()
    assert rid in r["request_ids"]
    with pytest.raises(ValueError):
        s.append(rid, "被打断后不能再追加")
    time.sleep(2.5)
    assert ev.of("speak_done", rid) == []
    assert [e for _, e in ev.of("interrupted") if rid in e.get("request_ids", [])]
    r2 = s.speak("打断之后的新请求。")
    assert wait_for(lambda: ev.of("speak_done", r2["request_id"]))
