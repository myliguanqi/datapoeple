"""P2：DanmakuManager 子进程（mock 源）启动、崩溃自动重启、热切换、干净退出；录制 → 回放；抖音源缺房间号时报错。"""
import json
import os
import threading
import time

import pytest

from live.core.events import LiveEvent
from live.danmaku.manager import DanmakuManager
from live.danmaku.recorder import Recorder
from live.danmaku.replay import ReplaySource
from live.settings.env import Env
from live.settings.store import SettingsCenter


def wait_for(cond, timeout=20.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def mgr(mock_cfg, env_file):
    mock_cfg["danmaku"].update(enabled=True, source="mock", reconnect_delay_s=1.0)
    mock_cfg["danmaku"]["mock"].update(rate_per_min=600, seed=7)
    sc = SettingsCenter.from_cfg(mock_cfg)
    events, statuses = [], []
    sc.bus.subscribe("live.event", lambda t, e: events.append(e), name="t-events")
    sc.bus.subscribe("danmaku.status", lambda t, s: statuses.append(s), name="t-status")
    m = DanmakuManager(mock_cfg, sc.bus, Env(env_file))
    sc.register(m, ["danmaku"])
    m.start()
    yield m, sc, events, statuses
    m.stop()
    sc.bus.close()


def test_mock_subprocess_events_and_status(mgr):
    m, sc, events, _ = mgr
    assert wait_for(lambda: m.status()["state"] == "connected")
    assert wait_for(lambda: len(events) >= 10)
    assert all(isinstance(e, LiveEvent) and e.platform == "mock" for e in events)
    types = {e.type for e in events}
    assert "room_stat" in types and types & {"chat", "member", "like"}
    st = m.status()
    assert st["pid"] and st["source"] == "mock" and st["events"] >= 10 and st["enabled"]
    assert m.recent_events(limit=5) and len(m.recent_events(limit=5)) == 5


def test_child_crash_auto_restart(mgr):
    m, sc, events, _ = mgr
    assert wait_for(lambda: m.status()["state"] == "connected")
    old = m.proc
    old_pid = old.pid
    old.kill()                                              # 模拟子进程崩溃
    assert wait_for(lambda: m.status()["state"] == "backoff" or (m.proc and m.proc.pid != old_pid), 10)
    assert wait_for(lambda: m.proc is not None and m.proc.pid != old_pid and m.status()["state"] == "connected", 20)
    assert m.status()["restarts"] >= 1
    n = len(events)
    assert wait_for(lambda: len(events) > n + 3)            # 重启后继续出事件


def test_hot_switch_on_settings_change_and_disable(mgr):
    m, sc, events, _ = mgr
    assert wait_for(lambda: m.status()["state"] == "connected")
    pid = m.proc.pid
    t0 = time.time()
    sc.update({"danmaku.mock.rate_per_min": 300})          # 改参数 → 重启子进程
    assert wait_for(lambda: m.proc is not None and m.proc.pid != pid and m.status()["state"] == "connected", 15)
    assert time.time() - t0 < 10                            # 验收：10 秒内切换完成
    old_proc = m.proc
    sc.update({"danmaku.enabled": False})
    assert wait_for(lambda: m.status()["state"] == "stopped" and m.proc is None, 10)
    assert not old_proc.is_alive()                          # 不残留子进程


def test_stop_leaves_no_process(mock_cfg, env_file):
    mock_cfg["danmaku"].update(enabled=True, source="mock")
    sc = SettingsCenter.from_cfg(mock_cfg)
    m = DanmakuManager(mock_cfg, sc.bus, Env(env_file))
    m.start()
    assert wait_for(lambda: m.status()["state"] == "connected")
    proc = m.proc
    m.stop()
    assert not proc.is_alive() and m.proc is None
    sc.bus.close()


def test_douyin_without_live_id_reports_error(mock_cfg, env_file):
    """真实抖音源（vendor fetcher 在子进程里导入）：没填房间号时报错并退避，不联网。"""
    mock_cfg["danmaku"].update(enabled=True, source="douyin", live_id="", reconnect_delay_s=30)
    sc = SettingsCenter.from_cfg(mock_cfg)
    m = DanmakuManager(mock_cfg, sc.bus, Env(env_file))
    m.start()
    try:
        assert wait_for(lambda: m.status()["state"] == "backoff", 30)
        st = m.status()
        assert "直播间号" in (st["error"] or "") and st["next_retry_in"] > 0
        assert st["cookie"]["configured"] is False
    finally:
        m.stop()
        sc.bus.close()


def test_inject_publishes_without_subprocess(mock_cfg, env_file):
    sc = SettingsCenter.from_cfg(mock_cfg)
    got = []
    sc.bus.subscribe("live.event", lambda t, e: got.append(e))
    m = DanmakuManager(mock_cfg, sc.bus, Env(env_file))
    ev = LiveEvent(type="room_stat", platform="simulate", data={"online": 321})
    m.inject(ev)
    sc.bus.flush()
    assert got == [ev] and m.last_online == 321 and m.proc is None
    sc.bus.close()


# ---------------------------------------------------------------- 录制 / 回放
def test_record_then_replay_order_and_timing(mock_cfg, tmp_path):
    mock_cfg["danmaku"]["record"] = True
    sc = SettingsCenter.from_cfg(mock_cfg)
    rec = Recorder(mock_cfg, sc.bus)
    base = 5000.0
    for i in range(6):
        sc.bus.publish("live.event", LiveEvent(type="chat", platform="mock", room_id="r1", ts=base + i * 0.5,
                                               data={"content": f"第{i}条"}))
    sc.bus.flush()
    rec.close()
    files = rec.list_files()
    assert len(files) == 1 and files[0]["name"].endswith("_r1.jsonl")
    lines = open(files[0]["path"], encoding="utf-8").read().splitlines()
    assert [json.loads(x)["data"]["content"] for x in lines] == [f"第{i}条" for i in range(6)]

    got, times = [], []
    src = ReplaySource({"replay_file": files[0]["path"], "replay_speed": 5.0}, lambda e: (got.append(e), times.append(time.monotonic())),
                       lambda s: None)
    th = threading.Thread(target=src.run, daemon=True)
    t0 = time.monotonic()
    th.start()
    assert wait_for(lambda: len(got) == 6, 10)
    src.stop()
    assert [e.data["content"] for e in got] == [f"第{i}条" for i in range(6)]
    assert all(e.platform == "replay" and e.data["_orig_ts"] == base + i * 0.5 for i, e in enumerate(got))
    elapsed = times[-1] - t0                                  # 原始跨度 2.5s / 5 倍速 ≈ 0.5s
    assert 0.35 <= elapsed <= 1.5, elapsed
    sc.bus.close()


def test_replay_events_not_recorded(mock_cfg):
    mock_cfg["danmaku"]["record"] = True
    sc = SettingsCenter.from_cfg(mock_cfg)
    rec = Recorder(mock_cfg, sc.bus)
    sc.bus.publish("live.event", LiveEvent(type="chat", platform="replay", room_id="r9", data={"content": "x"}))
    sc.bus.flush()
    assert rec.written == 0 and not os.path.exists(rec.dir) or not rec.list_files()
    sc.bus.close()
