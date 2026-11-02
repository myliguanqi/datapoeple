"""端到端测试（mock 模式）。先启动服务：
    python live_server.py --config live/configs/mock.yaml
再运行：python tests/live/test_e2e_mock.py      （服务地址可用环境变量 LIVE_BASE 覆盖）
依赖：pip install requests websockets

原说明：端到端测试 mock 模式：WS 帧流、说话、打断、切模型、换形象、推音频、离线任务、多会话。"""
import asyncio, json, struct, subprocess, sys, tempfile, time, wave, io, os
import numpy as np, cv2, requests, websockets

BASE = os.environ.get("LIVE_BASE", "http://127.0.0.1:8000")
WS = BASE.replace("http", "ws", 1) + "/ws/sessions/default"
HDR = struct.Struct("<4sBBHIdIII")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

class Collector:
    def __init__(self):
        self.packets = []   # (recv_time, seq, flags, seg, jpeg_len, audio_len)
        self.events = []    # (recv_time, dict)
        self.bad_jpeg = 0

    async def run(self, ws, until):
        while time.time() < until:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=max(0.01, until - time.time()))
            except asyncio.TimeoutError:
                break
            now = time.time()
            if isinstance(msg, bytes):
                magic, typ, flags, _, seq, pts, seg, jl, al = HDR.unpack_from(msg, 0)
                assert magic == b"FHL1" and typ == 1
                if len(self.packets) % 25 == 0:
                    img = cv2.imdecode(np.frombuffer(msg[32:32 + jl], np.uint8), cv2.IMREAD_COLOR)
                    if img is None or img.shape != (512, 512, 3):
                        self.bad_jpeg += 1
                self.packets.append((now, seq, flags, seg, jl, al))
            else:
                self.events.append((now, json.loads(msg)))

    def ev(self, typ, since=0):
        return [e for t, e in self.events if e.get("type") == typ and t >= since]

    def speaking_after(self, t0):
        return [p for p in self.packets if p[0] >= t0 and p[2] & 1]

async def main():
    st = wait_session()
    check("default 会话运行在 worker0/GPU0", st["gpu_id"] == 0 and st["model"] == "lite", f"gpu={st['gpu_id']} model={st['model']}")
    async with websockets.connect(WS, max_size=None) as ws:
        hello = json.loads(await ws.recv())
        check("hello 消息", hello["type"] == "hello" and hello["sample_rate"] == 16000 and hello["fps"] == 25)
        c = Collector()

        # 1. 空闲帧流
        t0 = time.time()
        await c.run(ws, t0 + 5)
        pk = [p for p in c.packets if p[0] >= t0 + 1]
        fps = len(pk) / (pk[-1][0] - pk[0][0]) if len(pk) > 1 else 0
        seqs = [p[1] for p in c.packets]
        check("空闲时持续出帧 ~25fps", 23 <= fps <= 27, f"{fps:.2f} fps, {len(c.packets)} 包")
        check("帧序号连续", all(b == a + 1 for a, b in zip(seqs, seqs[1:])))
        check("空闲帧不带说话标记", not any(p[2] & 1 for p in c.packets))
        check("每帧音频 640 采样(1280 字节)", all(p[5] == 1280 for p in c.packets))
        check("JPEG 可解码 512x512", c.bad_jpeg == 0)

        # 2. 说话 + 延迟
        t_speak = time.time()
        r = requests.post(f"{BASE}/api/sessions/default/speak", json={"text": "大家好，欢迎来到直播间！今天给大家介绍一款新产品。"}).json()
        await c.run(ws, time.time() + 9)
        sp = c.speaking_after(t_speak)
        lat = (sp[0][0] - t_speak) if sp else None
        check("说话帧出现", len(sp) > 25, f"{len(sp)} 帧")
        check("开口延迟(服务端出帧) < 2.5s", lat is not None and lat < 2.5, f"{lat:.2f}s" if lat else "无")
        check("segment 事件", len(c.ev("segment", t_speak)) >= 2, str([e["text"] for e in c.ev("segment", t_speak)]))
        check("playback start 事件", len([e for e in c.ev("playback", t_speak) if e["state"] == "start"]) >= 2)
        done = [e for e in c.ev("speak_done", t_speak) if e["request_id"] == r["request_id"]]
        check("speak_done 事件", len(done) == 1)
        tail = [p for p in c.packets if p[0] >= c.packets[-1][0] - 1.0]
        check("说完回到空闲", not any(p[2] & 1 for p in tail))

        # 3. 打断
        t_i = time.time()
        requests.post(f"{BASE}/api/sessions/default/speak", json={"text": "这是一段很长的话。" * 12})
        await c.run(ws, time.time() + 3.0)
        t_int = time.time()
        ir = requests.post(f"{BASE}/api/sessions/default/interrupt").json()
        await c.run(ws, time.time() + 4)
        after = [p for p in c.packets if p[0] >= t_int + 2.0]
        check("打断后 2s 内停止说话", after and not any(p[2] & 1 for p in after), f"丢弃 {len(ir['dropped_segments'])} 段")

        # 4. 切 pro 再切回 lite
        t_m = time.time()
        r = requests.post(f"{BASE}/api/sessions/default/model", json={"model": "pro"}, timeout=30).json()
        check("切换到 pro, 28 帧/块", r["chunk"]["chunk_frames"] == 28 and r["model"] == "pro")
        await c.run(ws, time.time() + 3)
        pk = [p for p in c.packets if p[0] >= t_m]
        check("切换期间帧不中断", len(pk) >= 3 * 25 - 10, f"{len(pk)} 帧/3s")
        st = requests.get(f"{BASE}/api/sessions/default").json()
        check("状态显示 pro", st["model"] == "pro")
        requests.post(f"{BASE}/api/sessions/default/speak", json={"text": "现在是Pro模型在说话。"})
        await c.run(ws, time.time() + 4)
        check("pro 模型下说话", len(c.speaking_after(t_m)) > 10)
        r = requests.post(f"{BASE}/api/sessions/default/model", json={"model": "lite"}, timeout=30).json()
        check("切回 lite", r["chunk"]["chunk_frames"] == 24)

        # 5. 换形象
        img = cv2.flip(cv2.imread(os.path.join(ROOT, "examples", "girl.png")), 1)
        ok, png = cv2.imencode(".png", img)
        t_a = time.time()
        r = requests.post(f"{BASE}/api/sessions/default/avatar", files={"file": ("flip.png", png.tobytes(), "image/png")},
                          data={"use_face_crop": "false"})
        await c.run(ws, time.time() + 1.5)
        check("换形象", r.status_code == 200 and len(c.ev("avatar", t_a)) == 1, r.text[:100])

        # 6. 推音频
        sr = 16000
        t = np.arange(sr * 2) / sr
        pcm = (np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)) * 0.3)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes((pcm * 32767).astype(np.int16).tobytes())
        t_p = time.time()
        r = requests.post(f"{BASE}/api/sessions/default/audio", files={"file": ("tone.wav", buf.getvalue(), "audio/wav")},
                          data={"text": "测试音频"})
        await c.run(ws, time.time() + 4)
        sp = c.speaking_after(t_p)
        check("推送音频驱动说话", r.status_code == 200 and len(sp) >= 40, f"{r.status_code} {len(sp)} 帧")

        # 7. WS 内发控制消息
        await ws.send(json.dumps({"type": "speak", "text": "通过WebSocket发送的文本。"}))
        t_w = time.time()
        await c.run(ws, time.time() + 4)
        acks = [e for e in c.ev("ack", t_w - 0.5) if e.get("for") == "speak"]
        check("WS speak 控制消息", len(acks) == 1 and len(c.speaking_after(t_w)) > 10)

        # 汇总
        seqs = [p[1] for p in c.packets]
        gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
        fillers = sum(1 for p in c.packets if p[2] & 2)
        dur = c.packets[-1][0] - c.packets[0][0]
        check("全程序号无跳变", gaps == 0, f"{len(seqs)} 帧 / {dur:.1f}s = {len(seqs)/dur:.2f}fps")
        print(f"补帧(filler) {fillers} 帧（切模型/换形象时出现属正常）")

    # 8. 离线任务（应调度到 worker1/GPU1）
    t_o = time.time()
    with open(os.path.join(ROOT, "examples", "girl.png"), "rb") as f:
        job = requests.post(f"{BASE}/api/offline/jobs", files={"image": ("girl.png", f, "image/png")},
                            data={"text": "你好，这是一段由图片和文字生成的数字人视频。", "model": "pro"}).json()
    while True:
        j = requests.get(f"{BASE}/api/offline/jobs/{job['job_id']}").json()
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    check("离线任务完成", j["status"] == "done", j.get("message"))
    check("离线任务调度到 worker1", j["worker"] == 1, f"worker={j['worker']}")
    if j["status"] == "done":
        mp4 = requests.get(BASE + j["video_url"]).content
        out = os.path.join(tempfile.gettempdir(), "flashhead_offline_test.mp4")
        open(out, "wb").write(mp4)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,duration",
                                "-of", "json", out], capture_output=True, text=True)
        streams = json.loads(probe.stdout)["streams"]
        kinds = {s["codec_type"]: s for s in streams}
        check("mp4 含 h264 视频 + aac 音频", kinds.get("video", {}).get("codec_name") == "h264" and kinds.get("audio", {}).get("codec_name") == "aac")
        vd, ad = float(kinds["video"]["duration"]), float(kinds["audio"]["duration"])
        check("音视频时长一致", abs(vd - ad) < 0.1, f"video {vd:.2f}s audio {ad:.2f}s, 预期 {j['duration_s']}s")
        print(f"离线用时 {time.time() - t_o:.1f}s -> {out}")

    # 同步模式
    r = requests.post(f"{BASE}/api/offline/jobs", data={"text": "同步模式测试。", "model": "lite", "wait": "true"}, timeout=120)
    check("离线同步模式直接返回 mp4", r.status_code == 200 and r.headers.get("content-type") == "video/mp4", f"{len(r.content)} 字节")

    # 9. 第二路直播（GPU1）
    t2 = time.time()
    r = requests.post(f"{BASE}/api/sessions", json={"session_id": "room2", "model": "pro", "gpu_id": 1}, timeout=30)
    ok = r.status_code == 200 and r.json()["gpu_id"] == 1
    check("在 GPU1 创建第二路直播", ok, r.text[:120])
    if ok:
        async with websockets.connect(BASE.replace("http", "ws", 1) + "/ws/sessions/room2", max_size=None) as ws2:
            await ws2.recv()
            c2 = Collector()
            await c2.run(ws2, time.time() + 5)
            pk2 = c2.packets
            fps2 = (len(pk2) - 1) / (pk2[-1][0] - pk2[0][0]) if len(pk2) > 1 else 0
            fill2 = sum(1 for p in pk2 if p[2] & 2)
            check("第二路出帧 ~25fps 且无补帧", 23 <= fps2 <= 27 and fill2 == 0, f"{fps2:.2f}fps, 首帧前等待 {pk2[0][0]-t2:.2f}s, 补帧 {fill2}")
        check("关闭第二路", requests.delete(f"{BASE}/api/sessions/room2").status_code == 200)

    sysinfo = requests.get(f"{BASE}/api/system?refresh=true").json()
    check("/api/system 两个 worker 就绪", [w["state"] for w in sysinfo["workers"]] == ["ready", "ready"])
    st = requests.get(f"{BASE}/api/sessions/default").json()
    print("会话状态:", {k: st[k] for k in ("model", "rtf", "gen_time_ema_s", "chunks", "fillers", "frames_out", "gen_errors")})
    check("无生成错误", st["gen_errors"] == 0)
    n_fail = sum(1 for _, ok in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} 通过")

asyncio.run(main())
