"""跨场长期记忆（移植参考项目 decision/user_store.py）：按平台 user_id 累计观众画像，落盘 live_data/viewers.json。

规则（照搬）：
- 建档门槛：只有**发过弹幕 / 送过礼（结算后的）/ 关注过 / 加粉丝团**的观众才首次入库；纯进房不建档（已建档的老观众进房照常累计 visits）；
- known_before：本进程启动前就在库里的人才算"往场认识的"——金主 / 熟客只认上几场记住的人，本场新人下一场才升级；
- 点赞事件常常没有 user_id → 不入库；
- 模拟注入 / mock 源 / 回放的事件默认不入库（interaction.memory.record_test_events 可打开）；
- 落盘节流（interaction.memory.flush_interval_s）+ 临时文件原子替换；退出时 flush。
只存：user_id、昵称、性别、财富等级（最高）、粉丝团等级、进场/送礼/发言统计、最近互动时间、最近一次提问。
**头像、城市、签名等不落盘**（隐私）。Web「弹幕互动 → 观众」可删除某位观众的记录。
"""
from __future__ import annotations

import json
import os
import threading
import time

from loguru import logger

from live.core.clock import SYSTEM_CLOCK, Clock
from live.core.events import LiveEvent

_CREATE_TYPES = ("chat", "gift", "follow", "fansclub")
TEST_PLATFORMS = ("mock", "simulate", "replay")


class ViewerMemory:
    def __init__(self, cfg: dict, path: str | None = None, clock: Clock = SYSTEM_CLOCK):
        self.cfg = cfg
        self.path = path or os.path.join(cfg["server"]["data_dir"], "viewers.json")
        self.clock = clock
        self._lock = threading.Lock()
        self._users: dict = self._load() if self.enabled else {}
        for u in self._users.values():
            self._ensure_fields(u)
        self._known_at_boot: set = set(self._users)
        self._dirty = False
        self._last_flush = clock.now()
        if self.enabled:
            logger.info(f"[长期记忆] 已加载 {len(self._users)} 位老观众 ← {self.path}")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg["interaction"]["memory"]["enabled"])

    # ------------------------------------------------------------------ 写
    def record(self, ev: LiveEvent) -> None:
        if not self.enabled or ev.user is None or not ev.user.user_id:
            return
        if ev.platform in TEST_PLATFORMS and not self.cfg["interaction"]["memory"].get("record_test_events"):
            return                                       # 测试来源不污染真实观众画像
        if ev.type == "gift" and ev.data.get("is_final") is False:
            return                                       # 连击中的包不算，等结算那条
        uid = ev.user.user_id
        t = ev.type
        now = self.clock.now()
        with self._lock:
            u = self._users.get(uid)
            if u is None:
                if t not in _CREATE_TYPES:
                    return
                u = self._users[uid] = self._new(now)
            u["last_seen"] = now
            p = ev.user
            if p.nickname:
                u["name"] = p.nickname
            if p.gender and p.gender != "unknown":
                u["gender"] = p.gender
            u["max_wealth_level"] = max(int(u.get("max_wealth_level") or 0), int(p.wealth_level or 0))
            if p.fans_level:
                u["fans_level"] = p.fans_level
            if t == "member":
                u["visits"] += 1
            elif t == "gift":
                cnt = int(ev.data.get("count") or 1)
                val = int(ev.data.get("total_diamonds") or 0)
                u["gift_count"] += cnt
                u["gift_value"] += val
                name = ev.data.get("gift_name") or ""
                if name:
                    u["last_gift"] = name
                    u["gifts"][name] = u["gifts"].get(name, 0) + cnt
            elif t == "chat":
                u["chat_count"] += 1
            elif t == "follow":
                u["followed"] = True
            elif t == "fansclub":
                pass
            else:
                return
            self._dirty = True
        self._maybe_flush()

    def note_question(self, user_id: str, question: str, product_id: str | None = None) -> None:
        with self._lock:
            u = self._users.get(user_id)
            if u is None:
                return
            u["last_question"] = question[:60]
            u["last_question_at"] = self.clock.now()
            if product_id:
                u["last_question_product"] = product_id
            self._dirty = True

    def delete(self, user_id: str) -> bool:
        with self._lock:
            ok = self._users.pop(user_id, None) is not None
            self._known_at_boot.discard(user_id)
            self._dirty = self._dirty or ok
        if ok:
            self.flush()
        return ok

    # ------------------------------------------------------------------ 读
    def profile(self, user_id) -> dict:
        uid = str(user_id or "")
        if not self.enabled or not uid:
            return {}
        with self._lock:
            u = self._users.get(uid)
            if u is None:
                return {}
            snap = dict(u, gifts=dict(u.get("gifts") or {}))
        snap["user_id"] = uid
        snap["known_before"] = uid in self._known_at_boot
        return snap

    def known_before(self, user_id: str) -> bool:
        return user_id in self._known_at_boot

    def search(self, q: str = "", limit: int = 50) -> list:
        q = (q or "").strip().lower()
        with self._lock:
            items = [(uid, dict(u)) for uid, u in self._users.items()
                     if not q or q in uid.lower() or q in str(u.get("name", "")).lower()]
        items.sort(key=lambda kv: -float(kv[1].get("last_seen") or 0))
        return [dict(u, user_id=uid, known_before=uid in self._known_at_boot, gifts=dict(u.get("gifts") or {}))
                for uid, u in items[:limit]]

    def stats(self) -> dict:
        with self._lock:
            n = len(self._users)
            donors = sum(1 for u in self._users.values() if u["gift_value"] > 0)
        return {"users": n, "donors": donors, "known_at_boot": len(self._known_at_boot)}

    # ------------------------------------------------------------------ 落盘
    def _maybe_flush(self) -> None:
        with self._lock:
            due = self._dirty and self.clock.now() - self._last_flush >= float(
                self.cfg["interaction"]["memory"]["flush_interval_s"])
        if due:
            self.flush()

    def flush(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if not self._dirty:
                return
            snapshot = {uid: dict(u, gifts=dict(u.get("gifts") or {})) for uid, u in self._users.items()}
            self._dirty = False
            self._last_flush = self.clock.now()
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"[长期记忆] 落盘失败（{self.path}）: {e}")

    @staticmethod
    def _new(now: float) -> dict:
        return {"name": "", "gender": "unknown", "first_seen": now, "last_seen": now, "visits": 0, "gift_count": 0,
                "gift_value": 0, "chat_count": 0, "followed": False, "last_gift": "", "gifts": {},
                "max_wealth_level": 0, "fans_level": 0, "last_question": "", "last_question_product": None}

    @classmethod
    def _ensure_fields(cls, u: dict) -> None:
        for k, v in cls._new(u.get("first_seen", 0.0)).items():
            u.setdefault(k, v)
        if not isinstance(u.get("gifts"), dict):
            u["gifts"] = {}

    def _load(self) -> dict:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError) as e:
                logger.warning(f"[长期记忆] 读取失败（{self.path}），从空开始: {e}")
        return {}
