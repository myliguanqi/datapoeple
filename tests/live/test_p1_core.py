"""P1：事件模型、事件总线、注册表、假时钟。"""
import json
import threading
import time

from live.core.bus import EventBus
from live.core.clock import FakeClock
from live.core.events import LiveEvent, ViewerProfile
from live.core.registry import Registry


def test_live_event_roundtrip_json():
    ev = LiveEvent(type="gift", platform="douyin", room_id="r1", ts=123.5, msg_id="m1",
                   user=ViewerProfile(user_id="u1", nickname="小王", gender="male", wealth_level=12,
                                      fans_club={"name": "美美团", "level": 5, "status": 1}, badges=["a"]),
                   data={"gift_name": "玫瑰", "count": 3, "is_final": True}, in_room={"consume": 30})
    d = json.loads(json.dumps(ev.to_dict(), ensure_ascii=False))
    back = LiveEvent.from_dict(d)
    assert back == ev
    assert back.user.fans_level == 5 and back.user_id == "u1" and back.nickname == "小王"


def test_live_event_without_user():
    ev = LiveEvent(type="room_stat", platform="mock", data={"online": 10})
    back = LiveEvent.from_dict(ev.to_dict())
    assert back.user is None and back.user_id == "" and back.data["online"] == 10


def test_bus_multi_subscribers_and_wildcards():
    bus = EventBus()
    got = {"a": [], "b": [], "all": []}
    bus.subscribe("live.event", lambda t, p: got["a"].append(p))
    bus.subscribe("live.*", lambda t, p: got["b"].append(t))
    bus.subscribe("*", lambda t, p: got["all"].append(t))
    assert bus.publish("live.event", 1) == 3
    assert bus.publish("settings.changed", 2) == 1
    assert bus.flush()
    assert got["a"] == [1] and got["b"] == ["live.event"] and got["all"] == ["live.event", "settings.changed"]
    bus.close()


def test_bus_slow_subscriber_drops_oldest_without_blocking_publisher():
    bus = EventBus()
    gate = threading.Event()
    seen = []

    def slow(t, p):
        gate.wait(2)
        seen.append(p)

    sub = bus.subscribe("x", slow, queue_size=3, name="slow")
    t0 = time.perf_counter()
    for i in range(10):
        bus.publish("x", i)
    assert time.perf_counter() - t0 < 0.5          # 发布不阻塞
    gate.set()
    assert bus.flush()
    assert sub.dropped >= 6
    assert seen[-1] == 9 and len(seen) <= 4         # 留下的是最新的
    bus.close()


def test_bus_handler_exception_isolated():
    bus = EventBus()
    ok = []

    def bad(t, p):
        raise RuntimeError("boom")

    s_bad = bus.subscribe("x", bad, name="bad")
    bus.subscribe("x", lambda t, p: ok.append(p), name="good")
    bus.publish("x", 1)
    bus.publish("x", 2)
    assert bus.flush()
    assert ok == [1, 2] and s_bad.errors == 2
    bus.close()


def test_bus_unsubscribe():
    bus = EventBus()
    got = []
    s = bus.subscribe("x", lambda t, p: got.append(p))
    bus.unsubscribe(s)
    assert bus.publish("x", 1) == 0
    bus.close()


def test_registry():
    r = Registry("测试")

    @r.register("a")
    class A:
        def __init__(self, v):
            self.v = v

    r.register("b", lambda v: ("b", v))
    assert r.create("a", 1).v == 1 and r.create("b", 2) == ("b", 2)
    assert r.names() == ["a", "b"] and "a" in r
    try:
        r.create("c")
        raise AssertionError
    except ValueError as e:
        assert "未知测试" in str(e)


def test_fake_clock():
    c = FakeClock(100.0)
    assert c() == 100.0
    c.advance(5)
    c.sleep(1)
    assert c.now() == 106.0 and c.monotonic() == 106.0
