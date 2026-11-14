"""互动接口：触发器表（查看 / 修改 / 恢复默认）、队列、决策日志、暂停/恢复互动；长期记忆里的观众（查、删）。

触发器属于当前人设模板；这里的接口是「人设」页触发器编辑器的兼容入口，实际写进当前人设。
出厂模板只读：先复制成用户模板再改（403 会给出提示）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from live.api.deps import get_ctx
from live.interaction.director import build_triggers
from live.personas.models import signals_of
from live.personas.service import PersonaInvalid

router = APIRouter()
EDITABLE = ("enabled", "when", "priority", "cooldown_s", "emotion", "target_energy", "directive", "lines",
            "max_stale_s", "use_llm", "role", "merge_clause", "merge_with")


class TriggerPatch(BaseModel):
    enabled: bool | None = None
    when: str | None = None
    priority: int | None = None
    cooldown_s: float | None = None
    emotion: str | None = None
    target_energy: float | None = None
    directive: str | None = None
    lines: list[str] | None = None
    max_stale_s: float | None = None
    use_llm: bool | None = None
    role: str | None = None
    merge_clause: str | None = None
    merge_with: list[str] | None = None


def _trigger_payload(ctx, name: str) -> dict:
    t = ctx.engine.director.trigger(name)
    if t is None:
        raise HTTPException(404, f"没有触发器 {name}")
    p = ctx.persona.active()
    src = ctx.persona.source_of(p) or p
    base = {x.name: x.describe() for x in build_triggers(src.get("triggers") or {}, None, signals_of(src))}
    d = t.describe()
    d["default"] = {k: base.get(name, {}).get(k) for k in EDITABLE}
    d["overridden"] = list((ctx.persona.trigger_diff(p) or {}).get(name) or [])
    d["last_fired"] = ctx.engine.director.last_fired.get(name)
    return d


@router.get("/api/interaction/triggers")
async def triggers(request: Request):
    ctx = get_ctx(request)
    p = ctx.persona.active()
    src = ctx.persona.source_of(p) or p
    base = {t.name: t.describe() for t in build_triggers(src.get("triggers") or {}, None, signals_of(src))}
    diff = ctx.persona.trigger_diff(p)
    out = []
    for t in ctx.engine.director.triggers:
        d = t.describe()
        d["default"] = {k: base.get(t.name, {}).get(k) for k in EDITABLE}
        d["overridden"] = list(diff.get(t.name) or [])
        d["last_fired"] = ctx.engine.director.last_fired.get(t.name)
        out.append(d)
    return out


@router.put("/api/interaction/triggers/{name}")
async def patch_trigger(request: Request, name: str, body: TriggerPatch):
    ctx = get_ctx(request)
    if ctx.engine.director.trigger(name) is None:
        raise HTTPException(404, f"没有触发器 {name}")
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "priority" in patch and not 0 <= patch["priority"] <= 100:
        raise HTTPException(400, "priority 应在 0~100")
    if "cooldown_s" in patch and patch["cooldown_s"] < 0:
        raise HTTPException(400, "cooldown_s 不能为负")
    if "lines" in patch:
        patch["lines"] = [x.strip() for x in patch["lines"] if x and x.strip()]
    try:
        ctx.persona.update_trigger(ctx.persona.active_id, name, patch)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except PersonaInvalid as e:
        raise HTTPException(400, str(e))
    return _trigger_payload(ctx, name)


@router.post("/api/interaction/triggers/{name}/reset")
async def reset_trigger(request: Request, name: str):
    ctx = get_ctx(request)
    try:
        ctx.persona.reset_trigger(ctx.persona.active_id, name)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError:
        raise HTTPException(404, f"复制来源里没有触发器 {name}")
    t = ctx.engine.director.trigger(name)
    return t.describe() if t else {"name": name, "removed": True}


@router.get("/api/interaction/queue")
async def queue(request: Request):
    return get_ctx(request).cue_queue.snapshot()


@router.get("/api/interaction/log")
async def log(request: Request, limit: int = 100):
    return get_ctx(request).engine.recent_log(max(1, min(limit, 300)))


@router.post("/api/interaction/pause")
async def pause(request: Request):
    ctx = get_ctx(request)
    ctx.settings.update({"host.pause_interaction": True})
    ctx.cue_queue.clear("互动已暂停")
    return {"pause_interaction": True}


@router.post("/api/interaction/resume")
async def resume(request: Request):
    get_ctx(request).settings.update({"host.pause_interaction": False})
    return {"pause_interaction": False}


# ---------------------------------------------------------------- 观众（长期记忆）
@router.get("/api/viewers")
async def viewers(request: Request, q: str = "", limit: int = 50):
    ctx = get_ctx(request)
    return {"stats": ctx.memory.stats(), "items": ctx.memory.search(q, max(1, min(limit, 500)))}


@router.get("/api/viewers/{user_id}")
async def viewer(request: Request, user_id: str):
    p = get_ctx(request).memory.profile(user_id)
    if not p:
        raise HTTPException(404, "长期记忆里没有这位观众（只有发过弹幕/送过礼/关注过的才建档）")
    return p


@router.delete("/api/viewers/{user_id}")
async def delete_viewer(request: Request, user_id: str):
    if not get_ctx(request).memory.delete(user_id):
        raise HTTPException(404, "长期记忆里没有这位观众")
    return {"ok": True}
