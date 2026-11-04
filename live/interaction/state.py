"""RoomState（移植参考项目 decision/state_merger.py，去掉 OCR/游戏层）：把弹幕事件合成"这一拍的直播间局面"。

- 各类事件进保鲜队列（interaction.window.*），过期的不再拿来说；**保鲜只用墙钟 clock.now()**（与事件 ts 同源）；
- 本场回头客：本场出现过的 user_id 再进场 → returning；
- 本场礼物榜：按人累计结算后的礼物钻石数 → gift_top（榜首易主由导演判断）；
- 在线人数：窗口内最新值与变化量（猛涨/下滑/破整）；贡献榜名单（冷场点名）；
- 弹幕按意图分流：产品提问、购买意向、闲聊分别排队（提问保鲜更久，不会被闲聊挤掉）；垃圾 / 无意义刷屏丢弃。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from live.core.clock import SYSTEM_CLOCK, Clock
from live.core.events import LiveEvent
from live.interaction import intent as I


@dataclass
class RoomSnapshot:
    ts: float
    last_gift: dict | None = None
    recent_chat: dict | None = None
    recent_question: dict | None = None
    recent_purchase: dict | None = None
    recent_member: dict | None = None
    recent_follow: dict | None = None
    recent_fansclub: dict | None = None
    recent_share: dict | None = None
    top_liker: dict | None = None
    online: int = 0
    online_delta: int = 0
    online_viewers: list = field(default_factory=list)
    gift_top: dict | None = None


def _who(ev: LiveEvent) -> dict:
    u = ev.user
    return {"user_id": u.user_id if u else "", "nickname": u.nickname if u else "", "gender": u.gender if u else "unknown",
            "wealth_level": u.wealth_level if u else 0, "fans_level": u.fans_level if u else 0,
            "profile": u.to_dict() if u else None, "msg_id": ev.msg_id, "ts": ev.ts}


class RoomState:
    INSTANT_TYPES = {"gift", "follow", "like", "member", "chat", "fansclub", "share"}

    def __init__(self, cfg: dict, intent: I.IntentClassifier, memory=None, clock: Clock = SYSTEM_CLOCK,
                 queue_max: int = 200):
        self.cfg = cfg
        self.intent = intent
        self.memory = memory
        self.clock = clock
        self.queue_max = queue_max
        self._lock = threading.Lock()
        self._q = {k: deque() for k in ("gift", "chat", "question", "purchase", "member", "follow", "fansclub", "share",
                                         "like")}
        self._online: deque = deque()
        self._last_rank: tuple | None = None
        self._seen_members: set = set()
        self._session_gifts: dict = {}
        self.counters: dict = {}

    def win(self, name: str) -> float:
        return float(self.cfg["interaction"]["window"][name])

    # ------------------------------------------------------------------ 事件入口
    def on_event(self, ev: LiveEvent) -> dict | None:
        """返回附加信息（如弹幕意图），供决策日志显示。"""
        t = ev.type
        self.counters[t] = self.counters.get(t, 0) + 1
        info = None
        with self._lock:
            if t == "gift":
                if ev.data.get("is_final") is False:
                    return None
                rec = {**_who(ev), "gift": ev.data.get("gift_name", "礼物"), "unit": int(ev.data.get("unit_diamonds") or 0),
                       "count": int(ev.data.get("count") or 1), "total": int(ev.data.get("total_diamonds") or 0)}
                self._q["gift"].append(rec)
                key = rec["user_id"] or rec["nickname"]
                if key:
                    g = self._session_gifts.setdefault(key, {"nickname": rec["nickname"], "user_id": rec["user_id"],
                                                             "gender": rec["gender"], "value": 0})
                    g["nickname"] = rec["nickname"] or g["nickname"]
                    g["value"] += rec["total"]
            elif t == "chat":
                content = str(ev.data.get("content") or "")
                it = self.intent.classify(content) if not ev.data.get("is_emoji") else I.Intent(I.NOISE)
                info = {"intent": it.to_dict()}
                rec = {**_who(ev), "content": content, "intent": it.kind}
                if it.kind == I.PRODUCT_QUESTION:
                    self._q["question"].append(rec)
                elif it.kind == I.PURCHASE_INTENT:
                    self._q["purchase"].append(rec)
                elif it.kind == I.CHITCHAT:
                    self._q["chat"].append(rec)
            elif t == "like":
                self._q["like"].append({"nickname": ev.nickname or "某位观众", "gender": ev.user.gender if ev.user else "unknown",
                                        "count": int(ev.data.get("count") or 1), "ts": ev.ts})
            elif t == "member":
                uid = ev.user_id
                returning = bool(uid) and uid in self._seen_members
                if uid:
                    self._seen_members.add(uid)
                prof = self.memory.profile(uid) if self.memory is not None and uid else {}
                self._q["member"].append({**_who(ev), "returning": returning, "memory": prof})
            elif t in ("follow", "fansclub", "share"):
                self._q[t].append({**_who(ev), **({"kind": ev.data.get("kind")} if t == "fansclub" else {})})
            elif t == "room_stat":
                online = ev.data.get("online")
                if online is not None:
                    self._online.append((ev.ts, int(online)))
                    while len(self._online) > self.queue_max:
                        self._online.popleft()
            elif t == "rank":
                names = [r.get("nickname") for r in (ev.data.get("ranks") or []) if r.get("nickname")]
                if names:
                    self._last_rank = (ev.ts, names)
            for q in self._q.values():
                while len(q) > self.queue_max:
                    q.popleft()
        return info

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> RoomSnapshot:
        now = self.clock.now()
        with self._lock:
            def pick(name, window):
                q = self._q[name]
                cutoff = now - self.win(window)
                while q and q[0]["ts"] < cutoff:
                    q.popleft()
                return dict(q[-1]) if q else None

            def first(name, window):
                # 产品提问按先来后到回答（最早那条还没过期就先答它）
                q = self._q[name]
                cutoff = now - self.win(window)
                while q and q[0]["ts"] < cutoff:
                    q.popleft()
                return dict(q[0]) if q else None

            snap = RoomSnapshot(ts=now,
                                last_gift=pick("gift", "gift_s"), recent_chat=pick("chat", "chat_s"),
                                recent_question=first("question", "question_s"), recent_purchase=pick("purchase", "question_s"),
                                recent_member=pick("member", "member_s"), recent_follow=pick("follow", "follow_s"),
                                recent_fansclub=pick("fansclub", "follow_s"), recent_share=pick("share", "follow_s"))
            likes = self._q["like"]
            cutoff = now - self.win("like_s")
            while likes and likes[0]["ts"] < cutoff:
                likes.popleft()
            if likes:
                tally, gender = {}, {}
                for e in likes:
                    tally[e["nickname"]] = tally.get(e["nickname"], 0) + e["count"]
                    gender[e["nickname"]] = e["gender"]
                name, cnt = max(tally.items(), key=lambda kv: kv[1])
                snap.top_liker = {"nickname": name, "count": cnt, "gender": gender.get(name, "unknown"), "ts": now}
            cutoff = now - self.win("online_s")
            while self._online and self._online[0][0] < cutoff:
                self._online.popleft()
            if self._online:
                snap.online = self._online[-1][1]
                snap.online_delta = snap.online - self._online[0][1]
            if self._last_rank and now - self._last_rank[0] <= self.win("rank_s"):
                snap.online_viewers = list(self._last_rank[1])
            if self._session_gifts:
                snap.gift_top = dict(max(self._session_gifts.values(), key=lambda r: r["value"]))
        return snap

    def consume_question(self, msg_id: str) -> None:
        """提问被回答后从队列移除（先来先答，答完下一条）。"""
        with self._lock:
            q = self._q["question"]
            self._q["question"] = deque(r for r in q if r.get("msg_id") != msg_id)

    def session_gift_rank(self, n: int = 10) -> list:
        with self._lock:
            return sorted((dict(v) for v in self._session_gifts.values()), key=lambda r: -r["value"])[:n]
