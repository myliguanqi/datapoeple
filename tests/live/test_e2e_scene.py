"""视频素材 + 直播场景端到端测试（mock 模式）：上传视频（ffmpeg 现场生成，不入库）→ 讲品中「立即播放（画中画，原声）」→
场景事件推到画面 WebSocket、讲品让路 → 场景页回传播完 → 回到数字人 + 产品卡片、断点续讲 → 静音全屏视频按时长自动结束。
先启动服务：
    python live_server.py --config live/configs/mock.yaml
再运行：python tests/live/test_e2e_scene.py      （服务地址可用环境变量 LIVE_BASE 覆盖；需要 ffmpeg）
依赖：pip install requests websockets

不调用任何计费接口。跑完删除测试产品与素材、恢复改过的设置。建议在 test_e2e_products.py 之后跑（见 docs/live/DEVELOPMENT.md）。
"""
import asyncio
import json
import os
import subprocess
import tempfile
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
            if requests.get(f"{BASE}/api/sessions/default", timeout=2).json().get("state") == "running":
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("default 会话未就绪")


def settings_state(keys):
    fields = {f["key"]: f for f in requests.get(f"{BASE}/api/settings").json()["fields"]}
    return {k: (fields[k]["value"], fields[k]["source"] == "runtime") for k in keys}


def restore_settings(old):
    requests.put(f"{BASE}/api/settings", json={"changes": {k: v for k, (v, rt) in old.items() if rt}})
    requests.post(f"{BASE}/api/settings/reset", json={"keys": [k for k, (_, rt) in old.items() if not rt]})


def make_clip(path, seconds, audio=True):
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=480x270:rate=25"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-c:a", "aac", "-shortest"]
    args += ["-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", path]
    subprocess.run(args, check=True, capture_output=True, timeout=60)


class Collector:
    def __init__(self, ws):
        self.ws = ws
        self.events = []        # (t, event)

    async def run(self, seconds):
        until = time.time() + seconds
        while time.time() < until:
            try:
                m = await asyncio.wait_for(self.ws.recv(), timeout=max(0.01, until - time.time()))
            except asyncio.TimeoutError:
                return
            if isinstance(m, str):
                self.events.append((time.time(), json.loads(m)))

    async def until(self, cond, timeout):
        end = time.time() + timeout
        while time.time() < end:
            r = cond()
            if r:
                return r
            await self.run(0.2)
        return cond()

    def scenes(self):
        return [e for _, e in self.events if e.get("type") == "scene"]

    def starts(self, since=0):
        return [e for t, e in self.events if t >= since and e.get("type") == "playback" and e.get("state") == "start"]


async def main():
    wait_session()
    keys = ("products.segment_gap_s", "products.video_slot", "host.pause_script", "scene.video.default_mode")
    old = settings_state(keys)
    requests.put(f"{BASE}/api/settings", json={"changes": {"products.segment_gap_s": 0.5, "products.video_slot": "off",
                                                           "host.pause_script": False}})
    tmp = tempfile.mkdtemp()
    pid, media_ids = None, []
    try:
        check("场景页 /scene 可访问", requests.get(f"{BASE}/scene").status_code == 200)
        make_clip(os.path.join(tmp, "介绍.mp4"), 6)
        make_clip(os.path.join(tmp, "静音.mp4"), 3, audio=False)
        p = requests.post(f"{BASE}/api/products", json={"name": "暖暖保温杯 500ml", "price_text": "直播间到手价 99 元",
                                                        "selling_points": ["双层真空", "倒置不漏水"]}).json()
        pid = p["id"]
        requests.put(f"{BASE}/api/products/{pid}/script", json={"segments": [
            {"text": "家人们看过来，这款暖暖保温杯采用双层真空设计，倒置也不漏水，上班通勤带着特别方便。", "confirmed": True},
            {"text": "容量五百毫升，装一次热水能喝一上午，颜色有奶白、雾蓝、樱花粉三种。", "confirmed": True}]})
        files = [("files", (n, open(os.path.join(tmp, n), "rb").read())) for n in ("介绍.mp4", "静音.mp4")]
        up = requests.post(f"{BASE}/api/media", files=files, data={"product_id": pid}).json()
        media_ids = [m["id"] for m in up]
        check("上传视频：时长 / 分辨率 / 音轨 / 封面", up[0]["has_audio"] and not up[1]["has_audio"] and up[0]["width"] == 480
              and 5 <= up[0]["duration_s"] <= 7 and bool(up[0]["cover_url"]), [(m["duration_s"], m["has_audio"]) for m in up])
        check("上传时关联到产品", requests.get(f"{BASE}/api/products/{pid}").json()["video_ids"] == media_ids)

        async with websockets.connect(WS + "/ws/sessions/default", max_size=None) as sws:
            await sws.recv()
            c = Collector(sws)
            requests.post(f"{BASE}/api/host/product/{pid}/activate")
            sc = requests.get(f"{BASE}/api/sessions/default/scene").json()
            check("场景：数字人 + 产品卡片", sc["layout"] == "dh_with_card" and sc["card"]["product"]["name"] == "暖暖保温杯 500ml"
                  and sc["canvas"]["width"] == 720 and sc["canvas"]["height"] == 1280, sc["layout"])
            first = await c.until(lambda: next((e for e in c.starts() if "双层真空" in (e.get("text") or "")), None), 15)
            check("讲品开始", first is not None)

            t_play = time.time()
            r = requests.post(f"{BASE}/api/sessions/default/scene/video",
                              json={"media_id": media_ids[0], "mode": "pip", "audio": "video"}).json()
            started = await c.until(lambda: next((e for e in c.scenes() if e["reason"] == "video_started"), None), 3)
            check("播放命令 → 画面 WebSocket 收到 scene 事件（画中画）", started is not None and started["scene"]["layout"] == "video_pip"
                  and started["scene"]["video"]["play_id"] == r["video"]["play_id"], started and started["scene"]["layout"])
            st = requests.get(f"{BASE}/api/host/status").json()
            check("带原声视频期间讲品暂停", st["video_blocking"] and not st["script_active"])
            await c.run(2.5)
            check("视频期间不开讲新的讲品段", not c.starts(since=t_play + 0.8), [e.get("text") for e in c.starts(since=t_play + 0.8)])
            t_end = time.time()
            await sws.send(json.dumps({"type": "scene.video_ended", "play_id": "not-this-one"}))
            await sws.send(json.dumps({"type": "scene.video_ended", "play_id": r["video"]["play_id"]}))
            ended = await c.until(lambda: next((e for e in c.scenes() if e["reason"] == "video_ended"), None), 3)
            check("场景页回传播完 → 回到数字人 + 产品卡片", ended is not None and ended["scene"]["layout"] == "dh_with_card"
                  and ended["scene"]["video"] is None)
            resumed = await c.until(lambda: next((e for e in c.starts(since=t_end) if "双层真空" in (e.get("text") or "")), None), 20)
            check("视频结束后断点续讲（被打断的第 1 段重讲）", resumed is not None, resumed and resumed.get("text"))

            t_mute = time.time()
            r2 = requests.post(f"{BASE}/api/sessions/default/scene/video",
                               json={"media_id": media_ids[1], "mode": "full", "audio": "mute"}).json()
            check("静音视频全屏、不挡讲品", r2["layout"] == "video_full" and not r2["blocks_script"])
            auto = await c.until(lambda: next((e for e in c.scenes() if e["reason"] == "video_ended" and
                                               (e["scene"].get("ts") or 0) > t_mute), None), 8)
            check("没有场景页回传时按时长自动结束", auto is not None, f"{(time.time() - t_mute):.1f}s")
            requests.post(f"{BASE}/api/sessions/default/scene/video", json={"media_id": media_ids[0], "mode": "split"})
            requests.post(f"{BASE}/api/sessions/default/scene/video/stop")
            stop = await c.until(lambda: next((e for e in c.scenes() if e["reason"] == "video_stopped"), None), 3)
            check("手动停止视频", stop is not None and stop["scene"]["video"] is None)
        r = requests.delete(f"{BASE}/api/media/{media_ids[0]}")
        check("删除被产品引用的视频先提示", r.status_code == 409, r.json().get("detail"))
    finally:
        if pid:
            requests.delete(f"{BASE}/api/products/{pid}")
        for mid in media_ids:
            requests.delete(f"{BASE}/api/media/{mid}?force=true")
        restore_settings(old)
        requests.post(f"{BASE}/api/sessions/default/scene/video/stop")
        requests.post(f"{BASE}/api/host/interrupt")
    check("清理：测试产品与素材已删除", not requests.get(f"{BASE}/api/media").json() or
          not any(m["id"] in media_ids for m in requests.get(f"{BASE}/api/media").json()))
    n_fail = sum(1 for _, ok in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} 通过")


asyncio.run(main())
