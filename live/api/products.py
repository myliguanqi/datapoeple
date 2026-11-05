"""产品接口：产品库 CRUD、排序、讲解稿（保存 / 用知识库生成 / 确认）。当前讲解中的产品在 /api/host/product/*。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from live.api.deps import get_ctx

router = APIRouter()


class ProductBody(BaseModel):
    name: str | None = None
    price_text: str | None = None
    selling_points: list[str] | str | None = None
    enabled: bool | None = None
    duration_s: float | str | None = None
    video_ids: list[str] | None = None
    notes: str | None = None


class OrderBody(BaseModel):
    ids: list[str]


class ScriptBody(BaseModel):
    segments: list


class GenerateBody(BaseModel):
    segments: int | None = None          # 生成几段（默认 products.generate_segments）
    replace_unconfirmed: bool = True     # 先去掉旧的待确认段（已确认的保留）


class ConfirmBody(BaseModel):
    ids: list[str] | None = None         # 不给 = 全部确认
    confirmed: bool = True


def describe(ctx, p) -> dict:
    d = p.to_dict()
    d["docs"] = len(ctx.kb.store.list_docs(p.id)) if ctx.kb else 0
    d["playable"] = len(ctx.script.playable(p)) if ctx.script else 0
    d["is_current"] = bool(ctx.script and ctx.script.product_id == p.id)
    return d


def _get(ctx, pid: str):
    p = ctx.products.get(pid)
    if p is None:
        raise HTTPException(404, f"没有产品 {pid}")
    return p


def _patch(body: ProductBody) -> dict:
    """只取请求里给了的字段；duration_s 显式传 null 表示「用默认时长」。"""
    return {k: v for k, v in body.model_dump().items() if v is not None or (k == "duration_s" and k in body.model_fields_set)}


@router.get("/api/products")
async def list_products(request: Request):
    ctx = get_ctx(request)
    ctx.script.resolve()
    return {"items": [describe(ctx, p) for p in ctx.products.list()], "active_id": ctx.script.product_id,
            "script": ctx.script.status()}


@router.post("/api/products")
async def create(request: Request, body: ProductBody):
    ctx = get_ctx(request)
    try:
        p = ctx.products.create(_patch(body))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return describe(ctx, p)


@router.put("/api/products/order")
async def reorder(request: Request, body: OrderBody):
    ctx = get_ctx(request)
    return [describe(ctx, p) for p in ctx.products.reorder(body.ids)]


@router.get("/api/products/{pid}")
async def get_product(request: Request, pid: str):
    ctx = get_ctx(request)
    d = describe(ctx, _get(ctx, pid))
    d["doc_list"] = ctx.kb.list_docs(pid)
    return d


@router.put("/api/products/{pid}")
async def update(request: Request, pid: str, body: ProductBody):
    ctx = get_ctx(request)
    _get(ctx, pid)
    try:
        p = ctx.products.update(pid, _patch(body))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return describe(ctx, p)


@router.delete("/api/products/{pid}")
async def delete(request: Request, pid: str, delete_docs: bool = True):
    ctx = get_ctx(request)
    _get(ctx, pid)
    n = await asyncio.to_thread(ctx.kb.delete_product_docs, pid) if delete_docs else 0
    if not delete_docs:
        for d in ctx.kb.list_docs(pid):
            ctx.kb.set_doc_product(d["id"], "")          # 不删资料就转成公共资料
    if ctx.script.product_id == pid and ctx.host.current and ctx.host.current["cue"].kind == "product":
        ctx.host.interrupt()
    ctx.products.delete(pid)
    ctx.script.resolve()
    return {"ok": True, "deleted_docs": n}


@router.put("/api/products/{pid}/script")
async def put_script(request: Request, pid: str, body: ScriptBody):
    ctx = get_ctx(request)
    _get(ctx, pid)
    try:
        p = ctx.products.set_segments(pid, body.segments)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return describe(ctx, p)


@router.post("/api/products/{pid}/script/generate")
async def generate(request: Request, pid: str, body: GenerateBody | None = None):
    ctx = get_ctx(request)
    p = _get(ctx, pid)
    body = body or GenerateBody()
    n = max(2, min(int(body.segments), 12)) if body.segments else None
    r = await asyncio.to_thread(ctx.script_gen.generate, p, n)
    p = ctx.products.add_segments(pid, r["segments"], replace_unconfirmed=body.replace_unconfirmed)
    return {"product": describe(ctx, p), "generated": len(r["segments"]), "source": r["source"], "error": r["error"],
            "refs": r["refs"], "ms": r["ms"]}


@router.post("/api/products/{pid}/script/confirm")
async def confirm(request: Request, pid: str, body: ConfirmBody | None = None):
    ctx = get_ctx(request)
    p = _get(ctx, pid)
    body = body or ConfirmBody()
    segs = [s.__dict__ | {"confirmed": body.confirmed if (body.ids is None or s.id in body.ids) else s.confirmed}
            for s in p.segments]
    return describe(ctx, ctx.products.set_segments(pid, segs))
