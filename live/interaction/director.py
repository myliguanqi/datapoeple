"""导演层（移植参考项目 decision/director.py 的平台层，去掉游戏信号，加上带货信号）。

    snapshot(RoomSnapshot) ──tick──▶ Cue（触发器名、优先级、情绪、语速、给 LLM 的指令与局势、兜底台词）或 None

- 信号派生 _derive()：礼物 S/M/L 分档（单笔总价值，互斥）、连续送礼、送礼后回踩、本场礼物榜榜首易主（挂起等空档播）、
  欢迎分级（金主回归 / 高财富等级 / 熟客 / 本场回头客 / 普通）、关注、粉丝团、分享、点赞最猛者、人数猛涨/下滑/破整（台阶锁存）、
  冷场点名；带货新增：产品提问、购买意向、闲聊弹幕。
- 触发器表：policies/commerce.yaml（出厂）+ 设置 interaction.triggers（Web 覆盖，热更新）；按 priority 高→低。
- 冷却：每个触发器 cooldown_s；点赞最猛者还是同一人时冷却 × interaction.like.repeat_cooldown_factor。
- 发声节流：优先级 < interaction.urgent_priority 的，要等"距上次发声 ≥ 动态间隔"（能量越高间隔越短）。
- 同拍合并：who（点名）+ what（场面）都命中且在 merge_with 白名单里 → 合成一句。
- _handled：同一条弹幕/礼物/进场只被一个触发器"消费"一次，不会被两个触发器重复念。
所有阈值每拍现读 cfg，Web 改了下一拍生效。
"""
from __future__ import annotations

import os
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import yaml
from loguru import logger

from live.core.clock import SYSTEM_CLOCK, Clock
from live.interaction.cue import Cue
from live.interaction.names import honor, short_name
from live.interaction.state import RoomSnapshot

POLICY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policies", "commerce.yaml")
SIGNALS = frozenset({
    "has_big_gift", "gift_s", "gift_m", "gift_l", "repeat_gift", "gift_followup", "gift_king_dethroned",
    "has_new_chat", "product_question", "purchase_intent",
    "has_new_member", "returning_member", "vip_member", "regular_member", "high_grade_member",
    "has_new_follow", "has_fansclub", "has_share", "has_like",
    "viewer_surge", "viewer_drain", "online_milestone", "has_online_viewers",
})
TRIGGER_FIELDS = ("when", "priority", "emotion", "target_energy", "cooldown_s", "max_stale_s", "use_llm", "role",
                  "merge_clause", "merge_with", "directive", "lines", "enabled")
# 信号 → 命中后"消费"掉的当事事件（防同一事件被两个触发器重复念）
_CONSUMES = {"has_big_gift": "gift", "gift_s": "gift", "gift_m": "gift", "gift_l": "gift", "repeat_gift": "gift",
             "has_new_chat": "chat", "product_question": "question", "purchase_intent": "purchase",
             "has_new_member": "member", "returning_member": "member", "vip_member": "member",
             "regular_member": "member", "high_grade_member": "member", "has_new_follow": "follow",
             "has_fansclub": "fansclub", "has_share": "share"}
ENERGY_IDLE = 0.25
ENERGY_TO_SPEED = 0.15


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


@dataclass
class Trigger:
    name: str
    when: str
    priority: int
    emotion: str = "轻松互动"
    target_energy: float = 0.3
    cooldown_s: float = 0.0
    max_stale_s: float | None = None
    use_llm: bool = False
    role: str = ""
    merge_clause: object = None
    merge_with: tuple = ()
    directive: str = ""
    lines: list = field(default_factory=list)
    enabled: bool = True
    overridden: tuple = ()
    cond: Callable = None

    def describe(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items() if k != "cond"}


def load_policy(path: str = POLICY_FILE) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_triggers(base: dict, overrides: dict | None = None) -> list:
    specs = {k: dict(v) for k, v in (base or {}).items() if isinstance(v, dict)}
    over = overrides or {}
    for name, ov in over.items():
        if isinstance(ov, dict):
            specs[name] = {**specs.get(name, {}), **ov}
    out = []
    for name, spec in specs.items():
        when = spec.get("when")
        if when != "always" and when not in SIGNALS:
            logger.warning(f"触发器 {name!r} 的 when={when!r} 不是已知信号，跳过")
            continue
        cond = (lambda s: True) if when == "always" else (lambda w: (lambda s: bool(s.get(w))))(when)
        mw = spec.get("merge_with") or []
        t = Trigger(name=name, when=when, priority=int(spec.get("priority", 0)), emotion=spec.get("emotion", "轻松互动"),
                    target_energy=float(spec.get("target_energy", 0.3)), cooldown_s=float(spec.get("cooldown_s", 0) or 0),
                    max_stale_s=(float(spec["max_stale_s"]) if spec.get("max_stale_s") else None),
                    use_llm=bool(spec.get("use_llm", False)), role=str(spec.get("role") or ""),
                    merge_clause=spec.get("merge_clause"), merge_with=tuple(mw if isinstance(mw, list) else [mw]),
                    directive=spec.get("directive", ""), lines=list(spec.get("lines") or []),
                    enabled=bool(spec.get("enabled", True)),
                    overridden=tuple(sorted(over.get(name, {}).keys())) if isinstance(over.get(name), dict) else (),
                    cond=cond)
        out.append(t)
    out.sort(key=lambda t: t.priority, reverse=True)
    return out


class Director:
    def __init__(self, cfg: dict, clock: Clock = SYSTEM_CLOCK, policy: dict | None = None, rng: random.Random | None = None,
                 context: Callable[[], dict] | None = None):
        self.cfg = cfg
        self.clock = clock
        self.base_policy = policy if policy is not None else load_policy()
        self.rng = rng or random.Random()
        self.context = context or (lambda: {})          # 主持人提供：当前产品名等
        self.triggers: list = []
        self.reload_triggers()
        self.energy = ENERGY_IDLE
        self.last_fired: dict = {}
        self.last_spoke_ts = -1e9
        self._recent_lines: deque = deque(maxlen=30)
        self._handled: dict = {}
        self._last_like_user = None
        self._last_gift_user = ""
        self._last_gift_ts = -1e9
        self._followup = None
        self._gift_king = None
        self._pending_dethrone = None
        self._max_online_ms = None             # 第一次看到在线人数只记基线，之后跨台阶才播
        self._pending_milestone = None

    # ------------------------------------------------------------------ 配置
    @property
    def ic(self) -> dict:
        return self.cfg["interaction"]

    def reload_triggers(self) -> None:
        self.triggers = build_triggers(self.base_policy, self.cfg["interaction"].get("triggers") or {})

    def apply_settings(self, settings, changed: set) -> None:
        if "interaction.triggers" in changed:
            self.reload_triggers()

    def trigger(self, name: str) -> Trigger | None:
        return next((t for t in self.triggers if t.name == name), None)

    # ------------------------------------------------------------------ 主入口
    def tick(self, s: RoomSnapshot, decay: bool = True) -> Cue | None:
        if decay:
            self.energy = max(ENERGY_IDLE, self.energy * float(self.ic["energy_decay"]))
        sig = self._derive(s)
        now = s.ts
        firing = []
        for trg in self.triggers:
            if not trg.enabled or not trg.cond(sig):
                continue
            cd = trg.cooldown_s
            if trg.when == "has_like":
                u = (sig.get("liker") or {}).get("nickname")
                if u and u == self._last_like_user:
                    cd *= float(self.ic["like"]["repeat_cooldown_factor"])
            if now - self.last_fired.get(trg.name, -1e9) < cd:
                continue
            firing.append(trg)
        if not firing:
            return None
        fired = firing[0]
        if fired.priority < int(self.ic["urgent_priority"]) and now - self.last_spoke_ts < self._speak_interval():
            return None
        partner = self._pick_partner(fired, sig)
        self._apply_effects(fired, sig, now)
        if partner is not None:
            self._apply_effects(partner, sig, now)
        self.last_spoke_ts = now
        self.energy = max(self.energy, fired.target_energy, partner.target_energy if partner else 0.0)
        speed = round(1.0 + ENERGY_TO_SPEED * (self.energy - ENERGY_IDLE), 2)
        if partner is not None:
            cue = self._merged(s, sig, fired, partner, speed)
            if cue is not None:
                return cue
        return self._single(s, sig, fired, speed)

    def _speak_interval(self) -> float:
        lo, hi = float(self.ic["speak_min_s"]), float(self.ic["speak_max_s"])
        e = min(1.0, max(0.0, (self.energy - ENERGY_IDLE) / (1.0 - ENERGY_IDLE)))
        return hi + (lo - hi) * e

    # ------------------------------------------------------------------ 信号
    def _fresh(self, kind: str, rec: dict | None) -> bool:
        return bool(rec) and (rec.get("msg_id") or rec.get("ts")) != self._handled.get(kind)

    def _derive(self, s: RoomSnapshot) -> dict:
        ic = self.ic
        g = s.last_gift
        has_big = self._fresh("gift", g) and int(g.get("total", 0)) >= int(ic["gift"]["min_value"])
        total = int((g or {}).get("total", 0))
        gift_l = has_big and total >= int(ic["gift"]["tier_l"])
        gift_m = has_big and not gift_l and total >= int(ic["gift"]["tier_m"])
        gift_s = has_big and not gift_l and not gift_m
        gift_key = (g or {}).get("user_id") or (g or {}).get("nickname") or ""
        repeat = (has_big and bool(gift_key) and gift_key == self._last_gift_user
                  and s.ts - self._last_gift_ts <= float(ic["gift"]["repeat_window_s"]))
        followup, followup_gift = False, None
        if self._followup:
            delays = self._followup["delays"]
            idx = self._followup["next"]
            if idx < len(delays) and s.ts - self._followup["ack_ts"] >= delays[idx]:
                followup, followup_gift = True, dict(self._followup["gift"])

        top = s.gift_top
        if top and int(top.get("value", 0)) >= int(ic["gift"]["king_min_value"]):
            key = top.get("user_id") or top.get("nickname")
            prev = self._gift_king
            if prev is not None and key != prev["key"] and prev["value"] > 0:
                self._pending_dethrone = {"new": dict(top), "prev": dict(prev), "ts": s.ts}
            if prev is None or key != prev["key"] or int(top["value"]) > prev["value"]:
                self._gift_king = {"key": key, "nickname": top.get("nickname", ""), "gender": top.get("gender", "unknown"),
                                   "value": int(top["value"])}
        if self._pending_dethrone and s.ts - self._pending_dethrone["ts"] > 30:
            self._pending_dethrone = None

        chat, q, buy = s.recent_chat, s.recent_question, s.recent_purchase
        member = s.recent_member
        has_member = self._fresh("member", member)
        mem = (member or {}).get("memory") or {}
        known = bool(mem.get("known_before"))
        vip = has_member and known and int(mem.get("gift_value", 0)) >= int(ic["welcome"]["vip_gift_value"])
        regular = has_member and known and not vip and int(mem.get("visits", 0)) >= int(ic["welcome"]["regular_visits"])
        high_grade = has_member and int((member or {}).get("wealth_level", 0)) >= int(ic["welcome"]["high_grade_level"])
        returning = has_member and bool((member or {}).get("returning"))

        liker = s.top_liker
        has_like = bool(liker) and int(liker.get("count", 0)) >= int(ic["like"]["min_count"])

        surge = s.online_delta >= int(ic["online"]["surge_min"])
        drain = s.online_delta <= -int(ic["online"]["drain_min"])
        step = int(ic["online"]["milestone_step"])
        if step > 0 and s.online > 0:
            level = s.online // step
            if self._max_online_ms is None:
                self._max_online_ms = level
            elif level > self._max_online_ms and s.online >= step:
                self._pending_milestone = {"online": level * step, "ts": s.ts}
                self._max_online_ms = level
        if self._pending_milestone and s.ts - self._pending_milestone["ts"] > 60:
            self._pending_milestone = None

        return {
            "gift": g, "has_big_gift": has_big, "gift_s": gift_s, "gift_m": gift_m, "gift_l": gift_l, "repeat_gift": repeat,
            "gift_followup": followup, "followup_gift": followup_gift,
            "gift_king_dethroned": self._pending_dethrone is not None, "gift_king": self._pending_dethrone,
            "chat": chat, "has_new_chat": self._fresh("chat", chat),
            "question": q, "product_question": self._fresh("question", q),
            "purchase": buy, "purchase_intent": self._fresh("purchase", buy),
            "member": member, "has_new_member": has_member, "returning_member": returning, "vip_member": vip,
            "regular_member": regular, "high_grade_member": high_grade,
            "follow": s.recent_follow, "has_new_follow": self._fresh("follow", s.recent_follow),
            "fansclub": s.recent_fansclub, "has_fansclub": self._fresh("fansclub", s.recent_fansclub),
            "share": s.recent_share, "has_share": self._fresh("share", s.recent_share),
            "liker": liker, "has_like": has_like,
            "online": s.online, "online_delta": s.online_delta, "viewer_surge": surge, "viewer_drain": drain,
            "online_milestone": self._pending_milestone is not None,
            "milestone": (self._pending_milestone or {}).get("online"),
            "online_viewers": list(s.online_viewers), "has_online_viewers": bool(s.online_viewers),
        }

    # ------------------------------------------------------------------ 命中副作用
    def _apply_effects(self, trg: Trigger, sig: dict, now: float) -> None:
        self.last_fired[trg.name] = now
        kind = _CONSUMES.get(trg.when)
        if kind:
            rec = sig.get(kind)
            if rec:
                self._handled[kind] = rec.get("msg_id") or rec.get("ts")
        if kind == "gift" and sig.get("gift"):
            g = sig["gift"]
            self._last_gift_user = g.get("user_id") or g.get("nickname") or ""
            self._last_gift_ts = now
            delays = [float(x) for x in (self.ic["gift"]["followup_delays_s"] or []) if float(x) > 0]
            if delays:
                self._followup = {"gift": dict(g), "ack_ts": now, "next": 0, "delays": delays}
        if trg.when == "gift_followup" and self._followup:
            self._followup["next"] += 1
            if self._followup["next"] >= len(self._followup["delays"]):
                self._followup = None
        if trg.when == "gift_king_dethroned":
            self._pending_dethrone = None
        if trg.when == "online_milestone":
            self._pending_milestone = None
        if trg.when == "has_like" and sig.get("liker"):
            self._last_like_user = sig["liker"].get("nickname")

    def _pick_partner(self, fired: Trigger, sig: dict) -> Trigger | None:
        if not self.ic.get("merge_enabled", True) or fired.role not in ("who", "what") or not fired.merge_clause:
            return None
        want = "what" if fired.role == "who" else "who"
        for trg in self.triggers:
            if trg is fired or not trg.enabled or trg.role != want or not trg.merge_clause:
                continue
            if trg.name not in fired.merge_with and fired.name not in trg.merge_with:
                continue
            if trg.cond(sig):
                return trg
        return None

    # ------------------------------------------------------------------ 产出
    def _actor(self, trg: Trigger, sig: dict) -> dict | None:
        kind = _CONSUMES.get(trg.when)
        if kind:
            return sig.get(kind)
        if trg.when == "gift_followup":
            return sig.get("followup_gift")
        return None

    def _situation(self, s: RoomSnapshot, sig: dict, trg: Trigger, fmt: dict) -> dict:
        actor = self._actor(trg, sig)
        sit = {"event": trg.name, "emotion": trg.emotion, "online": s.online, "online_delta": s.online_delta,
               "anchor": fmt["anchor"], "product": fmt["product"]}
        if actor:
            sit["viewer"] = {"称呼": short_name(actor.get("nickname"), actor.get("gender"), self._suffix()),
                             "性别": {"male": "男", "female": "女"}.get(actor.get("gender"), "未知"),
                             "财富等级": actor.get("wealth_level") or 0, "粉丝团等级": actor.get("fans_level") or 0}
        if trg.when in ("gift_s", "gift_m", "gift_l", "repeat_gift", "has_big_gift", "gift_followup"):
            g = sig.get("followup_gift") if trg.when == "gift_followup" else sig.get("gift")
            if g:
                sit["gift"] = {"name": g.get("gift"), "count": g.get("count"), "total_diamonds": g.get("total")}
        if trg.when == "has_new_chat" and sig.get("chat"):
            sit["chat"] = sig["chat"].get("content")
        if trg.when in ("product_question", "purchase_intent"):
            rec = sig.get("question" if trg.when == "product_question" else "purchase") or {}
            sit["question"] = rec.get("content")
        if trg.when == "has_new_member" or trg.when.endswith("_member"):
            mem = (sig.get("member") or {}).get("memory") or {}
            if mem:
                sit["history"] = {"来过": mem.get("visits", 0), "累计送礼钻石": mem.get("gift_value", 0),
                                  "往场就认识": bool(mem.get("known_before"))}
        return sit

    def _suffix(self) -> dict:
        return self.cfg["persona"].get("honorific") or {}

    def _big(self) -> dict:
        return self.cfg["persona"].get("big_honorific") or {}

    def _fmt(self, sig: dict) -> _SafeDict:
        sfx, big = self._suffix(), self._big()

        def sn(rec):
            return short_name((rec or {}).get("nickname"), (rec or {}).get("gender"), sfx) if rec else ""

        def hon(rec):
            return honor((rec or {}).get("gender"), big)

        g = sig.get("gift") or sig.get("followup_gift") or {}
        if sig.get("gift_followup") and sig.get("followup_gift") and not sig.get("has_big_gift"):
            g = sig["followup_gift"]
        member = sig.get("member") or {}
        mem = member.get("memory") or {}
        gk = sig.get("gift_king") or {}
        gk_new, gk_prev = gk.get("new") or {}, gk.get("prev") or {}
        viewers = sig.get("online_viewers") or []
        q = sig.get("question") or sig.get("purchase") or {}
        ctx = self.context() or {}
        anchor = self.cfg["persona"].get("anchor_name") or "主播"
        shop = self.cfg["persona"].get("shop_name") or ""
        mlv = int(member.get("wealth_level") or 0)
        glv = int(g.get("wealth_level") or 0)
        return _SafeDict({
            "anchor": anchor, "shop": shop, "shop_or_anchor": shop or anchor,
            "product": ctx.get("product_name") or "今天的好物",
            "online": sig.get("online", 0), "online_delta": abs(int(sig.get("online_delta", 0))),
            "milestone": sig.get("milestone") or sig.get("online", 0),
            "gift_user": sn(g), "gift_honor": hon(g), "gift_name": g.get("gift", "礼物"), "gift_count": g.get("count", 1),
            "gift_total": g.get("total", 0), "gift_grade_tag": f"（财富等级{glv}）" if glv > 0 else "",
            "member_user": sn(member), "member_honor": hon(member), "member_grade_level": mlv,
            "member_grade_tag": f"（财富等级{mlv}）" if mlv > 0 else "", "member_visits": mem.get("visits", 0),
            "member_gift_value": mem.get("gift_value", 0),
            "follow_user": sn(sig.get("follow")), "follow_honor": hon(sig.get("follow")),
            "fans_user": sn(sig.get("fansclub")), "share_user": sn(sig.get("share")),
            "chat_user": sn(sig.get("chat")), "chat_honor": hon(sig.get("chat")),
            "chat_content": (sig.get("chat") or {}).get("content", ""),
            "asker": sn(q), "asker_honor": hon(q), "question": q.get("content", ""),
            "like_user": sn(sig.get("liker")), "like_honor": hon(sig.get("liker")),
            "like_count": (sig.get("liker") or {}).get("count", 0),
            "viewer_name": short_name(self.rng.choice(viewers), "unknown", sfx) if viewers else "",
            "gift_king_user": sn(gk_new), "gift_king_honor": hon(gk_new), "gift_king_value": gk_new.get("value", 0),
            "gift_king_prev_user": sn(gk_prev),
        })

    def _pick_line(self, trg: Trigger, fmt: dict) -> str:
        if not trg.lines:
            return ""
        rendered = [str(ln).format_map(fmt) for ln in trg.lines]
        fresh = [x for x in rendered if x not in self._recent_lines]
        choice = self.rng.choice(fresh or rendered)
        self._recent_lines.append(choice)
        return choice

    def _source(self, trg: Trigger, sig: dict) -> dict | None:
        a = self._actor(trg, sig)
        if not a:
            return None
        return {"msg_id": a.get("msg_id"), "nickname": a.get("nickname"),
                "content": a.get("content") or (f"{a.get('gift')}×{a.get('count')}" if a.get("gift") else "")}

    def _cue(self, s, sig, trg: Trigger, speed: float, text: str, directive: str, reason: str, sit: dict) -> Cue:
        actor = self._actor(trg, sig) or {}
        q = ""
        if trg.when in ("product_question", "purchase_intent"):
            q = actor.get("content", "")
        return Cue(event=trg.name, priority=trg.priority, kind="interaction", emotion=trg.emotion,
                   energy=round(self.energy, 2), speed=speed, directive=directive, situation=sit, fallback_text=text,
                   reason=reason, use_llm=trg.use_llm, user=actor.get("profile"), question=q,
                   max_stale_s=trg.max_stale_s, source=self._source(trg, sig), created_at=self.clock.now())

    def _single(self, s, sig, trg: Trigger, speed: float) -> Cue:
        fmt = self._fmt(sig)
        text = self._pick_line(trg, fmt)
        directive = str(trg.directive or "").format_map(fmt)
        return self._cue(s, sig, trg, speed, text, directive, self._reason(trg, sig), self._situation(s, sig, trg, fmt))

    def _merged(self, s, sig, fired: Trigger, partner: Trigger, speed: float) -> Cue | None:
        who, what = (fired, partner) if fired.role == "who" else (partner, fired)
        fmt = self._fmt(sig)

        def clause(c):
            if isinstance(c, (list, tuple)):
                c = self.rng.choice([x for x in c if x] or [""])
            return str(c or "").format_map(fmt).strip()

        parts = [p for p in (clause(who.merge_clause), clause(what.merge_clause)) if p]
        if len(parts) < 2:
            return None
        text = "，".join(parts) + "！"
        self._recent_lines.append(text)
        directive = f"把两件事合成一句自然口播：① {who.directive} ② {what.directive}".format_map(fmt)
        sit = self._situation(s, sig, who, fmt)
        sit["event"] = f"{who.name}+{what.name}"
        cue = self._cue(s, sig, fired, speed, text, directive,
                        f"[合并] {self._reason(who, sig)} ✚ {self._reason(what, sig)}", sit)
        cue.source = self._source(who, sig)
        return cue

    def _reason(self, trg: Trigger, sig: dict) -> str:
        def nm(rec):
            return (rec or {}).get("nickname") or "?"
        w = trg.when
        if w in ("gift_s", "gift_m", "gift_l", "has_big_gift", "repeat_gift"):
            g = sig.get("gift") or {}
            tier = {"gift_l": "L档", "gift_m": "M档", "gift_s": "S档", "repeat_gift": "连续送礼"}.get(w, "")
            return f"『{nm(g)}』送了 {g.get('gift')}×{g.get('count')}（{g.get('total')}钻，{tier}）"
        if w == "gift_followup":
            return f"回踩送礼的『{nm(sig.get('followup_gift'))}』"
        if w == "gift_king_dethroned":
            d = sig.get("gift_king") or {}
            return f"礼物榜榜首易主：『{nm(d.get('new'))}』（{(d.get('new') or {}).get('value')}钻）超过『{nm(d.get('prev'))}』"
        if w == "product_question":
            return f"『{nm(sig.get('question'))}』问产品：{(sig.get('question') or {}).get('content')}"
        if w == "purchase_intent":
            return f"『{nm(sig.get('purchase'))}』想买：{(sig.get('purchase') or {}).get('content')}"
        if w == "has_new_chat":
            return f"『{nm(sig.get('chat'))}』发弹幕：{(sig.get('chat') or {}).get('content')}"
        if w in ("has_new_member", "returning_member", "vip_member", "regular_member", "high_grade_member"):
            m = sig.get("member") or {}
            mem = m.get("memory") or {}
            label = {"vip_member": f"金主回归（累计送礼{mem.get('gift_value', 0)}钻）",
                     "regular_member": f"熟客（第{mem.get('visits', 0)}次来）",
                     "high_grade_member": f"高财富等级（{m.get('wealth_level')}级）",
                     "returning_member": "本场回头客"}.get(w, "普通进场")
            return f"『{nm(m)}』进场，{label}"
        if w == "has_new_follow":
            return f"『{nm(sig.get('follow'))}』关注了"
        if w == "has_fansclub":
            return f"『{nm(sig.get('fansclub'))}』粉丝团{'升级' if (sig.get('fansclub') or {}).get('kind') == 'upgrade' else '加入'}"
        if w == "has_share":
            return f"『{nm(sig.get('share'))}』分享了直播间"
        if w == "has_like":
            return f"『{nm(sig.get('liker'))}』点赞最多（{(sig.get('liker') or {}).get('count')}次）"
        if w == "viewer_surge":
            return f"在线猛涨 +{sig.get('online_delta')}（现 {sig.get('online')}）"
        if w == "viewer_drain":
            return f"在线下滑 {sig.get('online_delta')}（现 {sig.get('online')}）"
        if w == "online_milestone":
            return f"在线破 {sig.get('online')}"
        if w == "has_online_viewers":
            return "冷场，点名在线观众"
        return "平静期"
