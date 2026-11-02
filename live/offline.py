"""离线任务：图片 + 文本（走 TTS）或音频 -> 完整 mp4。

流程：TTS/解码 -> 响度归一化 -> 按块切分 -> worker 逐块生成（低优先级，不抢直播）
     -> 帧通过管道喂给 ffmpeg，与音频合成 mp4。
优先调度到 gpu.offline_devices 指定的卡（双卡时建议：直播放 0 号卡，离线放 1 号卡）。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, fields

import numpy as np
from loguru import logger

from live.audio_utils import apply_fade, load_audio_file, normalize_rms, write_wav
from live.idle import IdleSource
from live.pool import PRIORITY_OFFLINE, WorkerPool
from live.tts.base import TTSEngine
from live.tts.text_split import split_sentences


@dataclass
class OfflineJob:
    job_id: str
    model: str
    image_path: str
    text: str | None = None
    audio_path: str | None = None
    voice: str | None = None
    seed: int = 9999
    use_face_crop: bool = False
    status: str = "queued"          # queued | tts | generating | encoding | done | failed
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    output_path: str | None = None
    duration_s: float | None = None
    worker: int | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    done_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "done_event"}
        d["video_url"] = f"/api/offline/jobs/{self.job_id}/video" if self.status == "done" else None
        return d


class OfflineManager:
    def __init__(self, cfg: dict, pool: WorkerPool, tts: TTSEngine):
        self.cfg = cfg
        self.pool = pool
        self.tts = tts
        self.jobs: dict = {}
        self.out_dir = os.path.join(cfg["server"]["data_dir"], "offline")
        os.makedirs(self.out_dir, exist_ok=True)
        self._sem = threading.Semaphore(max(1, int(cfg["offline"]["max_concurrent"])))

    def submit(self, image_path: str, text: str | None = None, audio_path: str | None = None,
               model: str | None = None, voice: str | None = None, seed: int | None = None,
               use_face_crop: bool = False) -> OfflineJob:
        if not text and not audio_path:
            raise ValueError("text 和 audio 至少提供一个")
        model = model or self.cfg["models"]["default_model"]
        if model not in ("lite", "pro"):
            raise ValueError(f"未知模型: {model}")
        job = OfflineJob(job_id=uuid.uuid4().hex[:12], model=model, image_path=image_path,
                         text=text, audio_path=audio_path, voice=voice,
                         seed=self.cfg["live"]["seed"] if seed is None else int(seed),
                         use_face_crop=use_face_crop)
        self.jobs[job.job_id] = job
        threading.Thread(target=self._run, args=(job,), name=f"job-{job.job_id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> OfflineJob | None:
        return self.jobs.get(job_id)

    def list(self) -> list:
        return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: -j.created_at)]

    # ------------------------------------------------------------------
    def _build_audio(self, job: OfflineJob) -> np.ndarray:
        sr = self.cfg["live"]["sample_rate"]
        sp = self.cfg["speech"]
        if job.audio_path:
            pcm = load_audio_file(job.audio_path, sr=sr, ffmpeg=self.cfg["server"]["ffmpeg"])
            return normalize_rms(pcm, sp["target_rms_db"], sp["max_gain_db"])
        gap = np.zeros(int(sr * sp["sentence_gap_ms"] / 1000), dtype=np.float32)
        parts = []
        sentences = split_sentences(job.text, max_chars=self.cfg["tts"]["max_sentence_chars"])
        for i, sent in enumerate(sentences):
            job.message = f"TTS {i + 1}/{len(sentences)}"
            pcm = self.tts.synthesize(sent, job.voice)
            pcm = normalize_rms(pcm, sp["target_rms_db"], sp["max_gain_db"])
            parts += [apply_fade(pcm, sr, sp["fade_ms"]), gap]
        if not parts:
            raise ValueError("文本为空")
        return np.concatenate(parts[:-1])

    def _run(self, job: OfflineJob) -> None:
        with self._sem:
            worker = None
            sid = f"job-{job.job_id}"
            ffmpeg_proc = None
            try:
                job.status = "tts"
                speech = self._build_audio(job)

                job.status, job.message = "generating", "等待显卡"
                worker = self.pool.pick("offline")
                worker.jobs.add(job.job_id)
                job.worker = worker.index
                info = worker.call("open_session", priority=PRIORITY_OFFLINE, sid=sid, model=job.model,
                                   image_path=job.image_path, seed=job.seed,
                                   use_face_crop=job.use_face_crop).result(1800)
                n = info["chunk_samples"]
                total = max(1, -(-speech.size // n))
                audio = np.concatenate([speech, IdleSource(self.cfg).read(total * n - speech.size)])
                job.duration_s = round(audio.size / info["sample_rate"], 3)

                wav_path = os.path.join(self.out_dir, f"{job.job_id}.wav")
                write_wav(wav_path, audio, info["sample_rate"])
                out_path = os.path.join(self.out_dir, f"{job.job_id}.mp4")
                log_path = os.path.join(self.out_dir, f"{job.job_id}.ffmpeg.log")
                cmd = [self.cfg["server"]["ffmpeg"], "-y", "-hide_banner", "-loglevel", "warning",
                       "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{info['width']}x{info['height']}",
                       "-r", str(info["fps"]), "-i", "pipe:0", "-i", wav_path,
                       "-map", "0:v:0", "-map", "1:a:0",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out_path]
                with open(log_path, "wb") as log_f:
                    ffmpeg_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log_f)
                    t0 = time.time()
                    for k in range(total):
                        res = worker.call("generate", priority=PRIORITY_OFFLINE, sid=sid,
                                          audio=audio[k * n:(k + 1) * n]).result(600)
                        ffmpeg_proc.stdin.write(res["frames"].tobytes())
                        job.progress = round((k + 1) / total, 4)
                        job.message = f"块 {k + 1}/{total}，{(time.time() - t0) / (k + 1):.2f}s/块"
                    job.status = "encoding"
                    ffmpeg_proc.stdin.close()
                    if ffmpeg_proc.wait() != 0:
                        raise RuntimeError(f"ffmpeg 合成失败，见 {log_path}")
                os.remove(wav_path)
                job.output_path = out_path
                job.status, job.message = "done", f"完成，用时 {time.time() - job.created_at:.1f}s"
                logger.info(f"离线任务 {job.job_id} 完成 -> {out_path}")
            except Exception as e:
                job.status, job.error = "failed", str(e)
                job.message = str(e).splitlines()[0] if str(e) else type(e).__name__
                logger.error(f"离线任务 {job.job_id} 失败: {e}")
                if ffmpeg_proc is not None and ffmpeg_proc.poll() is None:
                    ffmpeg_proc.kill()
            finally:
                if worker is not None:
                    try:
                        worker.call("close_session", priority=PRIORITY_OFFLINE, sid=sid).result(30)
                    except Exception:
                        pass
                    worker.jobs.discard(job.job_id)
                job.finished_at = time.time()
                job.done_event.set()
