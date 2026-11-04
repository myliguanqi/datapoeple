"""直播会话：一路永不停止的数字人音视频流。

三个线程：
  TTS 线程   文本 -> 切句 -> TTS -> 响度归一化 -> 写入 SpeechBuffer（一句一段，带 seg_id）
  生成线程   "按需提前"调度：缓冲的帧快播完时，从 SpeechBuffer 取一块语音（不够就用空闲底噪补齐），
             交给 worker 生成一块帧；帧和这块音频一一对应放进播放队列。
             切模型 / 换形象等控制命令也在这个线程里、两块之间执行，保证与生成串行。
  播放线程   严格按 fps 节拍，每次取一帧 + 对应的 sample_rate/fps 个采样，分发给所有 sink。
             队列空了（显卡跟不上）就重复上一帧补位，保证输出不断流。

延迟（从文本进来到开口）≈ TTS 首句耗时 + 一块生成耗时 + 提前量 + 客户端缓冲。
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass

import numpy as np
from loguru import logger

from live.audio_utils import apply_fade, float_to_int16, normalize_rms
from live.idle import IdleSource
from live.pool import PRIORITY_LIVE, WorkerError, WorkerHandle, WorkerUnavailable
from live.sinks import AVFrame, FrameSink
from live.speech_buffer import Segment, SpeechBuffer
from live.tts.base import TTSEngine
from live.tts.text_split import split_sentences


@dataclass
class SpeakRequest:
    request_id: str
    text: str
    voice: str | None
    epoch: int
    speed: float | None = None          # 语速比；None = 设置里的 tts.speed
    emotion: str | None = None          # 中间情绪标签（引擎支持才用）


class LiveSession:
    def __init__(self, sid: str, cfg: dict, worker: WorkerHandle, tts: TTSEngine, model: str,
                 avatar: str, seed: int, use_face_crop: bool):
        self.sid = sid
        self.cfg = cfg
        self.worker = worker
        self.tts = tts
        self.model = model
        self.avatar = avatar
        self.seed = seed
        self.use_face_crop = use_face_crop
        self.fps = cfg["live"]["fps"]
        self.sr = cfg["live"]["sample_rate"]
        self.spf = self.sr // self.fps             # 每帧对应的采样数（16000/25=640）
        self.speech = SpeechBuffer()
        self.idle = IdleSource(cfg)
        self.chunk: dict = {}                      # worker 返回的块信息（随模型变化）
        self.state = "init"                        # init | starting | running | error | closed
        self.error = None
        self.created_at = time.time()

        self._sinks: list = []
        self._sinks_lock = threading.Lock()
        self._frames: deque = deque()              # (image, audio_i16, seg_id)
        self._frames_cv = threading.Condition()
        self._ctrl: queue.Queue = queue.Queue()
        self._tts_q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list = []
        self._epoch = 0
        self._seg_ids = itertools.count(1)
        self._seg_meta: dict = {}                  # seg_id -> (request_id, text)
        self._req_last_seg: dict = {}              # request_id -> 最后一段 seg_id
        self._playing_seg = 0
        self._gen_ema = None
        self.stats = {"chunks": 0, "frames_out": 0, "fillers": 0, "gen_errors": 0,
                      "last_gen_s": 0.0, "speech_chunks": 0}

    # ================================================================== 生命周期
    def start(self, timeout: float = 600) -> None:
        self.state = "starting"
        self.worker.live_sessions.add(self.sid)
        try:
            self.chunk = self.worker.call(
                "open_session", priority=PRIORITY_LIVE, sid=self.sid, model=self.model,
                image_path=self.avatar, seed=self.seed, use_face_crop=self.use_face_crop,
            ).result(timeout)
        except Exception as e:
            self.worker.live_sessions.discard(self.sid)
            self.state, self.error = "error", str(e)
            raise
        for name, fn in (("gen", self._gen_loop), ("play", self._play_loop), ("tts", self._tts_loop)):
            t = threading.Thread(target=fn, name=f"{self.sid}-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self.state = "running"
        logger.info(f"[{self.sid}] 会话启动 worker{self.worker.index}/gpu{self.worker.gpu_id} "
                    f"model={self.model} chunk={self.chunk['chunk_frames']}帧")

    def close(self) -> None:
        if self.state == "closed":
            return
        self._stop.set()
        with self._frames_cv:
            self._frames_cv.notify_all()
        for t in self._threads:
            t.join(timeout=5)
        try:
            self.worker.call("close_session", priority=PRIORITY_LIVE, sid=self.sid).result(10)
        except Exception:
            pass
        self.worker.live_sessions.discard(self.sid)
        self.state = "closed"
        self._emit({"type": "closed"})
        with self._sinks_lock:
            for s in self._sinks:
                s.close()
        logger.info(f"[{self.sid}] 会话关闭")

    # ================================================================== sink
    def add_sink(self, sink: FrameSink) -> None:
        with self._sinks_lock:
            self._sinks.append(sink)

    def remove_sink(self, sink: FrameSink) -> None:
        with self._sinks_lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    def _emit(self, event: dict) -> None:
        event.setdefault("session_id", self.sid)
        event.setdefault("ts", time.time())
        with self._sinks_lock:
            sinks = list(self._sinks)
        for s in sinks:
            try:
                s.on_event(event)
            except Exception:
                logger.exception("sink.on_event 失败")

    # ================================================================== 对外接口（线程安全）
    def speak(self, text: str, voice: str | None = None, interrupt: bool = False,
              request_id: str | None = None, speed: float | None = None, emotion: str | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("text 为空")
        if interrupt:
            self.interrupt()
        req = SpeakRequest(request_id or uuid.uuid4().hex[:10], text, voice, self._epoch, speed, emotion)
        self._tts_q.put(req)
        self._emit({"type": "speak_queued", "request_id": req.request_id, "text": text})
        return {"request_id": req.request_id, "queued": self._tts_q.qsize()}

    def push_audio(self, pcm: np.ndarray, text: str = "", interrupt: bool = False,
                   request_id: str | None = None) -> dict:
        """直接推一段音频（外部 TTS / 录音），pcm 为 float32 单声道 sample_rate。"""
        if interrupt:
            self.interrupt()
        request_id = request_id or uuid.uuid4().hex[:10]
        seg_id = self._push_segment(request_id, text, self._postprocess(pcm))
        self._req_last_seg[request_id] = seg_id
        return {"request_id": request_id, "seg_id": seg_id, "seconds": round(pcm.size / self.sr, 3)}

    def interrupt(self) -> dict:
        """打断：丢弃未合成的文本和未生成的语音。已生成的帧默认继续播完（最多约 1 个块）。"""
        self._epoch += 1
        while True:
            try:
                self._tts_q.get_nowait()
            except queue.Empty:
                break
        dropped = self.speech.clear()
        dropped_frames = 0
        if self.cfg["live"]["interrupt_drop_queued"]:
            with self._frames_cv:
                dropped_frames = len(self._frames)
                self._frames.clear()
        self._emit({"type": "interrupted", "dropped_segments": dropped, "dropped_frames": dropped_frames})
        return {"dropped_segments": dropped, "dropped_frames": dropped_frames}

    def switch_model(self, model: str) -> Future:
        return self._control("switch_model", model=model)

    def set_avatar(self, image_path: str, use_face_crop: bool = False) -> Future:
        return self._control("set_avatar", image_path=image_path, use_face_crop=use_face_crop)

    def _control(self, name: str, **kwargs) -> Future:
        fut: Future = Future()
        self._ctrl.put((name, kwargs, fut))
        with self._frames_cv:
            self._frames_cv.notify_all()
        return fut

    def status(self) -> dict:
        chunk_s = self.chunk.get("chunk_seconds") or 1.0
        rtf = (self._gen_ema / chunk_s) if self._gen_ema else None
        seg = self._playing_seg
        return {
            "session_id": self.sid,
            "state": self.state,
            "error": self.error,
            "model": self.model,
            "avatar": self.avatar,
            "worker": self.worker.index,
            "gpu_id": self.worker.gpu_id,
            "chunk": self.chunk,
            "speaking": bool(seg),
            "playing_text": self._seg_meta.get(seg, (None, None))[1] if seg else None,
            "buffered_s": round(self._buffered_seconds(), 3),
            "speech_pending_s": round(self.speech.pending_samples() / self.sr, 3),
            "tts_queue": self._tts_q.qsize(),
            "gen_time_ema_s": round(self._gen_ema, 4) if self._gen_ema else None,
            "rtf": round(rtf, 3) if rtf else None,       # 生成耗时/块时长，<1 才能实时
            "realtime": (rtf < 1.0) if rtf else None,
            "uptime_s": round(time.time() - self.created_at, 1),
            **self.stats,
        }

    # ================================================================== TTS 线程
    def _postprocess(self, pcm: np.ndarray) -> np.ndarray:
        sp = self.cfg["speech"]
        pcm = np.asarray(pcm, dtype=np.float32)
        pcm = normalize_rms(pcm, sp["target_rms_db"], sp["max_gain_db"])
        return apply_fade(pcm, self.sr, sp["fade_ms"])

    def _push_segment(self, request_id: str, text: str, pcm: np.ndarray) -> int:
        seg_id = next(self._seg_ids)
        self._seg_meta[seg_id] = (request_id, text)
        if len(self._seg_meta) > 2000:   # 防止长时间直播无限增长
            for k in list(self._seg_meta)[:1000]:
                self._seg_meta.pop(k, None)
        self.speech.push(Segment(seg_id=seg_id, request_id=request_id, text=text, pcm=pcm))
        self._emit({"type": "segment", "seg_id": seg_id, "request_id": request_id, "text": text,
                    "seconds": round(pcm.size / self.sr, 3)})
        return seg_id

    def reload_idle(self) -> None:
        """idle.* 热更新：重建空闲音频源；新配置不可用（如文件读不了）时保留旧的并报错。"""
        try:
            self.idle = IdleSource(self.cfg)
            logger.info(f"[{self.sid}] 空闲音频已切换为 {self.idle.mode}")
        except Exception as e:
            logger.error(f"[{self.sid}] 空闲音频配置不可用，保留原设置: {e}")
            self._emit({"type": "error", "where": "idle", "message": f"空闲音频配置不可用: {e}"})

    def _tts_loop(self) -> None:
        while not self._stop.is_set():
            try:
                req: SpeakRequest = self._tts_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if req.epoch != self._epoch:
                continue
            # 每个请求重新读配置：Web 改句间停顿 / 单句字数后下一句就生效
            gap = np.zeros(int(self.sr * self.cfg["speech"]["sentence_gap_ms"] / 1000), dtype=np.float32)
            sentences = split_sentences(req.text, max_chars=self.cfg["tts"]["max_sentence_chars"])
            last_seg = None
            for i, sent in enumerate(sentences):
                if req.epoch != self._epoch or self._stop.is_set():
                    break
                t0 = time.time()
                try:
                    pcm = self.tts.synthesize(sent, req.voice, speed=req.speed, emotion=req.emotion)
                except Exception as e:
                    logger.error(f"[{self.sid}] TTS 失败: {e}")
                    self._emit({"type": "error", "where": "tts", "request_id": req.request_id,
                                "message": f"TTS 失败: {e}"})
                    continue
                if req.epoch != self._epoch or pcm.size == 0:
                    continue
                pcm = self._postprocess(pcm)
                if i < len(sentences) - 1:
                    pcm = np.concatenate([pcm, gap])
                logger.debug(f"[{self.sid}] TTS {time.time() - t0:.2f}s -> {pcm.size / self.sr:.2f}s: {sent}")
                last_seg = self._push_segment(req.request_id, sent, pcm)
            if last_seg is not None and req.epoch == self._epoch:
                self._req_last_seg[req.request_id] = last_seg

    # ================================================================== 生成线程
    def _buffered_seconds(self) -> float:
        return len(self._frames) / self.fps

    def _run_controls(self) -> None:
        while True:
            try:
                name, kwargs, fut = self._ctrl.get_nowait()
            except queue.Empty:
                return
            try:
                if name == "switch_model":
                    model = kwargs["model"]
                    if model != self.model:
                        self._emit({"type": "model_switching", "from": self.model, "to": model})
                        info = self.worker.call("switch_model", priority=PRIORITY_LIVE, sid=self.sid,
                                                model=model).result(self.cfg["gpu"]["worker_start_timeout"])
                        self.chunk, self.model, self._gen_ema = info, model, None
                        self._emit({"type": "model", "model": model, "chunk": info})
                    fut.set_result(self.chunk)
                elif name == "set_avatar":
                    info = self.worker.call("set_avatar", priority=PRIORITY_LIVE, sid=self.sid,
                                            **kwargs).result(120)
                    self.avatar, self.use_face_crop = kwargs["image_path"], kwargs["use_face_crop"]
                    self._emit({"type": "avatar", "avatar": self.avatar})
                    fut.set_result(info)
                else:
                    fut.set_exception(ValueError(f"未知控制命令 {name}"))
            except Exception as e:
                logger.error(f"[{self.sid}] 控制命令 {name} 失败: {e}")
                self._emit({"type": "error", "where": name, "message": str(e).splitlines()[0]})
                fut.set_exception(e)

    def _wait_for_slot(self) -> bool:
        """按需提前：等到缓冲帧快播完再生成下一块，让语音尽量晚地确定、降低延迟。"""
        lc = self.cfg["live"]
        while not self._stop.is_set():
            if not self._ctrl.empty():
                return True
            chunk_s = self.chunk["chunk_seconds"]
            lead = (self._gen_ema or chunk_s) * lc["safety_factor"] + lc["margin_s"]
            buffered = self._buffered_seconds()
            if buffered <= lead and buffered < lc["max_ahead_chunks"] * chunk_s:
                return True
            with self._frames_cv:
                self._frames_cv.wait(timeout=min(0.02, max(0.002, buffered - lead)))
        return False

    def _gen_loop(self) -> None:
        timeout = self.cfg["live"]["generate_timeout"]
        warned_slow = False
        while not self._stop.is_set():
            self._run_controls()
            if not self._wait_for_slot():
                break
            if not self._ctrl.empty():
                continue
            n = self.chunk["chunk_samples"]
            pulled = self.speech.pull(n)
            filled = pulled.speech_samples           # 语音总是从块开头连续填充
            audio = pulled.audio
            if filled < n:
                audio[filled:] = self.idle.read(n - filled)
            t0 = time.perf_counter()
            try:
                res = self.worker.call("generate", priority=PRIORITY_LIVE, sid=self.sid,
                                       audio=audio).result(timeout)
            except WorkerUnavailable as e:
                self.state, self.error = "error", str(e)
                self._emit({"type": "error", "where": "generate", "message": str(e), "fatal": True})
                logger.error(f"[{self.sid}] worker 不可用，会话停止: {e}")
                return
            except (WorkerError, Exception) as e:
                self.stats["gen_errors"] += 1
                self._emit({"type": "error", "where": "generate", "message": str(e).splitlines()[0]})
                logger.error(f"[{self.sid}] 生成失败: {e}")
                time.sleep(0.5)
                continue
            wall = time.perf_counter() - t0          # 包含进程间传输
            self._gen_ema = wall if self._gen_ema is None else 0.8 * self._gen_ema + 0.2 * wall
            self.stats["chunks"] += 1
            self.stats["last_gen_s"] = round(wall, 4)
            if filled:
                self.stats["speech_chunks"] += 1
            if self._gen_ema > self.chunk["chunk_seconds"] and not warned_slow and self.stats["chunks"] > 3:
                warned_slow = True
                self._emit({"type": "warning", "message":
                            f"{self.model} 生成速度跟不上实时（{self._gen_ema:.2f}s/块 > "
                            f"{self.chunk['chunk_seconds']:.2f}s），画面会卡顿；可改用 lite 或减少 sample_steps"})

            frames = res["frames"]
            seg_of_frame = np.zeros(len(frames), dtype=np.int64)
            for seg_id, off, length in pulled.pieces:
                a, b = off // self.spf, -(-(off + length) // self.spf)
                seg_of_frame[a:b] = seg_id
            pcm16 = float_to_int16(audio)
            with self._frames_cv:
                for i in range(len(frames)):
                    self._frames.append((frames[i], pcm16[i * self.spf:(i + 1) * self.spf], int(seg_of_frame[i])))
                self._frames_cv.notify_all()

    # ================================================================== 播放线程
    def _play_loop(self) -> None:
        period = 1.0 / self.fps
        with self._frames_cv:
            while not self._frames and not self._stop.is_set():
                self._frames_cv.wait(0.1)
        seq = 0
        last_image = None
        silence = np.zeros(self.spf, dtype=np.int16)
        t_next = time.perf_counter()
        while not self._stop.is_set():
            delay = t_next - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            with self._frames_cv:
                item = self._frames.popleft() if self._frames else None
                self._frames_cv.notify_all()
            if item is None:
                image, audio, seg_id, filler = last_image, silence, 0, True
                self.stats["fillers"] += 1
            else:
                image, audio, seg_id = item
                last_image, filler = image, False
            self._track_playback(seg_id)
            frame = AVFrame(seq=seq, pts=seq / self.fps, image=image, audio=audio, seg_id=seg_id,
                            filler=filler, model=self.model)
            with self._sinks_lock:
                sinks = list(self._sinks)
            for s in sinks:
                try:
                    s.on_frame(frame)
                except Exception:
                    logger.exception("sink.on_frame 失败")
            seq += 1
            self.stats["frames_out"] = seq
            t_next += period
            if time.perf_counter() - t_next > 1.0:   # 系统卡顿超过 1 秒：重新对齐节拍，不追帧
                t_next = time.perf_counter()

    def _track_playback(self, seg_id: int) -> None:
        prev = self._playing_seg
        if seg_id == prev:
            return
        self._playing_seg = seg_id
        if prev:
            req_id, text = self._seg_meta.get(prev, (None, None))
            self._emit({"type": "playback", "state": "end", "seg_id": prev, "request_id": req_id})
            if req_id and self._req_last_seg.get(req_id) == prev:
                self._req_last_seg.pop(req_id, None)
                self._emit({"type": "speak_done", "request_id": req_id})
        if seg_id:
            req_id, text = self._seg_meta.get(seg_id, (None, None))
            self._emit({"type": "playback", "state": "start", "seg_id": seg_id, "request_id": req_id,
                        "text": text})
