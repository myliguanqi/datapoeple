"""大模型客户端：接口（base.py）+ 实现（openai_compat.py、mock.py）+ 注册（LLM_PROVIDERS）+ 服务（service.py）。

新增接口类型：写一个 LLMClient 子类，在这里 LLM_PROVIDERS.register 一行，再把名字加进 schema 的 llm.provider choices。
"""
from __future__ import annotations

from live.core.registry import Registry

LLM_PROVIDERS = Registry("LLM 接口")


@LLM_PROVIDERS.register("openai")
def _openai(cfg, env):
    from live.llm.openai_compat import OpenAICompatLLM
    return OpenAICompatLLM(cfg, env)


@LLM_PROVIDERS.register("mock")
def _mock(cfg, env):
    from live.llm.mock import MockLLM
    return MockLLM()
