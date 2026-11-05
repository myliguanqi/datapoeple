"""产品知识库：文档解析（loaders）→ 切块（chunker）→ 检索后端（index）→ 存储（store）→ 服务（service.KnowledgeBase）。

注册表：
  LOADERS     扩展名 → 解析器（md / markdown / txt / html / htm / pdf）
  RETRIEVERS  knowledge.backend → 检索后端（fts 默认 / bm25 / hybrid）
新增格式或后端 = 新文件 + 这里注册一行（+ schema 的 choices），调用方不用改。
"""
from __future__ import annotations

from live.core.registry import Registry

LOADERS = Registry("文档解析器")
RETRIEVERS = Registry("检索后端")


def _register_loaders():
    from live.knowledge.loaders.html import HtmlLoader
    from live.knowledge.loaders.markdown import MarkdownLoader
    from live.knowledge.loaders.pdf import PdfLoader
    for cls in (MarkdownLoader, HtmlLoader, PdfLoader):
        for ext in cls.exts:
            LOADERS.register(ext, cls)


_register_loaders()


@RETRIEVERS.register("fts")
def _fts(store, **_kw):
    from live.knowledge.index.fts import FtsRetriever
    return FtsRetriever(store)


@RETRIEVERS.register("bm25")
def _bm25(store, **_kw):
    from live.knowledge.index.bm25 import BM25Retriever
    return BM25Retriever(store)


@RETRIEVERS.register("hybrid")
def _hybrid(store, embedder=None, **_kw):
    from live.knowledge.index.embedding import EmbeddingRetriever
    from live.knowledge.index.fts import FtsRetriever, fts5_available
    from live.knowledge.index.hybrid import HybridRetriever
    from live.knowledge.index.bm25 import BM25Retriever
    keyword = FtsRetriever(store) if fts5_available() else BM25Retriever(store)
    return HybridRetriever(store, keyword, EmbeddingRetriever(store, embedder))


SUPPORTED_EXTS = tuple(LOADERS.names())
