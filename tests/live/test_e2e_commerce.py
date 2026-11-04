"""带货全链路端到端测试（mock 模式）：模拟弹幕 → 互动引擎（导演）→ 主持人写词 → 会话播报。先启动服务：
    python live_server.py --config live/configs/mock.yaml
再运行：python tests/live/test_e2e_commerce.py      （服务地址可用环境变量 LIVE_BASE 覆盖）
依赖：pip install requests websockets

验证：高等级进场 → 产品提问 + 大礼物 + 普通弹幕 的播报顺序（按优先级）、文本来源（没配 LLM 时全是模板）、
speak_done、决策日志状态、人工插话（打断 + 合规过滤）、暂停互动。不调用任何计费接口。
"""
import asyncio
import json
import os
import time

import requests
import websockets

BASE = os.environ.get("LIVE_BASE", "http://127.0.0.1:8000")
WS = BASE.replace("http", "ws", 1)
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def wait_session():
    for _ in range(60):
        try:
            s = requests.get(f"{BASE}/api/sessions/default", timeout=2).json()
            if s.get("state") == "running":
                return s
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("default 会话未就绪")


def sim(**body):
    r = requests.post(f"{BASE}/api/danmaku/simulate", json=body, timeout=5)
    r.raise_for_status()
    return r.json()


class Collector:
    def __init__(self):
        self.session = []      # (t, event)
        self.console = []      # (t, msg)

    async def _run(self, ws, until, sink, only_text):
        while time.time() < until:
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=max(0.01, until - time.time()))
            except asyncio.TimeoutError:
                return
            if isinstance(m, str) or not only_text:
                sink.append((time.time(), json.loads(m)))

    async def run(self, sws, cws, seconds):
        until = time.time() + seconds
        await asyncio.gather(self._run(sws, until, self.session, True), self._run(cws, until, self.console, False))

    def actions(self):
        """cue_id -> 最新的决策日志条目"""
        out = {}
        for _, m in self.console:
            if m.get("topic") == "host.action":
                out[m["data"]["id"]] = m["data"]
        return out

    def start_time(self, rid):
        return next((t for t, e in self.session if e.get("type") == "playback" and e.get("state") == "start"
                     and rid and e.get("request_id") == rid), None)

    def starts(self):
        return [e for _, e in self.session if e.get("type") == "playback" and e.get("state") == "start"]

    def done(self, rid):
        return [e for _, e in self.session if e.get("type") == "speak_done" and e.get("request_id") == rid]


async def main():
    wait_session()
    st = requests.get(f"{BASE}/api/system").json()
    llm_ok = bool((st["services"].get("llm") or {}).get("available"))
    requests.post(f"{BASE}/api/interaction/resume")
    requests.post(f"{BASE}/api/host/script/resume")
    old_rec = next(f["value"] for f in requests.get(f"{BASE}/api/settings").json()["fields"]
                   if f["key"] == "interaction.memory.record_test_events")
    requests.put(f"{BASE}/api/settings", json={"changes": {"interaction.memory.record_test_events": True}})
    host = requests.get(f"{BASE}/api/host/status").json()
    check("主持人绑定 default 会话", host["enabled"] and host["session_ok"],
          json.dumps({k: host[k] for k in ("enabled", "session_ok", "session_id")}))

    async with websockets.connect(WS + "/ws/sessions/default", max_size=None) as sws, \
            websockets.connect(WS + "/ws/console", max_size=None) as cws:
        await sws.recv()
        await cws.recv()
        c = Collector()

        # 1. 高等级进场：主持人立刻开始欢迎
        sim(type="member", nickname="钻石大哥", gender="male", wealth_level=30, fans_level=6)
        await c.run(sws, cws, 1.0)
        first = next((a for a in c.actions().values() if a["event"] == "welcome_grade"), None)
        check("高等级进场 → welcome_grade 开始播报", first is not None and first["status"] in ("composing", "speaking"),
              first and first["status"])

        # 2. 欢迎还没说完时，同时来了 产品提问 + 大礼物 + 普通弹幕
        sim(type="chat", nickname="王阿姨", gender="female", wealth_level=6, content="这个保温杯多少钱")
        sim(type="gift", nickname="李姐", gender="female", wealth_level=12, gift_name="浪漫花束", unit_diamonds=299, count=1)
        sim(type="chat", nickname="小张", gender="male", content="主播今天好精神")
        await c.run(sws, cws, 28)

        acts = c.actions()
        by_event = {}
        for a in acts.values():
            by_event.setdefault(a["event"], a)
        order = [a["event"] for a in sorted(acts.values(), key=lambda a: a.get("ts") or 0)]
        spoken = [a for a in acts.values() if a["status"] in ("spoken", "speaking") and c.start_time(a.get("request_id"))]
        spoken.sort(key=lambda a: c.start_time(a.get("request_id")))
        spoken_events = [a["event"] for a in spoken]
        check("决策覆盖四类事件", {"welcome_grade", "product_question", "gift_l", "chat_reply"} <= set(order), order)
        check("播报顺序按优先级：欢迎 → 大礼物(63) → 产品提问(52)",
              spoken_events[:3] == ["welcome_grade", "gift_l", "product_question"], spoken_events)
        g = by_event.get("gift_l") or {}
        check("礼物答谢点名 + 报礼物名", "浪漫花束" in (g.get("text") or "") and "李姐" in (g.get("text") or ""), g.get("text"))
        q = by_event.get("product_question") or {}
        want_src = {"llm", "knowledge"} if llm_ok else {"template"}
        check(f"产品提问文本来源 = {'/'.join(sorted(want_src))}", q.get("text_source") in want_src, q.get("text_source"))
        ch = by_event.get("chat_reply") or {}
        check("普通弹幕：说了，或排队过期被丢弃（不说旧事）",
              ch.get("status") in ("spoken", "speaking") or (ch.get("status") == "dropped" and "过期" in (ch.get("note") or "")),
              f"{ch.get('status')} {ch.get('note') or ''}")
        for ev in ("welcome_grade", "gift_l", "product_question"):
            a = by_event.get(ev) or {}
            check(f"{ev} 收到 speak_done", bool(a.get("request_id")) and bool(c.done(a.get("request_id"))), a.get("status"))
        texts = [e.get("text") or "" for e in c.starts()]
        check("播报文本都来自决策日志", all(any(t in (a.get("text") or "") for a in acts.values()) for t in texts if t),
              texts[:4])

        # 3. 人工插话：打断当前播报，最高优先级，过合规
        sim(type="follow", nickname="新朋友", gender="female")
        await c.run(sws, cws, 0.8)
        t0 = time.time()
        requests.post(f"{BASE}/api/host/say", json={"text": "今天全网最低价，欢迎大家", "interrupt": True})
        await c.run(sws, cws, 8)
        manual = [a for a in c.actions().values() if a["event"] == "manual"]
        check("人工插话被说出且合规替换", bool(manual) and manual[-1].get("text") == "今天很划算价，欢迎大家"
              and "全网最低" in (manual[-1].get("compliance_hits") or []), manual and manual[-1].get("text"))
        start_manual = c.start_time(manual[-1].get("request_id")) if manual else None
        check("人工插话 3 秒内开口", start_manual is not None and start_manual - t0 < 3.0,
              f"{(start_manual - t0):.2f}s" if start_manual else "无")

        # 4. 暂停互动：礼物不再触发播报
        requests.post(f"{BASE}/api/interaction/pause")
        n = len(c.actions())
        sim(type="gift", nickname="小王", gift_name="跑车", unit_diamonds=1200, count=1)
        await c.run(sws, cws, 2)
        new = [a for a in c.actions().values() if a["event"] in ("gift_l", "gift_m", "gift_s", "repeat_gift")
               and (a.get("source") or {}).get("nickname") == "小王"]
        check("暂停互动后礼物不出决策", not new, f"{len(c.actions()) - n} 条新日志")
        requests.post(f"{BASE}/api/interaction/resume")

    log = requests.get(f"{BASE}/api/interaction/log?limit=50").json()
    check("/api/interaction/log 有记录", len(log) >= 5, f"{len(log)} 条")
    v = requests.get(f"{BASE}/api/viewers", params={"q": "李姐"}).json()
    check("长期记忆：送礼观众已建档", bool(v["items"]) and v["items"][0]["gift_value"] >= 299, v["stats"])
    for name in ("钻石大哥", "王阿姨", "李姐", "小张", "新朋友", "小王"):      # 清掉测试观众，别污染真实长期记忆
        for it in requests.get(f"{BASE}/api/viewers", params={"q": name}).json()["items"]:
            if it["user_id"].startswith("sim_"):
                requests.delete(f"{BASE}/api/viewers/{it['user_id']}")
    requests.put(f"{BASE}/api/settings", json={"changes": {"interaction.memory.record_test_events": old_rec}})
    n_fail = sum(1 for _, ok in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} 通过")


asyncio.run(main())
