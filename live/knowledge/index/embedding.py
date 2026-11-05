"""可选的向量检索：OpenAI 兼容 /embeddings（.env 的 EMBEDDING_API_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL，**按调用计费**），
向量存 kb.sqlite 的 chunk_vectors 表（float32 BLOB），检索时余弦相似度。

不在本机加载向量模型（运行机器内存 12GB，见任务书 08 §7）。HashEmbedder 是不联网的确定性假向量，只给测试用。
"""
from __future__ import annotations

import hashlib
import threading

import numpy as np
from loguru import logger

from live.knowledge.index.base import Retriever
from live.knowledge.models import Hit, Query


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    name = "base"
    model = ""
    billable = True

    @property
    def unavailable_reason(self) -> str | None:
        return None

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def embed(self, texts: list) -> list:
        raise NotImplementedError


class OpenAIEmbedder(Embedder):
    name = "openai"

    def __init__(self, env, timeout: float = 20.0):
        self.env = env
        self.timeout = timeout
        self.calls = 0

    @property
    def model(self) -> str:
        return self.env.get("EMBEDDING_MODEL") or ""

    @property
    def unavailable_reason(self) -> str | None:
        missing = [k for k in ("EMBEDDING_API_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL") if not self.env.get(k)]
        return f".env 未配置 {' / '.join(missing)}" if missing else None

    def url(self) -> str:
        u = (self.env.get("EMBEDDING_API_URL") or "").rstrip("/")
        return u if u.endswith("/embeddings") else u + "/embeddings"

    def embed(self, texts: list) -> list:
        import requests
        reason = self.unavailable_reason
        if reason:
            raise EmbeddingError(reason)
        self.calls += 1
        try:
            r = requests.post(self.url(), json={"model": self.model, "input": list(texts)}, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.env.get('EMBEDDING_API_KEY')}"})
        except requests.RequestException as e:
            raise EmbeddingError(f"Embedding 请求失败: {type(e).__name__}") from None
        if r.status_code != 200:
            raise EmbeddingError(f"Embedding HTTP {r.status_code}: {r.text[:200]}")
        data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(texts):
            raise EmbeddingError("Embedding 返回条数不对")
        return [d["embedding"] for d in data]


class HashEmbedder(Embedder):
    """测试用：字符二元组哈希到固定维度，不联网、确定性（共享字的文本相似度高）。"""
    name = "hash"
    model = "hash-256"
    billable = False

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.calls = 0

    def embed(self, texts: list) -> list:
        self.calls += 1
        out = []
        for t in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            s = "".join(ch for ch in str(t) if not ch.isspace())
            for i in range(len(s)):
                for g in (s[i], s[i:i + 2]):
                    v[int(hashlib.md5(g.encode("utf-8")).hexdigest()[:8], 16) % self.dim] += 1.0
            out.append(v.tolist())
        return out


class EmbeddingRetriever(Retriever):
    name = "embedding"
    BATCH = 32

    def __init__(self, store, embedder: Embedder):
        super().__init__(store)
        self.embedder = embedder
        self._lock = threading.Lock()
        self._cache = None                      # (ids, product_ids, matrix)
        self.missing = 0
        self.last_error = None
        with store.lock:
            store.conn.execute("CREATE TABLE IF NOT EXISTS chunk_vectors (chunk_id TEXT PRIMARY KEY, doc_id TEXT, "
                               "product_id TEXT, model TEXT, vec BLOB)")
            store.conn.commit()

    def add(self, chunks: list) -> None:
        if not chunks:
            return
        if not self.embedder.available:
            self.missing += len(chunks)
            self.last_error = self.embedder.unavailable_reason
            return
        rows = []
        try:
            for i in range(0, len(chunks), self.BATCH):
                part = chunks[i:i + self.BATCH]
                vecs = self.embedder.embed([c.index_text for c in part])
                rows += [(c.id, c.doc_id, c.product_id or "", self.embedder.model, np.asarray(v, np.float32).tobytes())
                         for c, v in zip(part, vecs)]
        except Exception as e:                   # noqa: BLE001  向量失败不影响关键词检索
            self.last_error = str(e)[:200]
            self.missing += len(chunks) - len(rows)
            logger.warning(f"向量化失败，仅用关键词检索: {self.last_error}")
        with self.store.lock:
            self.store.conn.executemany("INSERT OR REPLACE INTO chunk_vectors VALUES (?,?,?,?,?)", rows)
            self.store.conn.commit()
        self._cache = None

    def delete(self, doc_id: str) -> None:
        with self.store.lock:
            self.store.conn.execute("DELETE FROM chunk_vectors WHERE doc_id=?", (doc_id,))
            self.store.conn.commit()
        self._cache = None

    def set_product(self, doc_id: str, product_id: str) -> None:
        with self.store.lock:
            self.store.conn.execute("UPDATE chunk_vectors SET product_id=? WHERE doc_id=?", (product_id or "", doc_id))
            self.store.conn.commit()
        self._cache = None

    def rebuild(self, chunks: list) -> None:
        """只给还没有向量（或换了模型）的块补向量，避免重复计费。"""
        with self.store.lock:
            have = {r[0] for r in self.store.conn.execute("SELECT chunk_id FROM chunk_vectors WHERE model=?",
                                                          (self.embedder.model,)).fetchall()}
            ids = [c.id for c in chunks]
            self.store.conn.execute(f"DELETE FROM chunk_vectors WHERE chunk_id NOT IN ({','.join('?' * len(ids))})"
                                    if ids else "DELETE FROM chunk_vectors", ids)
            self.store.conn.commit()
        self.missing = 0
        self.add([c for c in chunks if c.id not in have])

    def _matrix(self):
        if self._cache is None:
            with self.store.lock:
                rows = self.store.conn.execute("SELECT chunk_id, product_id, vec FROM chunk_vectors WHERE model=?",
                                               (self.embedder.model,)).fetchall()
            if not rows:
                self._cache = ([], [], None)
            else:
                m = np.stack([np.frombuffer(r[2], np.float32) for r in rows])
                m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-8)
                self._cache = ([r[0] for r in rows], [r[1] or "" for r in rows], m)
        return self._cache

    def search(self, query: Query, k: int, product_ids: list | None = None) -> list[Hit]:
        if not self.embedder.available:
            return []
        with self._lock:
            ids, pids, m = self._matrix()
        if m is None:
            return []
        try:
            q = np.asarray(self.embedder.embed([query.text])[0], np.float32)
        except Exception as e:                   # noqa: BLE001
            self.last_error = str(e)[:200]
            return []
        q = q / (np.linalg.norm(q) + 1e-8)
        sims = m @ q
        allowed = None if product_ids is None else set(product_ids)
        order = [i for i in np.argsort(-sims) if allowed is None or pids[i] in allowed][:int(k)]
        chunks = self.store.get_chunks([ids[i] for i in order])
        return [Hit(chunk=chunks[ids[i]], score=float(sims[i]), backend=self.name) for i in order if ids[i] in chunks]

    def stats(self) -> dict:
        ids, _, _ = self._matrix()
        return {"backend": self.name, "embedder": self.embedder.name, "model": self.embedder.model,
                "available": self.embedder.available, "vectors": len(ids), "missing": self.missing,
                "last_error": self.last_error or self.embedder.unavailable_reason}
