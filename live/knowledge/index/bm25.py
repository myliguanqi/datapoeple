"""纯 Python BM25（rank_bm25）：sqlite3 不支持 FTS5 时的备选。索引在内存里，数据变化后下次检索时从 KBStore 重建
（知识库一般就几十到几千块，重建很快）。"""
from __future__ import annotations

import threading

from live.knowledge import text as T
from live.knowledge.index.base import Retriever
from live.knowledge.models import Hit, Query


class BM25Retriever(Retriever):
    name = "bm25"

    def __init__(self, store):
        super().__init__(store)
        self._lock = threading.Lock()
        self._dirty = True
        self._chunks: list = []
        self._bm25 = None

    def add(self, chunks: list) -> None:
        self._dirty = True

    def delete(self, doc_id: str) -> None:
        self._dirty = True

    def set_product(self, doc_id: str, product_id: str) -> None:
        self._dirty = True

    def rebuild(self, chunks: list) -> None:
        self._dirty = True

    def _ensure(self) -> None:
        if not self._dirty:
            return
        from rank_bm25 import BM25Okapi
        chunks = self.store.all_chunks()
        corpus = [T.index_tokens(c.index_text) or ["∅"] for c in chunks]
        self._chunks = chunks
        self._bm25 = BM25Okapi(corpus) if corpus else None
        self._dirty = False

    def search(self, query: Query, k: int, product_ids: list | None = None) -> list[Hit]:
        with self._lock:
            self._ensure()
            if self._bm25 is None or not query.terms:
                return []
            scores = self._bm25.get_scores(list(query.terms))
            chunks = self._chunks
        allowed = None if product_ids is None else set(product_ids)
        cand = [(s, c) for s, c in zip(scores, chunks)
                if (allowed is None or (c.product_id or "") in allowed)
                and any(t in c.index_text.lower() for t in query.terms)]     # 语料很小时 idf 可能为负，按"含检索词"过滤
        cand.sort(key=lambda x: -x[0])
        return [Hit(chunk=c, score=float(s), backend=self.name) for s, c in cand[:int(k)]]

    def stats(self) -> dict:
        return {"backend": self.name, "indexed": len(self._chunks) if not self._dirty else None}
