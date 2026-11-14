"""P4：互动 / 主持人 / 观众接口（不启动 worker：主持人没有会话可说，cue 停在队列里，只验证决策链与接口）。"""
import json
import time


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def sim(c, **body):
    r = c.post("/api/danmaku/simulate", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_event_to_cue_to_log(make_client):
    c, ctx = make_client()
    sim(c, type="gift", nickname="钻石大哥", gender="male", wealth_level=30, gift_name="浪漫花束", unit_diamonds=299, count=1)
    assert wait_for(lambda: c.get("/api/interaction/log").json())
    log = c.get("/api/interaction/log").json()
    e = log[0]
    assert e["event"] == "gift_l" and e["status"] == "queued" and "浪漫花束" in e["fallback_text"]
    assert e["source"]["nickname"] == "钻石大哥" and "L档" in e["reason"]
    q = c.get("/api/interaction/queue").json()
    assert q and q[0]["event"] == "gift_l"


def test_product_question_flow_and_memory(make_client):
    c, ctx = make_client()
    sim(c, type="chat", nickname="路人甲", content="随便聊聊")
    time.sleep(0.2)
    assert c.get("/api/viewers").json()["items"] == []          # 模拟事件默认不进长期记忆
    c.put("/api/settings", json={"changes": {"interaction.memory.record_test_events": True}})
    sim(c, type="chat", nickname="王阿姨", gender="female", content="这个保温杯多少钱")
    assert wait_for(lambda: any(x["event"] == "product_question" for x in c.get("/api/interaction/log").json()))
    v = c.get("/api/viewers", params={"q": "王阿姨"}).json()
    assert v["items"] and v["items"][0]["chat_count"] == 1 and v["items"][0]["last_question"] == "这个保温杯多少钱"
    uid = v["items"][0]["user_id"]
    assert c.get(f"/api/viewers/{uid}").json()["known_before"] is False
    assert c.delete(f"/api/viewers/{uid}").status_code == 200
    assert c.get(f"/api/viewers/{uid}").status_code == 404


def editable_persona(c):
    """出厂模板只读：复制一份并激活，触发器才能改。"""
    pid = c.post("/api/personas/commerce/duplicate", json={"name": "测试人设"}).json()["id"]
    assert c.post(f"/api/personas/{pid}/activate").status_code == 200
    return pid


def test_trigger_table_edit_and_reset(make_client):
    c, ctx = make_client()
    editable_persona(c)
    trs = c.get("/api/interaction/triggers").json()
    names = [t["name"] for t in trs]
    assert {"gift_l", "product_question", "welcome_member", "idle"} <= set(names)
    assert names == [t["name"] for t in sorted(trs, key=lambda t: -t["priority"])]
    r = c.put("/api/interaction/triggers/follow", json={"priority": 66, "lines": ["多谢{follow_user}关注！"]})
    assert r.status_code == 200 and r.json()["priority"] == 66 and r.json()["overridden"] == ["lines", "priority"]
    assert ctx.cfg["persona"]["triggers"]["follow"]["priority"] == 66               # 写进当前人设模板
    sim(c, type="follow", nickname="小美", gender="female")
    assert wait_for(lambda: any(x["event"] == "follow" for x in c.get("/api/interaction/log").json()))
    e = next(x for x in c.get("/api/interaction/log").json() if x["event"] == "follow")
    assert e["fallback_text"] == "多谢小美姐关注！" and e["priority"] == 66
    assert c.put("/api/interaction/triggers/nope", json={"priority": 1}).status_code == 404
    assert c.put("/api/interaction/triggers/follow", json={"priority": 500}).status_code == 400
    r = c.post("/api/interaction/triggers/follow/reset").json()
    assert r["priority"] == 50 and r["overridden"] == []


def test_disable_trigger_via_api(make_client):
    c, ctx = make_client()
    editable_persona(c)
    c.put("/api/interaction/triggers/gift_s", json={"enabled": False})
    sim(c, type="gift", nickname="小王", gift_name="玫瑰", unit_diamonds=1, count=1)
    time.sleep(0.3)
    assert not any(x["event"] == "gift_s" for x in c.get("/api/interaction/log").json())


def test_pause_resume_interaction(make_client):
    c, ctx = make_client()
    assert c.post("/api/interaction/pause").json() == {"pause_interaction": True}
    sim(c, type="gift", nickname="小王", gift_name="跑车", unit_diamonds=1200, count=1)
    time.sleep(0.3)
    assert c.get("/api/interaction/log").json() == []
    c.post("/api/interaction/resume")
    sim(c, type="follow", nickname="小李")
    assert wait_for(lambda: c.get("/api/interaction/log").json())


def test_host_say_status_and_console_ws(make_client):
    c, ctx = make_client()
    with c.websocket_connect("/ws/console") as ws:
        json.loads(ws.receive_text())
        r = c.post("/api/host/say", json={"text": "大家好，我是小美", "interrupt": False})
        assert r.status_code == 200 and r.json()["priority"] == 90
        st = c.get("/api/host/status").json()
        assert st["session_ok"] is False and st["queue_size"] == 1 and st["queue"][0]["event"] == "manual"
        sim(c, type="gift", nickname="小王", gift_name="玫瑰", unit_diamonds=1, count=2)
        for _ in range(100):
            m = json.loads(ws.receive_text())
            if m["topic"] == "interaction.cue":
                assert m["data"]["event"] == "gift_s"
                break
        else:
            raise AssertionError("没收到 interaction.cue")
    assert c.post("/api/host/say", json={"text": " "}).status_code == 400
    assert c.post("/api/host/script/next").status_code == 409
    assert c.post("/api/host/script/pause").json() == {"pause_script": True}
    assert ctx.cfg["host"]["pause_script"] is True


def test_system_reports_interaction(make_client):
    c, _ = make_client()
    st = c.get("/api/system").json()["services"]["interaction"]
    assert st["active"] is True and "memory" in st
