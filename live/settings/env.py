"""密钥与外部服务地址：只从 .env 读（入库的是 .env.example），模块不直接读 os.environ。

取值顺序：.env 文件里的非空值 > 进程环境变量（便于 CI / 临时覆盖）。
重新加载（POST /api/settings/reload-env）后发布 env.changed（只带键名，不带值），LLM/TTS 客户端据此重建。
Web 与日志都不回显密钥：status() 只给"已配置/未配置 + 来源"，非密钥项（地址、模型名）才给值。
"""
from __future__ import annotations

import os
import threading

from dotenv import dotenv_values
from loguru import logger

# (键, 是否密钥, 说明)
ENV_KEYS = [
    ("ADMIN_TOKEN", True, "Web/接口写操作口令；不设则只允许本机写"),
    ("LLM_API_URL", False, "OpenAI 兼容地址（写到 /v1 或完整 /chat/completions）"),
    ("LLM_API_KEY", True, "LLM 密钥"),
    ("LLM_MODEL", False, "LLM 模型名"),
    ("LLM_TIMEOUT", False, "LLM 超时（秒）"),
    ("EMBEDDING_API_URL", False, "Embedding 地址（可选）"),
    ("EMBEDDING_API_KEY", True, "Embedding 密钥（可选）"),
    ("EMBEDDING_MODEL", False, "Embedding 模型名（可选）"),
    ("VOLC_TTS_API_KEY", True, "火山 TTS 新控制台 API Key"),
    ("VOLC_APP_ID", True, "火山旧控制台 App ID"),
    ("VOLC_ACCESS_TOKEN", True, "火山旧控制台 Access Token"),
    ("VOLC_VOICE_TYPE", False, "火山声音复刻音色 ID（S_xxx）"),
    ("VOLC_RESOURCE_ID", False, "火山资源 ID（seed-icl-2.0 等）"),
    ("DOUYIN_COOKIE_FILE", False, "抖音登录 cookie 文件路径"),
]
SECRET_KEYS = {k for k, secret, _ in ENV_KEYS if secret}


def mask(value: str | None) -> str:
    """日志里描述一个密钥：只说长度，不露内容。"""
    return f"<{len(value)} 字符>" if value else "<未配置>"


class Env:
    def __init__(self, path: str | None, bus=None):
        self.path = path
        self.bus = bus
        self._file: dict = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> dict:
        data = {}
        if self.path and os.path.isfile(self.path):
            try:
                data = {k: (v or "") for k, v in dotenv_values(self.path, encoding="utf-8").items()}
            except Exception as e:
                logger.warning(f".env 解析失败（{self.path}）: {e}")
        with self._lock:
            self._file = data
        return data

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            v = self._file.get(key)
        if v not in (None, ""):
            return v
        v = os.environ.get(key)
        return v if v not in (None, "") else default

    def get_float(self, key: str, default: float) -> float:
        try:
            return float(self.get(key) or default)
        except ValueError:
            return default

    def source(self, key: str) -> str | None:
        with self._lock:
            if self._file.get(key):
                return "file"
        return "env" if os.environ.get(key) else None

    def reload(self) -> list:
        """重新读 .env，返回值有变化的键名，并发布 env.changed。"""
        before = {k: self.get(k) for k, _, _ in ENV_KEYS}
        self._load()
        changed = [k for k, _, _ in ENV_KEYS if self.get(k) != before[k]]
        logger.info(f".env 已重新加载，变化的键: {changed or '无'}")
        if self.bus is not None:
            self.bus.publish("env.changed", {"keys": changed})
        return changed

    def status(self) -> list:
        out = []
        for key, secret, desc in ENV_KEYS:
            v = self.get(key)
            item = {"key": key, "secret": secret, "desc": desc, "configured": bool(v), "source": self.source(key)}
            if not secret:
                item["value"] = v or ""
            out.append(item)
        return out
