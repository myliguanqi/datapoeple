"""知识库数据模型：Document（解析结果）→ Chunk（切块，进索引）→ Hit（检索命中）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Section:
    heading_path: list            # ["暖暖保温杯 500ml", "价格与优惠"]
    text: str
    page: int | None = None       # PDF 页码（从 1 开始）；md/html 为 None


@dataclass
class Document:
    title: str
    sections: list = field(default_factory=list)     # [Section]
    pages: int | None = None
    meta: dict = field(default_factory=dict)

    @property
    def text_len(self) -> int:
        return sum(len(s.text) for s in self.sections)


@dataclass
class Chunk:
    id: str
    doc_id: str
    product_id: str               # "" = 公共资料（不属于任何产品，如售后政策）
    seq: int
    heading: str                  # "暖暖保温杯 500ml / 价格与优惠"
    text: str
    page: int | None = None
    doc_name: str = ""

    @property
    def index_text(self) -> str:
        """进索引的文本：标题路径也参与检索（「价格与优惠」这类小标题很有信息量）。"""
        return f"{self.heading}\n{self.text}" if self.heading else self.text

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Hit:
    chunk: Chunk
    score: float                  # 越大越相关（各后端尺度不同，只在同一后端内可比）
    backend: str = ""
    matched: list = field(default_factory=list)      # 命中的查询词组（覆盖度判断用）

    def to_dict(self) -> dict:
        c = self.chunk
        return {"chunk_id": c.id, "doc_id": c.doc_id, "doc_name": c.doc_name, "product_id": c.product_id,
                "heading": c.heading, "page": c.page, "text": c.text, "score": round(self.score, 4),
                "backend": self.backend, "matched": list(self.matched)}


@dataclass
class Query:
    text: str                     # 原始问题
    terms: list                   # 检索词（去停用词 + 同义扩展），给关键词后端
    groups: list                  # [[同义的若干写法], …]：每组是问题里的一个信息点，用来算覆盖度
