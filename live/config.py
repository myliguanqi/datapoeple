"""配置加载：YAML 覆盖 DEFAULTS，结果是普通 dict（可直接 pickle 给 worker 子进程）。

所有配置项的默认值、类型、范围、中文说明都定义在 live/settings/schema.py（单一来源），
DEFAULTS 由 schema 生成。运行时的三层合并（默认 ← yaml ← Web 写入的 live_data/settings.json）
与热更新由 live/settings/store.py 的 SettingsCenter 负责；本模块保留给 worker / 测试直接用。
"""
from __future__ import annotations

import copy
import os

import yaml

from live.settings.schema import FIELDS, defaults_tree

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 所有可配置项及默认值。新增配置项请加到 live/settings/schema.py，再在 YAML 里覆盖。
DEFAULTS = defaults_tree()

PATH_KEYS = tuple(f.key for f in FIELDS if f.path)


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def read_yaml(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: str | None = None, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        _deep_merge(cfg, read_yaml(path))
    if overrides:
        _deep_merge(cfg, copy.deepcopy(overrides))
    _normalize(cfg)
    _ensure_persona(cfg)
    return cfg


def abs_path(val):
    """相对路径一律相对项目根目录（SoulX-FlashHead/），与从哪个目录启动无关。"""
    if val and isinstance(val, str) and not os.path.isabs(val):
        return os.path.normpath(os.path.join(PROJECT_ROOT, val))
    return val


def _ensure_persona(cfg: dict) -> None:
    """不经过 AppContext（worker / 单元测试直接 load_config）时，也带上默认人设模板。

    正式启动时 PersonaService 会立刻用 live_data 里的当前人设覆盖它。
    """
    if isinstance(cfg.get("persona"), dict) and cfg["persona"].get("id"):
        return
    from live.personas.store import load_builtin
    pid = (cfg.get("personas") or {}).get("default") or "commerce"
    try:
        cfg["persona"] = load_builtin(pid)
    except KeyError:
        cfg["persona"] = load_builtin("commerce")


def _normalize(cfg: dict) -> None:
    gpu = cfg["gpu"]
    gpu["devices"] = [int(d) for d in (gpu["devices"] or [0])]
    if gpu["mode"] == "single":
        gpu["devices"] = gpu["devices"][:1]
    for key in ("live_devices", "offline_devices"):
        if gpu[key] is not None:
            gpu[key] = [int(d) for d in gpu[key]]
    models = cfg["models"]
    if models["default_model"] not in ("lite", "pro"):
        raise ValueError(f"models.default_model 只能是 lite/pro: {models['default_model']}")
    models["preload"] = [m for m in (models["preload"] or []) if m in ("lite", "pro")]
    for key in PATH_KEYS:
        sect, _, leaf = key.rpartition(".")
        node = cfg
        for p in sect.split("."):
            node = node.get(p) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(leaf):
            node[leaf] = abs_path(node[leaf])
