"""弹幕接口：状态、连接/断开（改设置 → 热切换）、最近事件、模拟注入、mock/回放、登录 cookie（只写）、录制文件列表。"""
from __future__ import annotations

import hashlib
import os
import random
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from live.api.deps import get_ctx
from live.core.events import EVENT_TYPES, LiveEvent, ViewerProfile
from live.danmaku.cookie import cookie_path, cookie_status, write_cookie
from live.danmaku.mock import QUESTIONS

router = APIRouter()


class ConnectBody(BaseModel):
    live_id: str | None = None
    source: str | None = None


class MockBody(BaseModel):
    rate_per_min: int | None = None


class ReplayBody(BaseModel):
    file: str
    speed: float | None = None
    loop: bool | None = None


class CookieBody(BaseModel):
    cookie: str


class SimBody(BaseModel):
    type: str                                # chat | gift | like | member | follow | share | fansclub | room_stat | rank | stream_end | product_change
    nickname: str | None = None
    user_id: str | None = None
    gender: str = "unknown"
    wealth_level: int = 0
    fans_level: int = 0
    content: str | None = None               # chat 内容；type=chat 且 content 为空时随机挑一句产品提问
    gift_name: str = "玫瑰"
    unit_diamonds: int = 1
    count: int = 1
    online: int | None = None                # room_stat：在线人数
    online_delta: int | None = None          # room_stat：在当前在线人数基础上加减（人数猛涨/下滑）
    kind: str | None = None                  # fansclub: join | upgrade
    data: dict | None = None                 # 其他字段原样合并进 event.data


def _update(ctx, changes: dict) -> dict:
    r = ctx.settings.update(changes)
    if not r["ok"]:
        raise HTTPException(400, "；".join(f"{k} {v}" for k, v in r["errors"].items()))
    if not r["applied"]:              # 设置没变化（比如断线后再点连接）→ 主动重启
        ctx.danmaku.restart()
    return ctx.danmaku.status()


@router.get("/api/danmaku/status")
async def status(request: Request):
    return get_ctx(request).danmaku.status()


@router.post("/api/danmaku/connect")
async def connect(request: Request, body: ConnectBody | None = None):
    ctx = get_ctx(request)
    changes = {"danmaku.enabled": True}
    if body and body.source:
        changes["danmaku.source"] = body.source
    if body and body.live_id is not None:
        changes["danmaku.live_id"] = body.live_id.strip()
    src = changes.get("danmaku.source", ctx.cfg["danmaku"]["source"])
    lid = changes.get("danmaku.live_id", ctx.cfg["danmaku"]["live_id"])
    if src == "douyin" and not lid:
        raise HTTPException(400, "请先填写抖音直播间号（live.douyin.com/ 后面那串数字）")
    return _update(ctx, changes)


@router.post("/api/danmaku/disconnect")
async def disconnect(request: Request):
    ctx = get_ctx(request)
    ctx.settings.update({"danmaku.enabled": False})
    ctx.danmaku.restart()
    return ctx.danmaku.status()


@router.get("/api/danmaku/events")
async def events(request: Request, limit: int = 100, type: str | None = None):  # noqa: A002
    types = {t for t in (type or "").split(",") if t} or None
    return get_ctx(request).danmaku.recent_events(limit=max(1, min(limit, 2000)), types=types)


def _sim_user(body: SimBody) -> ViewerProfile:
    nick = (body.nickname or "").strip() or f"测试观众{random.randint(100, 999)}"
    uid = body.user_id or "sim_" + hashlib.md5(nick.encode("utf-8")).hexdigest()[:10]
    gender = body.gender if body.gender in ("male", "female", "unknown") else "unknown"
    fans = {"name": "测试团", "level": body.fans_level, "status": 1} if body.fans_level > 0 else None
    return ViewerProfile(user_id=uid, nickname=nick, gender=gender, wealth_level=max(0, body.wealth_level),
                         wealth_name=f"{body.wealth_level}级" if body.wealth_level > 0 else "", fans_club=fans)


@router.post("/api/danmaku/simulate")
async def simulate(request: Request, body: SimBody):
    ctx = get_ctx(request)
    if body.type not in EVENT_TYPES:
        raise HTTPException(400, f"type 只能是 {' / '.join(EVENT_TYPES)}")
    t = body.type
    user = _sim_user(body) if t in ("chat", "gift", "like", "member", "follow", "share", "fansclub") else None
    data: dict = {}
    if t == "chat":
        data = {"content": (body.content or "").strip() or random.choice(QUESTIONS), "is_emoji": False}
    elif t == "gift":
        count = max(1, body.count)
        unit = max(0, body.unit_diamonds)
        data = {"gift_id": body.gift_name, "gift_name": body.gift_name, "unit_diamonds": unit, "count": count,
                "total_diamonds": unit * count, "combo_id": f"{user.user_id}:{body.gift_name}:{time.time()}",
                "is_final": True, "comboable": False, "to_user": ""}
    elif t == "like":
        data = {"count": max(1, body.count), "total": None}
    elif t == "member":
        data = {"member_count": ctx.danmaku.last_online or 0, "enter_type": 0}
    elif t in ("follow", "share"):
        data = {"action": 1 if t == "follow" else 3, "share_type": 0 if t == "follow" else 1}
    elif t == "fansclub":
        data = {"kind": body.kind or "join", "content": f"{user.nickname} 加入了粉丝团"}
    elif t == "room_stat":
        base = ctx.danmaku.last_online or 100
        online = body.online if body.online is not None else base + (body.online_delta or 0)
        data = {"online": max(0, int(online)), "total_pv": None, "popularity": 0}
    data.update(body.data or {})
    ev = LiveEvent(type=t, platform="simulate", room_id=ctx.danmaku.status().get("room_id") or "simulate",
                   ts=time.time(), msg_id=f"sim{random.getrandbits(40):x}", user=user, data=data, raw_method="simulate")
    return ctx.danmaku.inject(ev).to_dict()


@router.post("/api/danmaku/mock/start")
async def mock_start(request: Request, body: MockBody | None = None):
    changes = {"danmaku.enabled": True, "danmaku.source": "mock"}
    if body and body.rate_per_min:
        changes["danmaku.mock.rate_per_min"] = body.rate_per_min
    return _update(get_ctx(request), changes)


@router.post("/api/danmaku/mock/stop")
async def mock_stop(request: Request):
    ctx = get_ctx(request)
    if ctx.cfg["danmaku"]["source"] == "mock":
        ctx.settings.update({"danmaku.enabled": False})
        ctx.danmaku.restart()
    return ctx.danmaku.status()


@router.post("/api/danmaku/replay")
async def replay(request: Request, body: ReplayBody):
    ctx = get_ctx(request)
    path = body.file
    if not os.path.isabs(path):                  # 只给文件名时在录制目录里找
        cand = os.path.join(ctx.cfg["server"]["data_dir"], "events", path)
        path = cand if os.path.isfile(cand) else path
    changes = {"danmaku.enabled": True, "danmaku.source": "replay", "danmaku.replay.file": path}
    if body.speed:
        changes["danmaku.replay.speed"] = body.speed
    if body.loop is not None:
        changes["danmaku.replay.loop"] = body.loop
    return _update(ctx, changes)


@router.get("/api/danmaku/recordings")
async def recordings(request: Request):
    return get_ctx(request).recorder.list_files()


@router.put("/api/danmaku/cookie")
async def put_cookie(request: Request, body: CookieBody):
    ctx = get_ctx(request)
    path = cookie_path(ctx.env)
    try:
        write_cookie(path, body.cookie)
    except ValueError as e:
        raise HTTPException(400, str(e))
    st = ctx.danmaku.status()
    if st.get("enabled") and st.get("source") == "douyin":
        ctx.danmaku.restart()                    # 用新 cookie 重连
    return cookie_status(path)


@router.delete("/api/danmaku/cookie")
async def delete_cookie(request: Request):
    path = cookie_path(get_ctx(request).env)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return cookie_status(path)
