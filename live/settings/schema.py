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
    F("tts.engine", "enum", "edge", "TTS 引擎", "edge 免费在线 / http 自建服务 / mock 不联网合成音",
      choices=("edge", "http", "mock"), group="tts"),
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

    # ---- mock / offline ----
    F("mock.speed", "dict", {"lite": 0.3, "pro": 0.7}, "mock 生成速度", "生成一块耗时 = 块时长 × speed；>1 模拟显卡跟不上",
      apply=RESTART, **_ADV),
    F("offline.max_concurrent", "int", 1, "离线任务并发数", apply=RESTART, min=1, max=8, **_ADV),
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
