"""P4：主持人编排（假会话：记录 begin/append/end/interrupt，由测试决定何时"说完"）。"""
import threading
import time

import pytest

from live.core.bus import EventBus
from live.host.composer import Composer
from live.host.host import Host
from live.interaction.compliance import Compliance
from live.interaction.cue import Cue
from live.interaction.queue import CueQueue
from live.llm.mock import MockLLM
from live.llm.service import LLMService
from live.settings.env import Env


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


class Script:
    """假讲品：两段讲解稿，记录打断（断点续讲）与完成。"""

    def __init__(self):
        self.segments = ["第一段：这款保温杯采用双层真空设计。", "第二段：容量五百毫升，适合通勤。"]
        self.i = 0
        self.interrupted = []
        self.done = []

    def active(self):
        return self.i < len(self.segments)

    def next_segment(self):
        if not self.active():
            return None
        return Cue(event="script", kind="product", priority=3, fallback_text=self.segments[self.i], interruptible=True,
                   reason=f"讲品第{self.i + 1}段")

    def on_interrupted(self, cue):
        self.interrupted.append(cue.fallback_text)          # 不前进：下次从这一段重讲（断点续讲）

    def on_done(self, cue):
        self.done.append(cue.fallback_text)
        self.i += 1


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def rig(mock_cfg, tmp_path):
    bus = EventBus()
    sess = FakeSession(bus)
    q = CueQueue(mock_cfg)
    mock = MockLLM(rules=[("多少钱", "这款到手价九十九元。点开小黄车就能看到！")], default="谢谢支持，喜欢的点点关注！")
    (tmp_path / "e.env").write_text("", encoding="utf-8")
    llm = LLMService(mock_cfg, Env(str(tmp_path / "e.env")), client=mock)
    composer = Composer(mock_cfg, llm, Compliance(mock_cfg))
    host = Host(mock_cfg, bus, Sessions(sess), q, composer)
    yield host, sess, q, mock, mock_cfg
    host.stop()
    bus.close()


def cue(event="gift_m", priority=60, text="感谢小王哥的玫瑰！", use_llm=False, **kw):
    return Cue(event=event, priority=priority, fallback_text=text, use_llm=use_llm, **kw)


def test_template_cue_spoken_then_next(rig):
    host, sess, q, _, _ = rig
    q.push(cue(text="感谢小王哥送的3个玫瑰！"))
    q.push(cue(event="follow", priority=50, text="感谢小美姐的关注！"))
    assert wait_for(lambda: len(sess.order) == 1 and sess.closed("r1"))
    assert sess.text("r1") == "感谢小王哥送的3个玫瑰！"
    time.sleep(0.2)
    assert len(sess.order) == 1                               # 上一句没说完，不会开始下一句
    sess.finish("r1")
    assert wait_for(lambda: len(sess.order) == 2 and sess.closed("r2"))
    assert sess.text("r2") == "感谢小美姐的关注！"


def test_high_priority_interrupts_script_then_resume(rig):
    host, sess, q, _, _ = rig
    script = Script()
    host.script = script
    assert wait_for(lambda: len(sess.order) == 1 and sess.closed("r1"))
    assert sess.reqs["r1"]["interruptible"] and "第一段" in sess.text("r1")      # 序数词「第一段」不被合规误伤
    q.push(cue(event="gift_l", priority=63, text="感谢大哥的浪漫花束！"))
    assert wait_for(lambda: sess.reqs["r1"]["interrupted"])
    assert wait_for(lambda: len(sess.order) == 2 and sess.closed("r2"))
    assert sess.text("r2") == "感谢大哥的浪漫花束！"
    assert script.interrupted and script.i == 0               # 断点：第一段没讲完，不前进
    sess.finish("r2")
    assert wait_for(lambda: len(sess.order) == 3 and sess.closed("r3"))
    assert "第一段" in sess.text("r3")                         # 续讲
    sess.finish("r3")
    assert wait_for(lambda: len(sess.order) == 4 and sess.closed("r4"))
    assert "第二段" in sess.text("r4") and script.done[0].startswith("第一段")


def test_low_priority_does_not_interrupt_and_is_skipped_during_script(rig):
    host, sess, q, _, cfg = rig
    host.script = Script()
    assert wait_for(lambda: len(sess.order) == 1)
    q.push(cue(event="welcome_member", priority=20, text="欢迎小李！"))   # < interrupt_priority 50：不打断
    time.sleep(0.3)
    assert not sess.reqs["r1"]["interrupted"]
    q.push(cue(event="idle", priority=0, text="冷场话术"))
    sess.finish("r1")
    assert wait_for(lambda: len(sess.order) == 2)
    assert sess.text("r2") == "欢迎小李！"                      # 讲完一段先回应排队的互动
    sess.finish("r2")
    assert wait_for(lambda: len(sess.order) == 3 and sess.closed("r3"))
    assert "第二段" in sess.text("r3")
    assert all("冷场话术" not in sess.text(r) for r in sess.order)   # 低于 min_priority_during_script 的跳过


def test_llm_stream_sentences_and_history(rig):
    host, sess, q, mock, cfg = rig
    q.push(cue(event="product_question", priority=52, text="具体看商品详情哦！", use_llm=True, question="这个多少钱",
               situation={"question": "这个多少钱"}))
    assert wait_for(lambda: len(sess.order) == 1 and sess.closed("r1"))
    assert sess.reqs["r1"]["texts"] == ["这款到手价九十九元。", "点开小黄车就能看到！"]
    sys_msg, user_msg = mock.calls[-1]["messages"]
    assert "小美" in sys_msg["content"] and "合规红线" in sys_msg["content"]
    assert '"max_chars": 70' in user_msg["content"] and "这个多少钱" in user_msg["content"]
    assert host.composer.llm.recent_lines()[-1] == "这款到手价九十九元。点开小黄车就能看到！"


def test_llm_timeout_falls_back(rig):
    host, sess, q, mock, cfg = rig
    mock.delay_s = 2.0
    cfg["host"]["compose_timeout_s"] = 0.3
    q.push(cue(event="chat_reply", priority=47, text="谢谢小王哥捧场！", use_llm=True))
    assert wait_for(lambda: len(sess.order) == 1 and sess.closed("r1"), 3)
    assert sess.text("r1") == "谢谢小王哥捧场！"


def test_llm_unavailable_uses_template(rig):
    host, sess, q, mock, cfg = rig
    cfg["host"]["use_llm"] = False
    q.push(cue(event="chat_reply", priority=47, text="模板话术！", use_llm=True))
    assert wait_for(lambda: len(sess.order) == 1 and sess.closed("r1"))
    assert sess.text("r1") == "模板话术！" and mock.calls == []


def test_manual_say_priority_compliance_and_interrupt(rig):
    host, sess, q, _, cfg = rig
    q.push(cue(event="welcome_member", priority=20, text="欢迎！"))
    assert wait_for(lambda: len(sess.order) == 1)
    host.say("今天全网最低价，百分百正品", interrupt=True)
    assert wait_for(lambda: sess.reqs["r1"]["interrupted"])
    assert wait_for(lambda: len(sess.order) == 2 and sess.closed("r2"))
    assert sess.text("r2") == "今天很划算价，正品"               # 人工插话也过合规
    with pytest.raises(ValueError):
        host.say("   ")


def test_pause_interaction_drops_interaction_cues(rig):
    host, sess, q, _, cfg = rig
    cfg["host"]["pause_interaction"] = True
    q.push(cue(text="不应该说"))
    time.sleep(0.4)
    assert sess.order == []
    host.say("人工插话照常")
    assert wait_for(lambda: len(sess.order) == 1 and sess.text("r1") == "人工插话照常")


def test_speak_timeout_releases(rig):
    host, sess, q, _, cfg = rig
    cfg["host"]["speak_timeout_s"] = 0.5
    q.push(cue(text="第一句"))
    q.push(cue(text="第二句"))
    assert wait_for(lambda: len(sess.order) == 2, 3)          # 没等到 speak_done 也会超时放行


def test_status_shape(rig):
    host, sess, q, _, _ = rig
    q.push(cue(text="正在说的话"))
    assert wait_for(lambda: host.status()["current"] is not None)
    st = host.status()
    assert st["current"]["event"] == "gift_m" and st["session_ok"] and st["queue_size"] == 0
