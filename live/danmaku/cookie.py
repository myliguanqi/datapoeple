"""抖音登录 cookie：游客模式收不到礼物（WebcastGiftMessage 只推给登录态连接），需要带登录 cookie。

cookie 等同账号密码：只存本地 secrets/（已 gitignore），Web 上只写不读，日志里只打长度。
支持三种粘贴格式（移植自参考项目 _extract_cookie）：整条 curl、浏览器请求头、纯 k=v; k=v 串。
"""
from __future__ import annotations

import os
import re
import time

from live.config import abs_path

DEFAULT_COOKIE_FILE = "secrets/douyin_cookie.txt"


def extract_cookie(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"(?:-b|--cookie)\s+'([^']*)'", s) or re.search(r'(?:-b|--cookie)\s+"([^"]*)"', s)
    if m:
        return m.group(1).strip()
    m = re.search(r'(?im)^\s*-H\s+[\'"]cookie:\s*(.+?)[\'"]\s*\\?\s*$', s)   # curl -H 'cookie: ...'
    if m:
        return m.group(1).strip()
    m = re.search(r'(?im)^\s*cookie:\s*(.+?)\s*$', s)
    if m:
        return m.group(1).strip()
    return " ".join(s.split("\n")).strip()


def cookie_path(env) -> str:
    return abs_path(env.get("DOUYIN_COOKIE_FILE") or DEFAULT_COOKIE_FILE)


def read_cookie(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return extract_cookie(f.read())
    except FileNotFoundError:
        return ""


def write_cookie(path: str, raw: str) -> str:
    cookie = extract_cookie(raw)
    if not cookie or "=" not in cookie:
        raise ValueError("没有识别出 cookie（支持整条 curl、请求头 cookie: 行、或 k=v; k=v 串）")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(cookie)
    os.replace(tmp, path)
    return cookie


def cookie_status(path: str) -> dict:
    """只返回是否已配置、长度、更新时间，不返回内容。"""
    c = read_cookie(path)
    if not c:
        return {"configured": False}
    mtime = os.path.getmtime(path)
    return {"configured": True, "length": len(c), "updated_at": mtime,
            "age_days": round((time.time() - mtime) / 86400, 1),
            "has_sessionid": "sessionid" in c}
