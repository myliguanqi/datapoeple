"""知识库存储：live_data/kb/kb.sqlite（docs 文档元数据、chunks 切块）+ live_data/kb/files/（原始文件，文件名是随机 ID）。

检索索引（FTS5 表、向量表）由 index/ 下的后端自己维护，可随时从 chunks 重建。线程安全（一把锁 + check_same_thread=False）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from live.knowledge.models import Chunk

DOC_FIELDS = ("id", "name", "ext", "product_id", "size", "sha1", "title", "pages", "chunks", "chars", "status",
              "error", "chunk_chars", "chunk_overlap", "created_at", "updated_at")


class KBStore:
    def __init__(self, root: str):
        self.root = root
        self.files_dir = os.path.join(root, "files")
        os.makedirs(self.files_dir, exist_ok=True)
        self.db_path = os.path.join(root, "kb.sqlite")
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS docs (
                    id TEXT PRIMARY KEY, name TEXT, ext TEXT, product_id TEXT DEFAULT '', size INTEGER, sha1 TEXT,
                    title TEXT, pages INTEGER, chunks INTEGER DEFAULT 0, chars INTEGER DEFAULT 0, status TEXT, error TEXT,
                    chunk_chars INTEGER, chunk_overlap INTEGER, created_at REAL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, doc_id TEXT, product_id TEXT DEFAULT '', seq INTEGER, heading TEXT, text TEXT,
                    page INTEGER);
                CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
                CREATE INDEX IF NOT EXISTS chunks_product ON chunks(product_id);
            """)
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------------ 文档
    def file_path(self, doc: dict) -> str:
        return os.path.join(self.files_dir, f"{doc['id']}{doc.get('ext') or ''}")

    def add_doc(self, doc: dict) -> dict:
        now = time.time()
        d = {k: doc.get(k) for k in DOC_FIELDS}
        d.update(created_at=now, updated_at=now, product_id=doc.get("product_id") or "", chunks=0, chars=0)
        with self.lock:
            self.conn.execute(f"INSERT INTO docs ({','.join(DOC_FIELDS)}) VALUES ({','.join('?' * len(DOC_FIELDS))})",
                              [d[k] for k in DOC_FIELDS])
            self.conn.commit()
        return d

    def update_doc(self, doc_id: str, **fields) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in DOC_FIELDS and k != "id"}
        fields["updated_at"] = time.time()
        with self.lock:
            self.conn.execute(f"UPDATE docs SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                              [*fields.values(), doc_id])
            if "product_id" in fields:
                self.conn.execute("UPDATE chunks SET product_id=? WHERE doc_id=?", (fields["product_id"] or "", doc_id))
            self.conn.commit()
        return self.get_doc(doc_id)

    def get_doc(self, doc_id: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
        return dict(r) if r else None

    def find_by_sha1(self, sha1: str, product_id: str) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM docs WHERE sha1=? AND product_id=?", (sha1, product_id or "")).fetchone()
        return dict(r) if r else None

    def list_docs(self, product_id: str | None = None) -> list:
        with self.lock:
            if product_id is None:
                rows = self.conn.execute("SELECT * FROM docs ORDER BY created_at").fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM docs WHERE product_id=? ORDER BY created_at",
                                         (product_id or "",)).fetchall()
        return [dict(r) for r in rows]

    def delete_doc(self, doc_id: str) -> dict | None:
        doc = self.get_doc(doc_id)
        if doc is None:
            return None
        with self.lock:
            self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            self.conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            self.conn.commit()
        try:
            os.remove(self.file_path(doc))
        except OSError:
            pass
        return doc

    # ------------------------------------------------------------------ 切块
    @staticmethod
    def _chunk(r, names: dict | None = None) -> Chunk:
        return Chunk(id=r["id"], doc_id=r["doc_id"], product_id=r["product_id"] or "", seq=r["seq"], heading=r["heading"] or "",
                     text=r["text"] or "", page=r["page"], doc_name=(names or {}).get(r["doc_id"], ""))

    def _names(self) -> dict:
        return {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM docs").fetchall()}

    def replace_chunks(self, doc_id: str, chunks: list) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            self.conn.executemany("INSERT INTO chunks (id, doc_id, product_id, seq, heading, text, page) VALUES (?,?,?,?,?,?,?)",
                                  [(c.id, c.doc_id, c.product_id or "", c.seq, c.heading, c.text, c.page) for c in chunks])
            self.conn.commit()

    def get_chunks(self, ids: list) -> dict:
        if not ids:
            return {}
        with self.lock:
            names = self._names()
            rows = self.conn.execute(f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(ids))})", list(ids)).fetchall()
        return {r["id"]: self._chunk(r, names) for r in rows}

    def chunks_of(self, doc_id: str) -> list:
        with self.lock:
            names = self._names()
            rows = self.conn.execute("SELECT * FROM chunks WHERE doc_id=? ORDER BY seq", (doc_id,)).fetchall()
        return [self._chunk(r, names) for r in rows]

    def all_chunks(self, product_ids: list | None = None) -> list:
        with self.lock:
            names = self._names()
            ready = {r["id"] for r in self.conn.execute("SELECT id FROM docs WHERE status='ready'").fetchall()}
            if product_ids is None:
                rows = self.conn.execute("SELECT * FROM chunks ORDER BY doc_id, seq").fetchall()
            else:
                rows = self.conn.execute(f"SELECT * FROM chunks WHERE product_id IN ({','.join('?' * len(product_ids))}) "
                                         "ORDER BY doc_id, seq", list(product_ids)).fetchall()
        return [self._chunk(r, names) for r in rows if r["doc_id"] in ready]

    def counts(self) -> dict:
        with self.lock:
            docs = self.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"docs": docs, "chunks": chunks}
