"""输出端（Sink）：会话的播放时钟每 1/fps 秒产出一帧（画面 + 对应 40ms 音频），分发给所有 sink。

已实现：WsHub —— WebSocket 推给浏览器/客户端（二进制帧协议见 docs/live/API.md）。
扩展：RTMP 推流、WebRTC、虚拟摄像头等，继承 FrameSink 实现 on_frame/on_event 即可，
     见 docs/live/DEVELOPMENT.md「新增输出端」。

注意：on_frame 在播放时钟线程里同步调用，必须很快返回（不要做网络 IO），
     耗时操作请自己开线程/队列。
"""
from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass

import cv2
import numpy as np

# ---- 二进制帧协议 v1（小端），头 32 字节 ----
#  0  4s  magic  b"FHL1"
#  4  B   type   1 = 音视频帧
#  5  B   flags  bit0 说话中  bit1 补帧(生成跟不上时重复上一帧)
#  6  H   reserved
#  8  I   seq    帧序号（会话内从 0 递增）
# 12  d   pts    秒 = seq / fps
# 20  I   seg_id 正在播放的语音段 id，0 = 空闲
# 24  I   jpeg_len
# 28  I   audio_len  字节数，int16 单声道 PCM，采样率见 hello 消息
# 32  ... jpeg 数据，紧接着 audio 数据
PACKET_HEADER = struct.Struct("<4sBBHIdIII")
PACKET_MAGIC = b"FHL1"
PACKET_TYPE_AV = 1
FLAG_SPEAKING = 1
FLAG_FILLER = 2


@dataclass
class AVFrame:
    seq: int
    pts: float
    image: np.ndarray      # [H, W, 3] uint8 RGB
    audio: np.ndarray      # [sample_rate/fps] int16
    seg_id: int
    filler: bool
    model: str


class FrameSink:
    def on_frame(self, frame: AVFrame) -> None:
        raise NotImplementedError

    def on_event(self, event: dict) -> None:
        pass

    def close(self) -> None:
        pass


def encode_av_packet(frame: AVFrame, jpeg: bytes) -> bytes:
    flags = (FLAG_SPEAKING if frame.seg_id else 0) | (FLAG_FILLER if frame.filler else 0)
    audio = frame.audio.astype("<i2", copy=False).tobytes()
    head = PACKET_HEADER.pack(PACKET_MAGIC, PACKET_TYPE_AV, flags, 0, frame.seq, frame.pts,
                              frame.seg_id, len(jpeg), len(audio))
    return head + jpeg + audio


class WsHub(FrameSink):
    """把帧编码成 JPEG（每帧只编码一次）后扇出给所有 WebSocket 客户端的 asyncio 队列。"""

    def __init__(self, cfg: dict, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.cfg = cfg                # jpeg_quality / client_queue_frames 每次读，Web 改了立即生效
        self.clients: set = set()     # 只在事件循环线程里增删
        self.dropped = 0

    @property
    def quality(self) -> int:
        return int(self.cfg["stream"]["jpeg_quality"])

    @property
    def max_queue(self) -> int:
        return int(self.cfg["stream"]["client_queue_frames"])

    # 以下两个在事件循环线程里调用
    def add_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.clients.add(q)
        return q

    def remove_client(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    @property
    def client_count(self) -> int:
        return len(self.clients)

    # 以下在播放时钟线程里调用
    def on_frame(self, frame: AVFrame) -> None:
        if not self.clients:
            return
        bgr = cv2.cvtColor(frame.image, cv2.COLOR_RGB2BGR)
        ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not ok:
            return
        self._post(encode_av_packet(frame, jpg.tobytes()))

    def on_event(self, event: dict) -> None:
        if self.clients:
            self._post(json.dumps(event, ensure_ascii=False))

    def close(self) -> None:
        self._post(None)   # 通知所有客户端断开

    def _post(self, data) -> None:
        try:
            self.loop.call_soon_threadsafe(self._fanout, data)
        except RuntimeError:   # 事件循环已关闭
            pass

    def _fanout(self, data) -> None:
        for q in list(self.clients):
            if q.qsize() >= self.max_queue:   # 慢客户端：丢最旧的，保证实时
                try:
                    q.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)
