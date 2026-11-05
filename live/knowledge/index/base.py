"""检索后端接口（Retriever）：add(chunks)、delete(doc_id)、rebuild(chunks)、search(query, k, product_ids) -> [Hit]。

product_ids：None = 全部；[产品ID, ""] = 该产品的资料 + 公共资料（"" 表示不属于任何产品）。
新增后端：写一个子类，在 live/knowledge/__init__.py 的 RETRIEVERS 注册一行，schema 的 knowledge.backend 加上名字。
"""
from __future__ import annotations

from live.knowledge.models import Hit, Query


class Retriever:
    name = "base"

    def __init__(self, store):
        self.store = store            # KBStore

    def add(self, chunks: list) -> None:
        raise NotImplementedError

    def delete(self, doc_id: str) -> None:
        raise NotImplementedError

    def rebuild(self, chunks: list) -> None:
        """丢掉现有索引，用这些块重建（切换后端、重建索引时调用）。"""
        raise NotImplementedError

    def search(self, query: Query, k: int, product_ids: list | None = None) -> list[Hit]:
        raise NotImplementedError

    def stats(self) -> dict:
        return {"backend": self.name}

    def close(self) -> None:
        pass
