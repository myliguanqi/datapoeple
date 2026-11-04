"""LLMClient 接口。

    client.chat(messages)                 -> str               非流式
    client.chat(messages, stream=True)    -> Iterator[str]     流式（逐个增量片段）
    messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
    可选参数：temperature、max_tokens、timeout、json_mode

失败（网络、超时、HTTP 错误、空回复）抛 LLMError；调用方回退到模板话术，绝不冷场。
"""
from __future__ import annotations

import json
import re
from typing import Iterator


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """没配置 / 被禁用 / 超出每日上限。"""


class LLMClient:
    name = "base"
    model = ""

    @property
    def available(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str | None:
        return None

    def chat(self, messages: list, *, stream: bool = False, temperature: float | None = None,
             max_tokens: int | None = None, timeout: float | None = None,
             json_mode: bool = False) -> str | Iterator[str]:
        raise NotImplementedError


def extract_json(text: str) -> dict | None:
    """LLM 即使被要求 JSON 也可能套 ```json 围栏或前后带话，做下容错（来自参考项目 _extract_json）。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, dict) else None
            except ValueError:
                pass
    return None
