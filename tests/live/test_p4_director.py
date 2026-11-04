"""P4：导演 / 局面 / 长期记忆 / 队列（假时钟，复刻参考项目 verify.py 的平台类用例 + 带货信号）。"""
import random

import pytest

from live.core.bus import EventBus
from live.core.clock import FakeClock
from live.core.events import LiveEvent, ViewerProfile
from live.interaction.cue import Cue
from live.interaction.director import Director
from live.interaction.engine import InteractionEngine
from live.interaction.memory import ViewerMemory
from live.interaction.names import short_name
from live.interaction.queue import CueQueue


class Room:
    """测试台：同一个假时钟驱动 记忆 + 局面 + 导演，cues 收集导演产出。"""

    def __init__(self, cfg, mem_path, clock=None, memory=None, idle=False):
        self.cfg = cfg
        if not idle:                     # 冷场话术（always）会在推进时钟时插话、占用发声间隔；专门的用例再打开
            cfg["interaction"]["triggers"] = {"idle": {"enabled": False}, "idle_interact": {"enabled": False}}
        self.clock = clock or FakeClock(1_000_000.0)
        self.bus = EventBus()
        self.mem = memory or ViewerMemory(cfg, path=str(mem_path), clock=self.clock)
        self.queue = CueQueue(cfg, self.clock)
        self.director = Director(cfg, self.clock, rng=random.Random(7))
        self.engine = InteractionEngine(cfg, self.bus, self.queue, self.mem, clock=self.clock, director=self.director,
                                        start_thread=False)
        self.cues = []
        self._n = 0

    def ev(self, type_, nick="", uid=None, gender="unknown", wealth=0, fans=0, **data):
        self._n += 1
        user = None
        if nick or uid:
            user = ViewerProfile(user_id=uid if uid is not None else f"u_{nick}", nickname=nick, gender=gender,
                                 wealth_level=wealth, fans_club={"name": "团", "level": fans, "status": 1} if fans else None)
        e = LiveEvent(type=type_, platform="mock", room_id="r", ts=self.clock.now(), msg_id=f"m{self._n}", user=user,
                      data=data)
        self.engine.on_event(e)                # 关键事件会立即 tick
        self.cues += self._drain()
        return e

    def gift(self, nick, gift="玫瑰", unit=1, count=1, **kw):
        return self.ev("gift", nick, gift_name=gift, unit_diamonds=unit, count=count, total_diamonds=unit * count,
                       is_final=True, **kw)

    def _drain(self):
        out = []
        while True:
            c = self.queue.get(timeout=0)
            if c is None:
                return out
            out.append(c)

    def tick(self, advance=0.0):
        if advance:
            self.clock.advance(advance)
        self.engine.tick()
        self.cues += self._drain()
        return self.cues

    def last(self):
        return self.cues[-1] if self.cues else None

    def events(self):
        return [c.event for c in self.cues]


@pytest.fixture
def room(mock_cfg, tmp_path):
    return Room(mock_cfg, tmp_path / "viewers.json")


def test_short_names():
    assert short_name("诗江传媒…王少", "male") == "王少哥"
    assert short_name("多次婉拒砂糖桔", "female") == "多次姐"
    assert short_name("六哥牛杂", "male") == "六哥"
    assert short_name("用户12345", "female") == "用户姐"
    assert short_name("abc", "unknown") == "用户"
    assert short_name("小王", "unknown") == "小王"


def test_gift_tiers_s_m_l(room):
    room.gift("李姐", "玫瑰", 1, 3, gender="female")
    c = room.last()
    assert c.event == "gift_s" and "李姐" in c.fallback_text and "玫瑰" in c.fallback_text and c.priority == 59
    room.tick(10)
    room.gift("小王", "加油鸭", 15, 1, gender="male")
    assert room.last().event == "gift_m" and "1个加油鸭" in room.last().fallback_text
    room.tick(10)
    room.gift("钻石大哥", "甜甜圈", 52, 1, gender="male")
    c = next(c for c in room.cues if c.event == "gift_l")      # 同一拍里榜首也易主了，会紧跟一条 gift_king_dethroned
    assert "甜甜圈" in c.fallback_text and "钻石哥" in c.fallback_text
    assert c.situation["gift"] == {"name": "甜甜圈", "count": 1, "total_diamonds": 52}


def test_same_gift_not_thanked_twice(room):
    room.gift("小王", "玫瑰", 1, 1)
    n = len(room.cues)
    room.tick(1)
    room.tick(1)
    assert [c.event for c in room.cues[n:]].count("gift_s") == 0


def test_repeat_gift_within_window(room):
    room.gift("小王", "玫瑰", 1, 5, gender="male")
    room.tick(60)
    room.gift("小王", "玫瑰", 1, 5, gender="male")
    assert room.last().event == "repeat_gift"
    room.tick(200)
    room.gift("小王", "玫瑰", 1, 5, gender="male")            # 超过 180s 窗口：普通答谢
    assert room.last().event == "gift_s"


def test_gift_followups_at_18_and_45s(room):
    room.gift("小王", "加油鸭", 15, 1, gender="male")
    assert room.last().event == "gift_m"
    room.tick(10)
    assert room.last().event == "gift_m"                      # 还没到 18s
    room.tick(9)
    assert room.last().event == "gift_followup" and "加油鸭" in room.last().fallback_text
    room.tick(26)
    assert room.events().count("gift_followup") == 2
    room.tick(60)
    assert room.events().count("gift_followup") == 2          # 只回踩两次


def test_gift_king_dethroned(room):
    room.gift("甲", "加油鸭", 15, 1)
    room.tick(5)
    room.gift("乙", "甜甜圈", 52, 1)
    evs = room.events()
    assert "gift_l" in evs and "gift_king_dethroned" in evs and evs.index("gift_l") < evs.index("gift_king_dethroned")
    c = next(c for c in room.cues if c.event == "gift_king_dethroned")
    assert "乙" in c.fallback_text and "甲" in c.fallback_text or "52" in c.fallback_text


def test_welcome_tiers_with_memory(mock_cfg, tmp_path):
    path = tmp_path / "v.json"
    r1 = Room(mock_cfg, path)
    r1.ev("member", "金主姐", gender="female")                 # 纯进场：不建档
    assert r1.mem.profile("u_金主姐") == {}
    r1.gift("金主姐", "浪漫花束", 299, 1, gender="female")      # 送礼：建档
    r1.ev("chat", "熟客哥", gender="male", content="来了")
    for _ in range(3):
        r1.tick(30)
        r1.ev("member", "熟客哥", gender="male")
    assert r1.mem.profile("u_熟客哥")["visits"] == 3 and r1.mem.profile("u_熟客哥")["known_before"] is False
    r1.mem.flush()

    r2 = Room(mock_cfg, path)                                 # 第二场：新进程重载
    assert r2.mem.profile("u_金主姐")["known_before"] is True
    r2.ev("member", "金主姐", gender="female")
    assert r2.last().event == "welcome_vip" and "金主姐" in r2.last().fallback_text
    r2.tick(30)
    r2.ev("member", "熟客哥", gender="male")
    assert r2.last().event == "welcome_regular"
    r2.tick(30)
    r2.ev("member", "土豪", gender="male", wealth=25)          # 首次来的高等级也认
    assert r2.last().event == "welcome_grade" and "土豪哥" in r2.last().fallback_text
    assert r2.last().situation["viewer"]["财富等级"] == 25
    r2.tick(30)
    r2.ev("member", "新大哥")
    r2.tick(30)
    r2.gift("新大哥", "跑车", 1200, 1)                          # 本场新人送大礼
    r2.tick(30)
    r2.tick(30)                                               # 先把两次送礼回踩（18s / 45s）走完
    r2.tick(30)
    r2.ev("member", "新大哥")
    assert r2.last().event == "welcome_back"                  # 不是金主（known_before=False），只是本场回头客
    r2.tick(30)
    r2.ev("member", "路人")
    assert r2.last().event == "welcome_member"


def test_like_threshold_and_repeat_cooldown(room):
    room.ev("like", "点赞王", count=3)
    assert "like" not in room.events()
    room.ev("like", "点赞王", count=3)
    assert room.last().event == "like" and "点赞" in room.last().fallback_text
    room.tick(8)
    room.ev("like", "点赞王", count=6)
    room.tick(25)                                             # 冷却 20s × 3 = 60s，同一人还不能再点名
    room.ev("like", "点赞王", count=6)
    assert room.events().count("like") == 1
    room.tick(40)
    room.ev("like", "点赞王", count=6)
    assert room.events().count("like") == 2


def test_product_question_priority_and_fifo(room):
    room.ev("chat", "甲", content="主播好")
    room.ev("chat", "乙", content="这个多少钱")
    room.ev("chat", "丙", content="有什么尺码")
    evs = room.events()
    assert evs[:1] == ["chat_reply"] and evs.count("product_question") >= 1
    qs = [c for c in room.cues if c.event == "product_question"]
    assert qs[0].question == "这个多少钱" and qs[0].use_llm      # 先来先答
    room.tick(5)
    qs = [c for c in room.cues if c.event == "product_question"]
    assert [c.question for c in qs] == ["这个多少钱", "有什么尺码"]
    assert room.mem.profile("u_乙")["last_question"] == "这个多少钱"


def test_purchase_intent_and_spam(room):
    room.ev("chat", "小张", content="几号链接，我要了")
    assert room.last().event == "purchase_intent" and room.last().priority == 55
    n = len(room.cues)
    room.tick(10)
    room.ev("chat", "广告", content="加微信领福利")
    room.ev("chat", "刷屏", content="1111")
    assert [c.event for c in room.cues[n:]] == []


def test_chat_not_answered_twice(room):
    room.ev("chat", "甲", content="主播好漂亮")
    room.tick(1)
    room.tick(1)
    assert room.events().count("chat_reply") == 1


def test_merge_member_with_surge(room):
    room.ev("room_stat", online=60)
    room.tick(10)
    room.ev("room_stat", online=90)
    room.ev("member", "小李", gender="male")
    merged = [c for c in room.cues if c.reason.startswith("[合并]")]
    assert merged and "欢迎小李哥" in merged[-1].fallback_text and "新朋友" in merged[-1].fallback_text


def test_viewer_drain_and_milestone(room):
    room.cfg["interaction"]["online"]["milestone_step"] = 50
    room.ev("room_stat", online=40)
    room.tick(20)
    assert room.events() == []                                # 第一次的在线人数只记基线，不播"破整"
    room.ev("room_stat", online=55)
    room.tick(20)
    assert room.events() == ["viewer_surge"]                  # 同一拍猛涨优先；破整挂起等空档
    room.tick(20)
    assert room.events()[-1] == "online_milestone" and "50" in room.last().fallback_text
    room.ev("room_stat", online=55)
    room.tick(10)
    room.ev("room_stat", online=45)                           # 45 秒窗口内净减 10
    room.tick(20)
    assert "viewer_drain" in room.events()


def test_non_urgent_waits_for_speak_interval(room):
    room.ev("member", "甲")
    assert room.last().event == "welcome_member"
    room.tick(1)
    room.ev("member", "乙")                                  # 优先级 20 < 24，刚说过一句，等发声间隔
    assert room.events().count("welcome_member") == 1
    room.tick(10)
    room.ev("member", "丙")
    assert room.events().count("welcome_member") == 1         # 能量低时间隔接近 speak_max_s（18s）
    room.tick(8)
    room.ev("member", "丁")
    assert room.events().count("welcome_member") == 2


def test_trigger_override_disable_and_priority(room):
    room.cfg["interaction"]["triggers"] = {"gift_s": {"enabled": False}, "follow": {"priority": 70, "lines": ["谢谢{follow_user}！"]}}
    room.director.reload_triggers()
    room.gift("小王", "玫瑰", 1, 1)
    assert "gift_s" not in room.events()
    room.ev("follow", "小美", gender="female")
    assert room.last().event == "follow" and room.last().priority == 70 and room.last().fallback_text == "谢谢小美姐！"


def test_paused_interaction_emits_nothing(room):
    room.cfg["host"]["pause_interaction"] = True
    room.gift("小王", "跑车", 1200, 1)
    assert room.cues == []
    room.cfg["host"]["pause_interaction"] = False
    room.tick(1)
    assert room.last().event == "gift_l"


def test_not_on_air_without_danmaku(mock_cfg, tmp_path):
    r = Room(mock_cfg, tmp_path / "v.json")
    r.engine.is_live = lambda: False
    r.tick(200)                                               # 没接弹幕、也没有事件：不出冷场话术
    assert r.cues == []
    r.ev("member", "甲")
    assert r.cues and r.last().event == "welcome_member"


# ---------------------------------------------------------------- 队列
def test_queue_priority_fifo_stale_and_coalesce(mock_cfg):
    clk = FakeClock(100.0)
    q = CueQueue(mock_cfg, clk)
    dropped = []
    q.on_drop = lambda c, reason: dropped.append((c.event, reason))
    q.push(Cue(event="welcome_member", priority=20))
    q.push(Cue(event="welcome_member", priority=20))          # 同类合并：只留最新
    q.push(Cue(event="chat_x", priority=47))
    q.push(Cue(event="chat_y", priority=47))
    q.push(Cue(event="gift_l", priority=63, max_stale_s=90))
    assert dropped == [("welcome_member", "被同类新事件合并")]
    assert q.get(timeout=0).event == "gift_l"
    assert [q.get(timeout=0).event for _ in range(2)] == ["chat_x", "chat_y"]
    clk.advance(11)                                           # 超过默认 10s
    assert q.get(timeout=0) is None and ("welcome_member", "排队过期") in dropped
    q.push(Cue(event="gift_m", priority=60, max_stale_s=60))
    clk.advance(30)
    assert q.peek_priority() == 60


def test_idle_chatter_in_lulls(mock_cfg, tmp_path):
    r = Room(mock_cfg, tmp_path / "v.json", idle=True)
    r.tick(1)
    assert r.last().event == "idle" and r.last().priority == 0
    r.tick(10)
    assert r.events().count("idle") == 1                      # 冷却 25s
    r.tick(20)
    assert r.events().count("idle") == 2
    r.ev("rank", ranks=[{"user_id": "a", "nickname": "王阿姨", "score": 3}])
    r.tick(30)
    assert "idle_interact" in r.events()
