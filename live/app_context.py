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
from live.host.composer import Composer
from live.host.host import Host
from live.host.session_tap import SessionTap
from live.interaction.compliance import Compliance
from live.interaction.engine import InteractionEngine
from live.interaction.memory import ViewerMemory
from live.interaction.queue import CueQueue
from live.llm.service import LLMService
from live.manager import SessionManager
from live.offline import OfflineManager
from live.pool import WorkerPool
from live.settings.env import Env
from live.settings.store import SettingsCenter
from live.core.usage import UsageMeter
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
        self.usage: UsageMeter | None = None
        self.tts: TTSService | None = None
        self.llm: LLMService | None = None
        self.pool: WorkerPool | None = None
        self.sessions: SessionManager | None = None
        self.offline: OfflineManager | None = None
        self.feed: EventFeed | None = None
        self.danmaku: DanmakuManager | None = None
        self.recorder: Recorder | None = None
        self.memory: ViewerMemory | None = None
        self.cue_queue: CueQueue | None = None
        self.engine: InteractionEngine | None = None
        self.compliance: Compliance | None = None
        self.composer: Composer | None = None
        self.host: Host | None = None
        self._subs: list = []

    # ------------------------------------------------------------------ 生命周期
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        cfg = self.cfg
        os.makedirs(os.path.join(cfg["server"]["data_dir"], "uploads"), exist_ok=True)
        self.usage = UsageMeter(os.path.join(cfg["server"]["data_dir"], "usage.json"))
        self.tts = TTSService(cfg, self.env, self.usage)
        self.llm = LLMService(cfg, self.env, self.usage)
        self._subs.append(self.bus.subscribe("env.changed", self.tts.on_env_changed, name="tts-env"))
        self._subs.append(self.bus.subscribe("env.changed", self.llm.on_env_changed, name="llm-env"))
        self.pool = WorkerPool(cfg)
        self.sessions = SessionManager(cfg, self.pool, self.tts, loop)
        self.offline = OfflineManager(cfg, self.pool, self.tts)
        self.feed = EventFeed(self.bus, loop, max_items=cfg["danmaku"]["feed"]["max_items"])
        self.danmaku = DanmakuManager(cfg, self.bus, self.env)
        self.recorder = Recorder(cfg, self.bus)
        # ---- 互动引擎 + 主持人 ----
        self.memory = ViewerMemory(cfg)
        self.cue_queue = CueQueue(cfg)
        self.engine = InteractionEngine(cfg, self.bus, self.cue_queue, self.memory, self.llm, context=self.host_context,
                                        is_live=lambda: self.danmaku.status().get("state") == "connected")
        self.compliance = Compliance(cfg)
        self.composer = Composer(cfg, self.llm, self.compliance)
        self.sessions.sink_factories.append(lambda sid: SessionTap(self.bus, sid))
        self.host = Host(cfg, self.bus, self.sessions, self.cue_queue, self.composer, self.engine)
        self._subs += [
            self.settings.register(self.tts, ["tts"], name="tts"),
            self.settings.register(self.llm, ["llm"], name="llm"),
            self.settings.register(self.sessions, ["idle"], name="sessions"),
            self.settings.register(self.danmaku, ["danmaku"], name="danmaku"),
            self.settings.register(self.recorder, ["danmaku.record"], name="recorder"),
            self.settings.register(_FeedSize(self.feed, cfg), ["danmaku.feed"], name="feed"),
            self.settings.register(self.engine, ["interaction", "host.pause_interaction"], name="interaction"),
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

    def host_context(self) -> dict:
        """导演渲染话术时用的上下文（当前在讲的产品等；P5 由讲品模块提供）。"""
        return {}

    def stop(self) -> None:
        if self.host is not None:
            self.host.stop()
        if self.engine is not None:
            self.engine.stop()               # 会 flush 长期记忆
        if self.danmaku is not None:
            self.danmaku.stop()             # 先停弹幕子进程，避免残留
        if self.recorder is not None:
            self.recorder.close()
        if self.feed is not None:
            self.feed.close()
        if self.sessions is not None:
            self.sessions.close_all()
        if getattr(self, "usage", None) is not None:
            self.usage.flush()
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
            "llm": self.llm.status() if self.llm else None,
            "danmaku": self.danmaku.status() if self.danmaku else None,
            "interaction": self.engine.stats() if self.engine else None,
        }


class _FeedSize:
    """danmaku.feed.max_items → EventFeed 环形缓冲大小。"""

    def __init__(self, feed: EventFeed, cfg: dict):
        self.feed, self.cfg = feed, cfg

    def apply_settings(self, settings, changed: set) -> None:
        self.feed.set_max_items(self.cfg["danmaku"]["feed"]["max_items"])
