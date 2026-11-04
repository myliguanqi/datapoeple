"""设置中心接口：schema + 当前值、保存（热更新）、恢复默认、历史版本与回滚、导出/导入、.env 重载与密钥状态。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from live.api.deps import get_ctx

router = APIRouter()


class ChangesBody(BaseModel):
    changes: dict


class ResetBody(BaseModel):
    keys: list[str] | None = None


class ImportBody(BaseModel):
    overrides: dict


def _result(r: dict):
    if not r.get("ok"):
        return JSONResponse({"detail": "设置校验失败", "errors": r.get("errors", {})}, status_code=400)
    return r


@router.get("/api/settings")
async def get_settings(request: Request):
    return get_ctx(request).settings.describe()


@router.put("/api/settings")
async def put_settings(request: Request, body: ChangesBody):
    return _result(get_ctx(request).settings.update(body.changes))


@router.post("/api/settings/reset")
async def reset_settings(request: Request, body: ResetBody | None = None):
    return _result(get_ctx(request).settings.reset((body.keys if body else None) or None))


@router.get("/api/settings/history")
async def settings_history(request: Request):
    return get_ctx(request).settings.history()


@router.post("/api/settings/rollback/{version}")
async def rollback_settings(request: Request, version: int):
    r = get_ctx(request).settings.rollback(version)
    if not r.get("ok") and "version" in r.get("errors", {}):
        raise HTTPException(404, r["errors"]["version"])
    return _result(r)


@router.get("/api/settings/export")
async def export_settings(request: Request):
    return get_ctx(request).settings.export()


@router.post("/api/settings/import")
async def import_settings(request: Request, body: ImportBody):
    return _result(get_ctx(request).settings.import_(body.overrides))


@router.post("/api/settings/reload-env")
async def reload_env(request: Request):
    ctx = get_ctx(request)
    changed = ctx.env.reload()
    return {"changed": changed, "secrets": ctx.env.status()}


@router.get("/api/settings/secrets-status")
async def secrets_status(request: Request):
    return get_ctx(request).env.status()
