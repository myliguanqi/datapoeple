"""Cue：导演（或人工插话、讲品）产出的"这一刻该说什么"。主持人（live/host）把它写成台词再说出口。"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field


@dataclass
class Cue:
    event: str                          # 触发器名（gift_m / product_question / manual / script …）
    priority: int = 0
    kind: str = "interaction"           # interaction | product | video | manual
    emotion: str = "轻松互动"
    energy: float = 0.3
    speed: float = 1.0
    directive: str = ""                 # 给 LLM 的指令
    situation: dict = field(default_factory=dict)   # 喂给 LLM 的结构化局势（当事人、礼物、弹幕原文……）
    fallback_text: str = ""             # 模板渲染好的兜底台词（LLM 失效 / use_llm=false 时直接说）
    reason: str = ""                    # 人话版"为什么这一拍要说话"（决策日志）
    use_llm: bool = False
    user: dict | None = None            # 当事观众 ViewerProfile.to_dict()
    question: str = ""                  # 产品提问原文
    product_id: str | None = None
    max_stale_s: float | None = None
    interruptible: bool = False         # 讲品段等低优先级播报可被打断
    source: dict | None = None          # 触发它的事件摘要 {type, msg_id, nickname, content}
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)
