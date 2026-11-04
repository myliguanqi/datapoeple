"""会话管理：创建/查找/关闭直播会话，每个会话自动挂一个 WebSocket 推流端。"""
from __future__ import annotations

import asyncio
import re
import threading
import uuid

from loguru import logger

from live.pool import WorkerPool
from live.session import LiveSession
from live.sinks import WsHub
from live.tts.base import TTSEngine

_SID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class SessionManager:
    def __init__(self, cfg: dict, pool: WorkerPool, tts: TTSEngine, loop: asyncio.AbstractEventLoop):
        self.cfg = cfg
        self.pool = pool
        self.tts = tts
        self.loop = loop
        self.sessions: dict = {}
        self.hubs: dict = {}
        self.sink_factories: list = []       # [(sid) -> FrameSink]：新会话自动挂上（如主持人的 SessionTap）
        self._lock = threading.Lock()

    def create(self, session_id: str | None = None, model: str | None = None, avatar: str | None = None,
               seed: int | None = None, use_face_crop: bool | None = None,
               gpu_id: int | None = None) -> LiveSession:
        """阻塞调用（要等 worker 准备好参考图），在线程里调用。"""
        lc = self.cfg["live"]
        sid = session_id or uuid.uuid4().hex[:8]
        if not _SID_RE.match(sid):
            raise ValueError("session_id 只能包含字母数字下划线和横线")
        model = model or self.cfg["models"]["default_model"]
        if model not in ("lite", "pro"):
            raise ValueError(f"未知模型: {model}")
        with self._lock:
            if sid in self.sessions and self.sessions[sid].state not in ("closed", "error"):
                raise ValueError(f"会话 {sid} 已存在")
            worker = self.pool.pick("live", gpu_id=gpu_id)
            sess = LiveSession(sid, self.cfg, worker, self.tts, model=model,
                               avatar=avatar or lc["default_avatar"],
                               seed=lc["seed"] if seed is None else int(seed),
                               use_face_crop=lc["use_face_crop"] if use_face_crop is None else bool(use_face_crop))
            hub = WsHub(self.cfg, self.loop)
            sess.add_sink(hub)
            for make in self.sink_factories:
                sess.add_sink(make(sid))
            self.sessions[sid] = sess
            self.hubs[sid] = hub
        try:
            sess.start()
        except Exception:
            with self._lock:
                self.sessions.pop(sid, None)
                self.hubs.pop(sid, None)
            raise
        return sess

    def get(self, sid: str) -> LiveSession | None:
        return self.sessions.get(sid)

    def hub(self, sid: str) -> WsHub | None:
        return self.hubs.get(sid)

    def list(self) -> list:
        return [s.status() for s in self.sessions.values()]

    def close(self, sid: str) -> bool:
        with self._lock:
            sess = self.sessions.pop(sid, None)
            self.hubs.pop(sid, None)
        if sess is None:
            return False
        sess.close()
        return True

    def apply_settings(self, settings, changed: set) -> None:
        """Reconfigurable：idle.* 变了就让每个会话重建空闲音频源。"""
        if any(k.startswith("idle.") for k in changed):
            for sess in list(self.sessions.values()):
                if sess.state == "running":
                    sess.reload_idle()

    def close_all(self) -> None:
        for sid in list(self.sessions):
            try:
                self.close(sid)
            except Exception:
                logger.exception(f"关闭会话 {sid} 失败")
