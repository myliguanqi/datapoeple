"""直播场景状态（每路会话一个 SceneState），由 SceneManager 统一管理，变化时：
  - 事件总线发布 scene.changed（控制台、主持人订阅）；
  - 推给这路会话的画面 WebSocket：{"type": "scene", "reason", "scene": 快照}（场景页 /scene 据此切布局、播视频）。

布局 layout：dh_full（只有数字人）| dh_with_card（数字人 + 产品卡片）| video_full（视频全屏）| video_pip（视频 + 数字人画中画）| split（上视频下数字人）
视频：play_video(media_id, mode=full|pip|split, audio=video|mute, loop) → 新的 play_id；结束有两条路：
  - 场景页播完回传 scene.video_ended（带 play_id，对不上的忽略）；
  - 服务端按时长兜底（没开场景页也会按时结束），loop 的不自动结束，要手动停。
视频带原声（audio=video 且文件有音轨）时 blocks_script() 为真：主持人暂停讲品，数字人照常空闲生成；静音视频不挡讲品（数字人可以配解说）。
画布尺寸、数字人/画中画/卡片位置、字幕、ducking、背景都来自 scene.* 设置（热更新：改了立即推一次新快照）。
"""
from __future__ import annotations

import threading
import time
import uuid

from loguru import logger

from live.core.clock import SYSTEM_CLOCK, Clock

MODES = {"full": "video_full", "pip": "video_pip", "split": "split"}
AUDIO = ("video", "mute")
END_GRACE_S = 1.5
DEFAULT_DISCLOSURE = "本直播间为 AI 数字人直播"


class SceneState:
    def __init__(self, sid: str):
        self.sid = sid
        self.version = 0
        self.video: dict | None = None
        self.card_visible = True
        self.subtitle: bool | None = None          # None = 跟设置 scene.subtitle.enabled
        self.last_reason = "init"
        self.last_video: dict | None = None


class SceneManager:
    def __init__(self, cfg: dict, bus=None, media=None, product_info=None, push=None, clock: Clock = SYSTEM_CLOCK,
                 start_thread: bool = True):
        self.cfg = cfg
        self.bus = bus
        self.media = media                          # MediaStore
        self.product_info = product_info or (lambda: None)     # () -> 当前产品 {id, name, price_text, selling_points}
        self.push = push                            # push(sid, event)：推给会话画面 WebSocket
        self.clock = clock
        self.scenes: dict = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        if start_thread:
            threading.Thread(target=self._loop, name="scene-timer", daemon=True).start()

    @property
    def sc(self) -> dict:
        return self.cfg["scene"]

    def get(self, sid: str) -> SceneState:
        with self._lock:
            st = self.scenes.get(sid)
            if st is None:
                st = self.scenes[sid] = SceneState(sid)
            return st

    # ------------------------------------------------------------------ 快照
    def _background(self) -> dict | None:
        bg = (self.sc.get("background") or "").strip()
        if not bg:
            return None
        if bg.startswith("#"):
            return {"color": bg}
        if bg.startswith("media:"):
            it = self.media.get(bg[6:]) if self.media else None
            return {"image": self.media.url(it)} if it else None
        return {"image": bg}

    def snapshot(self, sid: str) -> dict:
        st = self.get(sid)
        with self._lock:
            v = dict(st.video) if st.video else None
            prod = self.product_info() or None
            card_on = bool(self.sc["card"]["enabled"]) and st.card_visible and prod is not None
            layout = MODES[v["mode"]] if v else ("dh_with_card" if card_on else "dh_full")
            sub = self.sc["subtitle"]["enabled"] if st.subtitle is None else st.subtitle
            return {
                "session_id": sid, "version": st.version, "layout": layout, "reason": st.last_reason,
                "canvas": {"width": int(self.sc["width"]), "height": int(self.sc["height"]), "background": self._background(),
                           "dh_rect": dict(self.sc["dh_rect"]), "pip_rect": dict(self.sc["pip_rect"]),
                           "card_rect": dict(self.sc["card_rect"])},
                "card": {"enabled": card_on, "style": self.sc["card"]["style"],
                         "product": None if prod is None else {"id": prod.get("id"), "name": prod.get("name"),
                                                               "price_text": prod.get("price_text") or "",
                                                               "selling_points": list(prod.get("selling_points") or [])[:3]}},
                "video": v, "subtitle": {"enabled": bool(sub)}, "ducking": float(self.sc["ducking"]),
                "disclosure": (self.cfg["compliance"].get("ai_disclosure") or "").strip() or DEFAULT_DISCLOSURE,
                "blocks_script": self._blocks(st), "ts": time.time(),
            }

    def _publish(self, sid: str, reason: str) -> dict:
        st = self.get(sid)
        with self._lock:
            st.version += 1
            st.last_reason = reason
        snap = self.snapshot(sid)
        if self.bus is not None:
            self.bus.publish("scene.changed", {"session_id": sid, "reason": reason, "scene": snap})
        if self.push is not None:
            try:
                self.push(sid, {"type": "scene", "session_id": sid, "reason": reason, "scene": snap})
            except Exception:                        # noqa: BLE001  推送失败不影响状态
                logger.exception("场景推送失败")
        return snap

    def refresh_all(self, reason: str) -> None:
        for sid in list(self.scenes):
            self._publish(sid, reason)

    # ------------------------------------------------------------------ 视频
    def play_video(self, sid: str, media_id: str, mode: str | None = None, audio: str | None = None, loop: bool = False,
                   source: str = "manual") -> dict:
        it = self.media.get(media_id) if self.media else None
        if it is None:
            raise KeyError(media_id)
        if it["kind"] != "video":
            raise ValueError("这是图片，不能当视频播放（可以设为背景）")
        mode = mode or self.sc["video"]["default_mode"]
        audio = audio or self.sc["video"]["default_audio"]
        if mode not in MODES:
            raise ValueError("mode 只能是 full / pip / split")
        if audio not in AUDIO:
            raise ValueError("audio 只能是 video（原声）/ mute（静音）")
        st = self.get(sid)
        with self._lock:
            st.video = {"play_id": uuid.uuid4().hex[:10], "media_id": it["id"], "name": it["name"], "url": self.media.url(it),
                        "mode": mode, "audio": audio, "loop": bool(loop), "duration_s": it.get("duration_s"),
                        "has_audio": bool(it.get("has_audio")), "width": it.get("width"), "height": it.get("height"),
                        "started_at": self.clock.now(), "source": source}
        logger.info(f"场景 {sid}：播放视频「{it['name']}」（{mode} / {'原声' if audio == 'video' else '静音'}）")
        return self._publish(sid, "video_started")

    def stop_video(self, sid: str, reason: str = "video_stopped") -> dict | None:
        st = self.get(sid)
        with self._lock:
            if st.video is None:
                return None
            st.last_video = dict(st.video, ended_at=self.clock.now(), end_reason=reason)
            st.video = None
        return self._publish(sid, reason)

    def page_ended(self, sid: str, play_id: str) -> bool:
        """场景页回传：这个 play_id 播完了。对不上（已被换掉/停掉）的忽略。多个场景页同时回传只算第一个。"""
        st = self.scenes.get(sid)
        if st is None or st.video is None or st.video["play_id"] != play_id or st.video["loop"]:
            return False
        return self.stop_video(sid, "video_ended") is not None

    def tick(self) -> None:
        """服务端兜底：没开场景页（或场景页没回传）也按视频时长结束。"""
        now = self.clock.now()
        for sid, st in list(self.scenes.items()):
            v = st.video
            if v and not v["loop"] and v.get("duration_s") and now - v["started_at"] >= float(v["duration_s"]) + END_GRACE_S:
                self.stop_video(sid, "video_ended")

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self.tick()
            except Exception:                        # noqa: BLE001
                logger.exception("场景计时失败")

    @staticmethod
    def _blocks(st: SceneState) -> bool:
        v = st.video
        return bool(v and v["audio"] == "video" and v.get("has_audio"))

    def blocks_script(self, sid: str) -> bool:
        st = self.scenes.get(sid)
        return st is not None and self._blocks(st)

    # ------------------------------------------------------------------ 其他
    def patch(self, sid: str, card_visible: bool | None = None, subtitle: bool | None = None) -> dict:
        st = self.get(sid)
        with self._lock:
            if card_visible is not None:
                st.card_visible = bool(card_visible)
            if subtitle is not None:
                st.subtitle = bool(subtitle)
        return self._publish(sid, "patched")

    def on_media_deleted(self, media_id: str) -> None:
        for sid, st in list(self.scenes.items()):
            if st.video and st.video["media_id"] == media_id:
                self.stop_video(sid, "video_deleted")

    def on_products_changed(self, _topic=None, _payload=None) -> None:
        self.refresh_all("product")

    def apply_settings(self, settings, changed: set) -> None:
        self.refresh_all("settings")

    def close(self) -> None:
        self._stop.set()
