"""配置加载：YAML 覆盖 DEFAULTS，结果是普通 dict（可直接 pickle 给 worker 子进程）。"""
from __future__ import annotations

import copy
import os

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 所有可配置项及默认值。新增配置项请先加到这里，再在 YAML 里覆盖。
DEFAULTS = {
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "data_dir": "live_data",          # 上传文件、离线视频输出目录
        "ffmpeg": "ffmpeg",               # ffmpeg 可执行文件路径
    },
    "models": {
        "backend": "flashhead",           # flashhead | mock（mock 不需要模型和显卡，用来调框架）
        "ckpt_dir": "models/SoulX-FlashHead-1_3B",
        "wav2vec_dir": "models/wav2vec2-base-960h",
        "default_model": "lite",          # lite | pro
        "preload": ["lite", "pro"],       # worker 启动时加载的模型；两个都预加载切换才是秒切
        "compile": "auto",                # auto | true | false；torch.compile 需要 triton（Windows 装 triton-windows）
        "warmup": True,                   # 加载后空跑两次，避免第一次推理卡顿
        "quiet_pipeline_logs": True,      # 屏蔽官方 pipeline 每步的 print
        "sample_steps": {"lite": 4, "pro": 4},   # 去噪步数，官方蒸馏模型支持 4 或 2；pro 改 2 可提速
        "height": 512,
        "width": 512,
        "frame_num": 33,
        "motion_frames_latent_num": 2,
        "sample_shift": 5,
        "color_correction_strength": 1.0,
        "cached_audio_duration": 8,       # wav2vec 滑窗长度（秒）
    },
    "gpu": {
        "devices": [0],                   # 物理显卡编号，每张卡一个 worker 进程
        "mode": "multi_instance",         # single | multi_instance | sequence_parallel(未实现)
        "live_devices": None,             # 直播会话优先放的卡，None = 全部
        "offline_devices": None,          # 离线任务优先放的卡，None = 全部
        "stagger_start": True,            # worker 逐个启动，降低内存峰值（内存小时必须开）
        "worker_start_timeout": 1800,
    },
    "live": {
        "fps": 25,
        "sample_rate": 16000,
        "auto_start": True,               # 启动后自动建一个 default 会话
        "default_avatar": "examples/girl.png",
        "seed": 9999,
        "use_face_crop": False,
        "safety_factor": 1.3,             # 提前生成量 = 生成耗时 EMA * safety_factor + margin
        "margin_s": 0.15,
        "max_ahead_chunks": 2,            # 最多领先播放几个块（延迟上限保护）
        "interrupt_drop_queued": False,   # 打断时是否丢弃已生成未播放的帧（丢弃会有动作跳变）
        "generate_timeout": 120,
    },
    "speech": {
        "target_rms_db": -20.0,           # 语音响度归一化目标（dBFS），影响嘴型幅度；None 关闭
        "max_gain_db": 20.0,
        "fade_ms": 15,                    # 每句首尾淡入淡出，避免爆音
        "sentence_gap_ms": 120,           # 句子之间插入的停顿
    },
    "idle": {
        "mode": "noise",                  # noise | zeros | file；纯 zeros 容易让画面发僵
        "noise_db": -55.0,                # noise 模式的底噪电平（dBFS）
        "file": None,                     # file 模式：循环播放的空闲音频（呼吸声、环境底噪等）
        "file_gain_db": 0.0,
    },
    "stream": {
        "jpeg_quality": 85,
        "client_queue_frames": 75,        # 每个 WebSocket 客户端最多积压帧数，超出丢最旧的
    },
    "tts": {
        "engine": "edge",                 # edge | http | mock
        "voice": "zh-CN-XiaoxiaoNeural",
        "max_sentence_chars": 40,
        "edge": {"rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
        "http": {
            "url": "http://127.0.0.1:9880/tts",
            "method": "GET",              # GET | POST
            "params": {"text": "{text}", "text_lang": "zh"},
            "json": None,
            "headers": {},
            "timeout": 60,
        },
    },
    "mock": {
        # 生成一个块耗时 = 块时长 * speed（<1 才能实时）；>1 可以模拟"显卡跟不上"的卡顿
        "speed": {"lite": 0.3, "pro": 0.7},
    },
    "offline": {
        "max_concurrent": 1,              # 同时跑的离线任务数
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: str | None = None, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            _deep_merge(cfg, yaml.safe_load(f) or {})
    if overrides:
        _deep_merge(cfg, overrides)
    _normalize(cfg)
    return cfg


def _normalize(cfg: dict) -> None:
    gpu = cfg["gpu"]
    gpu["devices"] = [int(d) for d in (gpu["devices"] or [0])]
    if gpu["mode"] == "single":
        gpu["devices"] = gpu["devices"][:1]
    for key in ("live_devices", "offline_devices"):
        if gpu[key] is not None:
            gpu[key] = [int(d) for d in gpu[key]]
    models = cfg["models"]
    if models["default_model"] not in ("lite", "pro"):
        raise ValueError(f"models.default_model 只能是 lite/pro: {models['default_model']}")
    models["preload"] = [m for m in (models["preload"] or []) if m in ("lite", "pro")]
    # 相对路径一律相对项目根目录（SoulX-FlashHead/），与从哪个目录启动无关
    for sect, key in (("server", "data_dir"), ("models", "ckpt_dir"), ("models", "wav2vec_dir"),
                      ("live", "default_avatar"), ("idle", "file")):
        val = cfg[sect].get(key)
        if val and not os.path.isabs(val):
            cfg[sect][key] = os.path.normpath(os.path.join(PROJECT_ROOT, val))
