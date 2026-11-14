"""导演信号与触发器字段的公共定义。

- PLATFORM_SIGNALS：平台事件派生的固定信号（礼物、进场、关注、在线人数……），所有人设通用；
- 人设自己的弹幕意图名（persona.yaml 的 intents[].name）也是信号，由导演在每一拍按意图队列动态补充；
- TRIGGER_FIELDS：triggers.yaml 里允许的字段，Web 触发器编辑器也用这一份。

放在这里是为了让人设模板（live/personas/models.py）不必 import 导演实现，避免循环依赖。
"""
from __future__ import annotations

PLATFORM_SIGNALS = frozenset({
    "has_big_gift", "gift_s", "gift_m", "gift_l", "repeat_gift", "gift_followup", "gift_king_dethroned",
    "has_new_chat",
    "has_new_member", "returning_member", "vip_member", "regular_member", "high_grade_member",
    "has_new_follow", "has_fansclub", "has_share", "has_like",
    "viewer_surge", "viewer_drain", "online_milestone", "has_online_viewers",
})
TRIGGER_FIELDS = ("when", "priority", "emotion", "target_energy", "cooldown_s", "max_stale_s", "use_llm", "role",
                  "merge_clause", "merge_with", "directive", "lines", "enabled")
INTENT_WINDOWS = ("chat_s", "question_s", "gift_s", "member_s", "follow_s")
RESERVED_INTENTS = {"chitchat", "spam", "noise", "always"}
# 导演每拍快照里已有的键；人设意图名不能和它们重名，否则会把局面信号覆盖掉。
SNAPSHOT_KEYS = frozenset({
    "gift", "followup_gift", "chat", "member", "follow", "fansclub", "share", "liker", "question", "purchase",
    "online", "online_delta", "milestone", "online_viewers", "gift_king", "intent_records",
    "_intent_records",
})
