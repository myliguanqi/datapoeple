"""待播语音缓冲区：TTS / 外部音频写入，生成线程按块取走。

每段音频带一个 seg_id（一般一句话一段），取出时返回每个采样属于哪个 seg，
帧上会带着 seg_id 发给客户端，客户端据此显示字幕、判断是否在说话。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Segment:
    seg_id: int
    request_id: str
    text: str
    pcm: np.ndarray
    pos: int = 0

    @property
    def remaining(self) -> int:
        return self.pcm.size - self.pos


@dataclass
class Pulled:
    """一次取出的结果：pieces 是 (seg_id, 起始偏移, 长度)，未覆盖的部分由空闲音频补齐。"""
    audio: np.ndarray
    pieces: list = field(default_factory=list)

    @property
    def speech_samples(self) -> int:
        return sum(p[2] for p in self.pieces)


class SpeechBuffer:
    def __init__(self):
        self._segs: deque[Segment] = deque()
        self._lock = threading.Lock()

    def push(self, seg: Segment) -> None:
        with self._lock:
            self._segs.append(seg)

    def pull(self, n: int) -> Pulled:
        """最多取 n 个采样的语音（可能不足 n，也可能为 0）。"""
        out = np.zeros(n, dtype=np.float32)
        pieces = []
        filled = 0
        with self._lock:
            while filled < n and self._segs:
                seg = self._segs[0]
                take = min(seg.remaining, n - filled)
                out[filled:filled + take] = seg.pcm[seg.pos:seg.pos + take]
                pieces.append((seg.seg_id, filled, take))
                seg.pos += take
                filled += take
                if seg.remaining <= 0:
                    self._segs.popleft()
        return Pulled(audio=out, pieces=pieces)

    def clear(self) -> list:
        with self._lock:
            dropped = [s.seg_id for s in self._segs]
            self._segs.clear()
        return dropped

    def pending_samples(self) -> int:
        with self._lock:
            return sum(s.remaining for s in self._segs)

    def __len__(self) -> int:
        with self._lock:
            return len(self._segs)
