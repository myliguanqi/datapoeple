"""主持人接口：状态（正在说 / 下一句 / 队列）、人工插话、打断、讲品暂停/恢复/下一段、切换当前讲解的产品。"""
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


def _interrupt_product(ctx) -> None:
    """正在讲讲品段时打断它（切产品 / 跳段立即生效）；互动、人工插话不打断。"""
    cur = ctx.host.current
    if cur is not None and cur["cue"].kind == "product":
        ctx.host.interrupt()


@router.post("/api/host/script/next")
async def script_next(request: Request):
    ctx = get_ctx(request)
    script = ctx.host.script
    if script is None or not hasattr(script, "skip"):
        raise HTTPException(409, "没有讲品模块")
    if not script.active():
        raise HTTPException(409, (script.status() or {}).get("inactive_reason") or "当前没有可讲的讲解稿")
    script.skip()
    _interrupt_product(ctx)
    return ctx.host.status()


@router.post("/api/host/product/{pid}/activate")
async def activate_product(request: Request, pid: str):
    ctx = get_ctx(request)
    try:
        ctx.script.activate(pid)
    except KeyError:
        raise HTTPException(404, f"没有产品 {pid}")
    _interrupt_product(ctx)
    return ctx.host.status()


@router.post("/api/host/product/{direction}")
async def step_product(request: Request, direction: str):
    if direction not in ("next", "prev"):
        raise HTTPException(404, "只支持 next / prev")
    ctx = get_ctx(request)
    if ctx.script.step_product(1 if direction == "next" else -1) is None:
        raise HTTPException(409, "没有启用的产品")
    _interrupt_product(ctx)
    return ctx.host.status()
