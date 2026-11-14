"""PersonaService：当前人设的唯一来源。

- 当前人设解析成 dict 后原地写进 cfg["persona"]（只在内存，不进 settings.json）；其他模块每次用时读 cfg["persona"][...]；
- 当前是哪个记在 live_data/personas/active.json；没有就用 personas.default（部署配置）；
- 切换（activate）只在停播时允许：on_air() 返回非空原因就拒绝（PersonaBusy → API 409）；
  切换成功后调用注册的钩子做全量重置（导演状态、意图队列、互动队列、LLM 最近台词、节目单文件、场景），发布 persona.changed；
- 编辑当前人设（update / update_trigger）立即生效，但不清状态（钩子 reason="edit"）；出厂模板只读（PermissionError → 403）。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
from typing import Callable

from loguru import logger

from live.interaction.signals import TRIGGER_FIELDS
from live.personas import migrate
from live.personas.models import INTENT_NAME_RE, PROMPT_FILES, YAML_KEYS, normalize, summary, validate
from live.personas.store import PersonaStore, load_builtin

EDITABLE_KEYS = set(YAML_KEYS) | set(PROMPT_FILES) | {"triggers"}
TRIGGER_EDITABLE = TRIGGER_FIELDS


class PersonaBusy(RuntimeError):
    """在播，不能切换人设。"""


class PersonaInvalid(ValueError):
    def __init__(self, errors: dict):
        super().__init__("；".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class PersonaService:
    def __init__(self, cfg: dict, bus=None, store: PersonaStore | None = None):
        self.cfg = cfg
        self.bus = bus
        data_dir = cfg["server"]["data_dir"]
        self.store = store or PersonaStore(data_dir)
        self.programs_dir = os.path.join(data_dir, "programs")
        self.active_path = os.path.join(self.store.user_dir, "active.json")
        self.on_air: Callable[[], str | None] = lambda: None
        self._hooks: list = []
        self._lock = threading.RLock()
        active_hint = self._read_active()
        configured = (cfg.get("personas") or {}).get("default") or "commerce"
        self.migration = migrate.migrate_legacy(data_dir, self.store, active_hint or configured)
        pid = self.migration.get("persona_id") or active_hint or configured
        resolved_bad = False
        if not self.store.exists(pid):
            logger.warning(f"人设 {pid!r} 不存在，改用出厂「带货主播」")
            pid, resolved_bad = "commerce", True
        try:
            p = self.store.load(pid)
        except Exception as e:                       # noqa: BLE001  坏模板不阻止服务启动
            logger.error(f"人设 {pid!r} 读取失败（{e}），改用出厂「带货主播」")
            pid, p, resolved_bad = "commerce", load_builtin("commerce"), True
        err = validate(p)
        if err:
            logger.error(f"人设 {pid!r} 校验失败（{err}），改用出厂「带货主播」")
            pid, p, resolved_bad = "commerce", load_builtin("commerce"), True
        self._apply(p)
        if self.migration.get("persona_id") or resolved_bad or (active_hint and active_hint != pid):
            self._write_active(pid)
        logger.info(f"当前人设：{self.cfg['persona']['name']}（{pid}）")

    # ------------------------------------------------------------------ 状态
    @property
    def active_id(self) -> str:
        return self.cfg["persona"]["id"]

    def active(self) -> dict:
        return copy.deepcopy(self.cfg["persona"])

    def program_path(self, pid: str | None = None) -> str:
        return self.program_path_for(pid or self.active_id)

    def program_path_for(self, pid: str) -> str:
        return os.path.join(self.programs_dir, f"{pid}.json")

    def on_change(self, fn: Callable[[dict, str], None]) -> None:
        """注册钩子 fn(persona, reason)：reason = "switch"（切换，要清状态）| "edit"（改了当前人设）。"""
        self._hooks.append(fn)

    def busy_reason(self) -> str | None:
        try:
            return self.on_air()
        except Exception as e:                       # noqa: BLE001
            return f"无法判断是否在播：{e}"

    def _read_active(self) -> str | None:
        try:
            with open(self.active_path, "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("active")
        except (OSError, ValueError):
            return None

    def _write_active(self, pid: str) -> None:
        os.makedirs(os.path.dirname(self.active_path), exist_ok=True)
        tmp = f"{self.active_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"active": pid}, f, ensure_ascii=False)
        os.replace(tmp, self.active_path)

    def _apply(self, p: dict) -> None:
        self.cfg["persona"] = copy.deepcopy(p)       # 一次赋值，读线程不会看到清空到一半的 dict

    def _notify(self, reason: str, strict: bool = False) -> None:
        p = self.active()
        errors = []
        for fn in list(self._hooks):
            try:
                fn(p, reason)
            except Exception as e:                   # noqa: BLE001
                logger.exception("人设切换钩子出错")
                errors.append(e)
        if errors and strict:
            raise errors[0]
        if self.bus is not None:
            self.bus.publish("persona.changed", {"reason": reason, "persona": summary(p)})

    # ------------------------------------------------------------------ 查询
    def list(self) -> list:
        return [{**summary(p), "active": p["id"] == self.active_id} for p in self.store.list()]

    def get(self, pid: str) -> dict:
        p = self.store.load(pid)
        p["active"] = pid == self.active_id
        p["trigger_diff"] = self.trigger_diff(p)
        return p

    def source_of(self, p: dict) -> dict | None:
        src = p.get("source")
        if not src or src == p.get("id"):
            return p if p.get("builtin") else None
        try:
            return self.store.load(src)
        except KeyError:
            return None

    def trigger_diff(self, p: dict) -> dict:
        """每个触发器相对复制来源改过哪些字段（Web 上标「已改」）。"""
        src = self.source_of(p)
        if src is None or src is p:
            return {}
        out = {}
        for name, spec in (p.get("triggers") or {}).items():
            base = (src.get("triggers") or {}).get(name)
            if base is None:
                out[name] = ["新增"]
                continue
            changed = sorted(k for k in set(spec) | set(base) if spec.get(k) != base.get(k))
            if changed:
                out[name] = changed
        return out

    # ------------------------------------------------------------------ 修改
    def duplicate(self, pid: str, name: str | None = None) -> dict:
        """复制模板；节目单文件一起复制（知识库资料不复制，避免大文件悄悄翻倍）。"""
        with self._lock:
            p = self.store.load(pid)
            new_id = self.store.new_id()
            p["id"] = new_id
            p["builtin"] = False
            p["source"] = pid if p.get("source") in (None, pid) else p["source"]
            p["name"] = (name or f"{p['name']}（副本）").strip()[:30]
            err = validate(p)
            if err:
                raise PersonaInvalid(err)
            self.store.save(p)
            program_copied = False
            src = self.program_path_for(pid)
            if os.path.isfile(src):
                dst = self.program_path_for(new_id)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                program_copied = True
            logger.info(f"复制人设 {pid} → {new_id}（{p['name']}）"
                        f"{'，节目单已复制' if program_copied else ''}")
            out = self.get(new_id)
            out["program_copied"] = program_copied
            return out

    def _check_writable(self, pid: str) -> None:
        if self.store.is_builtin(pid):
            raise PermissionError("出厂模板只读：先「复制为新模板」再修改")
        if not self.store.exists(pid):
            raise KeyError(pid)

    def _save(self, p: dict) -> dict:
        p = normalize(p)
        err = validate(p)
        if err:
            raise PersonaInvalid(err)
        self.store.save(p)
        if p["id"] == self.active_id:
            self._apply(self.store.load(p["id"]))
            self._notify("edit")
        return self.get(p["id"])

    def update(self, pid: str, patch: dict) -> dict:
        with self._lock:
            self._check_writable(pid)
            unknown = set(patch) - EDITABLE_KEYS
            if unknown:
                raise PersonaInvalid({k: "不能修改的字段" for k in unknown})
            p = self.store.load(pid)
            for k, v in patch.items():
                if k in ("knowledge", "limits", "program") and isinstance(v, dict):
                    p[k] = {**p[k], **v}
                else:
                    p[k] = v
            return self._save(p)

    def update_trigger(self, pid: str, name: str, fields: dict) -> dict:
        with self._lock:
            self._check_writable(pid)
            p = self.store.load(pid)
            if name not in p["triggers"]:
                raise KeyError(name)
            bad = set(fields) - set(TRIGGER_EDITABLE)
            if bad:
                raise PersonaInvalid({k: "不能在这里修改" for k in bad})
            p["triggers"][name] = {**p["triggers"][name], **fields}
            return self._save(p)

    def add_trigger(self, pid: str, name: str, spec: dict) -> dict:
        with self._lock:
            self._check_writable(pid)
            name = (name or "").strip()
            if not INTENT_NAME_RE.match(name):
                raise PersonaInvalid({"name": "触发器名只能用小写字母、数字、下划线，字母开头"})
            p = self.store.load(pid)
            if name in p["triggers"]:
                raise PersonaInvalid({"name": f"触发器 {name} 已存在"})
            p["triggers"][name] = dict(spec or {})
            return self._save(p)

    def delete_trigger(self, pid: str, name: str) -> dict:
        with self._lock:
            self._check_writable(pid)
            p = self.store.load(pid)
            if name not in p["triggers"]:
                raise KeyError(name)
            if len(p["triggers"]) <= 1:
                raise PersonaInvalid({"triggers": "至少要保留一个触发器"})
            p["triggers"].pop(name)
            return self._save(p)

    def reset_trigger(self, pid: str, name: str) -> dict:
        """触发器恢复成复制来源（出厂模板）里的值。"""
        with self._lock:
            self._check_writable(pid)
            p = self.store.load(pid)
            src = self.source_of(p)
            base = ((src or {}).get("triggers") or {}).get(name)
            if base is None:
                raise KeyError(f"复制来源里没有触发器 {name}")
            p["triggers"][name] = copy.deepcopy(base)
            return self._save(p)

    def delete(self, pid: str, delete_data: bool = False) -> None:
        """删除用户模板。默认保留节目单和知识库资料（方便人工找回）；delete_data=true 时一起删除。"""
        with self._lock:
            self._check_writable(pid)
            if pid == self.active_id:
                raise PersonaBusy("不能删除当前人设：先切换到别的人设")
            self.store.delete(pid)
            if delete_data:
                for path in (self.program_path_for(pid), os.path.join(self.cfg["server"]["data_dir"], "kb", pid)):
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path)
                        elif os.path.isfile(path):
                            os.remove(path)
                    except OSError as e:
                        logger.warning(f"删除人设 {pid} 的数据失败（{path}）：{e}")
                logger.info(f"已删除人设 {pid} 的节目单和知识库数据")

    def activate(self, pid: str) -> dict:
        with self._lock:
            if not self.store.exists(pid):
                raise KeyError(pid)
            if pid == self.active_id:
                return self.get(pid)
            reason = self.busy_reason()
            if reason:
                raise PersonaBusy(reason)
            p = self.store.load(pid)
            err = validate(p)
            if err:
                raise PersonaInvalid(err)
            prev_id, prev_persona = self.active_id, self.active()
            self._apply(p)
            try:
                self._notify("switch", strict=True)      # KB 目录、节目单、队列全部换成功后才算切换完成
            except Exception as e:                       # noqa: BLE001
                logger.error(f"切换人设失败，回滚到 {prev_id}：{e}")
                self._apply(prev_persona)
                try:
                    self._notify("switch")
                except Exception:                        # noqa: BLE001
                    logger.exception("回滚人设失败")
                raise PersonaBusy(f"切换失败，已恢复「{prev_persona.get('name')}」：{e}")
            self._write_active(pid)
            logger.info(f"切换人设：{prev_id} → {pid}（{p['name']}）")
            return self.get(pid)


def default_persona(cfg: dict) -> dict:
    """不经过 PersonaService（worker / 单元测试直接 load_config）时的 cfg["persona"]：部署配置指定的出厂模板。"""
    pid = (cfg.get("personas") or {}).get("default") or "commerce"
    try:
        return load_builtin(pid)
    except KeyError:
        return load_builtin("commerce")
