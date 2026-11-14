"""人设切换端到端测试（mock 模式，先启动服务）：
    python live_server.py --config live/configs/mock.yaml
再运行：python tests/live/test_e2e_personas.py      （服务地址可用环境变量 LIVE_BASE 覆盖）

验证：停播后可切换到娱乐人设 → 节目单和知识库为空 → 注入「讲个笑话」命中 joke_request、没有带货意图残留 →
垃圾弹幕不回应 → 切回原人设、删除测试人设（连同节目单/知识库）。测试结束恢复原来的会话运行状态。
不调用任何计费接口。建议放在 test_e2e_mock.py 之后跑（模拟事件后 2 分钟内导演仍认为在播）。
"""
import os
import time

import requests

BASE = os.environ.get("LIVE_BASE", "http://127.0.0.1:8000")
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def api(method, path, **kw):
    r = requests.request(method, f"{BASE}{path}", timeout=15, **kw)
    return r


def get(path, **kw):
    return api("GET", path, **kw).json()


def wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.25)
    return None


def active_id():
    return get("/api/personas")["active_id"]


def default_running():
    return get("/api/sessions/default").get("state") == "running"


def stop_default():
    api("DELETE", "/api/sessions/default")
    wait(lambda: not default_running(), 10)


def start_default():
    r = api("POST", "/api/sessions", json={"session_id": "default"})
    assert r.status_code == 200, r.text
    wait(default_running, 30)


def log_events():
    return get("/api/interaction/log?limit=100")


def main():
    original = active_id()
    was_running = default_running()
    pid = None
    try:
        if was_running:
            stop_default()
        st = get("/api/danmaku/status")
        if st.get("state") not in ("stopped", "finished"):
            raise SystemExit("弹幕还没断开（/api/danmaku/status），先断开再跑这个 e2e")

        made = api("POST", "/api/personas/entertainment/duplicate", json={"name": "e2e娱乐测试人设"}).json()
        pid = made["id"]
        check("复制娱乐人设", made["name"] == "e2e娱乐测试人设" and made["builtin"] is False, pid)
        r = api("POST", f"/api/personas/{pid}/activate")
        check("停播后切换人设", r.status_code == 200 and active_id() == pid, str(r.status_code))

        products = get("/api/products")
        check("娱乐人设节目单为空", products["items"] == [], f"{len(products['items'])} 条")
        docs = get("/api/kb/docs")
        check("娱乐人设知识库为空", docs == [], f"{len(docs)} 份")

        start_default()
        api("POST", "/api/danmaku/simulate",
            json={"type": "chat", "nickname": "小李", "gender": "female", "content": "讲个笑话"})
        hit = wait(lambda: next((x for x in log_events() if x["event"] == "joke_request"), None))
        check("娱乐意图命中 joke_request", bool(hit), hit["fallback_text"] if hit else "无")
        check("没有带货意图残留",
              not any(x["event"] in ("product_question", "purchase_intent") for x in log_events()))

        n = len(log_events())
        api("POST", "/api/danmaku/simulate",
            json={"type": "chat", "nickname": "广告", "gender": "male", "content": "加微信领红包"})
        time.sleep(1.0)
        check("垃圾弹幕不回应", len(log_events()) == n, f"{n} → {len(log_events())}")

        stop_default()
        r = api("POST", f"/api/personas/{original}/activate")
        check("切回原人设", r.status_code == 200 and active_id() == original, original)
    finally:
        try:
            if default_running():
                stop_default()
        except Exception:
            pass
        if pid and active_id() == pid:
            try:
                api("POST", f"/api/personas/{original}/activate")
            except Exception:
                pass
        if pid:
            r = api("DELETE", f"/api/personas/{pid}", params={"delete_data": "true"})
            check("删除测试人设（含节目单/知识库）", r.status_code == 200, str(r.status_code))
        if was_running:
            try:
                start_default()
                check("恢复原会话运行", default_running())
            except Exception as e:
                check("恢复原会话运行", False, str(e))

    ok = sum(1 for _, x in results if x)
    print(f"\n{ok}/{len(results)} 通过")
    if ok != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
