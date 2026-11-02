"""Mock 推理后端：不需要模型、显卡、torch。

用参考图做轻微的平移/旋转/缩放（模拟头部微动），按音频能量画一张"嘴"，
块大小、运动帧数和真模型一致（lite 24 帧/块，pro 28 帧/块），生成耗时按 mock.speed 模拟。
用来联调直播时钟、空闲/说话切换、WebSocket 协议、网页客户端、离线任务。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np

from live.worker.runner_base import BaseRunner, read_image_rgb, resize_center_crop

MOTION_FRAMES = {"lite": 9, "pro": 5}   # 与真模型一致：(2-1)*vae_stride_t+1，LTX t=8，Wan t=4


@dataclass
class _MockSession:
    sid: str
    model: str
    base: np.ndarray
    frame_idx: int = 0
    mouth: float = 0.0


class MockRunner(BaseRunner):
    backend = "mock"

    def __init__(self, cfg: dict, gpu_id: int):
        super().__init__(cfg, gpu_id)
        speed = cfg["mock"].get("speed", 0.35)
        self.speed = speed if isinstance(speed, dict) else {"lite": speed, "pro": speed * 2.0}

    def info(self) -> dict:
        return {
            "backend": self.backend,
            "gpu_id": self.gpu_id,
            "gpu_name": f"mock-gpu-{self.gpu_id}",
            "models": dict(self.model_info),
            "sessions": list(self.sessions),
            "speed": self.speed,
        }

    def load_model(self, model: str) -> dict:
        if model not in MOTION_FRAMES:
            raise ValueError(f"未知模型: {model}")
        if model not in self.model_info:
            time.sleep(0.3)
            self.model_info[model] = self.make_chunk_info(model, MOTION_FRAMES[model])
        return self.model_info[model]

    def unload_model(self, model: str) -> None:
        self.model_info.pop(model, None)

    def open_session(self, sid: str, model: str, image_path: str, seed: int = 9999,
                     use_face_crop: bool = False) -> dict:
        self.load_model(model)
        base = resize_center_crop(read_image_rgb(image_path), self.height, self.width)
        self.sessions[sid] = _MockSession(sid=sid, model=model, base=base)
        return self.session_info(sid)

    def set_avatar(self, sid: str, image_path: str, use_face_crop: bool = False) -> dict:
        s = self.sessions[sid]
        s.base = resize_center_crop(read_image_rgb(image_path), self.height, self.width)
        return self.session_info(sid)

    def switch_model(self, sid: str, model: str) -> dict:
        self.load_model(model)
        self.sessions[sid].model = model
        return self.session_info(sid)

    def generate(self, sid: str, audio: np.ndarray) -> dict:
        t0 = time.perf_counter()
        s = self.sessions[sid]
        info = self.model_info[s.model]
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size != info["chunk_samples"]:
            raise ValueError(f"音频长度 {audio.size} != chunk_samples {info['chunk_samples']}")
        spf = self.sr // self.fps
        h, w = self.height, self.width
        frames = np.empty((info["chunk_frames"], h, w, 3), dtype=np.uint8)
        for i in range(info["chunk_frames"]):
            seg = audio[i * spf:(i + 1) * spf]
            db = 20 * np.log10(max(float(np.sqrt(np.mean(seg ** 2))), 1e-6))
            level = float(np.clip((db + 45) / 30, 0, 1))      # -45dB 闭嘴，-15dB 张到最大
            s.mouth = 0.5 * s.mouth + 0.5 * level
            t = s.frame_idx / self.fps
            dx = 4 * np.sin(2 * np.pi * 0.11 * t) + 2 * np.sin(2 * np.pi * 0.37 * t + 1)
            dy = 3 * np.sin(2 * np.pi * 0.17 * t + 2)
            ang = 1.5 * np.sin(2 * np.pi * 0.07 * t)
            m = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1 + 0.01 * np.sin(2 * np.pi * 0.05 * t))
            m[0, 2] += dx
            m[1, 2] += dy
            img = cv2.warpAffine(s.base, m, (w, h), borderMode=cv2.BORDER_REFLECT)
            cx, cy = int(w / 2 + dx), int(h * 0.70 + dy)
            cv2.ellipse(img, (cx, cy), (int(w * 0.07), int(2 + s.mouth * h * 0.05)), 0, 0, 360,
                        (60, 20, 30), -1)
            label = f"MOCK {s.model.upper()}  {'SPEAK' if level > 0.15 else 'IDLE'}  #{s.frame_idx}"
            cv2.putText(img, label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(img, label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
            frames[i] = img
            s.frame_idx += 1
        target = info["chunk_seconds"] * self.speed[s.model]
        elapsed = time.perf_counter() - t0
        if target > elapsed:
            time.sleep(target - elapsed)
        return {"frames": frames, "gen_time": time.perf_counter() - t0}

    def close_session(self, sid: str) -> None:
        self.sessions.pop(sid, None)
