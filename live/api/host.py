"""主持人接口：状态（正在说 / 下一句 / 队列）、人工插话、打断、讲品暂停/恢复/下一段（讲品内容 P5 接入）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from live.api.deps import get_ctx

router = APIRouter()


class SayBody(BaseModel):
    text: str
    interrupt: bool = False


@router.get("/api/host/status")
async def status(request: Request):
    return get_ctx(request).host.status()


@router.post("/api/host/say")
async def say(request: Request, body: SayBody):
    ctx = get_ctx(request)
    try:
        cue = ctx.host.say(body.text, interrupt=body.interrupt)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"cue_id": cue.id, "priority": cue.priority}


@router.post("/api/host/interrupt")
async def interrupt(request: Request):
    r = get_ctx(request).host.interrupt()
    if r is None:
        raise HTTPException(409, "主持的会话不可用")
    return r


@router.post("/api/host/script/pause")
async def script_pause(request: Request):
    get_ctx(request).settings.update({"host.pause_script": True})
    return {"pause_script": True}


@router.post("/api/host/script/resume")
async def script_resume(request: Request):
    get_ctx(request).settings.update({"host.pause_script": False})
    return {"pause_script": False}


@router.post("/api/host/script/next")
async def script_next(request: Request):
    ctx = get_ctx(request)
    script = ctx.host.script
    if script is None or not hasattr(script, "skip"):
        raise HTTPException(409, "还没有讲品内容（产品与讲解稿在 P5 接入）")
    script.skip()
    return ctx.host.status()
