"""Runner 接口：一个 worker 进程里的推理后端，可同时服务多个会话（直播会话 / 离线任务）。

关键概念 —— 块（chunk）：
  FlashHead 是自回归逐块生成的。每次 generate 输入"一块"音频，输出"一块"新帧：
    chunk_frames  = frame_num - motion_frames      Pro: 33-5=28 帧(1.12s)  Lite: 33-9=24 帧(0.96s)
    chunk_samples = chunk_frames * sample_rate / fps
  上一块的最后几帧会作为下一块的运动参考帧（motion frames），所以块与块之间是连续的，
  空闲（喂底噪）和说话（喂 TTS）之间切换不会跳。
"""
from __future__ import annotations

import os

import cv2
import numpy as np


class BaseRunner:
    backend = "base"

    def __init__(self, cfg: dict, gpu_id: int):
        self.cfg = cfg
        self.gpu_id = gpu_id
        self.fps = cfg["live"]["fps"]
        self.sr = cfg["live"]["sample_rate"]
        self.height = cfg["models"]["height"]
        self.width = cfg["models"]["width"]
        self.model_info: dict = {}   # model -> chunk 信息
        self.sessions: dict = {}

    # ---- 子类实现 ----
    def info(self) -> dict:
        raise NotImplementedError

    def load_model(self, model: str) -> dict:
        raise NotImplementedError

    def unload_model(self, model: str) -> None:
        raise NotImplementedError

    def open_session(self, sid: str, model: str, image_path: str, seed: int = 9999,
                     use_face_crop: bool = False) -> dict:
        raise NotImplementedError

    def set_avatar(self, sid: str, image_path: str, use_face_crop: bool = False) -> dict:
        raise NotImplementedError

    def switch_model(self, sid: str, model: str) -> dict:
        raise NotImplementedError

    def generate(self, sid: str, audio: np.ndarray) -> dict:
        """输入 chunk_samples 个 float32 采样，返回 {"frames": uint8[N,H,W,3] RGB, "gen_time": 秒}。"""
        raise NotImplementedError

    def close_session(self, sid: str) -> None:
        raise NotImplementedError

    # ---- 公共工具 ----
    def make_chunk_info(self, model: str, motion_frames: int) -> dict:
        chunk_frames = self.cfg["models"]["frame_num"] - motion_frames
        return {
            "model": model,
            "motion_frames": motion_frames,
            "chunk_frames": chunk_frames,
            "chunk_samples": chunk_frames * self.sr // self.fps,
            "chunk_seconds": chunk_frames / self.fps,
            "fps": self.fps,
            "sample_rate": self.sr,
            "height": self.height,
            "width": self.width,
        }

    def session_info(self, sid: str) -> dict:
        s = self.sessions[sid]
        return dict(self.model_info[s.model], session_id=sid)


def read_image_rgb(path: str) -> np.ndarray:
    """支持中文路径的读图（cv2.imread 在 Windows 上不支持非 ASCII 路径）。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"图片不存在: {path}")
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法解码图片: {path}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def resize_center_crop(img: np.ndarray, h: int, w: int) -> np.ndarray:
    ih, iw = img.shape[:2]
    scale = max(h / ih, w / iw)
    nh, nw = int(np.ceil(ih * scale)), int(np.ceil(iw * scale))
    img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    y0, x0 = (nh - h) // 2, (nw - w) // 2
    return np.ascontiguousarray(img[y0:y0 + h, x0:x0 + w])
