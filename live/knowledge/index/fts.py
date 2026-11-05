"""SQLite FTS5 + jieba 预分词（默认后端）：块文本先用 jieba 切成空格分隔的词再进 FTS5（unicode61 分词器按空格切），
查询时各检索词 OR 起来，按 bm25() 排序。分数 = -bm25（越大越相关）。索引表在 kb.sqlite 里，和切块同库。"""
from __future__ import annotations

from live.knowledge import text as T
from live.knowledge.index.base import Retriever
from live.knowledge.models import Hit, Query


def fts5_available() -> bool:
    import sqlite3
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


class FtsRetriever(Retriever):
    name = "fts"

    def __init__(self, store):
        super().__init__(store)
        with store.lock:
            store.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                               "tokens, chunk_id UNINDEXED, doc_id UNINDEXED, product_id UNINDEXED, tokenize='unicode61')")
            store.conn.commit()

    def add(self, chunks: list) -> None:
        rows = [(" ".join(T.index_tokens(c.index_text)), c.id, c.doc_id, c.product_id or "") for c in chunks]
        with self.store.lock:
            self.store.conn.executemany("INSERT INTO chunks_fts (tokens, chunk_id, doc_id, product_id) VALUES (?,?,?,?)", rows)
            self.store.conn.commit()

    def delete(self, doc_id: str) -> None:
        with self.store.lock:
            self.store.conn.execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc_id,))
            self.store.conn.commit()

    def set_product(self, doc_id: str, product_id: str) -> None:
        with self.store.lock:
            self.store.conn.execute("UPDATE chunks_fts SET product_id=? WHERE doc_id=?", (product_id or "", doc_id))
            self.store.conn.commit()

    def rebuild(self, chunks: list) -> None:
        with self.store.lock:
            self.store.conn.execute("DELETE FROM chunks_fts")
            self.store.conn.commit()
        self.add(chunks)

    def search(self, query: Query, k: int, product_ids: list | None = None) -> list[Hit]:
        terms = [t for t in query.terms if t.strip()]
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = "SELECT chunk_id, bm25(chunks_fts) AS s FROM chunks_fts WHERE chunks_fts MATCH ?"
        args: list = [match]
        if product_ids is not None:
            sql += f" AND product_id IN ({','.join('?' * len(product_ids))})"
            args += list(product_ids)
        sql += " ORDER BY s LIMIT ?"
        args.append(int(k))
        with self.store.lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        chunks = self.store.get_chunks([r["chunk_id"] for r in rows])
        return [Hit(chunk=chunks[r["chunk_id"]], score=-float(r["s"]), backend=self.name)
                for r in rows if r["chunk_id"] in chunks]

    def stats(self) -> dict:
        with self.store.lock:
            n = self.store.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        return {"backend": self.name, "indexed": n}
