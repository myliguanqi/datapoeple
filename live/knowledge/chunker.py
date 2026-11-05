"""切块：按章节 → 段落 → 句子切，每块不超过 chunk_chars 字，同一章节内相邻块重叠 overlap 字。

块不跨章节（标题路径是检索和回答的重要上下文）；单段过长先按句末标点切，单句仍过长再硬切。
"""
from __future__ import annotations

import re
import uuid

from live.knowledge.models import Chunk, Document

_SENT = re.compile(r"(?<=[。！？!?；;])")


def _pieces(text: str, limit: int) -> list:
    """把一段文本拆成都不超过 limit 的小片（段落 → 句子 → 硬切），保持原顺序。"""
    out = []
    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        if len(para) <= limit:
            out.append(para)
            continue
        for sent in (s for s in _SENT.split(para) if s):
            while len(sent) > limit:
                out.append(sent[:limit])
                sent = sent[limit:]
            if sent:
                out.append(sent)
    return out


def split_text(text: str, chunk_chars: int = 400, overlap: int = 50) -> list:
    chunk_chars = max(50, int(chunk_chars))
    overlap = max(0, min(int(overlap), chunk_chars // 2))
    chunks = []
    cur = ""
    for piece in _pieces(text, chunk_chars - overlap if overlap else chunk_chars):
        sep = "\n" if cur else ""
        if cur and len(cur) + len(sep) + len(piece) > chunk_chars:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap else ""
            cur = (tail + "\n" + piece) if tail else piece
            if len(cur) > chunk_chars:                # 重叠 + 新片仍超长：只保留能放下的重叠
                cur = cur[len(cur) - chunk_chars:]
            continue
        cur = cur + sep + piece
    if cur.strip():
        chunks.append(cur)
    return chunks


def chunk_document(doc: Document, doc_id: str, product_id: str = "", chunk_chars: int = 400, overlap: int = 50,
                   doc_name: str = "") -> list:
    out = []
    seq = 0
    for sec in doc.sections:
        heading = " / ".join(h for h in sec.heading_path if h)
        for text in split_text(sec.text, chunk_chars, overlap):
            out.append(Chunk(id=uuid.uuid4().hex[:16], doc_id=doc_id, product_id=product_id or "", seq=seq,
                             heading=heading, text=text, page=sec.page, doc_name=doc_name))
            seq += 1
    return out
