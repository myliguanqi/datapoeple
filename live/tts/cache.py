"""TTS 合成缓存（照抄参考项目 expression/tts_cache.py 的思路）：同一句话（同引擎、同音色、同语速档）只合成一次。

直播里模板兜底、讲解稿、欢迎话术高频重复（参考项目实测一场约一半是逐字重复），缓存命中即省一次计费请求。
- 两级：进程内 LRU（tts.cache.mem_max_entries）+ 磁盘 16k WAV（live_data/cache/tts/，tts.cache.persist，跨重启复用）；
- 键 = sha1(引擎签名 | 音色 | 语速档 | [情绪] | 文本)：换引擎、换音色、换火山资源 ID / 采样率都会自动失效；
  语速按 tts.cache.speed_round 取整（1.05 / 1.06 不会拆成两份）；引擎不吃情绪参数时情绪不进键；
- 合成失败（抛异常 / 空音频）不缓存，下次还会重试。
"""
from __future__ import annotations

import hashlib
import os
import threading
import wave
from collections import OrderedDict

import numpy as np
from loguru import logger

from live.audio_utils import float_to_int16


class TTSCache:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dir = os.path.join(cfg["server"]["data_dir"], "cache", "tts")
        self._mem: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.miss = 0

    @property
    def opts(self) -> dict:
        return self.cfg["tts"]["cache"]

    @property
    def enabled(self) -> bool:
        return bool(self.opts["enabled"])

    def _bucket(self, speed: float) -> str:
        step = float(self.opts["speed_round"] or 0)
        if step <= 0:
            return "*"
        return f"{round(float(speed) / step) * step:.3f}"

    def key(self, engine, text: str, voice: str | None, speed: float, emotion: str | None = None) -> str:
        parts = [engine.cache_signature, voice or engine.default_voice or "", self._bucket(speed)]
        if getattr(engine, "uses_emotion", False):
            parts.append(emotion or "")
        parts.append(text)
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.wav")

    def get(self, key: str) -> np.ndarray | None:
        with self._lock:
            pcm = self._mem.get(key)
            if pcm is not None:
                self._mem.move_to_end(key)
                self.hits += 1
                return pcm
        if self.opts["persist"]:
            path = self._path(key)
            if os.path.isfile(path):
                try:
                    with wave.open(path, "rb") as w:
                        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
                    if pcm.size:
                        self._put_mem(key, pcm)
                        with self._lock:
                            self.hits += 1
                        return pcm
                except (OSError, wave.Error, EOFError) as e:
                    logger.debug(f"TTS 磁盘缓存读取失败（{e}），重新合成")
        with self._lock:
            self.miss += 1
        return None

    def _put_mem(self, key: str, pcm: np.ndarray) -> None:
        with self._lock:
            self._mem[key] = pcm
            self._mem.move_to_end(key)
            while len(self._mem) > int(self.opts["mem_max_entries"]):
                self._mem.popitem(last=False)

    def put(self, key: str, pcm: np.ndarray, sr: int) -> None:
        if pcm is None or pcm.size == 0:
            return
        self._put_mem(key, pcm)
        if self.opts["persist"]:
            try:
                os.makedirs(self.dir, exist_ok=True)
                path = self._path(key)
                tmp = path + ".tmp"
                with wave.open(tmp, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes(float_to_int16(pcm).tobytes())
                os.replace(tmp, path)                 # 原子落盘，防半截文件
            except OSError as e:
                logger.debug(f"TTS 磁盘缓存写入失败: {e}")

    def stats(self) -> dict:
        total = self.hits + self.miss
        return {"enabled": self.enabled, "hits": self.hits, "miss": self.miss,
                "rate": round(self.hits / total, 3) if total else 0.0, "mem_entries": len(self._mem)}
