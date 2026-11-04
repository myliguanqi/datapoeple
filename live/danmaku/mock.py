"""模拟弹幕源：没有直播间也能开发调试。

opts（来自 danmaku.mock.*）：
  rate_per_min    每分钟事件数（chat/member/like/gift/follow…，在线人数与贡献榜另外定时推）
  mix             各类事件比例 {"chat": 0.35, "member": 0.3, ...}
  question_ratio  弹幕里产品提问的比例（另有约 10% 购买意向，其余闲聊）
  female_ratio    女性观众比例
  wealth_dist     财富等级分布 {"等级": 概率}
  seed            随机种子（可选，测试用）
  script          确定性脚本：[{"delay": 秒, "event": LiveEvent.to_dict()}, ...]，给了就只播脚本
  script_loop     脚本循环
"""
from __future__ import annotations

import random
import time

from live.core.events import LiveEvent, ViewerProfile
from live.danmaku.base import DanmakuSource

_SURNAMES = "王李张刘陈杨黄赵周吴徐孙马朱胡郭何林罗高"
_NICKS = ["小{s}", "{s}姐", "{s}哥", "{s}{s2}", "爱吃{f}的{s}", "{s}家的{f}", "用户{n}", "{s}{s2}要努力",
          "诗江传媒…{s}少", "{f}{f}", "快乐{f}", "{s}大大", "momo{n}", "{s}宝"]
_FOODS = ["砂糖桔", "柚子", "奶茶", "西瓜", "汤圆", "可乐", "柠檬", "芒果"]
GIFTS = [  # (名字, 单价钻石, 权重, 可连击)
    ("小心心", 1, 30, True), ("玫瑰", 1, 25, True), ("人气票", 1, 12, True), ("棒棒糖", 9, 8, True),
    ("加油鸭", 15, 6, True), ("甜甜圈", 52, 4, True), ("能量电池", 99, 3, False), ("浪漫花束", 299, 1.5, False),
    ("跑车", 1200, 0.4, False), ("嘉年华", 30000, 0.05, False),
]
QUESTIONS = [
    "这个多少钱", "价格多少啊", "有优惠券吗", "保质期多久", "有什么尺码", "我165穿什么码", "是什么材质的",
    "包邮吗", "多久发货", "能退换吗", "售后怎么样", "怎么买", "链接在哪", "适合几岁宝宝", "敏感肌能用吗",
    "一盒有多少", "和昨天那个比哪个好", "颜色有几种", "能用多久", "保温能保多久",
]
PURCHASE = ["要了", "拍了", "已下单", "几号链接", "怎么拍", "我要两件", "下单了主播"]
CHITCHAT = ["主播好", "来了来了", "好看", "666", "哈哈哈哈", "晚上好", "支持一下", "声音好听", "今天有什么新品",
            "主播是真人吗", "刚来", "点赞了", "又来看你了"]


class MockSource(DanmakuSource):
    name = "mock"

    def __init__(self, opts, emit, status):
        super().__init__(opts, emit, status)
        self.rng = random.Random(opts.get("seed"))
        self.rate = max(1.0, float(opts.get("rate_per_min", 30)))
        mix = {k: float(v) for k, v in (opts.get("mix") or {"chat": 1}).items() if float(v) > 0}
        self.mix = mix or {"chat": 1.0}
        self.question_ratio = float(opts.get("question_ratio", 0.3))
        self.female_ratio = float(opts.get("female_ratio", 0.6))
        wd = {int(k): float(v) for k, v in (opts.get("wealth_dist") or {"0": 1}).items() if float(v) > 0}
        self.wealth_dist = wd or {0: 1.0}
        self.room_id = "mock-room"
        self.online = int(opts.get("online_start", 120))
        self.like_total = 0
        self.users = [self._make_user(i) for i in range(int(opts.get("users", 160)))]
        self.script = opts.get("script")

    # ---------------------------------------------------------------- 观众池
    def _make_user(self, i: int) -> ViewerProfile:
        r = self.rng
        s = r.choice(_SURNAMES)
        tpl = r.choice(_NICKS)
        nick = tpl.format(s=s, s2=r.choice(_SURNAMES), f=r.choice(_FOODS), n=r.randint(10 ** 9, 10 ** 10 - 1))
        gender = "female" if r.random() < self.female_ratio else ("male" if r.random() < 0.9 else "unknown")
        levels, weights = zip(*self.wealth_dist.items())
        base = r.choices(levels, weights=weights)[0]
        wealth = max(0, base + (r.randint(-3, 4) if base else 0))
        fans = {"name": "美美团", "level": r.randint(1, 15), "status": 1} if r.random() < 0.25 else None
        return ViewerProfile(user_id=f"mock{10000 + i}", nickname=nick, gender=gender, wealth_level=wealth,
                             wealth_name=f"{wealth}级" if wealth else "", wealth_diamonds=wealth * wealth * 40,
                             fans_club=fans, user_level=r.randint(1, 40), followers=r.randint(0, 3000),
                             extra={"display_id": f"dy{10000 + i}"})

    def _ev(self, type_, user=None, data=None, in_room=None) -> LiveEvent:
        return LiveEvent(type=type_, platform="mock", room_id=self.room_id, ts=time.time(),
                         msg_id=f"m{self.rng.getrandbits(48):x}", user=user, data=data or {}, in_room=in_room or {},
                         raw_method="mock")

    def make_event(self, kind: str) -> LiveEvent:
        r = self.rng
        u = r.choice(self.users)
        if kind == "chat":
            x = r.random()
            if x < self.question_ratio:
                content = r.choice(QUESTIONS)
            elif x < self.question_ratio + 0.1:
                content = r.choice(PURCHASE)
            else:
                content = r.choice(CHITCHAT)
            return self._ev("chat", u, {"content": content, "is_emoji": False})
        if kind == "gift":
            name, unit, _, combo = r.choices(GIFTS, weights=[g[2] for g in GIFTS])[0]
            count = r.choice([1, 1, 1, 2, 3, 5, 10]) if combo else 1
            return self._ev("gift", u, {"gift_id": name, "gift_name": name, "unit_diamonds": unit, "count": count,
                                        "total_diamonds": unit * count, "combo_id": f"{u.user_id}:{name}:{r.random()}",
                                        "is_final": True, "comboable": combo, "to_user": ""},
                            {"consume": unit * count, "gift_count": 1})
        if kind == "like":
            n = r.randint(1, 15)
            self.like_total += n
            return self._ev("like", u, {"count": n, "total": self.like_total})
        if kind == "member":
            self.online += r.choice([0, 1, 1, 2])
            return self._ev("member", u, {"member_count": self.online, "enter_type": 0})
        if kind == "follow":
            return self._ev("follow", u, {"action": 1, "share_type": 0})
        if kind == "share":
            return self._ev("share", u, {"action": 3, "share_type": 1})
        if kind == "fansclub":
            return self._ev("fansclub", u, {"kind": r.choice(["join", "upgrade"]), "content": f"{u.nickname} 加入了粉丝团"})
        return self._ev("chat", u, {"content": r.choice(CHITCHAT), "is_emoji": False})

    def room_stat(self) -> LiveEvent:
        self.online = max(5, self.online + self.rng.randint(-4, 4))
        return self._ev("room_stat", None, {"online": self.online, "total_pv": self.online * 12 + 345,
                                             "popularity": self.online * 37})

    def rank(self) -> LiveEvent:
        top = sorted(self.rng.sample(self.users, k=min(8, len(self.users))), key=lambda u: -u.wealth_level)
        return self._ev("rank", None, {"ranks": [{"user_id": u.user_id, "nickname": u.nickname,
                                                  "score": u.wealth_level * 10} for u in top]})

    # ---------------------------------------------------------------- 主循环
    def run(self) -> None:
        self.status({"state": "connected", "room_id": self.room_id, "live_id": "mock", "logged_in": True,
                     "error": None})
        if self.script:
            self._run_script()
            return
        kinds, weights = zip(*self.mix.items())
        next_stat = next_rank = 0.0
        while not self.stopped:
            now = time.monotonic()
            if now >= next_stat:
                self.emit(self.room_stat())
                next_stat = now + 5
            if now >= next_rank:
                self.emit(self.rank())
                next_rank = now + 30
            self.emit(self.make_event(self.rng.choices(kinds, weights=weights)[0]))
            self._stop.wait(self.rng.expovariate(self.rate / 60.0))
        self.status({"state": "disconnected"})

    def _run_script(self) -> None:
        loop = bool(self.opts.get("script_loop"))
        while not self.stopped:
            for item in self.script:
                if self._stop.wait(float(item.get("delay", 0))):
                    return
                ev = LiveEvent.from_dict(item["event"])
                ev.ts = time.time()
                self.emit(ev)
            if not loop:
                break
        # 脚本播完保持"已连接"，直到被停止
        self._stop.wait()
