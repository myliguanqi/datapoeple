"""讲品 + 知识库端到端测试（mock 模式）：上传样例资料 → 生成讲解稿 → 确认 → 数字人讲品 → 插入产品提问（打断、基于资料回答）→ 断点续讲。
先启动服务：
    python live_server.py --config live/configs/mock.yaml
再运行：python tests/live/test_e2e_products.py      （服务地址可用环境变量 LIVE_BASE 覆盖）
依赖：pip install requests websockets

不调用任何计费接口（mock 配置没配 LLM：回答走资料原句，讲解稿走模板草稿）。跑完删除测试产品与资料、恢复改过的设置。
"""
import asyncio
import json
import os
import time

import requests
import websockets

BASE = os.environ.get("LIVE_BASE", "http://127.0.0.1:8000")
WS = BASE.replace("http", "ws", 1)
FX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "kb")
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


def settings_state(keys):
    """{key: (值, 是否 Web 覆盖)}：恢复时原来没覆盖的键用 reset 去掉覆盖，而不是写一个等于默认值的覆盖。"""
    fields = {f["key"]: f for f in requests.get(f"{BASE}/api/settings").json()["fields"]}
    return {k: (fields[k]["value"], fields[k]["source"] == "runtime") for k in keys}


def restore_settings(old):
    requests.put(f"{BASE}/api/settings", json={"changes": {k: v for k, (v, rt) in old.items() if rt}})
    requests.post(f"{BASE}/api/settings/reset", json={"keys": [k for k, (_, rt) in old.items() if not rt]})


def upload(names, product_id=""):
    files = [("files", (n, open(os.path.join(FX, n), "rb").read())) for n in names]
    r = requests.post(f"{BASE}/api/kb/docs", files=files, data={"product_id": product_id}, timeout=10)
    r.raise_for_status()
    return r.json()


def wait_ready(ids, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if all(requests.get(f"{BASE}/api/kb/docs/{i}").json()["status"] == "ready" for i in ids):
            return True
        time.sleep(0.2)
    return False


class Collector:
    def __init__(self):
        self.session, self.console = [], []

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
        out = {}
        for _, m in self.console:
            if m.get("topic") == "host.action":
                out[m["data"]["id"]] = m["data"]
        return out

    def starts(self):
        return [(t, e) for t, e in self.session if e.get("type") == "playback" and e.get("state") == "start"]

    async def until(self, sws, cws, cond, timeout):
        end = time.time() + timeout
        while time.time() < end:
            await self.run(sws, cws, 0.3)
            r = cond()
            if r:
                return r
        return None


async def main():
    wait_session()
    keys = ("products.segment_gap_s", "products.rotation_enabled", "host.pause_script", "host.pause_interaction")
    old = settings_state(keys)
    requests.put(f"{BASE}/api/settings", json={"changes": {"products.segment_gap_s": 0.5, "products.rotation_enabled": True,
                                                           "host.pause_script": False, "host.pause_interaction": False}})
    pid, global_docs = None, []
    try:
        p = requests.post(f"{BASE}/api/products", json={"name": "暖暖保温杯 500ml", "price_text": "直播间到手价 99 元",
                                                        "selling_points": ["双层真空", "倒置不漏水"]}).json()
        pid = p["id"]
        docs = upload(["product_a.md"], pid)
        global_docs = upload(["policy.pdf"])
        check("上传样例资料并入库", wait_ready([d["id"] for d in docs + global_docs]))

        # 回答测试：5 个问题，答案都来自资料
        asks = [("这个保温杯多少钱", "99 元"), ("咖啡保质期多久", "12 个月"), ("能放微波炉吗", "微波炉"), ("多久发货", "48 小时"),
                ("能退换吗", "七天无理由")]
        ok = []
        for q, fact in asks:
            a = requests.post(f"{BASE}/api/kb/answer", json={"question": q, "product_id": pid}).json()
            ok.append(a["source"] in ("knowledge", "llm") and (fact in a["text"] or a["source"] == "llm") and bool(a["refs"]))
        check("回答测试 5 问都来自资料", all(ok), ok)
        a = requests.post(f"{BASE}/api/kb/answer", json={"question": "敏感肌能用吗", "product_id": pid}).json()
        check("资料没有的问题不胡编（帮您确认）", "帮您确认" in a["text"], a["text"])

        g = requests.post(f"{BASE}/api/products/{pid}/script/generate", json={"segments": 4}).json()
        check("生成讲解稿（待确认）", g["generated"] == 4 and all(not s["confirmed"] for s in g["product"]["segments"]),
              f"{g['source']} {g['error'] or ''}")
        st = requests.get(f"{BASE}/api/host/status").json()
        check("待确认的讲解稿不播", not st["script_active"], st["script"] and st["script"].get("inactive_reason"))
        prod = requests.post(f"{BASE}/api/products/{pid}/script/confirm", json={}).json()
        segs = [s["text"].replace("{product}", "暖暖保温杯 500ml") for s in prod["segments"]]

        async with websockets.connect(WS + "/ws/sessions/default", max_size=None) as sws, \
                websockets.connect(WS + "/ws/console", max_size=None) as cws:
            await sws.recv()
            await cws.recv()
            c = Collector()
            requests.post(f"{BASE}/api/host/product/{pid}/activate")
            first = await c.until(sws, cws, lambda: next(((t, e) for t, e in c.starts() if e.get("text") and e["text"] in segs[0]), None), 15)
            check("确认后数字人开始讲品（第 1 段）", first is not None, first and first[1].get("text"))
            prod_acts = [a for a in c.actions().values() if a["kind"] == "product"]
            check("讲品段来源 = 讲解稿", bool(prod_acts) and prod_acts[0].get("text_source") == "script",
                  prod_acts and prod_acts[0].get("text_source"))

            requests.post(f"{BASE}/api/danmaku/simulate", json={"type": "chat", "nickname": "王阿姨", "gender": "female",
                                                                "content": "这个保温杯多少钱"})
            ans = await c.until(sws, cws, lambda: next((a for a in c.actions().values() if a["event"] == "product_question"
                                                        and a["status"] in ("speaking", "spoken")), None), 12)
            check("产品提问打断讲品并回答", ans is not None and any(a["status"] == "interrupted" for a in c.actions().values()
                                                          if a["kind"] == "product"), ans and ans.get("status"))
            check("回答来自知识库", ans is not None and ans.get("text_source") in ("knowledge", "llm") and bool(ans.get("refs") or ans.get("text")),
                  ans and f"{ans.get('text_source')} {ans.get('text')}")
            check("回答含资料里的价格", ans is not None and ("99" in (ans.get("text") or "") or ans.get("text_source") == "llm"),
                  ans and ans.get("text"))
            n_before = len(c.starts())
            resumed = await c.until(sws, cws, lambda: next(((t, e) for t, e in c.starts()[n_before:]
                                                            if e.get("text") and e["text"] in segs[0]), None), 30)
            check("答完断点续讲（从被打断的第 1 段重讲）", resumed is not None, resumed and resumed[1].get("text"))
            st = requests.get(f"{BASE}/api/host/status").json()
            check("主持人状态显示讲品进度", st["script_active"] and st["script"]["product_name"] == "暖暖保温杯 500ml",
                  json.dumps(st["script"], ensure_ascii=False)[:120])
            nxt = requests.post(f"{BASE}/api/host/script/next").json()
            check("下一段（跳段）", nxt["script"]["segment"] == 2, nxt["script"]["segment"])
            seg2 = await c.until(sws, cws, lambda: next(((t, e) for t, e in c.starts() if e.get("text") and e["text"] in segs[1]), None), 15)
            check("跳到第 2 段开讲", seg2 is not None, seg2 and seg2[1].get("text"))
    finally:
        if pid:
            requests.delete(f"{BASE}/api/products/{pid}")                 # 连同它的资料一起删
        for d in global_docs:
            requests.delete(f"{BASE}/api/kb/docs/{d['id']}")
        restore_settings(old)
        requests.post(f"{BASE}/api/host/interrupt")
    left = requests.get(f"{BASE}/api/products").json()["items"]
    check("清理：测试产品与资料已删除", not any(x["id"] == pid for x in left) and
          not any(d["id"] in {g["id"] for g in global_docs} for d in requests.get(f"{BASE}/api/kb/docs").json()))
    n_fail = sum(1 for _, ok in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} 通过")


asyncio.run(main())
