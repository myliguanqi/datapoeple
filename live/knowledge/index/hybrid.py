"""混合检索：关键词（FTS5 或 BM25）+ 向量，倒数排名融合（RRF：Σ 1/(60+名次)）。向量不可用时等同关键词检索。"""
from __future__ import annotations

from live.knowledge.index.base import Retriever
from live.knowledge.models import Hit, Query

RRF_K = 60


class HybridRetriever(Retriever):
    name = "hybrid"

    def __init__(self, store, keyword: Retriever, vector: Retriever):
        super().__init__(store)
        self.keyword = keyword
        self.vector = vector

    def add(self, chunks: list) -> None:
        self.keyword.add(chunks)
        self.vector.add(chunks)

    def delete(self, doc_id: str) -> None:
        self.keyword.delete(doc_id)
        self.vector.delete(doc_id)

    def set_product(self, doc_id: str, product_id: str) -> None:
        for r in (self.keyword, self.vector):
            if hasattr(r, "set_product"):
                r.set_product(doc_id, product_id)

    def rebuild(self, chunks: list) -> None:
        self.keyword.rebuild(chunks)
        self.vector.rebuild(chunks)

    def search(self, query: Query, k: int, product_ids: list | None = None) -> list[Hit]:
        fused: dict = {}
        for hits in (self.keyword.search(query, k * 2, product_ids), self.vector.search(query, k * 2, product_ids)):
            for rank, h in enumerate(hits):
                cur = fused.get(h.chunk.id)
                score = 1.0 / (RRF_K + rank + 1)
                if cur is None:
                    fused[h.chunk.id] = Hit(chunk=h.chunk, score=score, backend=f"{self.name}:{h.backend}")
                else:
                    cur.score += score
                    cur.backend = f"{self.name}:both"
        return sorted(fused.values(), key=lambda h: -h.score)[:int(k)]

    def stats(self) -> dict:
        return {"backend": self.name, "keyword": self.keyword.stats(), "vector": self.vector.stats()}
