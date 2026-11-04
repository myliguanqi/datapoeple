"""直播会话：创建/关闭、说话、推音频、打断、切模型、换形象，以及画面推流 WebSocket。"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from live.api.deps import get_ctx, ws_can_write
from live.audio_utils import decode_audio_bytes
from live.pool import WorkerUnavailable

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
router = APIRouter()


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


def get_session(request: Request, sid: str):
    sess = get_ctx(request).sessions.get(sid)
    if sess is None:
        raise HTTPException(404, f"会话 {sid} 不存在")
    if sess.state != "running":
        raise HTTPException(409, f"会话 {sid} 状态为 {sess.state}: {sess.error or ''}")
    return sess


async def save_upload(ctx, f: UploadFile, kind: str) -> str:
    ext = os.path.splitext(f.filename or "")[1].lower() or (".png" if kind == "image" else ".wav")
    if kind == "image" and ext not in IMAGE_EXTS:
        raise HTTPException(400, f"不支持的图片格式 {ext}")
    upload_dir = os.path.join(ctx.cfg["server"]["data_dir"], "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, f"{uuid.uuid4().hex[:12]}{ext}")   # 文件名重写为随机 ID，防路径穿越
    with open(path, "wb") as out:
        out.write(await f.read())
    return path


@router.get("/api/sessions")
async def list_sessions(request: Request):
    return get_ctx(request).sessions.list()


@router.post("/api/sessions")
async def create_session(request: Request, body: CreateSessionBody):
    try:
        sess = await asyncio.to_thread(get_ctx(request).sessions.create, **body.model_dump())
    except (ValueError, WorkerUnavailable) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e).splitlines()[0])
    return sess.status()


@router.get("/api/sessions/{sid}")
async def session_status(request: Request, sid: str):
    sess = get_ctx(request).sessions.get(sid)
    if sess is None:
        raise HTTPException(404, f"会话 {sid} 不存在")
    return sess.status()


@router.delete("/api/sessions/{sid}")
async def delete_session(request: Request, sid: str):
    ok = await asyncio.to_thread(get_ctx(request).sessions.close, sid)
    if not ok:
        raise HTTPException(404, f"会话 {sid} 不存在")
    return {"ok": True}


@router.post("/api/sessions/{sid}/speak")
async def speak(request: Request, sid: str, body: SpeakBody):
    try:
        return get_session(request, sid).speak(body.text, voice=body.voice, interrupt=body.interrupt,
                                               request_id=body.request_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/sessions/{sid}/audio")
async def push_audio(request: Request, sid: str, file: UploadFile = File(...), text: str = Form(""),
                     interrupt: bool = Form(False)):
    cfg = get_ctx(request).cfg
    sess = get_session(request, sid)
    data = await file.read()
    try:
        pcm = await asyncio.to_thread(decode_audio_bytes, data, cfg["live"]["sample_rate"], cfg["server"]["ffmpeg"])
    except Exception as e:
        raise HTTPException(400, str(e))
    return sess.push_audio(pcm, text=text, interrupt=interrupt)


@router.post("/api/sessions/{sid}/interrupt")
async def interrupt(request: Request, sid: str):
    return get_session(request, sid).interrupt()


@router.post("/api/sessions/{sid}/model")
async def switch_model(request: Request, sid: str, body: ModelBody):
    if body.model not in ("lite", "pro"):
        raise HTTPException(400, "model 只能是 lite / pro")
    try:
        chunk = await asyncio.wrap_future(get_session(request, sid).switch_model(body.model))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e).splitlines()[0])
    return {"model": body.model, "chunk": chunk}


@router.post("/api/sessions/{sid}/avatar")
async def set_avatar(request: Request, sid: str, file: UploadFile = File(...), use_face_crop: bool = Form(False)):
    sess = get_session(request, sid)
    path = await save_upload(get_ctx(request), file, "image")
    try:
        await asyncio.wrap_future(sess.set_avatar(path, use_face_crop=use_face_crop))
    except Exception as e:
        raise HTTPException(500, str(e).splitlines()[0])
    return {"avatar": path}


@router.websocket("/ws/sessions/{sid}")
async def ws_session(ws: WebSocket, sid: str):
    await ws.accept()
    ctx = ws.app.state.ctx
    sess = ctx.sessions.get(sid)
    hub = ctx.sessions.hub(sid)
    if sess is None or hub is None:
        await ws.send_text(json.dumps({"type": "error", "message": f"会话 {sid} 不存在", "fatal": True},
                                      ensure_ascii=False))
        await ws.close()
        return
    can_write = ws_can_write(ws)
    q = hub.add_client()
    await ws.send_text(json.dumps({"type": "hello", "protocol": 1, "session": sess.status(),
                                   "fps": sess.fps, "sample_rate": sess.sr, "can_write": can_write},
                                  ensure_ascii=False))

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
        # 客户端也可以通过同一条 WebSocket 发控制消息（JSON），与 REST 等价；控制消息同样要求写权限
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "ts": msg.get("ts")}))
                    continue
                if t in ("speak", "interrupt") and not can_write:
                    await ws.send_text(json.dumps({"type": "error", "message": "没有写权限（需要管理口令 ?token=）"},
                                                  ensure_ascii=False))
                    continue
                if t == "speak":
                    r = sess.speak(msg.get("text", ""), voice=msg.get("voice"),
                                   interrupt=bool(msg.get("interrupt")), request_id=msg.get("request_id"))
                    await ws.send_text(json.dumps({"type": "ack", "for": "speak", **r}))
                elif t == "interrupt":
                    await ws.send_text(json.dumps({"type": "ack", "for": "interrupt", **sess.interrupt()}))
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
