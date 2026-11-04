"""P2：抖音 protobuf → LiveEvent 全字段解析（用 betterproto 构造消息字节，不依赖真实直播间）、连击聚合。"""
import json

from live.danmaku.douyin import parse
from live.danmaku.douyin.parse import P, GiftComboAggregator, parse_cn_count, parse_message


def _user(**kw):
    base = dict(
        id=123456, id_str="123456", short_id=987, display_id="dy_wang", sec_uid="MS4wLjAB", nick_name="小王",
        gender=1, level=23, verified=True, age_range=3, city="杭州", signature="买买买",
        birthday=19900101, telephone="13800000000",
        avatar_thumb=P.Image(url_list_list=["https://p3.example/avatar.jpg"]),
        pay_grade=P.PayGrade(level=12, name="12级", total_diamond_count=4321, now_diamond=300, next_diamond=500,
                             upgrade_need_consume=200, next_name="13级"),
        fans_club=P.FansClub(data=P.FansClubData(club_name="美美团", level=5, user_fans_club_status=1)),
        follow_info=P.FollowInfo(follower_count=88, following_count=12, follow_status=1),
        badge_image_list=[P.Image(url_list_list=["https://b/1.png"], content=P.ImageContent(name="荣誉等级"))],
    )
    base.update(kw)
    return P.User(**base)


def _common(msg_id=111, room_id=7777):
    return P.Common(msg_id=msg_id, room_id=room_id)


def test_chat_full_profile_and_privacy():
    m = P.ChatMessage(common=_common(), user=_user(), content="这个多少钱",
                      public_area_common=P.PublicAreaCommon(user_consume_in_room=66, user_send_gift_cnt_in_room=3))
    [ev] = parse_message("WebcastChatMessage", bytes(m), now=100.0)
    assert ev.type == "chat" and ev.platform == "douyin" and ev.msg_id == "111" and ev.room_id == "7777"
    assert ev.data["content"] == "这个多少钱" and ev.data["is_emoji"] is False
    assert ev.in_room == {"consume": 66, "gift_count": 3}
    u = ev.user
    assert (u.user_id, u.nickname, u.gender, u.wealth_level, u.wealth_name, u.wealth_diamonds) == \
        ("123456", "小王", "male", 12, "12级", 4321)
    assert u.fans_club == {"name": "美美团", "level": 5, "status": 1} and u.fans_level == 5
    assert u.user_level == 23 and u.followers == 88 and u.follow_status == 1 and u.verified
    assert u.avatar_url == "https://p3.example/avatar.jpg"
    assert u.badges == [{"name": "荣誉等级", "url": "https://b/1.png"}]
    assert u.extra["short_id"] == "987" and u.extra["display_id"] == "dy_wang" and u.extra["sec_uid"] == "MS4wLjAB"
    assert u.extra["age_range"] == 3 and u.extra["following"] == 12 and u.extra["city"] == "杭州"
    assert u.extra["wealth_progress"] == {"now": 300, "next": 500, "need": 200, "next_name": "13级"}
    dumped = json.dumps(ev.to_dict(), ensure_ascii=False)
    assert "13800000000" not in dumped and "19900101" not in dumped      # 隐私字段不提取


def test_gender_mapping_and_custom_map():
    for g, want in ((1, "male"), (2, "female"), (0, "unknown"), (9, "unknown")):
        [ev] = parse_message("WebcastMemberMessage", bytes(P.MemberMessage(common=_common(), user=_user(gender=g))))
        assert ev.user.gender == want
    swapped = {"1": "female", "2": "male"}                       # 实测反了就对调
    [ev] = parse_message("WebcastMemberMessage", bytes(P.MemberMessage(user=_user(gender=1))), gender_map=swapped)
    assert ev.user.gender == "female"


def test_user_without_optional_fields():
    m = P.LikeMessage(common=_common(), count=5, total=1200, user=P.User(nick_name="路人"))
    [ev] = parse_message("WebcastLikeMessage", bytes(m))
    assert ev.type == "like" and ev.data == {"count": 5, "total": 1200}
    assert ev.user.user_id == "" and ev.user.nickname == "路人" and ev.user.fans_club is None
    assert ev.user.wealth_level == 0 and ev.user.followers is None


def test_emoji_chat():
    m = P.EmojiChatMessage(common=_common(), user=_user(), emoji_id=42, default_content="")
    [ev] = parse_message("WebcastEmojiChatMessage", bytes(m))
    assert ev.type == "chat" and ev.data["is_emoji"] and ev.data["content"] == "[表情]" and ev.data["emoji_id"] == 42


def test_gift_fields():
    m = P.GiftMessage(common=_common(), user=_user(), gift_id=463, combo_count=3, repeat_count=3, group_count=1,
                      repeat_end=1, group_id=5555, trace_id="t1", to_user=P.User(nick_name="主播"),
                      gift=P.GiftStruct(id=463, name="玫瑰", diamond_count=1, combo=True),
                      public_area_common=P.PublicAreaCommon(user_consume_in_room=10))
    [ev] = parse_message("WebcastGiftMessage", bytes(m))
    d = ev.data
    assert (d["gift_id"], d["gift_name"], d["unit_diamonds"], d["count"], d["total_diamonds"]) == ("463", "玫瑰", 1, 3, 3)
    assert d["combo_id"] == "123456:463:5555" and d["is_final"] is None and d["comboable"] and d["repeat_end"] == 1
    assert d["to_user"] == "主播" and d["raw"]["trace_id"] == "t1" and ev.in_room == {"consume": 10}


def test_gift_group_count_multiplies():
    m = P.GiftMessage(user=_user(), gift_id=1, combo_count=2, group_count=10, gift=P.GiftStruct(name="小心心", diamond_count=1))
    [ev] = parse_message("WebcastGiftMessage", bytes(m))
    assert ev.data["count"] == 20 and ev.data["total_diamonds"] == 20


def test_member_social_fansclub():
    [ev] = parse_message("WebcastMemberMessage", bytes(P.MemberMessage(common=_common(), user=_user(), member_count=321,
                                                                         enter_type=2, action=1)))
    assert ev.type == "member" and ev.data == {"member_count": 321, "enter_type": 2, "action": 1}
    [f] = parse_message("WebcastSocialMessage", bytes(P.SocialMessage(user=_user(), action=1, follow_count=9)))
    assert f.type == "follow" and f.data["follow_count"] == 9
    [s] = parse_message("WebcastSocialMessage", bytes(P.SocialMessage(user=_user(), action=3, share_type=1)))
    assert s.type == "share"
    [j] = parse_message("WebcastFansclubMessage", bytes(P.FansclubMessage(type=2, content="加入粉丝团", user=_user())))
    assert j.type == "fansclub" and j.data == {"kind": "join", "content": "加入粉丝团"}
    [u] = parse_message("WebcastFansclubMessage", bytes(P.FansclubMessage(type=1, user=_user())))
    assert u.data["kind"] == "upgrade"


def test_room_stat_chinese_numbers():
    m = P.RoomUserSeqMessage(common=_common(), online_user_for_anchor="1.2万", total=3, total_pv_for_anchor="35万",
                             popularity=888)
    [ev] = parse_message("WebcastRoomUserSeqMessage", bytes(m))
    assert ev.type == "room_stat" and ev.data["online"] == 12000 and ev.data["total_pv"] == 350000
    assert ev.data["popularity"] == 888 and ev.user is None
    [ev2] = parse_message("WebcastRoomUserSeqMessage", bytes(P.RoomUserSeqMessage(total=17)))
    assert ev2.data["online"] == 17                              # 没有字符串在线数时退回 total
    assert parse_cn_count("12w") == 120000 and parse_cn_count("3.5亿") == 350000000
    assert parse_cn_count("") is None and parse_cn_count("128人在线") == 128


def test_rank_stats_notice_control_product():
    m = P.RoomRankMessage(ranks_list=[P.RoomRankMessageRoomRank(user=_user(), score_str="1.5万"),
                                      P.RoomRankMessageRoomRank(user=P.User(), score_str="1")])
    [ev] = parse_message("WebcastRoomRankMessage", bytes(m))
    assert ev.data["ranks"] == [{"user_id": "123456", "nickname": "小王", "score": 15000}]
    [st] = parse_message("WebcastRoomStatsMessage", bytes(P.RoomStatsMessage(display_long="1.2万本场点赞", display_value=12000)))
    assert st.type == "room_stats" and st.data["display_value"] == 12000
    [rn] = parse_message("WebcastRoomMessage", bytes(P.RoomMessage(content="欢迎来到直播间", system_top_msg=True)))
    assert rn.type == "room_notice" and rn.data["content"] == "欢迎来到直播间"
    [end] = parse_message("WebcastControlMessage", bytes(P.ControlMessage(status=3)))
    assert end.type == "stream_end"
    assert parse_message("WebcastControlMessage", bytes(P.ControlMessage(status=1))) == []
    [pc] = parse_message("WebcastProductChangeMessage", bytes(P.ProductChangeMessage(
        update_toast="上新了", update_product_info_list=[P.ProductInfo(promotion_id=99, index=1)])))
    assert pc.type == "product_change" and pc.data["products"][0]["promotion_id"] == "99" and pc.data["toast"] == "上新了"


def test_unknown_and_ignored_methods():
    assert parse_message("WebcastSomethingNewMessage", b"\x08\x01") == []
    assert parse_message("WebcastRoomStreamAdaptationMessage", b"") == []
    assert not parse.is_known("WebcastSomethingNewMessage") and parse.is_known("WebcastRoomStreamAdaptationMessage")


# ---------------------------------------------------------------- 连击聚合
def _gift_ev(uid="u1", gift="玫瑰", count=1, end=0, group=1, combo=True, unit=1):
    m = P.GiftMessage(user=P.User(id_str=uid, nick_name=uid), gift_id=1 if gift == "玫瑰" else 2, combo_count=count,
                      repeat_count=count, repeat_end=end, group_id=group,
                      gift=P.GiftStruct(name=gift, diamond_count=unit, combo=combo))
    return parse_message("WebcastGiftMessage", bytes(m))[0]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_combo_packets_settle_once():
    clk = _Clock()
    agg = GiftComboAggregator(timeout_s=3, now_fn=clk)
    outs = []
    for n in (1, 2, 3, 4):
        outs += agg.feed(_gift_ev(count=n))
        clk.t += 0.3
    assert [e.data["is_final"] for e in outs] == [False] * 4 and outs[-1].data["count"] == 4
    finals = agg.feed(_gift_ev(count=5, end=1))
    assert len(finals) == 1 and finals[0].data["is_final"] and finals[0].data["count"] == 5
    assert finals[0].data["total_diamonds"] == 5
    assert agg.feed(_gift_ev(count=5, end=1)) == []             # 结束包重推：丢弃
    assert agg.pending == 0


def test_combo_timeout_settles():
    clk = _Clock()
    agg = GiftComboAggregator(timeout_s=3, now_fn=clk)
    agg.feed(_gift_ev(count=1))
    agg.feed(_gift_ev(count=7))
    clk.t += 2.0
    assert agg.flush() == []
    clk.t += 1.5
    [final] = agg.flush()
    assert final.data["is_final"] and final.data["count"] == 7


def test_combo_users_and_gifts_do_not_mix():
    clk = _Clock()
    agg = GiftComboAggregator(timeout_s=3, now_fn=clk)
    agg.feed(_gift_ev(uid="a", count=3))
    agg.feed(_gift_ev(uid="b", count=1))
    agg.feed(_gift_ev(uid="a", gift="小心心", count=2))
    clk.t += 5
    finals = sorted((e.user_id, e.data["gift_name"], e.data["count"]) for e in agg.flush())
    assert finals == [("a", "小心心", 2), ("a", "玫瑰", 3), ("b", "玫瑰", 1)]


def test_non_combo_gift_is_final_immediately():
    agg = GiftComboAggregator(timeout_s=3, now_fn=_Clock())
    [ev] = agg.feed(_gift_ev(gift="跑车", combo=False, unit=1200))
    assert ev.data["is_final"] and ev.data["total_diamonds"] == 1200


def test_new_combo_after_settle_is_counted():
    clk = _Clock()
    agg = GiftComboAggregator(timeout_s=3, now_fn=clk, dup_window_s=5)
    agg.feed(_gift_ev(count=3, end=1, group=1))
    clk.t += 1
    out = agg.feed(_gift_ev(count=1, group=2))                    # 新的一组连击（新 group_id）
    assert out and out[0].data["is_final"] is False
