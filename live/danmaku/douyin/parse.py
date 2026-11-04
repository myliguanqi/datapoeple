"""抖音 protobuf 消息 → LiveEvent 的纯函数（不联网、无副作用，单元测试直接用 betterproto 构造字节）。

字段对照见 docs/tasks/phase2-live-commerce/02-reference-douyin_zhibo.md §3。原则：
- 尽可能提取全部公开字段（性别、财富等级、粉丝团、关注数、徽章、本场消费……），平台特有的放 ViewerProfile.extra；
- **隐私红线**：生日 birthday、手机号 telephone 即使有值也不提取；城市/签名只在非空时放 extra（不落盘，见长期记忆）；
- 礼物原始包 is_final=None，由 GiftComboAggregator 聚合成"连击中"(False) 与"结算"(True)，互动引擎只处理结算的那条。

⚠ 需要上播实测核对的点（P2 用探针录真实数据后补到 02 §3「实测」列）：性别枚举、连击推送规律（combo_count /
repeat_count / group_count 的含义）、Social 消息里关注与分享的区分、带货房间商品消息的 method 名。
"""
from __future__ import annotations

import time

from live.core.events import LiveEvent, ViewerProfile
from live.danmaku.douyin.vendor import pb

P = pb()
DEFAULT_GENDER_MAP = {1: "male", 2: "female"}


# ---------------------------------------------------------------- 小工具
def parse_cn_count(s) -> int | None:
    """抖音人数字符串 → int：'1234'→1234、'1.2万'→12000、'12w'→120000、'3.5亿'→350000000、''/None→None。"""
    s = str(s or "").strip()
    if not s:
        return None
    try:
        mult = 1.0
        for unit, m in (("亿", 1e8), ("万", 1e4), ("w", 1e4), ("W", 1e4)):
            if unit in s:
                s = s.replace(unit, "")
                mult = m
                break
        s = "".join(ch for ch in s if ch.isdigit() or ch == ".")
        return int(round(float(s) * mult)) if s else None
    except ValueError:
        return None


def normalize_gender_map(gm) -> dict:
    out = {}
    for k, v in (gm or DEFAULT_GENDER_MAP).items():
        try:
            out[int(k)] = v if v in ("male", "female", "unknown") else "unknown"
        except (TypeError, ValueError):
            continue
    return out or dict(DEFAULT_GENDER_MAP)


def _img_url(img) -> str:
    try:
        urls = list(img.url_list_list or [])
        return urls[0] if urls else ""
    except AttributeError:
        return ""


def _img_name(img) -> str:
    try:
        c = img.content
        return (c.name or c.alternative_text or "").strip()
    except AttributeError:
        return ""


def user_to_profile(u, gender_map: dict | None = None) -> ViewerProfile | None:
    if u is None:
        return None
    uid = str(u.id_str or (u.id if u.id else "") or "")
    nick = u.nick_name or ""
    if not uid and not nick:
        return None
    gm = gender_map or DEFAULT_GENDER_MAP
    pg = u.pay_grade
    fc = u.fans_club.data if u.fans_club else None
    fi = u.follow_info
    badges = []
    for img in list(u.badge_image_list or []) + list(u.real_time_icons_list or []) + [u.medal]:
        name, url = _img_name(img), _img_url(img)
        if name or url:
            badges.append({"name": name, "url": url})
    extra = {}
    for key, val in (("short_id", u.short_id), ("display_id", u.display_id), ("sec_uid", u.sec_uid),
                     ("age_range", u.age_range), ("experience", u.experience),
                     ("following", fi.following_count if fi else 0),
                     ("fan_ticket_count", u.fan_ticket_count)):
        if val:
            extra[key] = str(val) if key == "short_id" else val
    if u.city:
        extra["city"] = u.city                # 只用于实时展示，不落盘
    if u.signature:
        extra["signature"] = u.signature
    if pg and (pg.next_diamond or pg.now_diamond or pg.upgrade_need_consume):
        extra["wealth_progress"] = {"now": int(pg.now_diamond), "next": int(pg.next_diamond),
                                    "need": int(pg.upgrade_need_consume), "next_name": pg.next_name}
    # 注意：birthday / telephone 故意不读（隐私红线）
    return ViewerProfile(
        user_id=uid,
        nickname=nick,
        gender=gm.get(int(u.gender or 0), "unknown"),
        wealth_level=int(pg.level) if pg else 0,
        wealth_name=(pg.name or "") if pg else "",
        wealth_diamonds=int(pg.total_diamond_count) if pg else 0,
        fans_club=({"name": fc.club_name, "level": int(fc.level), "status": int(fc.user_fans_club_status)}
                   if fc and (fc.club_name or fc.level) else None),
        user_level=int(u.level or 0),
        followers=int(fi.follower_count) if fi and fi.follower_count else None,
        follow_status=int(fi.follow_status) if fi and fi.follow_status else None,
        verified=bool(u.verified),
        avatar_url=_img_url(u.avatar_thumb),
        badges=badges,
        extra=extra,
    )


def _in_room(pac) -> dict:
    if pac is None:
        return {}
    out = {}
    if pac.user_consume_in_room:
        out["consume"] = int(pac.user_consume_in_room)
    if pac.user_send_gift_cnt_in_room:
        out["gift_count"] = int(pac.user_send_gift_cnt_in_room)
    return out


class _Ctx:
    def __init__(self, method, gender_map, msg_id, room_id, now):
        self.method, self.gm, self.msg_id, self.room_id, self.now = method, gender_map, msg_id, room_id, now

    def ev(self, type_, common=None, user=None, data=None, in_room=None) -> LiveEvent:
        mid = str(common.msg_id) if common is not None and common.msg_id else (str(self.msg_id) if self.msg_id else "")
        rid = str(common.room_id) if common is not None and common.room_id else self.room_id
        return LiveEvent(type=type_, platform="douyin", room_id=rid, ts=self.now, msg_id=mid,
                         user=user_to_profile(user, self.gm) if user is not None else None,
                         data=data or {}, in_room=in_room or {}, raw_method=self.method)


# ---------------------------------------------------------------- 各消息
def _chat(payload, c: _Ctx):
    m = P.ChatMessage().parse(payload)
    return [c.ev("chat", m.common, m.user, {"content": m.content, "is_emoji": False, "event_time": int(m.event_time)},
                 _in_room(m.public_area_common))]


def _emoji(payload, c: _Ctx):
    m = P.EmojiChatMessage().parse(payload)
    return [c.ev("chat", m.common, m.user, {"content": m.default_content or "[表情]", "is_emoji": True,
                                           "emoji_id": int(m.emoji_id)})]


def gift_count(m) -> int:
    """单次推送代表的礼物数量。⚠ 推送规律需实测：常见是连击过程中 repeat_count / combo_count 递增，
    结束时 repeat_end=1；group_count>1 表示一次送一组（如 10 个一组）。"""
    n = max(int(m.combo_count or 0), int(m.repeat_count or 0), 1)
    if int(m.group_count or 0) > 1:
        n *= int(m.group_count)
    return n


def _gift(payload, c: _Ctx):
    m = P.GiftMessage().parse(payload)
    g = m.gift
    unit = int(g.diamond_count or 0)
    gift_id = str(m.gift_id or g.id or "")
    uid = str(m.user.id_str or m.user.id or m.user.nick_name or "")
    group = str(m.group_id or "") or (m.trace_id or "")
    count = gift_count(m)
    data = {
        "gift_id": gift_id, "gift_name": g.name or "礼物", "unit_diamonds": unit, "count": count,
        "total_diamonds": unit * count, "combo_id": f"{uid}:{gift_id}:{group}", "is_final": None,
        "comboable": bool(g.combo), "repeat_end": int(m.repeat_end or 0),
        "to_user": m.to_user.nick_name if m.to_user and m.to_user.nick_name else "",
        "raw": {"combo_count": int(m.combo_count), "repeat_count": int(m.repeat_count),
                "group_count": int(m.group_count), "total_count": int(m.total_count),
                "group_id": str(m.group_id or ""), "trace_id": m.trace_id or "", "send_time": int(m.send_time or 0)},
    }
    return [c.ev("gift", m.common, m.user, data, _in_room(m.public_area_common))]


def _like(payload, c: _Ctx):
    m = P.LikeMessage().parse(payload)
    return [c.ev("like", m.common, m.user, {"count": int(m.count), "total": int(m.total)})]


def _member(payload, c: _Ctx):
    m = P.MemberMessage().parse(payload)
    return [c.ev("member", m.common, m.user, {"member_count": int(m.member_count), "enter_type": int(m.enter_type),
                                             "action": int(m.action)}, _in_room(m.public_area_common))]


def _social(payload, c: _Ctx):
    m = P.SocialMessage().parse(payload)
    # ⚠ 需实测：一般 action=1 是关注；share_type 非 0 或 action=3 视为分享
    kind = "share" if (m.share_type or m.action == 3) else "follow"
    return [c.ev(kind, m.common, m.user, {"action": int(m.action), "share_type": int(m.share_type),
                                         "share_target": m.share_target, "follow_count": int(m.follow_count)},
                 _in_room(m.public_area_common))]


def _fansclub(payload, c: _Ctx):
    m = P.FansclubMessage().parse(payload)
    kind = {1: "upgrade", 2: "join"}.get(int(m.type), "other")
    return [c.ev("fansclub", m.common_info, m.user, {"kind": kind, "content": m.content})]


def _room_user_seq(payload, c: _Ctx):
    m = P.RoomUserSeqMessage().parse(payload)
    online = parse_cn_count(m.online_user_for_anchor)   # 真·在线人数（字符串，如"1.2万"），不是 total
    if online is None:
        online = int(m.total or 0)
    return [c.ev("room_stat", m.common, None, {
        "online": online, "total_pv": parse_cn_count(m.total_pv_for_anchor), "popularity": int(m.popularity or 0),
        "total_user": int(m.total_user or 0),
        "raw": {"online_str": m.online_user_for_anchor, "total": int(m.total or 0), "total_pv_str": m.total_pv_for_anchor},
    })]


def _rank(payload, c: _Ctx):
    m = P.RoomRankMessage().parse(payload)
    ranks = []
    for r in m.ranks_list:
        if not r.user or not r.user.nick_name:
            continue
        ranks.append({"user_id": str(r.user.id_str or r.user.id or ""), "nickname": r.user.nick_name,
                      "score": parse_cn_count(r.score_str) or 0})
    return [c.ev("rank", m.common, None, {"ranks": ranks})]


def _room_stats(payload, c: _Ctx):
    m = P.RoomStatsMessage().parse(payload)
    return [c.ev("room_stats", m.common, None, {"display_long": m.display_long, "display_value": int(m.display_value),
                                               "total": int(m.total)})]


def _room_msg(payload, c: _Ctx):
    m = P.RoomMessage().parse(payload)
    if not m.content:
        return []
    return [c.ev("room_notice", m.common, None, {"content": m.content, "system_top_msg": bool(m.system_top_msg)})]


def _control(payload, c: _Ctx):
    m = P.ControlMessage().parse(payload)
    if int(m.status) == 3:                      # 下播
        return [c.ev("stream_end", m.common, None, {"status": 3})]
    return []


def _product_change(payload, c: _Ctx):
    m = P.ProductChangeMessage().parse(payload)
    products = [{"promotion_id": str(p.promotion_id), "index": int(p.index), "explain_type": int(p.explain_type)}
                for p in m.update_product_info_list]
    return [c.ev("product_change", m.common, None, {"products": products, "toast": m.update_toast, "total": int(m.total)})]


def _live_shopping(payload, c: _Ctx):
    m = P.LiveShoppingMessage().parse(payload)
    return [c.ev("product_change", m.common, None, {"products": [{"promotion_id": str(m.promotion_id)}],
                                                   "toast": "", "kind": "live_shopping", "msg_type": int(m.msg_type)})]


HANDLERS = {
    "WebcastChatMessage": _chat,
    "WebcastEmojiChatMessage": _emoji,
    "WebcastGiftMessage": _gift,
    "WebcastLikeMessage": _like,
    "WebcastMemberMessage": _member,
    "WebcastSocialMessage": _social,
    "WebcastFansclubMessage": _fansclub,
    "WebcastRoomUserSeqMessage": _room_user_seq,
    "WebcastRoomRankMessage": _rank,
    "WebcastRoomStatsMessage": _room_stats,
    "WebcastRoomMessage": _room_msg,
    "WebcastControlMessage": _control,
    "WebcastProductChangeMessage": _product_change,     # ⚠ method 名需抓包确认
    "WebcastLiveShoppingMessage": _live_shopping,       # ⚠ 同上
}
IGNORED = {"WebcastRoomStreamAdaptationMessage"}       # 每条都刷的流配置，纯噪音


def parse_message(method: str, payload: bytes, *, gender_map: dict | None = None, msg_id=0, room_id: str = "",
                  now: float | None = None) -> list:
    """未知 method 返回 []（由调用方记录"首次出现"）。解析异常向上抛，由调用方打 warning。"""
    fn = HANDLERS.get(method)
    if fn is None:
        return []
    return fn(payload, _Ctx(method, normalize_gender_map(gender_map), msg_id, room_id,
                            time.time() if now is None else now))


def is_known(method: str) -> bool:
    return method in HANDLERS or method in IGNORED


# ---------------------------------------------------------------- 连击礼物聚合
class GiftComboAggregator:
    """同一连击会推多条（数量递增，结束时 repeat_end=1）。按 combo_id（用户:礼物:组）聚合：
    - 不可连击的礼物：直接结算；
    - 连击中：产出 is_final=False 的进度事件（Web 可显示"连击中 ×N"）；
    - repeat_end=1 或超过 timeout_s 没有新包：结算一次（is_final=True，count 取聚合期间最大值）；
    - 结算后 dup_window_s 内同一连击的重复推送（数量不超过已结算值）丢弃，避免重复感谢、重复累计。
    """

    def __init__(self, timeout_s: float = 3.0, now_fn=time.time, dup_window_s: float = 5.0):
        self.timeout_s = float(timeout_s)
        self.dup_window_s = float(dup_window_s)
        self._now = now_fn
        self._open: dict = {}
        self._done: dict = {}

    def feed(self, ev: LiveEvent) -> list:
        if ev.type != "gift":
            return [ev]
        d = ev.data
        key = d.get("combo_id") or f"{ev.user_id}:{d.get('gift_id')}"
        now = self._now()
        done = self._done.get(key)
        if done and now - done[1] <= self.dup_window_s and int(d.get("count", 1)) <= done[0]:
            return []
        if not d.get("comboable"):
            return [self._final_event(ev, int(d.get("count", 1)), key, now)]
        st = self._open.get(key)
        if st is None:
            st = self._open[key] = {"ev": ev, "count": 0, "last": now}
        st["count"] = max(st["count"], int(d.get("count", 1)))
        st["last"] = now
        st["ev"] = ev
        if d.get("repeat_end"):
            self._open.pop(key, None)
            return [self._final_event(ev, st["count"], key, now)]
        interim = LiveEvent.from_dict(ev.to_dict())
        interim.data.update(count=st["count"], total_diamonds=int(d.get("unit_diamonds", 0)) * st["count"],
                            is_final=False)
        return [interim]

    def flush(self) -> list:
        now = self._now()
        out = []
        for key, st in list(self._open.items()):
            if now - st["last"] >= self.timeout_s:
                self._open.pop(key, None)
                out.append(self._final_event(st["ev"], st["count"], key, now))
        for key, (_, ts) in list(self._done.items()):
            if now - ts > max(30.0, self.dup_window_s):
                self._done.pop(key, None)
        return out

    def _final_event(self, ev: LiveEvent, count: int, key: str, now: float) -> LiveEvent:
        out = LiveEvent.from_dict(ev.to_dict())
        out.ts = now
        out.data.update(count=count, total_diamonds=int(ev.data.get("unit_diamonds", 0)) * count, is_final=True)
        self._done[key] = (count, now)
        return out

    @property
    def pending(self) -> int:
        return len(self._open)
