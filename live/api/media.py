"""视频素材与直播场景接口：素材上传（边收边写，不整个读进内存）/ 列表 / 文件（支持 Range，可拖进度）/ 封面 / 改名 / 删除，
会话场景的查看、卡片与字幕开关、播放 / 停止视频。场景页本身是 GET /scene（system.py）。"""
from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from live.api.deps import get_ctx
from live.media.store import MediaRejected

router = APIRouter()


class MediaPatch(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    product_ids: list[str] | None = None      # 关联到哪些产品（写在产品的 video_ids 上）


class ScenePatch(BaseModel):
    card_visible: bool | None = None
    subtitle: bool | None = None


class PlayBody(BaseModel):
    media_id: str
    mode: str | None = None                   # full | pip | split（默认 scene.video.default_mode）
    audio: str | None = None                  # video | mute（默认 scene.video.default_audio）
    loop: bool = False


def describe(ctx, it: dict) -> dict:
    d = dict(it)
    d["url"] = ctx.media.url(it)
    d["cover_url"] = f"/api/media/{it['id']}/cover" if ctx.media.cover_path(it) else None
    d["product_ids"] = [p.id for p in ctx.products.list() if it["id"] in p.video_ids]
    bg = (ctx.cfg["scene"].get("background") or "").strip()
    d["is_background"] = bg == f"media:{it['id']}"
    return d


def _get(ctx, media_id: str) -> dict:
    it = ctx.media.get(media_id)
    if it is None:
        raise HTTPException(404, "素材不存在")
    return it


async def _save_stream(ctx, f: UploadFile) -> str:
    _, ext = ctx.media.check_name(f.filename or "")
    limit = int(ctx.cfg["scene"]["media_max_mb"]) * 1024 * 1024
    tmp = ctx.media.new_temp(ext)
    size = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                part = await f.read(4 * 1024 * 1024)
                if not part:
                    break
                size += len(part)
                if size > limit:
                    raise MediaRejected(f"文件太大，上限 {ctx.cfg['scene']['media_max_mb']} MB")
                out.write(part)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return tmp


@router.get("/api/media")
async def list_media(request: Request, kind: str | None = None):
    ctx = get_ctx(request)
    return [describe(ctx, it) for it in ctx.media.list(kind)]


@router.post("/api/media")
async def upload(request: Request, files: list[UploadFile] = File(...), product_id: str = Form("")):
    ctx = get_ctx(request)
    pid = product_id.strip()
    if pid and ctx.products.get(pid) is None:
        raise HTTPException(404, f"没有产品 {pid}")
    out = []
    for f in files:
        try:
            tmp = await _save_stream(ctx, f)
            it = await asyncio.to_thread(ctx.media.add_file, f.filename or "", tmp)
            if pid and it["kind"] == "video":
                p = ctx.products.get(pid)
                ctx.products.update(pid, {"video_ids": list(p.video_ids) + [it["id"]]})
            out.append(describe(ctx, it))
        except MediaRejected as e:
            out.append({"name": f.filename, "status": "rejected", "error": str(e)})
    if out and all(d.get("status") == "rejected" for d in out):
        raise HTTPException(400, "；".join(f"{d['name']}：{d['error']}" for d in out))
    return out


@router.get("/api/media/{media_id}")
async def get_media(request: Request, media_id: str):
    ctx = get_ctx(request)
    return describe(ctx, _get(ctx, media_id))


@router.get("/api/media/{media_id}/file")
async def media_file(request: Request, media_id: str):
    ctx = get_ctx(request)
    it = _get(ctx, media_id)
    return FileResponse(ctx.media.file_path(it), media_type=it["mime"], headers={"Cache-Control": "no-cache"})


@router.get("/api/media/{media_id}/cover")
async def media_cover(request: Request, media_id: str):
    ctx = get_ctx(request)
    p = ctx.media.cover_path(_get(ctx, media_id))
    if not p:
        raise HTTPException(404, "没有封面")
    return FileResponse(p, media_type="image/jpeg")


@router.put("/api/media/{media_id}")
async def patch_media(request: Request, media_id: str, body: MediaPatch):
    ctx = get_ctx(request)
    _get(ctx, media_id)
    it = ctx.media.update(media_id, body.model_dump(exclude_none=True))
    if body.product_ids is not None:
        want = set(body.product_ids)
        for p in ctx.products.list():
            has = media_id in p.video_ids
            if (p.id in want) != has:
                ids = [x for x in p.video_ids if x != media_id] + ([media_id] if p.id in want else [])
                ctx.products.update(p.id, {"video_ids": ids})
    return describe(ctx, it)


@router.delete("/api/media/{media_id}")
async def delete_media(request: Request, media_id: str, force: bool = False):
    ctx = get_ctx(request)
    it = _get(ctx, media_id)
    users = [p for p in ctx.products.list() if media_id in p.video_ids]
    bg = (ctx.cfg["scene"].get("background") or "").strip() == f"media:{media_id}"
    if (users or bg) and not force:
        who = "、".join(f"「{p.name}」" for p in users) + ("、直播画面背景" if bg else "")
        raise HTTPException(409, f"这个素材正被 {who.lstrip('、')} 使用；确认删除请加 force=true（会同时解除关联）")
    for p in users:
        ctx.products.update(p.id, {"video_ids": [x for x in p.video_ids if x != media_id]})
    if bg:
        ctx.settings.update({"scene.background": ""})
    ctx.scenes.on_media_deleted(media_id)
    ctx.media.delete(media_id)
    return {"ok": True, "unlinked_products": [p.id for p in users], "background_cleared": bg, "name": it["name"]}


# ---------------------------------------------------------------- 会话场景
@router.get("/api/sessions/{sid}/scene")
async def get_scene(request: Request, sid: str):
    return get_ctx(request).scenes.snapshot(sid)


@router.put("/api/sessions/{sid}/scene")
async def put_scene(request: Request, sid: str, body: ScenePatch):
    return get_ctx(request).scenes.patch(sid, card_visible=body.card_visible, subtitle=body.subtitle)


@router.post("/api/sessions/{sid}/scene/video")
async def play_video(request: Request, sid: str, body: PlayBody):
    ctx = get_ctx(request)
    try:
        return ctx.scenes.play_video(sid, body.media_id, body.mode, body.audio, body.loop, source="manual")
    except KeyError:
        raise HTTPException(404, "素材不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/sessions/{sid}/scene/video/stop")
async def stop_video(request: Request, sid: str):
    ctx = get_ctx(request)
    r = ctx.scenes.stop_video(sid, "video_stopped")
    return r or ctx.scenes.snapshot(sid)
