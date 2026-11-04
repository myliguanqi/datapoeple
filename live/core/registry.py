"""实现注册表：每类能力（弹幕源、TTS、LLM、检索后端……）一个 Registry，按配置名创建实例。

    DANMAKU_SOURCES = Registry("弹幕源")

    @DANMAKU_SOURCES.register("mock")
    class MockSource(DanmakuSource): ...

    src = DANMAKU_SOURCES.create("mock", cfg)

新增实现 = 新文件 + 注册一行，调用方不用改。工厂可以是类，也可以是函数（懒加载第三方依赖时用函数）。
"""
from __future__ import annotations

from typing import Any, Callable


class Registry:
    def __init__(self, kind: str):
        self.kind = kind
        self._factories: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, factory: Callable[..., Any] | None = None):
        if factory is not None:
            self._factories[name] = factory
            return factory

        def deco(f):
            self._factories[name] = f
            return f
        return deco

    def create(self, name: str, *args, **kwargs):
        if name not in self._factories:
            raise ValueError(f"未知{self.kind}: {name}（可选：{', '.join(self.names()) or '无'}）")
        return self._factories[name](*args, **kwargs)

    def names(self) -> list:
        return sorted(self._factories)

    def __contains__(self, name: str) -> bool:
        return name in self._factories
