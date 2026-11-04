"""AppContext：统一构造所有服务并互相注入。FastAPI 路由从 request.app.state.ctx 取服务，测试可替换任意服务。

构造顺序：设置中心（含事件总线）+ .env → start(loop) 时创建 TTS、worker 池、会话、离线任务、控制台推送，
并把可热更新的模块注册到设置中心。后续阶段（弹幕、互动、主持人、知识库……）也在这里加。
"""
from __future__ import annotations

import asyncio
import os
import threading

from loguru import logger

from live.config import PROJECT_ROOT
from live.danmaku.manager import DanmakuManager
from live.danmaku.recorder import Recorder
from live.feed import EventFeed
from live.manager import SessionManager
from live.offline import OfflineManager
from live.pool import WorkerPool
from live.settings.env import Env
from live.settings.store import SettingsCenter
from live.tts.service import TTSService


class AppContext:
    def __init__(self, settings: SettingsCenter, env: Env | None = None, *, start_workers: bool = True):
        self.settings = settings
        self.cfg = settings.cfg
        self.bus = settings.bus
        self.env = env or Env(os.path.join(PROJECT_ROOT, ".env"), bus=self.bus)
        if self.env.bus is None:
            self.env.bus = self.bus
        self.start_workers = start_workers
        self.loop: asyncio.AbstractEventLoop | None = None
        self.tts: TTSService | None = None
        self.pool: WorkerPool | None = None
        self.sessions: SessionManager | None = None
        self.offline: OfflineManager | None = None
        self.feed: EventFeed | None = None
        self.danmaku: DanmakuManager | None = None
        self.recorder: Recorder | None = None
        self._subs: list = []

    # ------------------------------------------------------------------ 生命周期
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        cfg = self.cfg
        os.makedirs(os.path.join(cfg["server"]["data_dir"], "uploads"), exist_ok=True)
        self.tts = TTSService(cfg)
        self.pool = WorkerPool(cfg)
        self.sessions = SessionManager(cfg, self.pool, self.tts, loop)
        self.offline = OfflineManager(cfg, self.pool, self.tts)
        self.feed = EventFeed(self.bus, loop, max_items=cfg["danmaku"]["feed"]["max_items"])
        self.danmaku = DanmakuManager(cfg, self.bus, self.env)
        self.recorder = Recorder(cfg, self.bus)
        self._subs += [
            self.settings.register(self.tts, ["tts"], name="tts"),
            self.settings.register(self.sessions, ["idle"], name="sessions"),
            self.settings.register(self.danmaku, ["danmaku"], name="danmaku"),
            self.settings.register(self.recorder, ["danmaku.record"], name="recorder"),
            self.settings.register(_FeedSize(self.feed, cfg), ["danmaku.feed"], name="feed"),
        ]
        self.danmaku.start()
        if self.start_workers:
            threading.Thread(target=self._bootstrap, name="bootstrap", daemon=True).start()

    def _bootstrap(self) -> None:
        cfg = self.cfg
        self.pool.start()
        if cfg["live"]["auto_start"]:
            if not self.pool.wait_any_ready(timeout=cfg["gpu"]["worker_start_timeout"]):
                logger.error("没有可用的 worker，default 会话未创建（看 /api/system 里的错误信息）")
                return
            try:
                self.sessions.create(session_id="default")
            except Exception as e:
                logger.error(f"创建 default 会话失败: {e}")

    def stop(self) -> None:
        if self.danmaku is not None:
            self.danmaku.stop()             # 先停弹幕子进程，避免残留
        if self.recorder is not None:
            self.recorder.close()
        if self.feed is not None:
            self.feed.close()
        if self.sessions is not None:
            self.sessions.close_all()
        if self.pool is not None and self.start_workers:
            self.pool.stop()
        for s in self._subs:
            self.bus.unsubscribe(s)
        self._subs.clear()

    # ------------------------------------------------------------------ 状态
    @property
    def admin_token(self) -> str | None:
        return self.env.get("ADMIN_TOKEN")

    def services_status(self) -> dict:
        return {
            "tts": self.tts.status() if self.tts else None,
            "llm": None,          # P3 接入
            "danmaku": self.danmaku.status() if self.danmaku else None,
        }


class _FeedSize:
    """danmaku.feed.max_items → EventFeed 环形缓冲大小。"""

    def __init__(self, feed: EventFeed, cfg: dict):
        self.feed, self.cfg = feed, cfg

    def apply_settings(self, settings, changed: set) -> None:
        self.feed.set_max_items(self.cfg["danmaku"]["feed"]["max_items"])
