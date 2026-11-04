"""设置项 schema：每个可配置项只在这里定义一次（键、类型、默认值、范围、中文说明、Web 分组、热更新/需重启）。

- live/config.py 的 DEFAULTS 由这里生成（load_config 兼容层不变）。
- Web「直播间设置」页按这里自动生成表单（GET /api/settings 下发）。
- 新增配置项：在对应分组里加一行 F(...)，再按需在 live/configs/*.yaml 覆盖。

apply：
  hot      保存后立即生效（模块每次使用时读 cfg，或订阅 settings.changed 重建）
  restart  只保存，重启服务后生效（Web 标「需重启」）
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

HOT, RESTART = "hot", "restart"

# Web 左侧分组（顺序即显示顺序）
GROUPS = [
    ("danmaku", "直播间与弹幕"),
    ("interaction", "互动策略"),
    ("persona", "主播人设"),
    ("tts", "语音（TTS）"),
    ("llm", "大模型（LLM）"),
    ("knowledge", "知识库与讲品"),
    ("scene", "直播画面"),
    ("compliance", "合规词库"),
    ("avatar", "数字人与推流"),
    ("advanced", "高级"),
]
GROUP_IDS = {g for g, _ in GROUPS}
TYPES = ("int", "float", "bool", "str", "text", "enum", "list", "dict")


@dataclass(frozen=True)
class Field:
    key: str                     # 点路径，如 speech.target_rms_db
    type: str                    # int | float | bool | str | text(多行) | enum | list | dict
    default: Any
    label: str
    help: str = ""
    group: str = "advanced"
    apply: str = HOT
    min: float | None = None
    max: float | None = None
    choices: tuple = ()
    nullable: bool = False       # 允许 None（YAML 的 null）
    path: bool = False           # 路径：相对路径统一相对项目根目录
    item_type: str | None = None # list 的元素类型：int | float | str
    item_choices: tuple = ()     # list 元素的可选值
    readonly: bool = False       # Web 上只读（只能在 yaml 里改），如数据目录本身

    def describe(self) -> dict:
        return {
            "key": self.key, "type": self.type, "default": copy.deepcopy(self.default), "label": self.label,
            "help": self.help, "group": self.group, "apply": self.apply, "min": self.min, "max": self.max,
            "choices": list(self.choices), "nullable": self.nullable, "path": self.path,
            "item_type": self.item_type, "item_choices": list(self.item_choices), "readonly": self.readonly,
        }


def F(key, type, default, label, help="", **kw) -> Field:  # noqa: A002  简写，schema 表读起来像配置文件
    return Field(key=key, type=type, default=default, label=label, help=help, **kw)


# ============================================================================ 数字人与推流（现有配置迁入）
_AV = dict(group="avatar")
_ADV = dict(group="advanced")

FIELDS: list[Field] = [
    # ---- server ----
    F("server.host", "str", "0.0.0.0", "监听地址", "0.0.0.0 = 局域网可访问", apply=RESTART, **_ADV),
    F("server.port", "int", 8000, "端口", apply=RESTART, min=1, max=65535, **_ADV),
    F("server.data_dir", "str", "live_data", "数据目录", "上传文件、离线视频、设置覆盖、知识库等运行时数据（只能在 yaml 里改）",
      apply=RESTART, path=True, readonly=True, **_ADV),
    F("server.ffmpeg", "str", "ffmpeg", "ffmpeg 路径", "不在 PATH 里就写完整路径，如 D:/ffmpeg/bin/ffmpeg.exe",
      apply=RESTART, **_ADV),

    # ---- models ----
    F("models.backend", "enum", "flashhead", "推理后端", "mock 不需要模型和显卡，用来调框架",
      apply=RESTART, choices=("flashhead", "mock"), **_AV),
    F("models.ckpt_dir", "str", "models/SoulX-FlashHead-1_3B", "模型目录", apply=RESTART, path=True, **_AV),
    F("models.wav2vec_dir", "str", "models/wav2vec2-base-960h", "wav2vec 目录", apply=RESTART, path=True, **_AV),
    F("models.default_model", "enum", "lite", "默认模型", "新建会话和离线任务默认用的模型（对新建的生效）",
      choices=("lite", "pro"), **_AV),
    F("models.preload", "list", ["lite", "pro"], "预加载模型", "两个都预加载切换才是秒切",
      apply=RESTART, item_type="str", item_choices=("lite", "pro"), **_AV),
    F("models.compile", "enum", "auto", "torch.compile", "需要 triton（Windows 装 triton-windows）；auto = 有就开",
      apply=RESTART, choices=("auto", "true", "false"), **_AV),
    F("models.warmup", "bool", True, "加载后预热", "空跑两次，避免第一次推理卡顿", apply=RESTART, **_AV),
    F("models.quiet_pipeline_logs", "bool", True, "屏蔽官方 pipeline 日志", apply=RESTART, **_AV),
    F("models.sample_steps", "dict", {"lite": 4, "pro": 4}, "去噪步数", "官方蒸馏模型支持 4 或 2；pro 改 2 可提速",
      apply=RESTART, **_AV),
    F("models.height", "int", 512, "画面高度", apply=RESTART, min=64, max=2048, **_AV),
    F("models.width", "int", 512, "画面宽度", apply=RESTART, min=64, max=2048, **_AV),
    F("models.frame_num", "int", 33, "每块帧数（含运动帧）", apply=RESTART, min=9, max=129, **_AV),
    F("models.motion_frames_latent_num", "int", 2, "运动帧 latent 数", apply=RESTART, min=1, max=8, **_AV),
    F("models.sample_shift", "float", 5, "sample_shift", apply=RESTART, min=0, max=20, **_AV),
    F("models.color_correction_strength", "float", 1.0, "颜色校正强度", apply=RESTART, min=0, max=1, **_AV),
    F("models.cached_audio_duration", "int", 8, "wav2vec 滑窗（秒）", apply=RESTART, min=2, max=30, **_AV),

    # ---- gpu ----
    F("gpu.devices", "list", [0], "显卡编号", "每张卡一个 worker 进程", apply=RESTART, item_type="int", **_AV),
    F("gpu.mode", "enum", "multi_instance", "多卡模式", "sequence_parallel 未实现（需 Linux + NCCL）",
      apply=RESTART, choices=("single", "multi_instance", "sequence_parallel"), **_AV),
    F("gpu.live_devices", "list", None, "直播优先用的卡", "空 = 全部", apply=RESTART, item_type="int",
      nullable=True, **_AV),
    F("gpu.offline_devices", "list", None, "离线任务优先用的卡", "空 = 全部", apply=RESTART, item_type="int",
      nullable=True, **_AV),
    F("gpu.stagger_start", "bool", True, "worker 逐个启动", "降低内存峰值（内存小时必须开）", apply=RESTART, **_AV),
    F("gpu.worker_start_timeout", "int", 1800, "worker 启动超时（秒）", apply=RESTART, min=10, max=7200, **_AV),

    # ---- live ----
    F("live.fps", "int", 25, "帧率", apply=RESTART, min=1, max=60, **_AV),
    F("live.sample_rate", "int", 16000, "采样率", apply=RESTART, min=8000, max=48000, **_AV),
    F("live.auto_start", "bool", True, "启动后自动建 default 会话", apply=RESTART, **_AV),
    F("live.default_avatar", "str", "examples/girl.png", "默认形象图片", "对新建会话生效", path=True, **_AV),
    F("live.seed", "int", 9999, "随机种子", "对新建会话生效", **_AV),
    F("live.use_face_crop", "bool", False, "默认人脸裁剪", "对新建会话生效", **_AV),
    F("live.safety_factor", "float", 1.3, "提前量系数", "提前生成量 = 生成耗时 × 系数 + 余量；越小延迟越低、越容易补帧",
      min=1.0, max=3.0, **_AV),
    F("live.margin_s", "float", 0.15, "提前量余量（秒）", min=0.0, max=2.0, **_AV),
    F("live.max_ahead_chunks", "int", 2, "最多领先几个块", "延迟上限保护", min=1, max=10, **_AV),
    F("live.interrupt_drop_queued", "bool", False, "打断时丢弃已生成帧", "丢弃会有动作跳变", **_AV),
    F("live.generate_timeout", "int", 120, "单块生成超时（秒）", apply=RESTART, min=5, max=1200, **_AV),

    # ---- speech ----
    F("speech.target_rms_db", "float", -20.0, "语音响度目标（dBFS）", "影响嘴型幅度：嘴张得小就调大（如 -16）；留空关闭归一化",
      min=-40, max=-6, nullable=True, **_AV),
    F("speech.max_gain_db", "float", 20.0, "最大增益（dB）", min=0, max=40, **_AV),
    F("speech.fade_ms", "int", 15, "句首尾淡入淡出（毫秒）", "避免爆音", min=0, max=200, **_AV),
    F("speech.sentence_gap_ms", "int", 120, "句间停顿（毫秒）", "太短会显得一直在动嘴", min=0, max=2000, **_AV),

    # ---- idle ----
    F("idle.mode", "enum", "noise", "空闲音频", "noise 底噪 / zeros 静音 / file 循环播放录音；纯 zeros 容易发僵",
      choices=("noise", "zeros", "file"), **_AV),
    F("idle.noise_db", "float", -55.0, "底噪电平（dBFS）", "noise 模式；-65 ~ -45 之间试", min=-90, max=-20, **_AV),
    F("idle.file", "str", None, "空闲音频文件", "file 模式循环播放（呼吸声、环境底噪）", path=True, nullable=True, **_AV),
    F("idle.file_gain_db", "float", 0.0, "空闲音频增益（dB）", min=-30, max=30, **_AV),

    # ---- stream ----
    F("stream.jpeg_quality", "int", 85, "JPEG 质量", min=30, max=100, **_AV),
    F("stream.client_queue_frames", "int", 75, "客户端最大积压帧", "超出丢最旧的", min=10, max=500, **_AV),

    # ---- tts（现有） ----
    F("tts.engine", "enum", "volc", "TTS 引擎", "volc 火山声音复刻（凭证在 .env）/ edge 免费在线 / http 自建服务 / mock 不联网合成音；"
      "首选不可用时按「回退顺序」换下一个", choices=("volc", "edge", "http", "mock"), group="tts"),
    F("tts.voice", "str", "zh-CN-XiaoxiaoNeural", "默认音色（edge/http）", group="tts"),
    F("tts.max_sentence_chars", "int", 40, "单句最大字数", "超过按逗号再切", min=10, max=200, group="tts"),
    F("tts.edge.rate", "str", "+0%", "edge 语速", "如 +10%", group="tts"),
    F("tts.edge.volume", "str", "+0%", "edge 音量", group="tts"),
    F("tts.edge.pitch", "str", "+0Hz", "edge 音调", group="tts"),
    F("tts.http.url", "str", "http://127.0.0.1:9880/tts", "http TTS 地址", "{text} {voice} 会被替换", group="tts"),
    F("tts.http.method", "enum", "GET", "http 方法", choices=("GET", "POST"), group="tts"),
    F("tts.http.params", "dict", {"text": "{text}", "text_lang": "zh"}, "http 查询参数", group="tts"),
    F("tts.http.json", "dict", None, "http JSON 请求体", "非空时用 POST", nullable=True, group="tts"),
    F("tts.http.headers", "dict", {}, "http 请求头", group="tts"),
    F("tts.http.timeout", "int", 60, "http 超时（秒）", min=1, max=600, group="tts"),
    # ---- 第二阶段 P3：火山 TTS、回退、缓存、语速、用量保护 ----
    F("tts.fallback", "list", ["edge", "mock"], "回退顺序", "首选引擎不可用（没配凭证、调用失败、超出每日上限）时依次尝试",
      item_type="str", item_choices=("volc", "edge", "http", "mock"), group="tts"),
    F("tts.runtime_fallback", "bool", True, "单句失败时用回退引擎补上", "关掉则这一句跳过不说（音色不会中途变）", group="tts"),
    F("tts.speed", "float", 1.0, "默认语速", "1.0 正常；火山映射到 speech_rate，edge 映射到 rate", min=0.5, max=2.0, group="tts"),
    F("tts.volc.voice_type", "str", "", "火山音色 ID", "声音复刻音色 S_xxx；留空用 .env 的 VOLC_VOICE_TYPE", group="tts"),
    F("tts.volc.resource_id", "str", "", "火山资源 ID", "seed-icl-1.0 / seed-icl-1.0-concurr / seed-icl-2.0；留空用 .env 或 seed-icl-2.0",
      group="tts"),
    F("tts.volc.sample_rate", "enum", 16000, "火山请求采样率", "数字人要 16k；⚠ 若服务端不支持 16000 改 24000（自动重采样到 16k）",
      choices=(16000, 24000), group="tts"),
    F("tts.volc.timeout_s", "float", 10.0, "火山合成超时（秒）", min=2, max=60, group="tts"),
    F("tts.volc.base_url", "str", "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse", "火山接口地址",
      "V3 HTTP SSE 单向流式", group="tts"),
    F("tts.cache.enabled", "bool", True, "合成缓存", "同一句话（同音色、同语速档）只合成一次，之后直接复用；换音色/引擎自动失效", group="tts"),
    F("tts.cache.persist", "bool", True, "缓存落盘", "写到 live_data/cache/tts/，重启后仍可命中", group="tts"),
    F("tts.cache.speed_round", "float", 0.1, "缓存语速档", "语速按这个粒度取整进缓存键（0 = 语速不进键）", min=0, max=0.5, group="tts"),
    F("tts.cache.mem_max_entries", "int", 1024, "内存缓存条数", min=16, max=20000, group="tts"),
    F("tts.daily_max_chars", "int", 0, "计费 TTS 每日字数上限", "0 = 不限；超过后当天改用回退引擎并在 Web 告警（防止意外烧钱）",
      min=0, max=10_000_000, group="tts"),
    # ---- 第二阶段 P3：大模型 ----
    F("llm.provider", "enum", "openai", "LLM 接口类型", "openai = OpenAI 兼容 /chat/completions（地址/密钥/模型在 .env）；mock = 不联网的假回复（调试用）",
      choices=("openai", "mock"), group="llm"),
    F("llm.temperature", "float", 0.8, "温度", "越高越随机", min=0, max=2, group="llm"),
    F("llm.max_tokens", "int", 400, "最大 token 数", "混合推理模型（如 deepseek-v4 系列）要留够，否则思考完正文为空", min=16, max=8192,
      group="llm"),
    F("llm.stream", "bool", True, "流式输出", "按句交给 TTS，降低首句延迟", group="llm"),
    F("llm.history_size", "int", 5, "防复读记忆条数", "最近说过的几句话会告诉 LLM，避免重复", min=0, max=50, group="llm"),
    F("llm.daily_max_calls", "int", 0, "每日调用次数上限", "0 = 不限；超过后当天全部走模板话术并在 Web 告警", min=0, max=1_000_000,
      group="llm"),

    # ---- mock / offline ----
    F("mock.speed", "dict", {"lite": 0.3, "pro": 0.7}, "mock 生成速度", "生成一块耗时 = 块时长 × speed；>1 模拟显卡跟不上",
      apply=RESTART, **_ADV),
    F("offline.max_concurrent", "int", 1, "离线任务并发数", apply=RESTART, min=1, max=8, **_ADV),
]


# ============================================================================ 直播间与弹幕（第二阶段 P2）
_DM = dict(group="danmaku")
FIELDS += [
    F("danmaku.enabled", "bool", False, "接入弹幕", "打开后按「弹幕来源」启动弹幕子进程；关掉即断开", **_DM),
    F("danmaku.source", "enum", "douyin", "弹幕来源", "douyin 抖音直播间 / mock 模拟弹幕 / replay 回放录制的 JSONL",
      choices=("douyin", "mock", "replay"), **_DM),
    F("danmaku.live_id", "str", "", "抖音直播间号", "live.douyin.com/ 后面那串数字（web_rid）；修改后立即重连到新房间", **_DM),
    F("danmaku.reconnect_delay_s", "float", 5.0, "断线重连初始间隔（秒）", "指数退避，上限 60 秒", min=1, max=60, **_DM),
    F("danmaku.combo_timeout_s", "float", 3.0, "连击礼物结算超时（秒）", "同一连击多久没有新包就按最后的数量结算一次（⚠ 推送规律需实测）",
      min=0.5, max=10, **_DM),
    F("danmaku.gender_map", "dict", {"1": "male", "2": "female"}, "性别枚举映射",
      "抖音 User.gender 数字 → male/female；上播后用已知性别的号核对，反了就对调", **_DM),
    F("danmaku.sniff", "bool", False, "协议探针", "记录首次出现的消息类型、裸解未适配的消息（排查新消息类型时开）", **_DM),
    F("danmaku.record", "bool", False, "录制弹幕事件", "写入 live_data/events/日期_房间.jsonl，可回放；含观众昵称，注意隐私", **_DM),
    F("danmaku.feed.max_items", "int", 500, "Web 事件流保留条数", min=50, max=5000, **_DM),
    F("danmaku.mock.rate_per_min", "int", 30, "模拟弹幕：每分钟事件数", min=1, max=1200, **_DM),
    F("danmaku.mock.mix", "dict", {"chat": 0.35, "member": 0.3, "like": 0.2, "gift": 0.1, "follow": 0.05},
      "模拟弹幕：各类事件比例", "会自动归一化；可加 share / fansclub", **_DM),
    F("danmaku.mock.question_ratio", "float", 0.3, "模拟弹幕：产品提问占弹幕比例", min=0, max=1, **_DM),
    F("danmaku.mock.female_ratio", "float", 0.6, "模拟弹幕：女性观众比例", min=0, max=1, **_DM),
    F("danmaku.mock.wealth_dist", "dict", {"0": 0.45, "6": 0.3, "15": 0.15, "25": 0.08, "40": 0.02},
      "模拟弹幕：财富等级分布", "等级 → 概率（会归一化），实际等级在该档上下浮动", **_DM),
    F("danmaku.replay.file", "str", "", "回放文件", "录制的 JSONL（相对路径相对项目根目录）", path=True, **_DM),
    F("danmaku.replay.speed", "float", 1.0, "回放倍速", min=0.1, max=50, **_DM),
    F("danmaku.replay.loop", "bool", False, "循环回放", **_DM),
]


# ============================================================================ 互动策略 / 主持人 / 人设 / 合规（第二阶段 P4）
import os as _os

_POLICY_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "interaction", "policies")


def _read_policy(name: str) -> str:
    try:
        with open(_os.path.join(_POLICY_DIR, name), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


_IA = dict(group="interaction")
_PS = dict(group="persona")
_CP = dict(group="compliance")
FIELDS += [
    F("interaction.enabled", "bool", True, "互动总开关", "关掉后弹幕只显示不回应（讲品、人工插话照常）", **_IA),
    F("interaction.tick_s", "float", 1.0, "导演节拍（秒）", "关键事件（礼物、弹幕、进场…）到了会立即算一拍", min=0.2, max=5, **_IA),
    F("interaction.urgent_priority", "int", 24, "紧急优先级", "优先级 ≥ 此值的互动不受发声间隔限制（即收即播）", min=0, max=100, **_IA),
    F("interaction.max_stale_s", "float", 10.0, "队列过期时间（秒）", "排队超过这么久还没说的就丢掉（单个触发器可覆盖）", min=1, max=300, **_IA),
    F("interaction.speak_min_s", "float", 2.0, "最短发声间隔（秒）", "场子热（能量高）时两次开口的最短间隔", min=0, max=60, **_IA),
    F("interaction.speak_max_s", "float", 18.0, "最长沉默（秒）", "场子冷时多久至少说一句（非紧急互动按能量在两者间插值）", min=1, max=300, **_IA),
    F("interaction.energy_decay", "float", 0.9, "能量衰减系数", "每拍能量 × 该系数", min=0.5, max=1.0, **_IA),
    F("interaction.merge_enabled", "bool", True, "同拍合并", "同一拍里「点名」和「场面」两类触发器合成一句说", **_IA),
    F("interaction.window.chat_s", "float", 4.0, "弹幕保鲜（秒）", "超过就不回了", min=1, max=60, **_IA),
    F("interaction.window.question_s", "float", 15.0, "产品提问保鲜（秒）", "提问比闲聊保留更久", min=1, max=120, **_IA),
    F("interaction.window.gift_s", "float", 5.0, "礼物保鲜（秒）", min=1, max=60, **_IA),
    F("interaction.window.member_s", "float", 5.0, "进场保鲜（秒）", min=1, max=60, **_IA),
    F("interaction.window.follow_s", "float", 6.0, "关注保鲜（秒）", min=1, max=60, **_IA),
    F("interaction.window.like_s", "float", 6.0, "点赞统计窗口（秒）", "窗口内累计最多的那位被点名", min=1, max=60, **_IA),
    F("interaction.window.online_s", "float", 45.0, "在线人数变化窗口（秒）", "用于判断猛涨 / 下滑", min=5, max=600, **_IA),
    F("interaction.window.rank_s", "float", 90.0, "在线观众名单保鲜（秒）", "冷场点名用", min=10, max=600, **_IA),
    F("interaction.gift.min_value", "int", 1, "感谢礼物门槛（钻）", "单笔总价值 ≥ 此值才感谢", min=0, max=100000, **_IA),
    F("interaction.gift.tier_m", "int", 10, "礼物 M 档（钻）", "单笔总价值 ≥ 此值为 M 档（正式答谢）", min=1, max=100000, **_IA),
    F("interaction.gift.tier_l", "int", 50, "礼物 L 档（钻）", "单笔总价值 ≥ 此值为 L 档（重磅答谢）", min=1, max=1000000, **_IA),
    F("interaction.gift.followup_delays_s", "list", [18.0, 45.0], "送礼后回踩（秒）", "答谢后隔多久再提一次送礼的人", item_type="float",
      **_IA),
    F("interaction.gift.repeat_window_s", "float", 180.0, "连续送礼窗口（秒）", "同一人在这段时间内再送礼走「连续送礼」话术", min=10, max=3600,
      **_IA),
    F("interaction.gift.king_min_value", "int", 10, "礼物榜榜首门槛（钻）", "本场累计 ≥ 此值才算榜首（防 1 钻易主刷屏）", min=1, max=1000000,
      **_IA),
    F("interaction.welcome.vip_gift_value", "int", 50, "金主门槛（钻）", "往场累计送礼 ≥ 此值的老观众进场 = 金主回归", min=1, max=10000000,
      **_IA),
    F("interaction.welcome.regular_visits", "int", 3, "熟客门槛（次）", "往场累计进场 ≥ 此次数 = 熟客", min=1, max=10000, **_IA),
    F("interaction.welcome.high_grade_level", "int", 20, "高财富等级门槛", "进场者财富等级 ≥ 此值 = 高规格欢迎（首次来也认）", min=1, max=100,
      **_IA),
    F("interaction.like.min_count", "int", 5, "点赞点名门槛", "窗口内累计点赞 ≥ 此值才点名", min=1, max=10000, **_IA),
    F("interaction.like.repeat_cooldown_factor", "float", 3.0, "同一人点赞冷却放大", "点赞最多的还是上次那个人时，冷却乘这个倍数",
      min=1, max=20, **_IA),
    F("interaction.online.surge_min", "int", 5, "人数猛涨门槛", "窗口内在线净涨 ≥ 此值（投流来人时承接）", min=1, max=100000, **_IA),
    F("interaction.online.drain_min", "int", 5, "人数下滑门槛", "窗口内在线净减 ≥ 此值", min=1, max=100000, **_IA),
    F("interaction.online.milestone_step", "int", 50, "在线破整台阶", "在线每过这个整数台阶感谢一次（0 = 关）", min=0, max=1000000, **_IA),
    F("interaction.triggers", "dict", {}, "触发器覆盖", "按触发器名覆盖出厂表（policies/commerce.yaml）的字段：enabled / priority / cooldown_s / "
      "emotion / directive / lines / max_stale_s / use_llm。一般在「弹幕互动 → 触发器」表格里改", **_IA),
    F("interaction.intent.product_keywords", "list",
      ["多少钱", "价格", "价钱", "几块", "优惠", "券", "便宜", "尺码", "码数", "多大", "尺寸", "规格", "颜色", "材质", "成分", "配料",
       "保质期", "保修", "质保", "发货", "快递", "包邮", "运费", "售后", "退", "换货", "怎么用", "能用", "适合", "效果", "多久", "多重",
       "容量", "产地", "正品", "链接", "有货", "库存", "赠品", "区别", "哪个好", "介绍", "讲讲"],
      "产品提问关键词", "弹幕命中任一个就当产品提问（走知识库回答）", item_type="str", **_IA),
    F("interaction.intent.purchase_keywords", "list",
      ["要了", "拍了", "已拍", "下单", "已下单", "买了", "怎么拍", "怎么买", "几号链接", "上链接", "我要", "来一单", "抢到"],
      "购买意向关键词", "命中就引导去小黄车下单", item_type="str", **_IA),
    F("interaction.intent.spam_keywords", "list", ["加微信", "加v", "vx", "私我", "代刷", "兼职", "引流"],
      "垃圾弹幕关键词", "命中的弹幕不回应", item_type="str", **_IA),
    F("interaction.intent.llm_classify", "bool", False, "关键词没命中时用 LLM 判意图", "多一次 LLM 调用（计费），默认关", **_IA),
    F("interaction.memory.enabled", "bool", True, "跨场长期记忆", "记住发过弹幕/送过礼/关注过的观众（金主、熟客识别）；纯进场不建档", **_IA),
    F("interaction.memory.flush_interval_s", "float", 30.0, "长期记忆落盘间隔（秒）", min=1, max=600, **_IA),

    F("host.enabled", "bool", True, "主持人开关", "关掉后数字人不自动说话（人工插话除外）", **_PS),
    F("host.session_id", "str", "default", "主持的直播会话", "主持人只控制这一路会话", **_PS),
    F("host.interrupt_priority", "int", 50, "打断讲品的优先级", "新互动优先级 ≥ 此值时打断正在讲的讲品段，回应完再续讲", min=0, max=100,
      **_PS),
    F("host.manual_priority", "int", 90, "人工插话优先级", min=0, max=100, **_PS),
    F("host.pause_interaction", "bool", False, "暂停互动", "控制台上的开关：暂停后弹幕不回应（队列清空）", **_PS),
    F("host.pause_script", "bool", False, "暂停讲品", "控制台上的开关", **_PS),
    F("host.min_priority_during_script", "int", 10, "讲品时忽略的低优先级", "讲品进行中，优先级低于此值的互动（冷场话术等）直接跳过",
      min=0, max=100, **_PS),
    F("host.use_llm", "bool", True, "允许 LLM 写词", "触发器标了 use_llm 且 LLM 可用时才用；关掉则全部走模板", **_PS),
    F("host.compose_timeout_s", "float", 6.0, "写词超时（秒）", "LLM 超过这么久没给出第一句就用兜底话术", min=0.5, max=60, **_PS),
    F("host.reply_max_chars", "int", 40, "互动台词最大字数", min=10, max=200, **_PS),
    F("host.answer_max_chars", "int", 70, "产品问答最大字数", min=10, max=300, **_PS),
    F("host.speak_timeout_s", "float", 60.0, "单句播报超时（秒）", "没收到说完事件的兜底，防止主持人卡住", min=5, max=600, **_PS),
    F("persona.anchor_name", "str", "小美", "主播自称", **_PS),
    F("persona.shop_name", "str", "", "店铺名", **_PS),
    F("persona.prompt", "text", _read_policy("persona_commerce.md"), "人设提示词", "LLM 写词时的系统提示；{anchor} {shop_line} 会被替换", **_PS),
    F("persona.honorific", "dict", {"male": "哥", "female": "姐", "unknown": ""}, "称呼后缀",
      "昵称简称后面加的字：小王哥 / 李姐；未知性别默认不加", **_PS),
    F("persona.big_honorific", "dict", {"male": "大哥", "female": "大姐", "unknown": "家人"}, "尊称", "礼物答谢里的「大哥/大姐」", **_PS),

    F("compliance.enabled", "bool", True, "合规过滤", "所有要说出口的文本（模板、LLM、讲解稿、人工插话）都过一遍", **_CP),
    F("compliance.banned_words", "list",
      ["最好", "最佳", "最强", "最优", "最低价", "最便宜", "最划算", "全网最低", "史上最", "第一", "唯一", "顶级", "国家级", "世界级", "首选",
       "100%", "百分百", "绝对", "万能", "永久", "根治", "包治", "治愈", "药到病除", "特效", "神药", "稳赚", "保本", "躺赚", "零风险",
       "加微信", "加v", "加vx", "私信我", "抽奖", "返现", "返利"],
      "违禁词", "广告法极限词 + 平台禁语；命中有替换的换掉，没替换的删掉", item_type="str", **_CP),
    F("compliance.replacements", "dict",
      {"最好": "很好", "最佳": "很棒", "最强": "很强", "最优": "很优秀", "最低价": "实惠价", "最便宜": "很实惠", "最划算": "很划算",
       "全网最低": "很划算", "第一": "很受欢迎", "唯一": "少有", "顶级": "高品质", "首选": "好选择", "绝对": "真的", "万能": "多用途",
       "百分百": "", "100%": ""},
      "替换表", "违禁词 → 替换成的词（空字符串 = 删掉）", **_CP),
    F("compliance.allow_phrases", "list",
      ["第一时间", "第一次", "第一个", "第一步", "第一段", "第一种", "第一天", "第一眼", "第一批", "第一件", "第一口", "第一波",
       "第一轮", "第一期", "第一条", "第一点", "第一句", "最近", "最后", "最终", "最初"],
      "放行短语", "包含违禁词但意思正常的短语（序数词、时间词），不做替换；注意「第一名」「销量第一」不要加进来", item_type="str", **_CP),
    F("compliance.block_on_hit", "bool", False, "命中即整句丢弃", "开了之后命中违禁词的句子不说，改用兜底话术", **_CP),
    F("compliance.ai_disclosure", "str", "本直播间为 AI 数字人直播", "AI 标识文案", "场景页常驻显示（不能关闭，只能改文案）", **_CP),
]


# ============================================================================ 工具
def _index(fields_: list[Field]) -> dict[str, Field]:
    out = {}
    for f in fields_:
        if f.key in out:
            raise ValueError(f"schema 重复定义: {f.key}")
        if f.type not in TYPES:
            raise ValueError(f"{f.key}: 未知类型 {f.type}")
        if f.group not in GROUP_IDS:
            raise ValueError(f"{f.key}: 未知分组 {f.group}")
        out[f.key] = f
    return out


FIELD_MAP: dict[str, Field] = _index(FIELDS)


def get_field(key: str) -> Field | None:
    return FIELD_MAP.get(key)


def set_in(tree: dict, key: str, value) -> None:
    parts = key.split(".")
    node = tree
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = value


_MISSING = object()


def get_in(tree: dict, key: str, default=_MISSING):
    node = tree
    for p in key.split("."):
        if not isinstance(node, dict) or p not in node:
            if default is _MISSING:
                raise KeyError(key)
            return default
        node = node[p]
    return node


def has_in(tree: dict, key: str) -> bool:
    return get_in(tree, key, _MISSING) is not _MISSING


def defaults_tree() -> dict:
    """schema 默认值组装成嵌套 dict（即 live.config.DEFAULTS）。"""
    tree: dict = {}
    for f in FIELDS:
        set_in(tree, f.key, copy.deepcopy(f.default))
    return tree


def flatten(tree: dict, prefix: str = "") -> dict:
    """嵌套 dict -> {点路径: 值}，只展开到 schema 叶子（dict 类型的字段整体作为一个值）。"""
    out = {}
    for k, v in (tree or {}).items():
        key = f"{prefix}{k}"
        f = FIELD_MAP.get(key)
        if isinstance(v, dict) and (f is None or f.type != "dict"):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


class ValidationError(ValueError):
    pass


def _num(f: Field, v, kind):
    if isinstance(v, bool):
        raise ValidationError("应为数字")
    if isinstance(v, str):
        v = v.strip()
        try:
            v = float(v)
        except ValueError:
            raise ValidationError("应为数字") from None
    if not isinstance(v, (int, float)):
        raise ValidationError("应为数字")
    if kind is int:
        if float(v) != int(v):
            raise ValidationError("应为整数")
        v = int(v)
    else:
        v = float(v)
    if f.min is not None and v < f.min:
        raise ValidationError(f"不能小于 {f.min:g}")
    if f.max is not None and v > f.max:
        raise ValidationError(f"不能大于 {f.max:g}")
    return v


def _item(f: Field, v):
    t = f.item_type
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValidationError("列表元素应为整数")
        try:
            iv = int(float(v))
        except ValueError:
            raise ValidationError("列表元素应为整数") from None
        v = iv
    elif t == "float":
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValidationError("列表元素应为数字") from None
    elif t == "str":
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            raise ValidationError("列表元素应为文本")
        v = str(v)
    if f.item_choices and v not in f.item_choices:
        raise ValidationError(f"列表元素只能是 {'/'.join(map(str, f.item_choices))}")
    return v


def validate(f: Field, value):
    """校验并规范化一个值，返回规范化后的值；不合法抛 ValidationError（中文原因）。"""
    if value is None or (value == "" and f.nullable and f.type not in ("str", "text")):
        if f.nullable:
            return None
        raise ValidationError("不能为空")
    t = f.type
    if t == "int":
        return _num(f, value, int)
    if t == "float":
        return _num(f, value, float)
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            return value.lower() in ("true", "1", "yes", "on")
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValidationError("应为开/关")
    if t in ("str", "text"):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise ValidationError("应为文本")
        if f.nullable and value.strip() == "":
            return None
        if f.max is not None and len(value) > f.max:
            raise ValidationError(f"不能超过 {int(f.max)} 字")
        return value if t == "text" else value.strip()
    if t == "enum":
        if isinstance(value, bool):          # YAML 里 compile: true 这类
            value = "true" if value else "false"
        for c in f.choices:
            if value == c or (isinstance(value, str) and str(c) == value):
                return c
        raise ValidationError(f"只能是 {' / '.join(map(str, f.choices))}")
    if t == "list":
        if isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list):
            raise ValidationError("应为列表")
        items = [_item(f, v) for v in value]
        if f.min is not None and len(items) < f.min:
            raise ValidationError(f"至少 {int(f.min)} 项")
        if f.max is not None and len(items) > f.max:
            raise ValidationError(f"最多 {int(f.max)} 项")
        return items
    if t == "dict":
        if not isinstance(value, dict):
            raise ValidationError("应为对象（JSON）")
        return copy.deepcopy(value)
    raise ValidationError(f"未知类型 {t}")


def cross_check(get) -> dict:
    """跨字段校验：get(key) 返回保存后的有效值。返回 {key: 错误原因}。"""
    errors = {}
    if get("idle.mode") == "file" and not get("idle.file"):
        errors["idle.file"] = "空闲音频选了 file 模式，必须填空闲音频文件"
    return errors
