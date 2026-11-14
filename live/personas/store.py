"""人设模板的读写：出厂模板 live/personas/builtin/<id>/（只读），用户模板 live_data/personas/<id>/（复制出来的，可改）。

目录结构：persona.yaml + prompt.md + rag_prompt.md + script_prompt.md + triggers.yaml。
用户模板 id 由程序生成（"u" + 随机 8 位十六进制），不用用户输入的名字当目录名（防路径穿越）。
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import uuid

import yaml
from loguru import logger

from live.personas.models import PROMPT_FILES, YAML_KEYS, normalize

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "builtin")
BUILTIN_ORDER = ("commerce", "explainer", "entertainment")
_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_builtin_cache: dict = {}


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _read_yaml(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _dump(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)


def load_dir(path: str, pid: str, builtin: bool) -> dict:
    meta = _read_yaml(os.path.join(path, "persona.yaml"))
    raw = {k: meta[k] for k in YAML_KEYS if k in meta}
    for key, fname in PROMPT_FILES.items():
        raw[key] = _read_text(os.path.join(path, fname))
    raw["triggers"] = _read_yaml(os.path.join(path, "triggers.yaml"))
    raw.update(id=pid, builtin=builtin, source=meta.get("source") or (pid if builtin else None))
    return normalize(raw)


def load_builtin(pid: str) -> dict:
    """出厂模板（只读，按进程缓存）；返回深拷贝。"""
    if pid not in _builtin_cache:
        path = os.path.join(BUILTIN_DIR, pid)
        if not _ID_RE.match(pid or "") or not os.path.isfile(os.path.join(path, "persona.yaml")):
            raise KeyError(pid)
        _builtin_cache[pid] = load_dir(path, pid, builtin=True)
    return copy.deepcopy(_builtin_cache[pid])


def builtin_ids() -> list:
    found = sorted(d for d in os.listdir(BUILTIN_DIR) if os.path.isfile(os.path.join(BUILTIN_DIR, d, "persona.yaml")))
    return [x for x in BUILTIN_ORDER if x in found] + [x for x in found if x not in BUILTIN_ORDER]


class PersonaStore:
    def __init__(self, data_dir: str):
        self.user_dir = os.path.join(data_dir, "personas")

    # ------------------------------------------------------------------ 读
    def user_ids(self) -> list:
        if not os.path.isdir(self.user_dir):
            return []
        out = []
        for d in os.listdir(self.user_dir):
            if _ID_RE.match(d) and os.path.isfile(os.path.join(self.user_dir, d, "persona.yaml")):
                out.append(d)
        return sorted(out, key=lambda d: os.path.getmtime(os.path.join(self.user_dir, d, "persona.yaml")))

    def ids(self) -> list:
        return builtin_ids() + [x for x in self.user_ids() if x not in set(builtin_ids())]

    def exists(self, pid: str) -> bool:
        return pid in self.ids()

    def is_builtin(self, pid: str) -> bool:
        return pid in builtin_ids()

    def load(self, pid: str) -> dict:
        if self.is_builtin(pid):
            return load_builtin(pid)
        if not _ID_RE.match(pid or "") or pid not in self.user_ids():
            raise KeyError(pid)
        return load_dir(os.path.join(self.user_dir, pid), pid, builtin=False)

    def list(self) -> list:
        out = []
        for pid in self.ids():
            try:
                out.append(self.load(pid))
            except Exception as e:                   # noqa: BLE001  坏模板不影响其他模板
                logger.warning(f"人设模板 {pid} 读取失败: {e}")
        return out

    # ------------------------------------------------------------------ 写（只写用户模板）
    def new_id(self) -> str:
        while True:
            pid = "u" + uuid.uuid4().hex[:8]
            if not self.exists(pid):
                return pid

    def save(self, p: dict) -> None:
        pid = p["id"]
        if self.is_builtin(pid) or not _ID_RE.match(pid or ""):
            raise PermissionError(f"出厂模板 {pid} 只读")
        d = os.path.join(self.user_dir, pid)
        os.makedirs(d, exist_ok=True)
        meta = {k: p.get(k) for k in YAML_KEYS}
        meta["source"] = p.get("source")
        for key, fname in PROMPT_FILES.items():
            _atomic_write(os.path.join(d, fname), p.get(key) or "")
        _atomic_write(os.path.join(d, "triggers.yaml"), _dump(p.get("triggers") or {}))
        _atomic_write(os.path.join(d, "persona.yaml"), _dump(meta))     # 最后写：它存在才算一个完整的模板

    def delete(self, pid: str) -> None:
        if self.is_builtin(pid):
            raise PermissionError(f"出厂模板 {pid} 不能删除")
        if pid not in self.user_ids():
            raise KeyError(pid)
        shutil.rmtree(os.path.join(self.user_dir, pid))
