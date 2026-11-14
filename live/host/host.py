"""主持人编排循环（03 §8）：绑定一路直播会话（host.session_id），是唯一决定"数字人下一句说什么"的地方。

    循环：
      1. 正在播报（当前请求没收到 speak_done、没超时）：
           讲品段（interruptible）且队列里最高优先级 ≥ host.interrupt_priority → 打断，记下断点，回到 2；否则等
      2. cue = CueQueue.get()（过期的已丢）：写词（Composer，独立线程）→ 追加式请求逐句交给会话；
         讲品进行中、优先级 < host.min_priority_during_script 的低优先级互动直接跳过
      3. 没有 cue：讲品（script 提供下一段，P5）；都没有就空闲（数字人照常空闲生成）
      4. 每一步更新决策日志（发布 host.action）

人工插话：say(text, interrupt) → kind=manual、优先级 host.manual_priority 的 cue；interrupt=true 先打断当前播报。
暂停：host.pause_interaction（互动 cue 不说、队列清空）、host.pause_script（讲品不讲）。
script 接口（ProductScript 实现）：active() -> bool、next_segment() -> Cue | None、on_interrupted(cue)、on_done(cue)。
  next_segment 也可能给出 kind="video" 的视频节点 → 交给场景（scenes.play_video），不开口。
场景（SceneManager，可为 None）：会话正在放带原声的视频时不讲品（script_active 为假），视频一开播就打断正在讲的讲品段；
  互动、人工插话照常（数字人声音在场景页按 scene.ducking 压低）。
"""
from __future__ import annotations

import threading
import time

from loguru import logger

from live.core.clock import SYSTEM_CLOCK, Clock
from live.host.composer import Composer
from live.interaction.cue import Cue
from live.interaction.queue import CueQueue


class Host:
    def __init__(self, cfg: dict, bus, sessions, queue: CueQueue, composer: Composer, engine=None,
                 clock: Clock = SYSTEM_CLOCK, script=None, start_thread: bool = True, scenes=None):
        self.cfg = cfg
        self.bus = bus
        self.sessions = sessions             # SessionManager（或测试替身：.get(sid) 返回会话）
        self.queue = queue
        self.composer = composer
        self.engine = engine                 # InteractionEngine（决策日志）；可为 None
        self.clock = clock
        self.script = script
        self.scenes = scenes
        self._cv = threading.Condition()
        self.current: dict | None = None
        self.history: list = []
        self._stop = threading.Event()
        self.sub = bus.subscribe("session.event", self._on_session_event, queue_size=5000, name="host") if bus else None
        self.scene_sub = bus.subscribe("scene.changed", self._on_scene, name="host-scene") if bus else None
        self._thread = None
        if start_thread:
            self._thread = threading.Thread(target=self._loop, name="host", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ 配置
    @property
    def hc(self) -> dict:
        return self.cfg["host"]

    @property
    def session_id(self) -> str:
        return self.hc["session_id"] or "default"

    def _session(self):
        sess = self.sessions.get(self.session_id)
        return sess if sess is not None and getattr(sess, "state", "running") == "running" else None

    def video_blocking(self) -> bool:
        return self.scenes is not None and self.scenes.blocks_script(self.session_id)

    def script_active(self) -> bool:
        return (self.script is not None and not self.hc["pause_script"] and not self.video_blocking()
                and bool(getattr(self.script, "active", lambda: False)()))

    def _on_scene(self, _topic, ev: dict) -> None:
        """带原声的视频开播：正在讲的讲品段让路（打断，视频结束后从这段续讲）。"""
        if ev.get("session_id") != self.session_id or ev.get("reason") != "video_started":
            return
        if not (ev.get("scene") or {}).get("blocks_script"):
            return
        with self._cv:
            cur = self.current
        if cur is not None and cur["cue"].kind == "product":
            sess = self._session()
            if sess is not None:
                sess.interrupt()
            with self._cv:
                if self.current is cur:
                    self._finish_locked("interrupted", "播放带原声的视频，讲品让路")

    # ------------------------------------------------------------------ 会话事件
    def _on_session_event(self, _topic, ev: dict) -> None:
        if ev.get("session_id") != self.session_id:
            return
        t = ev.get("type")
        with self._cv:
            cur = self.current
            if cur is None:
                return
            if t == "speak_done" and ev.get("request_id") == cur["request_id"]:
                self._finish_locked("spoken")
            elif t == "interrupted" and cur["request_id"] in (ev.get("request_ids") or []):
                self._finish_locked("interrupted")
            elif t == "closed":
                self._finish_locked("session_closed")

    def _finish_locked(self, status: str, note: str | None = None) -> None:
        cur = self.current
        if cur is None:
            return
        self.current = None
        cur["cancel"].set()
        cue = cur["cue"]
        dur = round(self.clock.now() - cur["started"], 2)
        self.history.append({"id": cue.id, "event": cue.event, "kind": cue.kind, "status": status, "text": cur.get("text"),
                             "duration_s": dur, "ts": self.clock.now()})
        del self.history[:-50]
        if self.engine is not None:
            self.engine.update(cue.id, status=status, duration_s=dur, **({"note": note} if note else {}))
        if self.script is not None and cue.kind == "product":
            try:
                (self.script.on_interrupted if status == "interrupted" else self.script.on_done)(cue)
            except Exception:
                logger.exception("讲品回调失败")
        self._cv.notify_all()

    # ------------------------------------------------------------------ 对外
    def say(self, text: str, interrupt: bool = False, priority: int | None = None) -> Cue:
        text = (text or "").strip()
        if not text:
            raise ValueError("text 为空")
        cue = Cue(event="manual", kind="manual", priority=int(self.hc["manual_priority"] if priority is None else priority),
                  fallback_text=text, reason="人工插话", max_stale_s=120, created_at=self.clock.now())
        if self.engine is not None:
            self.engine.add_log(cue)
        if interrupt:
            self.interrupt()
        self.queue.push(cue)
        return cue

    def interrupt(self) -> dict | None:
        sess = self._session()
        if sess is None:
            return None
        r = sess.interrupt()
        with self._cv:
            if self.current is not None and self.current["request_id"] in (r.get("request_ids") or [self.current["request_id"]]):
                self._finish_locked("interrupted", "手动打断")
        return r

    def reset(self, reason: str = "切换人设") -> None:
        """切换人设：丢掉正在说的 cue、队列与历史，避免新人设继承旧台词。"""
        self.queue.clear(reason)
        with self._cv:
            cur = self.current
            self.current = None
            if cur is not None:
                cur["cancel"].set()
            self.history.clear()
            self._cv.notify_all()

    def status(self) -> dict:
        with self._cv:
            cur = self.current
            cur_d = None if cur is None else {
                "cue_id": cur["cue"].id, "event": cur["cue"].event, "kind": cur["cue"].kind, "priority": cur["cue"].priority,
                "reason": cur["cue"].reason, "text": cur.get("text"), "text_source": cur.get("source"),
                "request_id": cur["request_id"], "interruptible": cur["interruptible"],
                "elapsed_s": round(self.clock.now() - cur["started"], 1)}
        return {"enabled": bool(self.hc["enabled"]), "session_id": self.session_id, "session_ok": self._session() is not None,
                "current": cur_d, "queue": self.queue.snapshot()[:10], "queue_size": len(self.queue),
                "pause_interaction": bool(self.hc["pause_interaction"]), "pause_script": bool(self.hc["pause_script"]),
                "script_active": self.script_active(), "video_blocking": self.video_blocking(),
                "script": self.script.status() if self.script is not None and hasattr(self.script, "status") else None,
                "history": list(reversed(self.history[-10:]))}

    def stop(self) -> None:
        self._stop.set()
        self.queue.wake()
        with self._cv:
            self._cv.notify_all()
        if self.bus is not None:
            for sub in (self.sub, self.scene_sub):
                if sub is not None:
                    self.bus.unsubscribe(sub)

    # ------------------------------------------------------------------ 循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                logger.exception("主持人循环出错")
                time.sleep(0.5)

    def step(self, wait: float = 0.1) -> None:
        """跑一步（测试可以直接调用）。"""
        if not self.hc["enabled"]:
            time.sleep(0.2)
            return
        sess = self._session()
        if sess is None:
            time.sleep(0.3)
            return
        with self._cv:
            cur = self.current
        if cur is not None:
            self._while_speaking(sess, cur, wait)
            return
        cue = self.queue.get(timeout=wait)
        if cue is not None:
            if cue.kind == "interaction" and self.hc["pause_interaction"]:
                if self.engine is not None:
                    self.engine.update(cue.id, status="dropped", note="互动已暂停")
                return
            if (cue.kind == "interaction" and self.script_active()
                    and cue.priority < int(self.hc["min_priority_during_script"])):
                if self.engine is not None:
                    self.engine.update(cue.id, status="dropped", note="讲品中，跳过低优先级互动")
                return
            self._start(sess, cue)
            return
        if self.script_active():
            seg = self.script.next_segment()
            if seg is not None:
                if self.engine is not None:
                    self.engine.add_log(seg)
                if seg.kind == "video":
                    self._play_video(seg)
                else:
                    self._start(sess, seg)

    def _play_video(self, cue: Cue) -> None:
        """讲品流程里的视频节点：交给场景播放，不开口；这个节点随即算"完成"（视频本身挡不挡讲品由场景判断）。"""
        sit = cue.situation or {}
        status, note = "spoken", None
        try:
            if self.scenes is None:
                raise RuntimeError("没有场景模块")
            self.scenes.play_video(self.session_id, sit.get("media_id"), sit.get("mode"), sit.get("audio"), source="script")
        except Exception as e:                       # noqa: BLE001  素材被删等：记日志，讲品继续
            status, note = "failed", f"视频播放失败：{e}"
            logger.warning(note)
        if self.engine is not None:
            self.engine.update(cue.id, status=status, text=f"播放视频「{sit.get('name') or sit.get('media_id')}」",
                               text_source="video", note=note)
        try:
            self.script.on_done(cue)
        except Exception:                            # noqa: BLE001
            logger.exception("讲品回调失败")

    def _while_speaking(self, sess, cur: dict, wait: float) -> None:
        top = self.queue.peek_priority()
        if cur["interruptible"] and top is not None and top >= int(self.hc["interrupt_priority"]):
            logger.info(f"高优先级互动（{top}）打断讲品段")
            sess.interrupt()
            with self._cv:
                if self.current is cur:
                    self._finish_locked("interrupted", f"被优先级 {top} 的互动打断")
            return
        if self.clock.now() - cur["started"] > float(self.hc["speak_timeout_s"]):
            with self._cv:
                if self.current is cur:
                    self._finish_locked("timeout", "没收到说完事件，超时放行")
            return
        with self._cv:
            if self.current is cur:
                self._cv.wait(timeout=wait)

    def _start(self, sess, cue: Cue) -> None:
        interruptible = bool(cue.interruptible or cue.kind == "product")
        rid = sess.begin_request(interruptible=interruptible)
        cancel = threading.Event()
        cur = {"cue": cue, "request_id": rid, "started": self.clock.now(), "interruptible": interruptible,
               "cancel": cancel, "text": "", "source": None}
        with self._cv:
            self.current = cur
        if self.engine is not None:
            self.engine.update(cue.id, status="composing", request_id=rid,
                               latency_ms=round((self.clock.now() - cue.created_at) * 1000))
        speed = float(self.cfg["tts"]["speed"]) * float(cue.speed or 1.0)

        def emit(sentence: str) -> None:
            if cancel.is_set():
                return
            try:
                sess.append(rid, sentence, speed=speed, emotion=cue.emotion)
                cur["text"] += sentence
            except ValueError:
                cancel.set()                         # 请求已被打断

        def run():
            try:
                res = self.composer.compose(cue, emit, cancel)
            except Exception as e:
                logger.exception("写词失败")
                res = None
                err = str(e)
            sess.end_request(rid)
            if self.engine is not None:
                if res is not None:
                    self.engine.update(cue.id, status="speaking" if res.text else "empty", text=res.text,
                                       text_source=res.source, compliance_hits=res.hits, compose_ms=res.ms,
                                       note=res.error, refs=res.refs)
                else:
                    self.engine.update(cue.id, status="failed", note=err)
            cur["source"] = res.source if res else None
            if res is None or not res.text:
                with self._cv:
                    if self.current is cur and not sess.request_active(rid):
                        self._finish_locked("empty")

        threading.Thread(target=run, name=f"compose-{cue.event}", daemon=True).start()
