"""可编排的假 LLM（测试 / 无密钥调试）：不联网，记录收到的 messages，可模拟延迟、超时、报错、流式按句。

    llm = MockLLM(rules=[("多少钱", "这款现在到手价 99 元，详情看小黄车哦。")], default="收到，马上安排！")
    llm.calls[-1]["messages"]   # 断言收到了什么
"""
from __future__ import annotations

import re
import time
from typing import Callable

from live.llm.base import LLMClient, LLMError


class MockLLM(LLMClient):
    name = "mock"
    model = "mock"

    def __init__(self, rules: list | None = None, default: str | Callable | None = None, delay_s: float = 0.0,
                 fail: Exception | str | None = None, chunk_chars: int = 3, chunk_delay_s: float = 0.0):
        self.rules = list(rules or [])          # [(正则或子串或 callable(messages)->bool, 回复或 callable(messages)->str)]
        self.default = default
        self.delay_s = delay_s
        self.fail = fail
        self.chunk_chars = chunk_chars
        self.chunk_delay_s = chunk_delay_s
        self.calls: list = []

    def reply_for(self, messages: list) -> str:
        last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        for cond, reply in self.rules:
            hit = cond(messages) if callable(cond) else bool(re.search(cond, last))
            if hit:
                return reply(messages) if callable(reply) else reply
        if callable(self.default):
            return self.default(messages)
        if self.default is not None:
            return self.default
        return f"收到：{str(last)[:20]}"

    def chat(self, messages, *, stream=False, temperature=None, max_tokens=None, timeout=None, json_mode=False):
        self.calls.append({"messages": [dict(m) for m in messages], "stream": stream, "temperature": temperature,
                           "max_tokens": max_tokens, "json_mode": json_mode})
        if self.delay_s:
            if timeout is not None and self.delay_s > timeout:
                time.sleep(timeout)
                raise LLMError("LLM 超时（mock）")
            time.sleep(self.delay_s)
        if self.fail:
            raise self.fail if isinstance(self.fail, Exception) else LLMError(str(self.fail))
        text = self.reply_for(messages)
        if not stream:
            return text

        def gen():
            for i in range(0, len(text), max(1, self.chunk_chars)):
                if self.chunk_delay_s:
                    time.sleep(self.chunk_delay_s)
                yield text[i:i + self.chunk_chars]
        return gen()
