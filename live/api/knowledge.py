"""知识库接口：文档上传（多文件，可关联产品）/ 列表 / 详情 / 改关联产品 / 删除 / 重建索引、检索测试、回答测试（可让数字人说）。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from live.api.deps import get_ctx
from live.knowledge.service import UploadRejected

router = APIRouter()


class DocPatch(BaseModel):
    product_id: str | None = None


class ReindexBody(BaseModel):
    doc_id: str | None = None


class AnswerBody(BaseModel):
    question: str
    product_id: str | None = None
    speak: bool = False
    interrupt: bool = False
    nickname: str | None = None          # 模拟提问观众（称呼按性别），可不填
    gender: str | None = None


def _check_product(ctx, product_id: str | None) -> str:
    pid = (product_id or "").strip()
    if pid and ctx.products.get(pid) is None:
        raise HTTPException(404, f"没有产品 {pid}")
    return pid


async def _read_limited(f: UploadFile, limit: int) -> bytes:
    """边读边数，超过上限立即拒绝（不把超大文件整个读进内存）。"""
    buf = bytearray()
    while True:
        part = await f.read(1024 * 1024)
        if not part:
            return bytes(buf)
        buf += part
        if len(buf) > limit:
            raise UploadRejected(f"文件太大，上限 {limit // 1048576} MB")


@router.get("/api/kb/status")
async def status(request: Request):
    return get_ctx(request).kb.status()


@router.get("/api/kb/docs")
async def list_docs(request: Request, product_id: str | None = None):
    """不带 product_id = 全部；product_id= （空）= 公共资料；product_id=xxx = 该产品的资料。"""
    return get_ctx(request).kb.list_docs(product_id)


@router.post("/api/kb/docs")
async def upload(request: Request, files: list[UploadFile] = File(...), product_id: str = Form("")):
    ctx = get_ctx(request)
    pid = _check_product(ctx, product_id)
    limit = int(ctx.cfg["knowledge"]["max_doc_mb"]) * 1024 * 1024
    out = []
    for f in files:
        try:
            data = await _read_limited(f, limit)
            out.append(await asyncio.to_thread(ctx.kb.ingest, f.filename or "", data, pid))
        except UploadRejected as e:
            out.append({"name": f.filename, "status": "rejected", "error": str(e)})
    if out and all(d.get("status") == "rejected" for d in out):
        raise HTTPException(400, "；".join(f"{d['name']}：{d['error']}" for d in out))
    return out


@router.get("/api/kb/docs/{doc_id}")
async def get_doc(request: Request, doc_id: str, chunks: bool = False):
    d = get_ctx(request).kb.get_doc(doc_id, with_chunks=chunks)
    if d is None:
        raise HTTPException(404, "文档不存在")
    return d


@router.put("/api/kb/docs/{doc_id}")
async def patch_doc(request: Request, doc_id: str, body: DocPatch):
    ctx = get_ctx(request)
    pid = _check_product(ctx, body.product_id)
    d = ctx.kb.set_doc_product(doc_id, pid)
    if d is None:
        raise HTTPException(404, "文档不存在")
    return d


@router.delete("/api/kb/docs/{doc_id}")
async def delete_doc(request: Request, doc_id: str):
    d = await asyncio.to_thread(get_ctx(request).kb.delete, doc_id)
    if d is None:
        raise HTTPException(404, "文档不存在")
    return {"ok": True, "id": doc_id}


@router.post("/api/kb/reindex")
async def reindex(request: Request, body: ReindexBody | None = None):
    ctx = get_ctx(request)
    doc_id = body.doc_id if body else None
    if doc_id and ctx.kb.get_doc(doc_id) is None:
        raise HTTPException(404, "文档不存在")
    return {"queued": ctx.kb.reindex(doc_id)}


@router.get("/api/kb/search")
async def search(request: Request, q: str, product_id: str | None = None, k: int | None = None, include_global: bool = True):
    if not q.strip():
        raise HTTPException(400, "q 不能为空")
    ctx = get_ctx(request)
    k = max(1, min(int(k), 20)) if k else None
    r = await asyncio.to_thread(ctx.kb.search, q, product_id, k, include_global)
    r.pop("_hits", None)
    return r


@router.post("/api/kb/answer")
async def answer(request: Request, body: AnswerBody):
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "question 不能为空")
    ctx = get_ctx(request)
    pid = _check_product(ctx, body.product_id) or None
    asker = {"nickname": body.nickname, "gender": body.gender or "unknown"} if body.nickname else None
    r = await asyncio.to_thread(ctx.kb.answer, q, pid, asker)
    if body.speak and r["text"]:
        cue = ctx.host.say(r["text"], interrupt=body.interrupt)
        r["cue_id"] = cue.id
    return r
