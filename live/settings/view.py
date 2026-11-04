"""SettingsView（只读视图）与 Reconfigurable 协议。"""
from __future__ import annotations

import copy
from typing import Protocol, runtime_checkable

from live.settings.schema import get_in


class SettingsView:
    """对运行中配置 cfg 的只读访问：get("speech.target_rms_db")、section("interaction") 返回副本。"""

    def __init__(self, cfg: dict):
        self._cfg = cfg

    def get(self, key: str, default=None):
        return get_in(self._cfg, key, default)

    def section(self, prefix: str) -> dict:
        v = get_in(self._cfg, prefix, {})
        return copy.deepcopy(v) if isinstance(v, dict) else {}

    @property
    def raw(self) -> dict:
        """运行中的 cfg 本体（只读使用；热更新时 SettingsCenter 原地改它）。"""
        return self._cfg


@runtime_checkable
class Reconfigurable(Protocol):
    """可热更新的模块：SettingsCenter.register(obj, prefixes) 后，相关键变更时被调用。

    changed 只含本模块关心前缀下、且 apply=hot 的键；需重启的键不会传进来。
    """

    def apply_settings(self, settings: SettingsView, changed: set) -> None: ...
