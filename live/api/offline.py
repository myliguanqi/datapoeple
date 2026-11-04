"""离线视频任务：图片 + 文本/音频 -> mp4。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from live.api.deps import get_ctx
from live.api.sessions import save_upload

router = APIRouter()


@router.post("/api/offline/jobs")
async def create_job(request: Request, image: UploadFile | None = File(None), text: str = Form(""),
                     audio: UploadFile | None = File(None), model: str = Form(""),
                     voice: str = Form(""), seed: int | None = Form(None),
                     use_face_crop: bool = Form(False), wait: bool = Form(False)):
    ctx = get_ctx(request)
    image_path = await save_upload(ctx, image, "image") if image and image.filename else ctx.cfg["live"]["default_avatar"]
    audio_path = await save_upload(ctx, audio, "audio") if audio and audio.filename else None
    try:
        job = ctx.offline.submit(image_path=image_path, text=text.strip() or None, audio_path=audio_path,
                                 model=model or None, voice=voice or None, seed=seed,
                                 use_face_crop=use_face_crop)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if wait:   # 同步模式：等生成完直接返回 mp4
        await asyncio.to_thread(job.done_event.wait)
        if job.status != "done":
            return JSONResponse(job.to_dict(), status_code=500)
        return FileResponse(job.output_path, media_type="video/mp4", filename=f"{job.job_id}.mp4")
    return job.to_dict()


@router.get("/api/offline/jobs")
async def list_jobs(request: Request):
    return get_ctx(request).offline.list()


@router.get("/api/offline/jobs/{job_id}")
async def job_status(request: Request, job_id: str):
    job = get_ctx(request).offline.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.to_dict()


@router.get("/api/offline/jobs/{job_id}/video")
async def job_video(request: Request, job_id: str):
    job = get_ctx(request).offline.get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(404, "视频未生成")
    return FileResponse(job.output_path, media_type="video/mp4", filename=f"{job_id}.mp4")
