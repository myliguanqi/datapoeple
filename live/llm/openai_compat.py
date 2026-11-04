"""OpenAI 兼容 /chat/completions 客户端（改写自参考项目 expression/llm_client.py），凭证读 .env：

    LLM_API_URL   写到 /v1 或完整的 /chat/completions 都可以（自动规范化）
    LLM_API_KEY   Authorization: Bearer ...
    LLM_MODEL     如 deepseek-v4-flash
    LLM_TIMEOUT   秒（默认 10）

支持非流式与流式（SSE `data:` 行，`[DONE]` 结束；推理模型的 reasoning_content 忽略，只取正文 content）。
每次调用都现读 .env 的值：Web 上「重新加载 .env」后立即生效。
"""
from __future__ import annotations

import json
import time
from typing import Iterator

from live.llm.base import LLMClient, LLMError


def normalize_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/chat/completions"):
        return u
    return u + "/chat/completions"


class OpenAICompatLLM(LLMClient):
    name = "openai"

    def __init__(self, cfg: dict, env, session=None):
        import requests
        self.cfg = cfg
        self.env = env
        self.session = session or requests.Session()

    @property
    def url(self) -> str:
        return normalize_url(self.env.get("LLM_API_URL") or "")

    @property
    def model(self) -> str:   # type: ignore[override]
        return self.env.get("LLM_MODEL") or ""

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        missing = [k for k in ("LLM_API_URL", "LLM_API_KEY", "LLM_MODEL") if not self.env.get(k)]
        return f"LLM 未配置（.env 缺 {'、'.join(missing)}）" if missing else None

    def _timeout(self, timeout: float | None) -> float:
        return float(timeout if timeout is not None else self.env.get_float("LLM_TIMEOUT", 10.0))

    def chat(self, messages, *, stream=False, temperature=None, max_tokens=None, timeout=None, json_mode=False):
        reason = self.unavailable_reason
        if reason:
            raise LLMError(reason)
        lc = self.cfg["llm"]
        body = {"model": self.model, "messages": messages,
                "temperature": float(lc["temperature"] if temperature is None else temperature),
                "max_tokens": int(lc["max_tokens"] if max_tokens is None else max_tokens), "stream": bool(stream)}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.env.get('LLM_API_KEY')}", "Content-Type": "application/json"}
        to = self._timeout(timeout)
        if not stream:
            try:
                r = self.session.post(self.url, headers=headers, json=body, timeout=(min(to, 10.0), to))
            except Exception as e:
                raise LLMError(f"LLM 请求失败: {type(e).__name__}: {e}"[:300]) from None
            if r.status_code != 200:
                raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
            try:
                content = r.json()["choices"][0]["message"].get("content") or ""
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise LLMError(f"LLM 返回格式不对: {e}") from None
            if not content.strip():
                raise LLMError("LLM 返回空内容（混合推理模型可能需要调大 llm.max_tokens）")
            return content
        return self._stream(headers, body, to)

    def _stream(self, headers, body, timeout: float) -> Iterator[str]:
        try:
            r = self.session.post(self.url, headers=headers, json=body, timeout=(min(timeout, 10.0), timeout),
                                  stream=True)
        except Exception as e:
            raise LLMError(f"LLM 请求失败: {type(e).__name__}: {e}"[:300]) from None
        if r.status_code != 200:
            raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}")

        def gen():
            t0 = time.monotonic()
            got = False
            try:
                for raw in r.iter_lines():
                    if time.monotonic() - t0 > timeout * 3:
                        raise LLMError("LLM 流式输出超时")
                    if not raw:
                        continue
                    line = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0].get("delta") or {}
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    piece = delta.get("content")
                    if piece:
                        got = True
                        yield piece
            finally:
                close = getattr(r, "close", None)
                if close:
                    close()
            if not got:
                raise LLMError("LLM 流式返回空内容")
        return gen()
