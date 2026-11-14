"""人设模板接口：列表 / 详情 / 切换 / 复制 / 编辑 / 删除 / 触发器编辑。

人设模板 = 提示词 + 弹幕意图 + 触发器 + 称呼 + 字数限制 + 知识库兜底 + 节目单叫法；
节目单（产品/主题）与知识库资料按人设隔离，视频素材库共用。出厂模板只读，复制后编辑。
切换只在停播（无运行中的会话、弹幕未连接、知识库不忙）时允许，409 返回原因。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from live.api.deps import get_ctx
from live.interaction.director import build_triggers
from live.personas.models import signals_of
from live.personas.service import PersonaBusy, PersonaInvalid

router = APIRouter()


class DuplicateBody(BaseModel):
    name: str | None = None


def _svc(request: Request):
    return get_ctx(request).persona


def _invalid(e: PersonaInvalid):
    return JSONResponse(status_code=400, content={"detail": str(e), "errors": e.errors})


def _handle(fn):
    try:
        return fn()
    except KeyError as e:
        raise HTTPException(404, f"没有人设模板 {e.args[0] if e.args else ''}")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except PersonaBusy as e:
        raise HTTPException(409, str(e))
    except PersonaInvalid as e:
        return _invalid(e)


@router.get("/api/personas")
async def list_personas(request: Request):
    svc = _svc(request)
    active = svc.active()
    return {"items": svc.list(), "active_id": svc.active_id, "active": active,
            "program": active.get("program") or {}, "busy": svc.busy_reason()}


@router.get("/api/personas/active")
async def active_persona(request: Request):
    svc = _svc(request)
    p = svc.get(svc.active_id)
    p["busy"] = svc.busy_reason()
    return p


@router.get("/api/personas/{pid}")
async def get_persona(request: Request, pid: str):
    return _handle(lambda: _svc(request).get(pid))


@router.post("/api/personas/{pid}/activate")
async def activate(request: Request, pid: str):
    return _handle(lambda: _svc(request).activate(pid))


@router.post("/api/personas/{pid}/duplicate")
async def duplicate(request: Request, pid: str, body: DuplicateBody | None = None):
    body = body or DuplicateBody()
    return _handle(lambda: _svc(request).duplicate(pid, body.name))


@router.put("/api/personas/{pid}")
async def update(request: Request, pid: str, body: dict):
    return _handle(lambda: _svc(request).update(pid, body or {}))


@router.delete("/api/personas/{pid}")
async def delete(request: Request, pid: str, delete_data: bool = False):
    return _handle(lambda: (_svc(request).delete(pid, delete_data=delete_data), {"ok": True})[1])


@router.get("/api/personas/{pid}/triggers")
async def persona_triggers(request: Request, pid: str):
    """任意人设的触发器表（人设页编辑器用；非当前人设没有 last_fired）。"""
    svc = _svc(request)
    p = _handle(lambda: svc.get(pid))
    src = svc.source_of(p) or p
    base = {t.name: t.describe() for t in build_triggers(src.get("triggers") or {}, None, signals_of(src))}
    out = []
    for t in build_triggers(p.get("triggers") or {}, None, signals_of(p)):
        d = t.describe()
        d["default"] = {k: base.get(t.name, {}).get(k) for k in
                        ("enabled", "when", "priority", "cooldown_s", "emotion", "target_energy", "directive", "lines",
                         "max_stale_s", "use_llm", "role", "merge_clause", "merge_with")}
        d["overridden"] = list((p.get("trigger_diff") or {}).get(t.name) or [])
        d["last_fired"] = (request.app.state.ctx.engine.director.last_fired.get(t.name)
                           if pid == svc.active_id and request.app.state.ctx.engine else None)
        out.append(d)
    return out


@router.post("/api/personas/{pid}/triggers")
async def add_trigger(request: Request, pid: str, body: dict):
    body = dict(body or {})
    name = str(body.pop("name", "") or "")
    return _handle(lambda: _svc(request).add_trigger(pid, name, body))


@router.put("/api/personas/{pid}/triggers/{name}")
async def update_trigger(request: Request, pid: str, name: str, body: dict):
    return _handle(lambda: _svc(request).update_trigger(pid, name, body or {}))


@router.delete("/api/personas/{pid}/triggers/{name}")
async def delete_trigger(request: Request, pid: str, name: str):
    return _handle(lambda: _svc(request).delete_trigger(pid, name))


@router.post("/api/personas/{pid}/triggers/{name}/reset")
async def reset_trigger(request: Request, pid: str, name: str):
    return _handle(lambda: _svc(request).reset_trigger(pid, name))
