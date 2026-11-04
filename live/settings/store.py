"""设置中心：三层合并 + 校验 + 原子持久化 + 版本快照/回滚 + 热更新通知。

层次（后者覆盖前者）：
  schema 默认值  ←  live/configs/*.yaml（部署配置）  ←  live_data/settings.json（Web 写入）  ←  启动参数（--port 等）

运行中的配置是**一个** dict（self.cfg），所有模块共享同一个对象。热更新时原地改它：
  - 每次使用时读 cfg 的模块（如 speech.*、live.safety_factor）立即生效；
  - 缓存了对象的模块（TTS 引擎、空闲音频源）实现 Reconfigurable，经 register() 订阅后重建。
apply=restart 的键只保存，不改 self.cfg，GET /api/settings 里标 pending_restart。

变更流程：update(changes) → 逐项校验（失败整批不生效、返回逐项错误）→ 写 settings.json（临时文件 + os.replace）
→ 写一份版本快照（保留最近 keep_history 份）→ 原地改 cfg → 发布 settings.changed。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time

from loguru import logger

from live.config import _deep_merge, abs_path, load_config, read_yaml
from live.core.bus import EventBus
from live.settings import schema as S
from live.settings.view import SettingsView


def _atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _normalize(f: S.Field, value):
    return abs_path(value) if f.path else value


class SettingsCenter:
    def __init__(self, cfg: dict, *, yaml_layer: dict | None = None, cli_layer: dict | None = None,
                 runtime_path: str | None = None, history_dir: str | None = None, bus: EventBus | None = None,
                 keep_history: int = 20, runtime_applied: bool = False):
        """cfg：已合并好的运行配置（会被原地修改）。一般用 from_files() / from_cfg() 构造。"""
        self.cfg = cfg
        self.bus = bus or EventBus()
        self.view = SettingsView(cfg)
        self.keep_history = keep_history
        data_dir = cfg["server"]["data_dir"]
        self.runtime_path = runtime_path or os.path.join(data_dir, "settings.json")
        self.history_dir = history_dir or os.path.join(data_dir, "settings_history")
        self._yaml = S.flatten(yaml_layer or {})
        self._cli = S.flatten(cli_layer or {})
        self._lock = threading.RLock()
        self._runtime: dict = self._load_runtime()
        self.version = self._latest_version()
        if not runtime_applied:                      # from_cfg：运行时覆盖还没进 cfg，这里补上
            for key, v in self._runtime.items():
                f = S.get_field(key)
                if f and key not in self._cli:
                    S.set_in(self.cfg, key, _normalize(f, v))

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_files(cls, yaml_path: str | None, cli_overrides: dict | None = None, *,
                   bus: EventBus | None = None, runtime_path: str | None = None) -> "SettingsCenter":
        """正式启动：读 yaml + Web 覆盖 + 启动参数，合并成运行配置。"""
        yaml_layer = read_yaml(yaml_path)
        cli = copy.deepcopy(cli_overrides or {})
        probe = load_config(yaml_path, cli)          # 先算出 data_dir，才知道 settings.json 在哪
        rpath = runtime_path or os.path.join(probe["server"]["data_dir"], "settings.json")
        runtime = cls._read_runtime_file(rpath)
        runtime.pop("server.data_dir", None)
        tree: dict = {}
        for key, v in runtime.items():
            S.set_in(tree, key, v)
        cfg = load_config(yaml_path, _deep_merge(tree, cli))
        return cls(cfg, yaml_layer=yaml_layer, cli_layer=cli, runtime_path=rpath, bus=bus, runtime_applied=True)

    @classmethod
    def from_cfg(cls, cfg: dict, *, bus: EventBus | None = None, runtime_path: str | None = None,
                 history_dir: str | None = None) -> "SettingsCenter":
        """传入已合并好的 cfg（老接口 create_app(cfg)、测试）：与默认值不同的键算作 yaml 层。"""
        defaults = S.flatten(S.defaults_tree())
        flat = S.flatten(cfg)
        yaml_like = {}
        for k, v in flat.items():
            f = S.get_field(k)
            if f is not None and _normalize(f, defaults.get(k)) != v:
                S.set_in(yaml_like, k, v)
        return cls(cfg, yaml_layer=yaml_like, runtime_path=runtime_path, history_dir=history_dir, bus=bus)

    # ------------------------------------------------------------------ 持久化
    @staticmethod
    def _read_runtime_file(path: str) -> dict:
        if not path or not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"设置覆盖文件读取失败（{path}），忽略: {e}")
            return {}
        overrides = data.get("overrides", data) if isinstance(data, dict) else {}
        out = {}
        for key, v in (overrides or {}).items():
            f = S.get_field(key)
            if f is None:
                logger.warning(f"设置覆盖里有未知键 {key}，忽略")
                continue
            try:
                out[key] = S.validate(f, v)
            except S.ValidationError as e:
                logger.warning(f"设置覆盖 {key}={v!r} 不合法（{e}），忽略")
        return out

    def _load_runtime(self) -> dict:
        data = self._read_runtime_file(self.runtime_path)
        data.pop("server.data_dir", None)
        return data

    def _latest_version(self) -> int:
        try:
            names = [n for n in os.listdir(self.history_dir) if n.endswith(".json")]
            return max((int(n.split(".")[0]) for n in names if n.split(".")[0].isdigit()), default=0)
        except FileNotFoundError:
            return 0

    def _persist(self, runtime: dict, source: str, keys: list) -> int:
        version = self.version + 1
        _atomic_write_json(self.runtime_path, {"version": version, "saved_at": time.time(), "overrides": runtime})
        _atomic_write_json(os.path.join(self.history_dir, f"{version:06d}.json"),
                           {"version": version, "ts": time.time(), "source": source, "keys": keys,
                            "overrides": runtime})
        self.version = version
        self._prune_history()
        return version

    def _prune_history(self) -> None:
        try:
            names = sorted(n for n in os.listdir(self.history_dir) if n.endswith(".json"))
        except FileNotFoundError:
            return
        for n in names[:-self.keep_history]:
            try:
                os.remove(os.path.join(self.history_dir, n))
            except OSError:
                pass

    # ------------------------------------------------------------------ 取值
    def get(self, key: str, default=None):
        """运行中的值（需重启的项改了但没重启时，这里仍是旧值）。"""
        return S.get_in(self.cfg, key, default)

    def _base(self, key: str):
        """不算 Web 覆盖时的值：启动参数 > yaml > 默认。"""
        if key in self._cli:
            return self._cli[key]
        if key in self._yaml:
            return self._yaml[key]
        return S.FIELD_MAP[key].default

    def saved(self, key: str):
        """保存后的有效值（重启后会用的值）。"""
        if key in self._cli:
            return self._cli[key]
        if key in self._runtime:
            return self._runtime[key]
        return self._base(key)

    def source(self, key: str) -> str:
        if key in self._cli:
            return "cli"
        if key in self._runtime:
            return "runtime"
        if key in self._yaml:
            return "yaml"
        return "default"

    def pending_restart(self) -> list:
        out = []
        for f in S.FIELDS:
            if f.apply == S.RESTART and _normalize(f, self.saved(f.key)) != self.get(f.key):
                out.append(f.key)
        return out

    def describe(self) -> dict:
        pending = set(self.pending_restart())
        items = []
        for f in S.FIELDS:
            d = f.describe()
            d["default"] = copy.deepcopy(f.default)
            d["value"] = copy.deepcopy(self.saved(f.key))
            d["running"] = copy.deepcopy(self.get(f.key))
            d["source"] = self.source(f.key)
            d["pending_restart"] = f.key in pending
            d["locked"] = f.readonly or f.key in self._cli   # 启动参数指定的项 Web 改不了
            items.append(d)
        return {"version": self.version, "groups": [{"id": g, "label": l} for g, l in S.GROUPS],
                "fields": items, "pending_restart": sorted(pending)}

    # ------------------------------------------------------------------ 修改
    def update(self, changes: dict, source: str = "update") -> dict:
        errors: dict = {}
        normalized: dict = {}
        for key, v in (changes or {}).items():
            f = S.get_field(key)
            if f is None:
                errors[key] = "未知设置项"
                continue
            if f.readonly:
                errors[key] = "只能在配置文件里修改"
                continue
            if key in self._cli:
                errors[key] = "该项由启动参数指定，Web 上不能改"
                continue
            try:
                normalized[key] = S.validate(f, v)
            except S.ValidationError as e:
                errors[key] = str(e)
        with self._lock:
            if not errors:
                errors.update(self._cross_errors({**self._runtime, **normalized}))
            if errors:
                return {"ok": False, "errors": errors}
            new_runtime = {**self._runtime, **normalized}
            return self._commit(new_runtime, source)

    def reset(self, keys: list | None = None) -> dict:
        """恢复默认：去掉 Web 覆盖（keys 为空 = 全部），回到 yaml/默认值。"""
        with self._lock:
            new_runtime = {} if not keys else {k: v for k, v in self._runtime.items() if k not in set(keys)}
            errors = self._cross_errors(new_runtime)
            if errors:
                return {"ok": False, "errors": errors}
            return self._commit(new_runtime, "reset")

    def history(self) -> list:
        out = []
        try:
            names = sorted((n for n in os.listdir(self.history_dir) if n.endswith(".json")), reverse=True)
        except FileNotFoundError:
            return out
        for n in names:
            try:
                with open(os.path.join(self.history_dir, n), "r", encoding="utf-8") as f:
                    d = json.load(f)
                out.append({"version": d.get("version"), "ts": d.get("ts"), "source": d.get("source"),
                            "keys": d.get("keys", []), "current": d.get("version") == self.version})
            except Exception:
                continue
        return out

    def rollback(self, version: int) -> dict:
        path = os.path.join(self.history_dir, f"{int(version):06d}.json")
        if not os.path.isfile(path):
            return {"ok": False, "errors": {"version": f"版本 {version} 不存在（只保留最近 {self.keep_history} 份）"}}
        with open(path, "r", encoding="utf-8") as f:
            overrides = json.load(f).get("overrides") or {}
        return self.import_(overrides, source=f"rollback:{version}")

    def export(self) -> dict:
        with self._lock:
            return {"version": self.version, "overrides": copy.deepcopy(self._runtime)}

    def import_(self, overrides: dict, source: str = "import") -> dict:
        """整体替换 Web 覆盖层（导入 / 回滚）。"""
        errors, normalized = {}, {}
        for key, v in (overrides or {}).items():
            f = S.get_field(key)
            if f is None:
                errors[key] = "未知设置项"
                continue
            if f.readonly or key in self._cli:
                continue
            try:
                normalized[key] = S.validate(f, v)
            except S.ValidationError as e:
                errors[key] = str(e)
        with self._lock:
            if not errors:
                errors.update(self._cross_errors(normalized))
            if errors:
                return {"ok": False, "errors": errors}
            return self._commit(normalized, source)

    def _cross_errors(self, runtime: dict) -> dict:
        def prospective(k):
            if k in self._cli:
                return self._cli[k]
            return runtime[k] if k in runtime else self._base(k)
        new = S.cross_check(prospective)
        old = S.cross_check(self.saved)
        return {k: v for k, v in new.items() if k not in old}   # 只报这次改动新引入的问题

    def _commit(self, new_runtime: dict, source: str) -> dict:
        old_saved = {f.key: self.saved(f.key) for f in S.FIELDS}
        old_runtime = self._runtime
        self._runtime = new_runtime
        changed = [k for k in old_saved if self.saved(k) != old_saved[k]]
        override_changed = sorted(set(old_runtime) ^ set(new_runtime) |
                                  {k for k in new_runtime if old_runtime.get(k) != new_runtime[k]})
        version = self._persist(new_runtime, source, changed or override_changed) \
            if (changed or override_changed) else self.version
        hot, restart, old_vals, new_vals = [], [], {}, {}
        for key in changed:
            f = S.FIELD_MAP[key]
            if f.apply == S.HOT:
                old_vals[key] = copy.deepcopy(self.get(key))
                S.set_in(self.cfg, key, _normalize(f, copy.deepcopy(self.saved(key))))
                new_vals[key] = copy.deepcopy(self.get(key))
                hot.append(key)
            else:
                restart.append(key)
        if changed:
            logger.info(f"设置已保存 v{version}（{source}）：即时生效 {hot or '无'}，需重启 {restart or '无'}")
            self.bus.publish("settings.changed", {"keys": hot, "restart_keys": restart, "old": old_vals,
                                                  "new": new_vals, "version": version, "source": source})
        return {"ok": True, "applied": hot, "restart": restart, "version": version,
                "pending_restart": self.pending_restart()}

    # ------------------------------------------------------------------ 订阅
    def register(self, obj, prefixes, name: str | None = None):
        """obj.apply_settings(view, changed_keys) 在 prefixes 下的热更新键变化时被调用（总线工作线程里）。"""
        prefixes = tuple(prefixes)

        def match(k: str) -> bool:
            return any(p == "*" or k == p or k.startswith(p + ".") for p in prefixes)

        def handler(_topic, payload):
            keys = {k for k in (payload or {}).get("keys", []) if match(k)}
            if keys:
                obj.apply_settings(self.view, keys)

        return self.bus.subscribe("settings.changed", handler, name=name or f"settings->{type(obj).__name__}")
