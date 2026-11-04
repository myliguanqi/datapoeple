"""抖音弹幕源（只在弹幕子进程里运行）。移植自参考项目 douyin_zhibo/danmaku/danmaku_source.py。

依赖 vendor/douyin_fetcher 的接口（抖音改签名时用上游同名文件整体覆盖 vendor 目录，这里不动）：
  liveMan.DouyinLiveWebFetcher(live_id, abogus_file)   .start() 阻塞 / .stop() / .room_id / .session / .ws
  liveMan.generateSignature(wss, script_file)            模块级函数（这里换成绝对路径版本）
  _wsOnOpen / _wsOnMessage / _wsOnClose / _wsOnError / _sendHeartbeat   子类覆盖
  protobuf.douyin.PushFrame / Response                   解包

与参考项目的差异：
- vendor 用相对路径加载 sign.js / a_bogus.js，参考项目靠 os.chdir；这里传绝对路径，不改进程工作目录；
- 登录 cookie 的 WebSocketApp monkeypatch 只发生在弹幕子进程里（只对 douyin/webcast 的连接注入），不影响主进程；
- 消息解析交给 parse.py 纯函数，连击礼物经 GiftComboAggregator 聚合，按 msg_id 去重。
"""
from __future__ import annotations

import functools
import gzip
import threading
import time
from collections import OrderedDict

from loguru import logger

from live.danmaku.base import DanmakuSource
from live.danmaku.cookie import read_cookie
from live.danmaku.douyin import parse
from live.danmaku.douyin.vendor import ABOGUS_JS, SIGN_JS, ensure_path

ensure_path()
import websocket  # noqa: E402  vendor 依赖 websocket-client==1.7.0

import liveMan  # noqa: E402
from protobuf.douyin import PushFrame, Response  # noqa: E402

# sign.js 用绝对路径（vendor 默认按相对路径读，必须 chdir 才能找到）
liveMan.generateSignature = functools.partial(liveMan.generateSignature, script_file=SIGN_JS)

_COOKIE_PATCHED = False


def install_cookie_patch(cookie: str) -> None:
    """游客模式收不到礼物：在 vendor 创建 WebSocketApp 之前把完整登录 cookie 合进 WSS 请求头。
    只改 URL 含 douyin / webcast 的连接（避免冲掉其他库自己的鉴权头）。只在子进程里调用。"""
    global _COOKIE_PATCHED
    if _COOKIE_PATCHED or not cookie:
        return
    real = websocket.WebSocketApp

    def patched(*args, **kwargs):
        url = kwargs.get("url") or (args[0] if args else "")
        if isinstance(url, str) and ("douyin" in url or "webcast" in url):
            hdr = kwargs.get("header")
            if isinstance(hdr, dict):
                hdr["cookie"] = cookie
            elif isinstance(hdr, list):
                kwargs["header"] = [h for h in hdr if not h.lower().startswith("cookie")] + [f"cookie: {cookie}"]
        return real(*args, **kwargs)

    websocket.WebSocketApp = patched
    liveMan.websocket.WebSocketApp = patched
    _COOKIE_PATCHED = True


def _pb_walk(data: bytes):
    """无 schema 的 protobuf 解码：(字段号, 类型, 值) 列表；非法抛异常。探针用。"""
    out, i, n = [], 0, len(data)

    def rv(p):
        shift = val = 0
        while True:
            if p >= n:
                raise ValueError("varint 越界")
            b = data[p]
            p += 1
            val |= (b & 0x7F) << shift
            if not b & 0x80:
                return val, p
            shift += 7
            if shift > 63:
                raise ValueError("varint 过长")

    while i < n:
        tag, i = rv(i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            raise ValueError("字段号 0")
        if wt == 0:
            v, i = rv(i)
            out.append((fn, "varint", v))
        elif wt == 1:
            out.append((fn, "fixed64", data[i:i + 8]))
            i += 8
        elif wt == 2:
            ln, i = rv(i)
            if i + ln > n:
                raise ValueError("ld 越界")
            out.append((fn, "bytes", data[i:i + ln]))
            i += ln
        elif wt == 5:
            out.append((fn, "fixed32", data[i:i + 4]))
            i += 4
        else:
            raise ValueError(f"未知 wire type {wt}")
    return out


def pb_dump(data: bytes, depth: int = 0, max_depth: int = 5) -> str:
    pad = "  " * depth
    try:
        fields = _pb_walk(data)
    except Exception:
        return f"{pad}<不是合法 protobuf，hex={data[:24].hex()}…>"
    lines = []
    for fn, kind, val in fields:
        if kind == "varint":
            lines.append(f"{pad}#{fn} int = {val}")
        elif kind != "bytes":
            lines.append(f"{pad}#{fn} {kind} = {val.hex()}")
        else:
            s = None
            try:
                cand = val.decode("utf-8")
                if cand and all(ch.isprintable() or ch in "\n\r\t" for ch in cand):
                    s = cand
            except UnicodeDecodeError:
                pass
            if s is not None:
                lines.append(f"{pad}#{fn} str = {s!r}")
            elif depth < max_depth and val:
                try:
                    _pb_walk(val)
                    lines.append(f"{pad}#{fn} message:")
                    lines.append(pb_dump(val, depth + 1, max_depth))
                except Exception:
                    lines.append(f"{pad}#{fn} bytes[{len(val)}] = {val[:24].hex()}…")
            else:
                lines.append(f"{pad}#{fn} bytes[{len(val)}] = {val[:24].hex()}…")
    return "\n".join(lines)


class EventDouyinFetcher(liveMan.DouyinLiveWebFetcher):
    _DUMP_LIMIT = 2
    _GIFT_TRAP = ["小心心", "仙女棒", "玫瑰", "棒棒糖", "比心", "为你打call", "甜筒", "能量"]
    _TRAP_LIMIT = 8

    def __init__(self, live_id: str, source: "DouyinSource", cookie: str = ""):
        super().__init__(live_id, abogus_file=ABOGUS_JS)
        self.src = source
        self.ws = None
        self.sniff = False
        self._seen_methods: set = set()
        self._dump_count: dict = {}
        self._trap = [k.encode("utf-8") for k in self._GIFT_TRAP]
        self._trap_hits = 0
        if cookie:
            self.session.headers["Cookie"] = cookie          # HTTP 侧也带登录态

    # ---- 连接生命周期（覆盖父类的 print）----
    def _wsOnOpen(self, ws):
        logger.info(f"抖音 WSS 已连接 room_id={self.room_id}")
        self.src.status({"state": "connected", "room_id": str(self.room_id or ""), "error": None})
        threading.Thread(target=self._sendHeartbeat, name="douyin-hb", daemon=True).start()

    def _wsOnError(self, ws, error):
        logger.warning(f"抖音 WSS 错误: {error}")
        self.src.status({"error": str(error)[:300]})

    def _wsOnClose(self, ws, *args):
        # 父类这里会调 get_room_status（要 Node + a_bogus），断线时没必要；由 DanmakuManager 退避重连
        logger.info(f"抖音 WSS 已关闭 {args[:2]}")
        self.src.status({"state": "disconnected"})

    def _sendHeartbeat(self):
        while not self.src.stopped:
            try:
                self.ws.send(PushFrame(payload_type="hb").SerializeToString(), websocket.ABNF.OPCODE_PING)
            except Exception as e:
                logger.warning(f"心跳发送失败，停止心跳: {e}")
                return
            time.sleep(5)

    def stop(self):
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass

    # ---- 消息分发：父类只解 gzip 且 except: pass 吞掉一切，这里压缩/未压缩都能解、异常打 warning ----
    def _wsOnMessage(self, ws, message):
        try:
            package = PushFrame().parse(message)
            payload = package.payload
            try:
                payload = gzip.decompress(payload)
            except (OSError, EOFError):
                pass                                    # 登录态下有些帧没压缩
            response = Response().parse(payload)
        except Exception as e:
            logger.debug(f"WS 解包跳过: {e}")
            return
        if response.need_ack:
            try:
                ack = PushFrame(log_id=package.log_id, payload_type="ack",
                                payload=response.internal_ext.encode("utf-8")).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                logger.debug(f"ack 回发失败: {e}")
        for msg in response.messages_list:
            self._dispatch(msg.method, msg.payload, msg.msg_id)

    def _dispatch(self, method: str, payload: bytes, msg_id: int = 0) -> None:
        if method not in self._seen_methods:
            self._seen_methods.add(method)
            if not parse.is_known(method):
                logger.info(f"[method] 首次出现未适配的消息类型 {method}（打开 danmaku.sniff 可看裸解内容）")
        if self.sniff:
            self._sniff(method, payload)
        try:
            events = parse.parse_message(method, payload, gender_map=self.src.gender_map, msg_id=msg_id,
                                         room_id=str(self.room_id or ""))
        except Exception as e:
            logger.warning(f"[parse] {method} 解析异常: {e!r}")
            return
        for ev in events:
            self.src.on_parsed(ev)

    def _sniff(self, method: str, payload: bytes) -> None:
        if self._trap_hits < self._TRAP_LIMIT and any(b in payload for b in self._trap):
            self._trap_hits += 1
            logger.info(f"[gift-trap] 命中 method={method}\n{pb_dump(payload)}")
        if not parse.is_known(method):
            c = self._dump_count.get(method, 0)
            if c < self._DUMP_LIMIT:
                self._dump_count[method] = c + 1
                logger.info(f"[dump] {method} #{c + 1}\n{pb_dump(payload)}")


class DouyinSource(DanmakuSource):
    name = "douyin"

    def __init__(self, opts, emit, status):
        super().__init__(opts, emit, status)
        self.live_id = str(opts.get("live_id") or "").strip()
        self.gender_map = parse.normalize_gender_map(opts.get("gender_map"))
        self.agg = parse.GiftComboAggregator(timeout_s=float(opts.get("combo_timeout_s", 3.0)))
        self._recent_ids: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.cookie = read_cookie(opts.get("cookie_file") or "")
        self.fetcher: EventDouyinFetcher | None = None
        self._sniff = bool(opts.get("sniff"))

    def run(self) -> None:
        if not self.live_id:
            self.status({"state": "error", "error": "没有设置直播间号（danmaku.live_id）"})
            return
        install_cookie_patch(self.cookie)
        self.status({"state": "connecting", "live_id": self.live_id, "logged_in": bool(self.cookie)})
        if self.cookie:
            logger.info(f"已加载登录 cookie（{len(self.cookie)} 字符），以登录态连接 → 可收礼物")
        else:
            logger.warning("未配置登录 cookie，游客模式连接 → 收不到礼物")
        self.fetcher = EventDouyinFetcher(self.live_id, self, cookie=self.cookie)
        self.fetcher.sniff = self._sniff
        if self.stopped:
            return
        try:
            if not self.fetcher.room_id:
                self.status({"state": "error", "error": "获取 room_id 失败：直播间号不对或当前没在直播"})
                return
            self.status({"room_id": str(self.fetcher.room_id)})
            self.fetcher._connectWebSocket()                 # 阻塞直到断开
        except Exception as e:
            self.status({"state": "error", "error": f"连接失败: {e}"[:300]})
            logger.error(f"抖音连接失败: {e}")

    def stop(self) -> None:
        super().stop()
        if self.fetcher is not None:
            self.fetcher.stop()

    def set_sniff(self, on: bool) -> None:
        self._sniff = bool(on)
        if self.fetcher is not None:
            self.fetcher.sniff = self._sniff
        logger.info(f"协议探针 {'打开' if on else '关闭'}")

    def on_parsed(self, ev) -> None:
        if ev.msg_id:
            with self._lock:
                if ev.msg_id in self._recent_ids:
                    return                                   # 同一条消息重推
                self._recent_ids[ev.msg_id] = 1
                while len(self._recent_ids) > 4000:
                    self._recent_ids.popitem(last=False)
        with self._lock:
            out = self.agg.feed(ev)
        for e in out:
            self.emit(e)

    def tick(self) -> None:
        with self._lock:
            out = self.agg.flush()
        for e in out:
            self.emit(e)
