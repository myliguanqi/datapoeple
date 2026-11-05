"""产品模型：名称、价格/机制文案、卖点、讲解稿（分段，每段有「待确认 / 已确认」）、关联视频、上架状态、轮播顺序与时长。

讲解稿段落里可以写占位符 {product}（产品名）、{price}（价格文案）、{anchor}（主播自称），播报时替换。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field

NAME_MAX = 60
TEXT_MAX = 500
SEGMENTS_MAX = 50
POINTS_MAX = 20


def new_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class ScriptSegment:
    text: str
    confirmed: bool = False            # 人工确认过才参与轮播（products.require_confirmed_script）
    source: str = "manual"             # manual | llm | template
    hits: list = field(default_factory=list)      # 生成时合规过滤命中的词
    id: str = field(default_factory=new_id)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, d: dict) -> "ScriptSegment":
        return cls(text=str(d.get("text") or "").strip(), confirmed=bool(d.get("confirmed")), source=d.get("source") or "manual",
                   hits=list(d.get("hits") or []), id=d.get("id") or new_id(), updated_at=float(d.get("updated_at") or time.time()))


@dataclass
class Product:
    name: str
    price_text: str = ""               # 价格/机制文案："日常价 129，直播间到手 99"
    selling_points: list = field(default_factory=list)
    segments: list = field(default_factory=list)  # [ScriptSegment]
    video_ids: list = field(default_factory=list)
    enabled: bool = True               # 上架：参与轮播
    order: int = 0
    duration_s: float | None = None    # None = 用 products.duration_s
    notes: str = ""
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["segments"] = [asdict(s) for s in self.segments]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Product":
        return cls(name=str(d.get("name") or "").strip(), price_text=str(d.get("price_text") or ""),
                   selling_points=[str(x) for x in d.get("selling_points") or []],
                   segments=[ScriptSegment.from_dict(s) for s in d.get("segments") or []],
                   video_ids=[str(x) for x in d.get("video_ids") or []], enabled=bool(d.get("enabled", True)),
                   order=int(d.get("order") or 0), duration_s=d.get("duration_s"), notes=str(d.get("notes") or ""),
                   id=d.get("id") or new_id(), created_at=float(d.get("created_at") or time.time()),
                   updated_at=float(d.get("updated_at") or time.time()))


def validate_patch(patch: dict) -> dict:
    """校验并规范化产品字段（新建/修改共用），不合法抛 ValueError（中文原因）。"""
    out = {}
    if "name" in patch:
        name = str(patch["name"] or "").strip()
        if not name:
            raise ValueError("产品名称不能为空")
        if len(name) > NAME_MAX:
            raise ValueError(f"产品名称不能超过 {NAME_MAX} 字")
        out["name"] = name
    if "price_text" in patch:
        out["price_text"] = str(patch["price_text"] or "").strip()[:200]
    if "selling_points" in patch:
        pts = patch["selling_points"]
        if isinstance(pts, str):
            pts = pts.split("\n")
        pts = [str(x).strip() for x in pts or [] if str(x).strip()]
        if len(pts) > POINTS_MAX:
            raise ValueError(f"卖点最多 {POINTS_MAX} 条")
        out["selling_points"] = [p[:200] for p in pts]
    if "duration_s" in patch:
        v = patch["duration_s"]
        if v in (None, ""):
            out["duration_s"] = None
        else:
            v = float(v)
            if not 30 <= v <= 7200:
                raise ValueError("讲解时长应在 30~7200 秒之间（留空 = 用默认）")
            out["duration_s"] = v
    if "enabled" in patch:
        out["enabled"] = bool(patch["enabled"])
    if "video_ids" in patch:
        out["video_ids"] = [str(x) for x in patch["video_ids"] or []]
    if "notes" in patch:
        out["notes"] = str(patch["notes"] or "")[:2000]
    if "order" in patch:
        out["order"] = int(patch["order"])
    return out


def validate_segments(items: list) -> list:
    if not isinstance(items, list):
        raise ValueError("segments 应为列表")
    if len(items) > SEGMENTS_MAX:
        raise ValueError(f"讲解稿最多 {SEGMENTS_MAX} 段")
    out = []
    for it in items:
        it = {"text": it} if isinstance(it, str) else dict(it or {})
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        if len(text) > TEXT_MAX:
            raise ValueError(f"每段不能超过 {TEXT_MAX} 字")
        it["text"] = text
        out.append(ScriptSegment.from_dict(it))
    return out
