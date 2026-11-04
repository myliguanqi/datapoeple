"""P2：弹幕接口（模拟注入、最近事件、cookie 只写、mock 启停）与 /ws/console 节流推送。"""
import json
import os
import time


def _recv_until(ws, pred, max_msgs=200):
    for _ in range(max_msgs):
        msg = json.loads(ws.receive_text())
        if pred(msg):
            return msg
    raise AssertionError("没收到期望的消息")


def test_simulate_and_events_endpoint(make_client):
    c, ctx = make_client()
    r = c.post("/api/danmaku/simulate", json={"type": "chat", "nickname": "李姐", "gender": "female",
                                              "wealth_level": 3, "content": "保温多久"})
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["type"] == "chat" and ev["user"]["nickname"] == "李姐" and ev["user"]["gender"] == "female"
    assert ev["user"]["user_id"].startswith("sim_")
    r2 = c.post("/api/danmaku/simulate", json={"type": "gift", "nickname": "小王", "gift_name": "玫瑰",
                                               "count": 3, "unit_diamonds": 1})
    assert r2.json()["data"]["total_diamonds"] == 3 and r2.json()["data"]["is_final"] is True
    evs = c.get("/api/danmaku/events?limit=10").json()
    assert [e["type"] for e in evs][-2:] == ["chat", "gift"]
    assert [e["type"] for e in c.get("/api/danmaku/events?type=gift").json()] == ["gift"]
    assert c.post("/api/danmaku/simulate", json={"type": "nope"}).status_code == 400


def test_simulate_surge_uses_last_online(make_client):
    c, ctx = make_client()
    c.post("/api/danmaku/simulate", json={"type": "room_stat", "online": 100})
    r = c.post("/api/danmaku/simulate", json={"type": "room_stat", "online_delta": 30}).json()
    assert r["data"]["online"] == 130


def test_console_ws_throttles_live_events(make_client):
    c, ctx = make_client()
    with c.websocket_connect("/ws/console") as ws:
        assert json.loads(ws.receive_text())["topic"] == "hello"
        c.post("/api/danmaku/simulate", json={"type": "chat", "nickname": "甲", "content": "多少钱"})
        msg = _recv_until(ws, lambda m: m["topic"] == "live.event")
        assert msg["data"]["type"] == "chat" and msg["data"]["data"]["content"] == "多少钱"
        for i in range(5):
            c.post("/api/danmaku/simulate", json={"type": "like", "nickname": "乙" if i < 3 else "丙", "count": 2})
        for i in range(3):
            c.post("/api/danmaku/simulate", json={"type": "member", "nickname": f"路人{i}"})
        c.post("/api/danmaku/simulate", json={"type": "member", "nickname": "大佬", "wealth_level": 30})
        vip = _recv_until(ws, lambda m: m["topic"] == "live.event" and m["data"]["type"] == "member")
        assert vip["data"]["user"]["nickname"] == "大佬"            # 高财富等级单独推
        ctx.bus.flush()
        ctx.feed.flush_now()
        got = {}
        while len(got) < 2:
            m = _recv_until(ws, lambda m: m["topic"] == "live.event" and m["data"]["type"].endswith("_batch"))
            got[m["data"]["type"]] = m["data"]
        assert got["like_batch"]["count"] == 10 and got["like_batch"]["events"] == 5
        assert got["like_batch"]["users"][0] == {"nickname": "乙", "count": 6}
        assert got["member_batch"]["count"] == 3 and len(got["member_batch"]["users"]) == 3


def test_connect_douyin_requires_live_id(make_client):
    c, ctx = make_client()
    ctx.settings.update({"danmaku.source": "douyin", "danmaku.live_id": ""})
    r = c.post("/api/danmaku/connect", json={})
    assert r.status_code == 400 and "直播间号" in r.json()["detail"]


def test_cookie_write_only(make_client, tmp_path):
    cookie_file = tmp_path / "secrets" / "dy.txt"
    c, ctx = make_client(env_text=f"DOUYIN_COOKIE_FILE={cookie_file}\n")
    curl = "curl 'https://live.douyin.com/' -H 'accept: */*' -b 'ttwid=abc; sessionid=SECRET123; odin_tt=x'"
    r = c.put("/api/danmaku/cookie", json={"cookie": curl})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] and body["has_sessionid"] and "SECRET123" not in json.dumps(body)
    assert cookie_file.read_text(encoding="utf-8") == "ttwid=abc; sessionid=SECRET123; odin_tt=x"
    st = c.get("/api/danmaku/status").json()
    assert st["cookie"]["configured"] and "SECRET123" not in json.dumps(st)
    assert c.put("/api/danmaku/cookie", json={"cookie": "   "}).status_code == 400
    assert c.delete("/api/danmaku/cookie").json() == {"configured": False}
    assert not os.path.exists(cookie_file)


def test_mock_start_stop_via_api(make_client):
    c, ctx = make_client()
    r = c.post("/api/danmaku/mock/start", json={"rate_per_min": 600})
    assert r.status_code == 200 and ctx.cfg["danmaku"]["source"] == "mock" and ctx.cfg["danmaku"]["enabled"]
    deadline = time.time() + 20
    while time.time() < deadline and c.get("/api/danmaku/status").json()["state"] != "connected":
        time.sleep(0.1)
    assert c.get("/api/danmaku/status").json()["state"] == "connected"
    deadline = time.time() + 10
    while time.time() < deadline and len(c.get("/api/danmaku/events").json()) < 3:
        time.sleep(0.1)
    assert len(c.get("/api/danmaku/events").json()) >= 3
    assert c.get("/api/system").json()["services"]["danmaku"]["source"] == "mock"
    proc = ctx.danmaku.proc
    c.post("/api/danmaku/mock/stop")
    assert c.get("/api/danmaku/status").json()["state"] == "stopped"
    assert proc is not None and not proc.is_alive()


def test_remote_cannot_simulate(make_client):
    c, _ = make_client(host="192.168.3.4")
    assert c.post("/api/danmaku/simulate", json={"type": "chat"}).status_code == 403
    assert c.get("/api/danmaku/status").status_code == 200
