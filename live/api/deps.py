"""路由公共依赖：取 AppContext、写操作鉴权。

鉴权规则（所有 /api/ 下的 POST/PUT/PATCH/DELETE，以及 WebSocket 里的控制消息）：
  - .env 设了 ADMIN_TOKEN：请求头 X-Admin-Token 必须相等（WebSocket 用 ?token=），否则 401；
  - 没设：只允许本机（127.0.0.1 / ::1）执行写操作，其他来源 403。
读接口（GET）和画面推流不受限：局域网内其他人能看画面，但不能改设置。
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, WebSocket

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def get_ctx(request: Request):
    return request.app.state.ctx


def is_loopback(host: str | None) -> bool:
    host = (host or "").strip("[]")
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.") or host.startswith("::ffff:127.")


def check_write(token_required: str | None, given: str | None, client_host: str | None) -> tuple[int, str] | None:
    """返回 None 表示放行；否则 (状态码, 原因)。"""
    if token_required:
        if given and hmac.compare_digest(given.encode(), token_required.encode()):
            return None
        return 401, "需要管理口令（请求头 X-Admin-Token）"
    if is_loopback(client_host):
        return None
    return 403, "未设置 ADMIN_TOKEN 时只允许本机执行写操作"


def require_write(request: Request) -> None:
    ctx = get_ctx(request)
    denied = check_write(ctx.admin_token, request.headers.get("x-admin-token"),
                         request.client.host if request.client else None)
    if denied:
        raise HTTPException(*denied)


def ws_can_write(ws: WebSocket) -> bool:
    ctx = ws.app.state.ctx
    return check_write(ctx.admin_token, ws.query_params.get("token") or ws.headers.get("x-admin-token"),
                       ws.client.host if ws.client else None) is None
