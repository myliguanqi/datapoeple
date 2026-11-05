"""系统状态、音色列表、网页入口。"""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from live import __version__
from live.api.deps import get_ctx
from live.config import PROJECT_ROOT

WEB_DIR = os.path.join(PROJECT_ROOT, "web")
router = APIRouter()


@router.get("/")
async def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@router.get("/scene")
async def scene_page():
    """直播场景页（OBS 浏览器源）：/scene?session=default"""
    return FileResponse(os.path.join(WEB_DIR, "scene.html"))


@router.get("/api/system")
async def system(request: Request, refresh: bool = False):
    ctx = get_ctx(request)
    cfg = ctx.cfg
    pool = ctx.pool
    if refresh:
        await asyncio.to_thread(lambda: [w.refresh_info() for w in pool.workers if w.state == "ready"])
    return {
        "version": __version__,
        "backend": cfg["models"]["backend"],
        "gpu_mode": cfg["gpu"]["mode"],
        "default_model": cfg["models"]["default_model"],
        "tts_engine": ctx.tts.name if ctx.tts else cfg["tts"]["engine"],
        "fps": cfg["live"]["fps"],
        "sample_rate": cfg["live"]["sample_rate"],
        "workers": pool.describe(),
        "sessions": ctx.sessions.list(),
        "services": ctx.services_status(),
        "admin": {"token_set": bool(ctx.admin_token)},
        "settings": {"version": ctx.settings.version, "pending_restart": ctx.settings.pending_restart()},
    }


@router.get("/api/tts/voices")
async def voices(request: Request):
    ctx = get_ctx(request)
    items = await asyncio.to_thread(ctx.tts.voices)
    ids = [v["id"] for v in items]
    want = ctx.cfg["tts"]["voice"]
    default = want if want in ids else (ids[0] if ids else None)
    return {"engine": ctx.tts.name, "default": default, "voices": items}
