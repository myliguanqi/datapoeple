"""真实 FlashHead 推理后端（lite / pro）。

设计要点：
- 直接用官方 FlashHeadPipeline，不改官方代码；不经过 flash_head/inference.py
  （那里用模块级全局变量保存参数，lite/pro 同进程共存时会互相覆盖）。
- 官方 pipeline 把"模型权重"和"会话状态"（参考图 latent、运动帧、随机数发生器）放在同一个对象上。
  为了让一个模型同时服务多个会话（例如直播 + 离线任务），这里在切换会话时
  保存/恢复 STATE_KEYS 里的属性。
- lite/pro 切换时，把旧模型最后输出的几帧像素用新模型的 VAE 重新编码成运动帧，
  动作能接上，不会跳回参考图姿态。
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import time
from dataclasses import dataclass, field

import numpy as np
from loguru import logger

from live.worker import compat
from live.worker.runner_base import BaseRunner

# FlashHeadPipeline 上与会话相关的属性（prepare_params / reset_person_name / generate 会写）
STATE_KEYS = (
    "cond_image_dict", "frame_num", "motion_frames_num", "color_correction_strength",
    "target_h", "target_w", "lat_h", "lat_w", "generator", "timesteps",
    "cond_image_tensor_dict", "ref_img_latent_dict", "person_name",
    "original_color_reference", "ref_img_latent", "latent_motion_frames",
)
KEEP_TAIL_FRAMES = 16   # 保留最后多少帧像素，用于模型切换时重建运动帧（需 >= 两个模型的 motion_frames）


@dataclass
class _Session:
    sid: str
    model: str
    image_path: str
    seed: int
    use_face_crop: bool
    audio_win: np.ndarray
    pipe_state: dict = field(default_factory=dict)
    tail: object = None   # torch.Tensor [3, K, H, W]，上一块输出的最后 K 帧


class FlashHeadRunner(BaseRunner):
    backend = "flashhead"

    def __init__(self, cfg: dict, gpu_id: int):
        super().__init__(cfg, gpu_id)
        self.stubbed_xfuser = compat.install_xfuser_stub()
        import torch
        self.torch = torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用：请检查显卡驱动和 PyTorch CUDA 版本，或用 mock 后端调试")
        torch.cuda.set_device(0)   # 进程启动时已用 CUDA_VISIBLE_DEVICES 限定到一张卡
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        self.compile = compat.resolve_compile(cfg["models"]["compile"])
        self.quiet = cfg["models"].get("quiet_pipeline_logs", True)
        self.pipes: dict = {}     # model -> FlashHeadPipeline
        self.active: dict = {}    # model -> 当前状态装载在 pipeline 上的 sid

    # ------------------------------------------------------------------ 模型
    def info(self) -> dict:
        torch = self.torch
        free, total = torch.cuda.mem_get_info()
        return {
            "backend": self.backend,
            "gpu_id": self.gpu_id,
            "gpu_name": torch.cuda.get_device_name(0),
            "mem_free_gb": round(free / 2**30, 2),
            "mem_total_gb": round(total / 2**30, 2),
            "models": {m: self.model_info[m] for m in self.pipes},
            "sessions": list(self.sessions),
            "compile": self.compile,
            "attention": compat.attention_backend(),
            "xfuser_stub": self.stubbed_xfuser,
            "torch": torch.__version__,
        }

    def load_model(self, model: str) -> dict:
        if model in self.pipes:
            return self.model_info[model]
        if model not in ("lite", "pro"):
            raise ValueError(f"未知模型: {model}")
        mc = self.cfg["models"]
        sub = {"lite": ("Model_Lite", "VAE_LTX"), "pro": ("Model_Pro", "VAE_Wan")}[model]
        for d in sub:
            p = os.path.join(mc["ckpt_dir"], d)
            if not os.path.exists(p):
                raise FileNotFoundError(f"缺少模型目录 {p}，请先按 README_LIVE.md 下载 SoulX-FlashHead-1_3B")
        if not os.path.exists(mc["wav2vec_dir"]):
            raise FileNotFoundError(f"缺少 wav2vec 目录 {mc['wav2vec_dir']}")

        from flash_head.src.pipeline import flash_head_pipeline as fhp
        fhp.COMPILE_MODEL = self.compile
        fhp.COMPILE_VAE = self.compile
        fhp.USE_PARALLEL_VAE = False

        t0 = time.time()
        logger.info(f"加载 {model} 模型 (compile={self.compile}) ...")
        pipe = fhp.FlashHeadPipeline(
            checkpoint_dir=mc["ckpt_dir"], model_type=model, wav2vec_dir=mc["wav2vec_dir"],
            device="cuda", use_usp=False,
        )
        motion = (mc["motion_frames_latent_num"] - 1) * pipe.config.vae_stride[0] + 1
        self.pipes[model] = pipe
        self.active[model] = None
        self.model_info[model] = self.make_chunk_info(model, motion)
        logger.info(f"{model} 加载完成 {time.time() - t0:.1f}s, chunk={self.model_info[model]}")
        if mc.get("warmup", True):
            self._warmup(model)
        return self.model_info[model]

    def unload_model(self, model: str) -> None:
        busy = [s.sid for s in self.sessions.values() if s.model == model]
        if busy:
            raise RuntimeError(f"模型 {model} 正在被会话使用: {busy}")
        self.pipes.pop(model, None)
        self.active.pop(model, None)
        self.model_info.pop(model, None)
        import gc
        gc.collect()
        self.torch.cuda.empty_cache()

    def _warmup(self, model: str) -> None:
        t0 = time.time()
        img = self.cfg["live"].get("default_avatar")
        tmp = None
        if not img or not os.path.exists(img):
            import cv2
            tmp = os.path.join(tempfile.gettempdir(), "flashhead_warmup.png")
            cv2.imwrite(tmp, np.full((self.height, self.width, 3), 128, np.uint8))
            img = tmp
        sid = f"__warmup_{model}__"
        self.open_session(sid, model, img)
        n = self.model_info[model]["chunk_samples"]
        for _ in range(2):
            self.generate(sid, np.zeros(n, dtype=np.float32))
        self.close_session(sid)
        logger.info(f"{model} 预热完成 {time.time() - t0:.1f}s")

    # ------------------------------------------------------------------ 会话状态切换
    def _save_active(self, model: str) -> None:
        sid = self.active.get(model)
        if sid is not None and sid in self.sessions and self.sessions[sid].model == model:
            pipe = self.pipes[model]
            self.sessions[sid].pipe_state = {k: getattr(pipe, k) for k in STATE_KEYS if hasattr(pipe, k)}
        self.active[model] = None

    def _activate(self, s: _Session):
        pipe = self.pipes[s.model]
        if self.active.get(s.model) != s.sid:
            self._save_active(s.model)
            for k, v in s.pipe_state.items():
                setattr(pipe, k, v)
            self.active[s.model] = s.sid
        return pipe

    def _prepare(self, s: _Session) -> None:
        """在 pipeline 上为会话 s 准备参考图（会覆盖 pipeline 上的状态，所以先保存当前占用者）。"""
        mc = self.cfg["models"]
        pipe = self.pipes[s.model]
        self._save_active(s.model)
        with self._quiet():
            pipe.prepare_params(
                cond_image_path_or_dir=s.image_path,
                target_size=(self.height, self.width),
                frame_num=mc["frame_num"],
                motion_frames_num=self.model_info[s.model]["motion_frames"],
                sampling_steps=int(mc["sample_steps"][s.model]),
                seed=int(s.seed),
                shift=mc["sample_shift"],
                color_correction_strength=mc["color_correction_strength"],
                use_face_crop=bool(s.use_face_crop),
            )
        s.pipe_state = {}
        self.active[s.model] = s.sid

    def _quiet(self):
        return contextlib.redirect_stdout(io.StringIO()) if self.quiet else contextlib.nullcontext()

    # ------------------------------------------------------------------ 会话 API
    def open_session(self, sid: str, model: str, image_path: str, seed: int = 9999,
                     use_face_crop: bool = False) -> dict:
        if sid in self.sessions:
            self.close_session(sid)
        self.load_model(model)
        win = int(self.cfg["models"]["cached_audio_duration"] * self.sr)
        s = _Session(sid=sid, model=model, image_path=os.path.abspath(image_path), seed=seed,
                     use_face_crop=use_face_crop, audio_win=np.zeros(win, dtype=np.float32))
        self.sessions[sid] = s
        try:
            self._prepare(s)
        except Exception:
            self.sessions.pop(sid, None)
            raise
        return self.session_info(sid)

    def set_avatar(self, sid: str, image_path: str, use_face_crop: bool = False) -> dict:
        s = self.sessions[sid]
        old = (s.image_path, s.use_face_crop)
        s.image_path, s.use_face_crop = os.path.abspath(image_path), use_face_crop
        try:
            self._prepare(s)       # 换形象会把运动帧重置为新参考图
        except Exception:
            s.image_path, s.use_face_crop = old
            self._prepare(s)
            raise
        s.tail = None
        return self.session_info(sid)

    def switch_model(self, sid: str, model: str) -> dict:
        s = self.sessions[sid]
        if model == s.model:
            return self.session_info(sid)
        self.load_model(model)      # 未预加载时会在这里同步加载（期间直播画面会停住）
        old = s.model
        if self.active.get(old) == sid:
            self.active[old] = None
        s.model = model
        s.pipe_state = {}
        try:
            self._prepare(s)
        except Exception:
            s.model = old
            self._prepare(s)
            raise
        if s.tail is not None:
            torch = self.torch
            pipe = self.pipes[model]
            mfn = self.model_info[model]["motion_frames"]
            with torch.no_grad():
                frames = s.tail[:, -mfn:].unsqueeze(0).to(pipe.device, pipe.param_dtype)
                pipe.latent_motion_frames = pipe.vae.encode(frames)
        logger.info(f"[{sid}] 模型切换 {old} -> {model}")
        return self.session_info(sid)

    def generate(self, sid: str, audio: np.ndarray) -> dict:
        torch = self.torch
        s = self.sessions[sid]
        info = self.model_info[s.model]
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size != info["chunk_samples"]:
            raise ValueError(f"音频长度 {audio.size} != chunk_samples {info['chunk_samples']}")
        pipe = self._activate(s)
        s.audio_win = np.concatenate([s.audio_win[audio.size:], audio])

        t0 = time.perf_counter()
        with torch.no_grad(), self._quiet():
            emb = self._audio_embedding(pipe, s.audio_win)
            sample = pipe.generate(emb.to(pipe.device))           # [3, T, H, W]，[-1, 1]
            s.tail = sample[:, -KEEP_TAIL_FRAMES:].to(torch.bfloat16)
            new = sample[:, info["motion_frames"]:]
            frames = ((new + 1.0) * 127.5).clamp(0, 255).to(torch.uint8)
            frames = frames.permute(1, 2, 3, 0).contiguous().cpu().numpy()   # [N, H, W, 3] RGB
        return {"frames": frames, "gen_time": time.perf_counter() - t0}

    def _audio_embedding(self, pipe, audio_win: np.ndarray):
        """与官方 inference.get_audio_embedding 一致：8 秒滑窗，取最后 frame_num 帧，每帧带前后各 2 帧上下文。"""
        torch = self.torch
        mc = self.cfg["models"]
        emb = pipe.preprocess_audio(audio_win, sr=self.sr, fps=self.fps)
        end = int(mc["cached_audio_duration"] * self.fps)
        start = end - mc["frame_num"]
        offsets = torch.arange(5) - 2
        idx = (torch.arange(start, end).unsqueeze(1) + offsets.unsqueeze(0)).clamp(0, end - 1)
        return emb[idx][None, ...].contiguous()

    def close_session(self, sid: str) -> None:
        s = self.sessions.pop(sid, None)
        if s is not None and self.active.get(s.model) == sid:
            self.active[s.model] = None
