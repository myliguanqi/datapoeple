"""统一事件模型：所有弹幕平台（抖音 / mock / 回放）都转成 LiveEvent，下游只认这一种结构。

LiveEvent 必须能 JSON 序列化：子进程用 to_dict() 经 multiprocessing.Queue 传给主进程，
录制回放写 JSONL，Web 面板直接推 to_dict()。平台特有字段放 ViewerProfile.extra / LiveEvent.data。

各 type 的 data 约定（见 docs/live/ARCHITECTURE.md「统一事件模型」）：
  chat           {"content", "is_emoji"}
  gift           {"gift_id", "gift_name", "unit_diamonds", "count", "total_diamonds", "combo_id", "is_final", "to_user"}
                 is_final=True 为连击结算后的那一条；互动引擎只处理 final
  like           {"count", "total"}
  member         {"member_count", "enter_type"}
  follow / share {"action", "share_type"}
  fansclub       {"kind": "join" | "upgrade", "content"}
  room_stat      {"online", "total_pv", "popularity"}
  rank           {"ranks": [{"user_id", "nickname", "score"}]}
  room_stats     {"display_long", "display_value", "total"}
  room_notice    {"content"}
  product_change {"products": [...], "toast"}
  stream_end     {}
  system         {"message"}（数据源自身的状态提示，如 mock 源启动）
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields

EVENT_TYPES = (
    "chat", "gift", "like", "member", "follow", "share", "fansclub", "room_stat", "rank",
    "room_stats", "room_notice", "product_change", "stream_end", "system",
)
GENDERS = ("male", "female", "unknown")


@dataclass
class ViewerProfile:
    user_id: str                       # 平台内唯一 ID（统一转字符串）；拿不到时为 ""
    nickname: str = ""
    gender: str = "unknown"            # male | female | unknown
    wealth_level: int = 0              # 财富等级（抖音 pay_grade.level，观众榜"钻石X"的 X）
    wealth_name: str = ""
    wealth_diamonds: int = 0           # 累计送出钻石
    fans_club: dict | None = None      # {"name", "level", "status"}
    user_level: int = 0
    followers: int | None = None
    follow_status: int | None = None
    verified: bool = False
    avatar_url: str = ""               # 只用于实时展示，不落盘
    badges: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)   # 平台特有：short_id、display_id、sec_uid、age_range…

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ViewerProfile | None":
        if not d:
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def fans_level(self) -> int:
        return int((self.fans_club or {}).get("level") or 0)


@dataclass
class LiveEvent:
    type: str
    platform: str                      # douyin | mock | replay
    room_id: str = ""
    ts: float = field(default_factory=time.time)   # 墙钟秒
    msg_id: str = ""                   # 平台消息 ID，去重用
    user: ViewerProfile | None = None
    data: dict = field(default_factory=dict)
    in_room: dict = field(default_factory=dict)    # {"consume": int, "gift_count": int}（本场消费 / 本场送礼次数）
    raw_method: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.type, "platform": self.platform, "room_id": self.room_id, "ts": self.ts,
            "msg_id": self.msg_id, "user": self.user.to_dict() if self.user else None,
            "data": dict(self.data), "in_room": dict(self.in_room), "raw_method": self.raw_method,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LiveEvent":
        return cls(
            type=d["type"], platform=d.get("platform", ""), room_id=str(d.get("room_id") or ""),
            ts=float(d.get("ts") or time.time()), msg_id=str(d.get("msg_id") or ""),
            user=ViewerProfile.from_dict(d.get("user")), data=dict(d.get("data") or {}),
            in_room=dict(d.get("in_room") or {}), raw_method=d.get("raw_method", ""),
        )

    @property
    def user_id(self) -> str:
        return self.user.user_id if self.user else ""

    @property
    def nickname(self) -> str:
        return self.user.nickname if self.user else ""
