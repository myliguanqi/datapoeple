"""AppContext：统一构造所有服务并互相注入。FastAPI 路由从 request.app.state.ctx 取服务，测试可替换任意服务。

构造顺序：设置中心（含事件总线）+ .env → start(loop) 时创建 TTS/LLM、worker 池、会话、离线任务、控制台推送、弹幕、
产品库与讲品轮播、知识库、互动引擎、写词与主持人，并把可热更新的模块注册到设置中心。
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
from live.knowledge.service import KnowledgeBase
from live.llm.service import LLMService
from live.manager import SessionManager
from live.media.scene import SceneManager
from live.media.store import MediaStore
from live.offline import OfflineManager
from live.personas.service import PersonaService
from live.pool import WorkerPool
from live.products.generator import ScriptGenerator
from live.products.script import ProductScript
from live.products.store import ProductStore
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
        self.persona: PersonaService | None = None
        self.danmaku: DanmakuManager | None = None
        self.recorder: Recorder | None = None
        self.memory: ViewerMemory | None = None
        self.cue_queue: CueQueue | None = None
        self.engine: InteractionEngine | None = None
        self.compliance: Compliance | None = None
        self.composer: Composer | None = None
        self.host: Host | None = None
        self.products: ProductStore | None = None
        self.script: ProductScript | None = None
        self.kb: KnowledgeBase | None = None
        self.script_gen: ScriptGenerator | None = None
        self.media: MediaStore | None = None
        self.scenes: SceneManager | None = None
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
        # ---- 人设模板：必须在产品库 / 知识库之前（迁移会把旧数据归到当前人设）----
        self.persona = PersonaService(cfg, self.bus)
        self.pool = WorkerPool(cfg)
        self.sessions = SessionManager(cfg, self.pool, self.tts, loop)
        self.offline = OfflineManager(cfg, self.pool, self.tts)
        self.feed = EventFeed(self.bus, loop, max_items=cfg["danmaku"]["feed"]["max_items"])
        self.danmaku = DanmakuManager(cfg, self.bus, self.env)
        self.recorder = Recorder(cfg, self.bus)
        # ---- 节目单与知识库（每人设一份）----
        self.products = ProductStore(self.persona.program_path(), bus=self.bus)
        self.media = MediaStore(cfg, bus=self.bus)
        self.script = ProductScript(cfg, self.products, bus=self.bus, video_lookup=self.media.get)
        self.scenes = SceneManager(cfg, self.bus, self.media, product_info=self.script.current_info, push=self._push_scene)
        self.kb = KnowledgeBase(cfg, self.bus, self.env, product_lookup=self.product_info,
                                current_product=self.script.current_info)
        # ---- 互动引擎 + 主持人 ----
        self.memory = ViewerMemory(cfg)
        self.cue_queue = CueQueue(cfg)
        self.engine = InteractionEngine(cfg, self.bus, self.cue_queue, self.memory, self.llm, context=self.host_context,
                                        is_live=lambda: self.danmaku.status().get("state") == "connected")
        self.compliance = Compliance(cfg)
        self.composer = Composer(cfg, self.llm, self.compliance, retrieve=self.kb.retrieve)
        self.kb.composer = self.composer
        self.kb.trigger_lookup = self.engine.director.trigger
        self.script_gen = ScriptGenerator(cfg, self.llm, self.kb, self.compliance)
        self.sessions.sink_factories.append(lambda sid: SessionTap(self.bus, sid))
        self.host = Host(cfg, self.bus, self.sessions, self.cue_queue, self.composer, self.engine, script=self.script,
                         scenes=self.scenes)
        self.persona.on_air = self._persona_busy_reason
        self.persona.on_change(self._on_persona_change)
        self._subs.append(self.bus.subscribe("env.changed", self.kb.on_env_changed, name="kb-env"))
        self._subs.append(self.bus.subscribe("products.changed", self.scenes.on_products_changed, name="scene-products"))
        self._subs += [
            self.settings.register(self.tts, ["tts"], name="tts"),
            self.settings.register(self.llm, ["llm"], name="llm"),
            self.settings.register(self.sessions, ["idle"], name="sessions"),
            self.settings.register(self.danmaku, ["danmaku"], name="danmaku"),
            self.settings.register(self.recorder, ["danmaku.record"], name="recorder"),
            self.settings.register(_FeedSize(self.feed, cfg), ["danmaku.feed"], name="feed"),
            self.settings.register(self.engine, ["interaction", "host.pause_interaction"], name="interaction"),
            self.settings.register(self.kb, ["knowledge"], name="knowledge"),
            self.settings.register(self.scenes, ["scene", "compliance.ai_disclosure"], name="scene"),
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
        """导演渲染话术时用的上下文：当前在讲的节目条目（话术里的 {product} / {item} 占位符）。"""
        return self.script.context() if self.script is not None else {}

    # ------------------------------------------------------------------ 人设
    def _persona_busy_reason(self) -> str | None:
        """只有停播（且弹幕断开、知识库不忙）时才允许切换人设。"""
        if self.sessions is not None:
            running = [s for s in self.sessions.list() if s.get("state") in ("starting", "running")]
            if running:
                names = "、".join(s.get("session_id") or "?" for s in running[:3])
                return f"正在直播（会话 {names}）；先在「数字人与系统」页关闭会话再切换人设"
        if self.danmaku is not None:
            st = self.danmaku.status()
            if st.get("state") not in ("stopped", "finished"):
                return "弹幕已连接或正在连接；先在「弹幕互动」页断开弹幕再切换人设"
        if self.kb is not None and self.kb.busy():
            return "知识库正在入库，等入库完成后再切换人设"
        return None

    def _on_persona_change(self, persona: dict, reason: str) -> None:
        """切换 / 编辑当前人设后的全量（或软）重置，保证没有上一个人设的残留。"""
        if reason == "switch" and self.host is not None:
            self.host.interrupt()
            self.host.reset("切换人设")              # 先取消旧 cue，别让它在 script 切换后回调
        if self.engine is not None:
            if reason == "switch":
                self.engine.reset("切换人设")
            else:
                self.engine.reload_persona("人设已修改")
        if reason != "switch":
            if self.scenes is not None:
                self.scenes.refresh_all("persona")
            return
        # ---- 切换：节目单、知识库、LLM 防复读记忆、场景全部换到新人设 ----
        if self.products is not None and self.persona is not None:
            self.products.switch_path(self.persona.program_path(), reason="persona")
        if self.script is not None:
            self.script.reset()
        if self.kb is not None:
            self.kb.switch_persona(persona.get("id") or self.persona.active_id)
        if self.llm is not None:
            self.llm.clear_history()
        if self.scenes is not None:
            for sid in list(getattr(self.scenes, "scenes", {})):
                self.scenes.stop_video(sid, "persona_switch")
            self.scenes.refresh_all("persona")
        logger.info(f"人设已切换：{persona.get('name')}（{persona.get('id')}）")

    def _push_scene(self, sid: str, event: dict) -> None:
        """场景变化推给这路会话的画面 WebSocket（场景页 /scene 收到后切布局、播视频）。会话还没建好就不推。"""
        hub = self.sessions.hub(sid) if self.sessions is not None else None
        if hub is not None:
            hub.on_event(event)

    def product_info(self, pid: str | None) -> dict | None:
        p = self.products.get(pid) if (self.products is not None and pid) else None
        return None if p is None else {"id": p.id, "name": p.name, "price_text": p.price_text}

    def stop(self) -> None:
        if self.host is not None:
            self.host.stop()
        if self.engine is not None:
            self.engine.stop()               # 会 flush 长期记忆
        if self.danmaku is not None:
            self.danmaku.stop()             # 先停弹幕子进程，避免残留
        if self.kb is not None:
            self.kb.close()
        if self.scenes is not None:
            self.scenes.close()
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
        p = self.persona.active() if self.persona is not None else None
        return {
            "persona": None if p is None else {"id": p.get("id"), "name": p.get("name"),
                                               "anchor_name": p.get("anchor_name"),
                                               "busy": self.persona.busy_reason() if self.persona else None},
            "tts": self.tts.status() if self.tts else None,
            "llm": self.llm.status() if self.llm else None,
            "danmaku": self.danmaku.status() if self.danmaku else None,
            "interaction": self.engine.stats() if self.engine else None,
            "knowledge": self.kb.status() if self.kb else None,
        }


class _FeedSize:
    """danmaku.feed.max_items → EventFeed 环形缓冲大小。"""

    def __init__(self, feed: EventFeed, cfg: dict):
        self.feed, self.cfg = feed, cfg

    def apply_settings(self, settings, changed: set) -> None:
        self.feed.set_max_items(self.cfg["danmaku"]["feed"]["max_items"])
