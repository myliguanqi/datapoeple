"""控制台 WebSocket：/ws/console 推送设置变更、弹幕事件、决策日志等（见 live/feed.py）。只读。"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/console")
async def ws_console(ws: WebSocket):
    await ws.accept()
    feed = ws.app.state.ctx.feed
    q = feed.add_client()
    await ws.send_text(json.dumps({"topic": "hello", "ts": time.time(),
                                   "data": {"recent": feed.snapshot(limit=50)}}, ensure_ascii=False, default=str))

    async def sender():
        while True:
            await ws.send_text(await q.get())

    async def receiver():
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"topic": "pong", "ts": msg.get("ts")}))

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        feed.remove_client(q)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, WebSocketDisconnect, Exception):
                pass
