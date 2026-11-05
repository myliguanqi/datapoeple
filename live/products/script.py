"""ProductScript：讲品轮播，实现主持人的 script 接口（live/host/host.py）。

    active()            讲品开着（products.rotation_enabled）且有能讲的产品
    next_segment()      主持人空闲时取下一段 → Cue(kind="product", interruptible=True)；段间停顿 products.segment_gap_s 内返回 None
    on_done(cue)        这段讲完 → 下一段（讲完最后一段从头再讲）
    on_interrupted(cue) 被高优先级互动打断 → 不前进，回应完从这一段重讲（断点续讲）
    skip()              跳到下一段（/api/host/script/next）
    activate(pid)       切换当前产品（Web「设为当前讲解」、控制台上一个/下一个）

按时长轮换：当前产品讲了 ≥ duration_s（产品单独设置，否则 products.duration_s）后，在段落边界换到下一个启用的产品。
只播已确认的段（products.require_confirmed_script，默认开）。时间从可注入时钟取（测试用假时钟）。
视频节点（products.video_slot）：产品关联了视频时，每讲完一轮（end）或每轮开讲前（start）产出一条 kind="video" 的 cue，
主持人交给场景播放（布局/声音用 scene.video.default_*），多个视频轮流播。
"""
from __future__ import annotations

import re
import threading

from live.core.clock import SYSTEM_CLOCK, Clock
from live.interaction.cue import Cue

_PH = re.compile(r"\{(product|price|anchor|shop)\}")


class ProductScript:
    def __init__(self, cfg: dict, store, clock: Clock = SYSTEM_CLOCK, bus=None, video_lookup=None):
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.bus = bus
        self.video_lookup = video_lookup or (lambda media_id: None)     # media_id -> 素材 dict（kind=video）| None
        self.video_round = -1                                           # 已经插播过视频的轮次
        self.video_count = 0                                            # 这个产品插播过几次（多个视频轮流）
        self._lock = threading.RLock()
        self.product_id: str | None = None
        self.index = 0
        self.started_at = clock.now()
        self.inflight: dict | None = None          # {"cue_id", "index", "product_id"}
        self.last_end = -1e9
        self.spoken = 0
        self.rotations = 0

    @property
    def pc(self) -> dict:
        return self.cfg["products"]

    # ------------------------------------------------------------------ 选产品
    def playable(self, p) -> list:
        need = bool(self.pc["require_confirmed_script"])
        return [s for s in p.segments if s.text.strip() and (s.confirmed or not need)]

    def candidates(self) -> list:
        return [p for p in self.store.ordered() if p.enabled and self.playable(p)]

    def resolve(self):
        """当前产品（不要求有讲解稿）：Web 设置的 active_id 优先（启用的）；没设或被停用/删除了就取第一个可讲的（再没有就第一个启用的）。"""
        with self._lock:
            pid = self.product_id or self.store.active_id
            p = self.store.get(pid) if pid else None
            if p is not None and p.enabled:
                if self.product_id != p.id:
                    self._switch(p.id, persist=False)
                return p
            cands = self.candidates() or [x for x in self.store.ordered() if x.enabled]
            if not cands:
                if self.product_id is not None:
                    self._switch(None, persist=False)
                return None
            self._switch(cands[0].id)
            return cands[0]

    def current(self):
        """当前产品且有可播的讲解稿；否则 None（讲品不讲）。"""
        p = self.resolve()
        return p if p is not None and self.playable(p) else None

    def current_info(self) -> dict | None:
        """给话术占位符 {product}、知识库检索范围用。"""
        p = self.resolve()
        return None if p is None else {"id": p.id, "name": p.name, "price_text": p.price_text,
                                       "selling_points": list(p.selling_points)}

    def _switch(self, pid: str | None, persist: bool = True) -> None:
        self.product_id = pid
        self.index = 0
        self.video_round = -1
        self.video_count = 0
        self.started_at = self.clock.now()
        self.inflight = None
        if persist:
            try:
                self.store.set_active(pid)
            except KeyError:
                pass

    def activate(self, pid: str) -> None:
        if self.store.get(pid) is None:
            raise KeyError(pid)
        with self._lock:
            self._switch(pid)
            self.last_end = -1e9

    def step_product(self, delta: int) -> str | None:
        """上一个 / 下一个（在启用的产品里循环）。"""
        prods = [p for p in self.store.ordered() if p.enabled]
        if not prods:
            return None
        ids = [p.id for p in prods]
        cur = self.product_id or self.store.active_id
        i = ids.index(cur) if cur in ids else -1
        nxt = ids[(i + delta) % len(ids)]
        self.activate(nxt)
        return nxt

    def duration(self, p) -> float:
        return float(p.duration_s or self.pc["duration_s"])

    # ------------------------------------------------------------------ 主持人接口
    def active(self) -> bool:
        return bool(self.pc["rotation_enabled"]) and self.current() is not None

    def render(self, text: str, p) -> str:
        persona = self.cfg["persona"]
        vals = {"product": p.name, "price": p.price_text or "", "anchor": persona.get("anchor_name") or "主播",
                "shop": persona.get("shop_name") or ""}
        return _PH.sub(lambda m: vals[m.group(1)], text)

    def next_segment(self) -> Cue | None:
        with self._lock:
            if not self.pc["rotation_enabled"] or self.inflight is not None:
                return None
            now = self.clock.now()
            if now - self.last_end < float(self.pc["segment_gap_s"]):
                return None
            p = self.current()
            if p is None:
                return None
            if self.pc["video_slot"] == "end":                      # 讲完一轮：先插播这个产品的视频，再考虑换产品
                cue = self._video_cue(p, need_started=True)
                if cue is not None:
                    return cue
            if now - self.started_at >= self.duration(p):           # 到时间了：段落边界换下一个产品
                cands = self.candidates()
                ids = [c.id for c in cands]
                if len(ids) > 1 and p.id in ids:
                    nxt = cands[(ids.index(p.id) + 1) % len(ids)]
                    self._switch(nxt.id)
                    self.rotations += 1
                    p = nxt
                else:
                    self.started_at = now
            if self.pc["video_slot"] == "start":
                cue = self._video_cue(p, need_started=False)
                if cue is not None:
                    return cue
            segs = self.playable(p)
            if not segs:
                return None
            idx = self.index % len(segs)
            seg = segs[idx]
            cue = Cue(event="product_rotation", kind="product", priority=int(self.pc["priority"]),
                      fallback_text=self.render(seg.text, p), interruptible=True, product_id=p.id,
                      reason=f"讲品「{p.name}」第 {idx + 1}/{len(segs)} 段", emotion="自然介绍", created_at=now,
                      source={"product_id": p.id, "segment_id": seg.id, "index": idx, "total": len(segs)})
            self.inflight = {"cue_id": cue.id, "index": self.index, "product_id": p.id}    # 原始计数（÷段数 = 第几轮）
            return cue

    def videos(self, p) -> list:
        return [v for v in (self.video_lookup(mid) for mid in p.video_ids) if v and v.get("kind") == "video"]

    def _video_cue(self, p, need_started: bool) -> Cue | None:
        """在一轮的边界（index 是段数的整数倍）且这一轮还没播过视频时产出视频 cue。"""
        segs = self.playable(p)
        vids = self.videos(p)
        if not segs or not vids or self.index % len(segs) != 0 or (need_started and self.index == 0):
            return None
        rnd = self.index // len(segs)
        if self.video_round == rnd:
            return None
        v = vids[self.video_count % len(vids)]
        sc = self.cfg["scene"]["video"]
        cue = Cue(event="video_slot", kind="video", priority=int(self.pc["priority"]), product_id=p.id,
                  reason=f"讲品「{p.name}」插播视频「{v['name']}」", created_at=self.clock.now(), fallback_text="",
                  situation={"media_id": v["id"], "name": v["name"], "mode": sc["default_mode"], "audio": sc["default_audio"]},
                  source={"product_id": p.id, "media_id": v["id"]})
        self.inflight = {"cue_id": cue.id, "index": self.index, "product_id": p.id, "video_round": rnd}
        return cue

    def _match(self, cue) -> bool:
        return self.inflight is not None and self.inflight["cue_id"] == cue.id

    def on_done(self, cue) -> None:
        with self._lock:
            if not self._match(cue):
                return
            if "video_round" in self.inflight:                      # 视频节点：记下这一轮播过了，段落不前进
                self.video_round = self.inflight["video_round"]
                self.video_count += 1
                self.inflight = None
                self.last_end = self.clock.now()
                return
            self.index = self.inflight["index"] + 1
            self.inflight = None
            self.last_end = self.clock.now()
            self.spoken += 1

    def on_interrupted(self, cue) -> None:
        with self._lock:
            if not self._match(cue):
                return
            if "video_round" in self.inflight:
                self.video_round = self.inflight["video_round"]     # 视频没播成也不反复重试
                self.video_count += 1
            else:
                self.index = self.inflight["index"]          # 断点续讲：这一段回头重讲
            self.inflight = None
            self.last_end = -1e9

    def skip(self) -> None:
        with self._lock:
            if self.inflight is not None:
                self.index = self.inflight["index"] + 1
                self.inflight = None                     # 之后这条 cue 的打断/完成回调会被忽略
            else:
                self.index += 1
            self.last_end = -1e9

    # ------------------------------------------------------------------ 状态 / 上下文
    def context(self) -> dict:
        info = self.current_info()
        return {} if info is None else {"product_name": info["name"], "product_id": info["id"], "price_text": info["price_text"]}

    def status(self) -> dict:
        with self._lock:
            p = self.resolve()
            segs = self.playable(p) if p else []
            now = self.clock.now()
            reason = None
            if not self.pc["rotation_enabled"]:
                reason = "讲品轮播已关闭（products.rotation_enabled）"
            elif p is None:
                reason = "没有启用的产品"
            elif not segs:
                reason = ("当前产品还没有已确认的讲解稿" if self.pc["require_confirmed_script"] else "当前产品还没有讲解稿")
            nxt_idx = (self.inflight["index"] if self.inflight else self.index) % len(segs) if segs else None
            return {
                "product_id": p.id if p else None, "product_name": p.name if p else None,
                "segment": (nxt_idx + 1) if nxt_idx is not None else None, "segments": len(segs),
                "speaking": self.inflight is not None,
                "next_text": self.render(segs[nxt_idx].text, p)[:60] if segs and nxt_idx is not None else None,
                "elapsed_s": round(now - self.started_at, 1), "duration_s": self.duration(p) if p else None,
                "spoken": self.spoken, "rotations": self.rotations, "inactive_reason": reason,
                "rotation": [{"id": c.id, "name": c.name} for c in self.candidates()],
            }
