"""HTTP / WebSocket 服务入口。

    python -m live.server --config live/configs/live.yaml
    python -m live.server --config live/configs/mock.yaml        # 无模型调试

接口文档：docs/live/API.md；网页客户端：http://<host>:<port>/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

from live import __version__
from live.audio_utils import decode_audio_bytes
from live.config import PROJECT_ROOT, load_config
from live.manager import SessionManager
from live.offline import OfflineManager
from live.pool import WorkerPool, WorkerUnavailable
from live.tts import create_tts

WEB_DIR = os.path.join(PROJECT_ROOT, "web")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class CreateSessionBody(BaseModel):
    session_id: str | None = None
    model: str | None = None
    avatar: str | None = None          # 服务器上的图片路径；网页上传请用 /avatar 接口
    seed: int | None = None
    use_face_crop: bool | None = None
    gpu_id: int | None = None


class SpeakBody(BaseModel):
    text: str
    voice: str | None = None
    interrupt: bool = False
    request_id: str | None = None


class ModelBody(BaseModel):
    model: str


def create_app(cfg: dict) -> FastAPI:
    ctx: dict = {}
    upload_dir = os.path.join(cfg["server"]["data_dir"], "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    def bootstrap() -> None:
        pool: WorkerPool = ctx["pool"]
        pool.start()
        if cfg["live"]["auto_start"]:
            if not pool.wait_any_ready(timeout=cfg["gpu"]["worker_start_timeout"]):
                logger.error("没有可用的 worker，default 会话未创建（看 /api/system 里的错误信息）")
                return
            try:
                ctx["sessions"].create(session_id="default")
            except Exception as e:
                logger.error(f"创建 default 会话失败: {e}")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        tts = create_tts(cfg)
        pool = WorkerPool(cfg)
        ctx.update(tts=tts, pool=pool, sessions=SessionManager(cfg, pool, tts, loop),
                   offline=OfflineManager(cfg, pool, tts))
        threading.Thread(target=bootstrap, name="bootstrap", daemon=True).start()
        yield
        ctx["sessions"].close_all()
        pool.stop()

    app = FastAPI(title="SoulX-FlashHead Live", version=__version__, lifespan=lifespan)

    # ------------------------------------------------------------------ 工具
    def get_session(sid: str):
        sess = ctx["sessions"].get(sid)
        if sess is None:
            raise HTTPException(404, f"会话 {sid} 不存在")
        if sess.state != "running":
            raise HTTPException(409, f"会话 {sid} 状态为 {sess.state}: {sess.error or ''}")
        return sess

    async def save_upload(f: UploadFile, kind: str) -> str:
        ext = os.path.splitext(f.filename or "")[1].lower() or (".png" if kind == "image" else ".wav")
        if kind == "image" and ext not in IMAGE_EXTS:
            raise HTTPException(400, f"不支持的图片格式 {ext}")
        path = os.path.join(upload_dir, f"{uuid.uuid4().hex[:12]}{ext}")
        with open(path, "wb") as out:
            out.write(await f.read())
        return path

    def run_blocking(fn, *a, **kw):
        return asyncio.to_thread(fn, *a, **kw)

    # ------------------------------------------------------------------ 系统
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    @app.get("/api/system")
    async def system(refresh: bool = False):
        pool: WorkerPool = ctx["pool"]
        if refresh:
            await run_blocking(lambda: [w.refresh_info() for w in pool.workers if w.state == "ready"])
        return {
            "version": __version__,
            "backend": cfg["models"]["backend"],
            "gpu_mode": cfg["gpu"]["mode"],
            "default_model": cfg["models"]["default_model"],
            "tts_engine": cfg["tts"]["engine"],
            "fps": cfg["live"]["fps"],
            "sample_rate": cfg["live"]["sample_rate"],
            "workers": pool.describe(),
            "sessions": ctx["sessions"].list(),
        }

    @app.get("/api/tts/voices")
    async def voices():
        items = await run_blocking(ctx["tts"].voices)
        ids = [v["id"] for v in items]
        default = cfg["tts"]["voice"] if cfg["tts"]["voice"] in ids else (ids[0] if ids else None)
        return {"engine": cfg["tts"]["engine"], "default": default, "voices": items}

    # ------------------------------------------------------------------ 直播会话
    @app.get("/api/sessions")
    async def list_sessions():
        return ctx["sessions"].list()

    @app.post("/api/sessions")
    async def create_session(body: CreateSessionBody):
        try:
            sess = await run_blocking(ctx["sessions"].create, **body.model_dump())
        except (ValueError, WorkerUnavailable) as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, str(e).splitlines()[0])
        return sess.status()

    @app.get("/api/sessions/{sid}")
    async def session_status(sid: str):
        sess = ctx["sessions"].get(sid)
        if sess is None:
            raise HTTPException(404, f"会话 {sid} 不存在")
        return sess.status()

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        ok = await run_blocking(ctx["sessions"].close, sid)
        if not ok:
            raise HTTPException(404, f"会话 {sid} 不存在")
        return {"ok": True}

    @app.post("/api/sessions/{sid}/speak")
    async def speak(sid: str, body: SpeakBody):
        try:
            return get_session(sid).speak(body.text, voice=body.voice, interrupt=body.interrupt,
                                          request_id=body.request_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/sessions/{sid}/audio")
    async def push_audio(sid: str, file: UploadFile = File(...), text: str = Form(""),
                         interrupt: bool = Form(False)):
        sess = get_session(sid)
        data = await file.read()
        try:
            pcm = await run_blocking(decode_audio_bytes, data, cfg["live"]["sample_rate"], cfg["server"]["ffmpeg"])
        except Exception as e:
            raise HTTPException(400, str(e))
        return sess.push_audio(pcm, text=text, interrupt=interrupt)

    @app.post("/api/sessions/{sid}/interrupt")
    async def interrupt(sid: str):
        return get_session(sid).interrupt()

    @app.post("/api/sessions/{sid}/model")
    async def switch_model(sid: str, body: ModelBody):
        if body.model not in ("lite", "pro"):
            raise HTTPException(400, "model 只能是 lite / pro")
        try:
            chunk = await asyncio.wrap_future(get_session(sid).switch_model(body.model))
        except Exception as e:
            raise HTTPException(500, str(e).splitlines()[0])
        return {"model": body.model, "chunk": chunk}

    @app.post("/api/sessions/{sid}/avatar")
    async def set_avatar(sid: str, file: UploadFile = File(...), use_face_crop: bool = Form(False)):
        sess = get_session(sid)
        path = await save_upload(file, "image")
        try:
            await asyncio.wrap_future(sess.set_avatar(path, use_face_crop=use_face_crop))
        except Exception as e:
            raise HTTPException(500, str(e).splitlines()[0])
        return {"avatar": path}

    @app.websocket("/ws/sessions/{sid}")
    async def ws_session(ws: WebSocket, sid: str):
        await ws.accept()
        sess = ctx["sessions"].get(sid)
        hub = ctx["sessions"].hub(sid)
        if sess is None or hub is None:
            await ws.send_text(json.dumps({"type": "error", "message": f"会话 {sid} 不存在", "fatal": True}))
            await ws.close()
            return
        q = hub.add_client()
        await ws.send_text(json.dumps({"type": "hello", "protocol": 1, "session": sess.status(),
                                       "fps": sess.fps, "sample_rate": sess.sr}, ensure_ascii=False))

        async def sender():
            while True:
                data = await q.get()
                if data is None:
                    await ws.close()
                    return
                if isinstance(data, bytes):
                    await ws.send_bytes(data)
                else:
                    await ws.send_text(data)

        async def receiver():
            # 客户端也可以通过同一条 WebSocket 发控制消息（JSON），与 REST 等价
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "speak":
                        r = sess.speak(msg.get("text", ""), voice=msg.get("voice"),
                                       interrupt=bool(msg.get("interrupt")), request_id=msg.get("request_id"))
                        await ws.send_text(json.dumps({"type": "ack", "for": "speak", **r}))
                    elif t == "interrupt":
                        await ws.send_text(json.dumps({"type": "ack", "for": "interrupt", **sess.interrupt()}))
                    elif t == "ping":
                        await ws.send_text(json.dumps({"type": "pong", "ts": msg.get("ts")}))
                except Exception as e:
                    await ws.send_text(json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False))

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            hub.remove_client(q)
            for t in tasks:
                t.cancel()
            for t in tasks:   # 取走异常（断线时的 WebSocketDisconnect 等），避免 "never retrieved" 告警
                try:
                    await t
                except (asyncio.CancelledError, WebSocketDisconnect, Exception):
                    pass

    # ------------------------------------------------------------------ 离线任务
    @app.post("/api/offline/jobs")
    async def create_job(image: UploadFile | None = File(None), text: str = Form(""),
                         audio: UploadFile | None = File(None), model: str = Form(""),
                         voice: str = Form(""), seed: int | None = Form(None),
                         use_face_crop: bool = Form(False), wait: bool = Form(False)):
        image_path = await save_upload(image, "image") if image and image.filename else cfg["live"]["default_avatar"]
        audio_path = await save_upload(audio, "audio") if audio and audio.filename else None
        try:
            job = ctx["offline"].submit(image_path=image_path, text=text.strip() or None, audio_path=audio_path,
                                        model=model or None, voice=voice or None, seed=seed,
                                        use_face_crop=use_face_crop)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if wait:   # 同步模式：等生成完直接返回 mp4
            await run_blocking(job.done_event.wait)
            if job.status != "done":
                return JSONResponse(job.to_dict(), status_code=500)
            return FileResponse(job.output_path, media_type="video/mp4", filename=f"{job.job_id}.mp4")
        return job.to_dict()

    @app.get("/api/offline/jobs")
    async def list_jobs():
        return ctx["offline"].list()

    @app.get("/api/offline/jobs/{job_id}")
    async def job_status(job_id: str):
        job = ctx["offline"].get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        return job.to_dict()

    @app.get("/api/offline/jobs/{job_id}/video")
    async def job_video(job_id: str):
        job = ctx["offline"].get(job_id)
        if job is None or job.status != "done":
            raise HTTPException(404, "视频未生成")
        return FileResponse(job.output_path, media_type="video/mp4", filename=f"{job_id}.mp4")

    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
    return app


def _windows_timer_resolution() -> None:
    """Windows 默认 sleep 精度约 15ms，会让 25fps 节拍抖动；调到 1ms。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="SoulX-FlashHead 实时直播服务")
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "live", "configs", "live.yaml"))
    parser.add_argument("--mock", action="store_true", help="使用 mock 后端（不加载模型）")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--devices", help="覆盖 gpu.devices，例如 0,1")
    args = parser.parse_args()

    overrides: dict = {}
    if args.mock:
        overrides.setdefault("models", {})["backend"] = "mock"
    if args.host:
        overrides.setdefault("server", {})["host"] = args.host
    if args.port:
        overrides.setdefault("server", {})["port"] = args.port
    if args.devices:
        overrides.setdefault("gpu", {})["devices"] = [int(x) for x in args.devices.split(",")]
    cfg = load_config(args.config, overrides)

    if shutil.which(cfg["server"]["ffmpeg"]) is None and not os.path.exists(cfg["server"]["ffmpeg"]):
        logger.warning(f"找不到 ffmpeg（{cfg['server']['ffmpeg']}）：edge-tts 解码、推送音频、离线视频会失败")
    _windows_timer_resolution()

    import uvicorn
    logger.info(f"backend={cfg['models']['backend']} gpu={cfg['gpu']['devices']} mode={cfg['gpu']['mode']} "
                f"tts={cfg['tts']['engine']}  ->  http://127.0.0.1:{cfg['server']['port']}/")
    uvicorn.run(create_app(cfg), host=cfg["server"]["host"], port=cfg["server"]["port"], log_level="info")


if __name__ == "__main__":
    main()
