# 二次开发指南

## 代码地图（按"想改什么"找文件）

| 想改的 | 文件 |
|---|---|
| 新增/修改配置项 | `live/config.py` 的 `DEFAULTS`（先加在这里，带注释），再在 `live/configs/*.yaml` 覆盖 |
| HTTP / WebSocket 接口 | `live/server.py` |
| 直播调度、空闲/说话、打断、事件 | `live/session.py` |
| 空闲音频策略 | `live/idle.py` |
| TTS | `live/tts/`（`create_tts` 里注册） |
| 输出端（RTMP、WebRTC、虚拟摄像头……） | `live/sinks.py` 的 `FrameSink` |
| 真实模型推理 | `live/worker/flashhead_runner.py` |
| 显卡调度 / 多进程 RPC | `live/pool.py`、`live/worker/process.py` |
| 离线视频 | `live/offline.py` |
| 网页 | `web/app.js`（界面）、`web/player.js`（播放器，可单独复用） |

约定：
- **主进程不 import torch**。需要 GPU 的逻辑全部放进 Runner，经 RPC 调用。
- Runner 新增可远程调用的方法，要加到 `live/worker/__init__.py` 的 `RUNNER_METHODS` 白名单，MockRunner 也要实现一份。
- 播放线程里调用的 `FrameSink.on_frame` 必须很快返回，耗时操作自己开线程。
- 所有相对路径都相对项目根目录（`config._normalize` 统一转成绝对路径）。

## 测试

```bat
:: 1) 端到端（mock，不需要模型/显卡）：先起服务，再跑测试
python live_server.py --config live/configs/mock.yaml
python tests/live/test_e2e_mock.py          :: 依赖 requests websockets；33 项

:: 2) 真实推理后端的逻辑测试（需要 torch+CUDA+官方依赖，不需要权重）
python tests/live/test_runner_logic.py      :: 18 项
```

`test_runner_logic.py` 用官方 `FlashHeadPipeline` 的 `prepare_params / generate` 原代码，只把 DiT、VAE、wav2vec
换成微型假模块，验证：Windows 无 xfuser 时能导入、块大小、多会话状态互不干扰（逐位一致）、
lite→pro 切换的运动帧接续、换形象、卸载保护。它**不能**验证真实权重的加载、画质和速度。

**拿到模型后第一件事**：
1. `python live_server.py --config live/configs/live.yaml`，看两个 worker 是否 `ready`、`attention` 和 `compile` 是什么；
2. 网页连接 default，看右上角 RTF（Lite 应明显 < 1）；
3. 切 Pro，看 RTF；> 1 时试 `models.sample_steps.pro: 2`；
4. 对比空闲 `idle.mode` 的 noise / zeros / file 三种效果，定下默认值。

调试技巧：
- `mock.speed` 设成 > 1（如 `{lite: 1.3}`）可以模拟显卡跟不上，观察补帧、warning 事件和客户端表现；
- `models.quiet_pipeline_logs: false` 打开官方 pipeline 每步耗时的打印；
- FastAPI 自带调试页面 `http://127.0.0.1:8000/docs`。

## 扩展：新增 TTS 引擎

```python
# live/tts/my_tts.py
import numpy as np
from live.tts.base import TTSEngine
from live.audio_utils import decode_audio_bytes

class MyTTS(TTSEngine):
    name = "my"
    def synthesize(self, text: str, voice: str | None = None) -> np.ndarray:
        audio_bytes = call_my_service(text, voice or self.default_voice)   # 任意格式
        return decode_audio_bytes(audio_bytes, sr=self.sr, ffmpeg=self.ffmpeg)  # -> float32 16k
```

再在 `live/tts/__init__.py` 的 `create_tts` 里加一个分支，配置 `tts.engine: my`。
大多数自建服务（GPT-SoVITS、CosyVoice、IndexTTS 的 HTTP 接口）不用写代码，直接配 `tts.engine: http`（见 `live/tts/http_tts.py` 顶部的例子）。

本地 TTS 模型如果放在主进程里跑，会和 FastAPI 抢 CPU/GIL；建议单独起服务、用 http 引擎对接，或者放到第二张卡上。

## 扩展：新增输出端（RTMP / WebRTC / 虚拟摄像头）

实现 `FrameSink`，在会话创建后 `session.add_sink(sink)`（例如在 `SessionManager.create` 里按配置挂上）。
`on_frame` 每 40ms 被调用一次，`frame.image` 是 RGB uint8，`frame.audio` 是 640 个 int16 采样，二者是同一时刻的。

### RTMP 推流（未实现，参考草稿）

推荐用 PyAV 在进程内封装，音视频时间戳由帧序号直接算出，天然同步（避免 Windows 上给 ffmpeg 同时喂两路管道的麻烦）：

```python
import queue, threading, av, numpy as np
from live.sinks import FrameSink

class RtmpSink(FrameSink):
    """草稿，未测试。url 例如 rtmp://live-push.example.com/live/xxxx"""
    def __init__(self, url, fps=25, sr=16000, width=512, height=512):
        self.q = queue.Queue(maxsize=100)
        self.args = (url, fps, sr, width, height)
        threading.Thread(target=self._run, daemon=True).start()

    def on_frame(self, frame):
        try:
            self.q.put_nowait(frame)
        except queue.Full:
            pass                      # 推流跟不上就丢，绝不阻塞播放线程

    def _run(self):
        url, fps, sr, w, h = self.args
        out = av.open(url, mode="w", format="flv")
        vs = out.add_stream("libx264", rate=fps)
        vs.width, vs.height, vs.pix_fmt = w, h, "yuv420p"
        vs.options = {"preset": "veryfast", "tune": "zerolatency", "g": str(fps * 2)}
        as_ = out.add_stream("aac", rate=44100)
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=44100)
        while True:
            f = self.q.get()
            vf = av.VideoFrame.from_ndarray(f.image, format="rgb24")
            vf.pts = f.seq
            for p in vs.encode(vf):
                out.mux(p)
            af = av.AudioFrame.from_ndarray(f.audio.reshape(1, -1), format="s16", layout="mono")
            af.sample_rate, af.pts = sr, f.seq * (sr // fps)
            for rf in resampler.resample(af):
                for p in as_.encode(rf):
                    out.mux(p)
```

各平台（B 站、视频号、快手……）的推流地址在各自的直播后台获取；抖音一般需要用直播伴侣，可以走"虚拟摄像头"方案。

### WebRTC（未实现，思路）

用 aiortc：自定义 `VideoStreamTrack` / `AudioStreamTrack`，`recv()` 从一个由 `on_frame` 填充的 asyncio 队列里取帧，
`pts = seq`（time_base 1/fps）、音频 `pts = seq*640`（time_base 1/16000）。信令可以加一个 `POST /api/sessions/{sid}/webrtc/offer`。
好处是浏览器端延迟更低（不需要自己做抖动缓冲），能穿透 NAT；坏处是依赖多、调试复杂。

### 虚拟摄像头（未实现，思路）

零代码方案：OBS「浏览器源」加载 `http://127.0.0.1:8000/?view=1&session=default`（网页纯画面模式，音画已同步），
再开 OBS 虚拟摄像头给直播伴侣用。下面是不经过 OBS 的方案：

`pyvirtualcam` 输出画面给 OBS / 直播伴侣，声音用 `sounddevice` 播到 VB-Cable 虚拟声卡。
注意两路是分开的，需要在 OBS 里微调音频延迟对齐口型。

## 扩展：接大模型自动回复

```
弹幕/评论 → LLM 生成回复文本 → POST /speak（interrupt 看业务）
                                  ↑
监听 WebSocket 的 speak_done 事件，知道上一句什么时候说完，再决定下一条
```

`speak` 支持很长的文本（会自动切句、边合成边播），LLM 流式输出时也可以按句子多次调用 `speak`（不打断，自动排队）。

## 多卡单路加速（sequence_parallel，未实现）

需求：**同一路**画面用两张卡一起算（主要为了让 Pro 达到实时）。现状和可选方案：

| 方案 | 平台 | 结论 |
|---|---|---|
| 官方 USP（xfuser 序列并行，torchrun 多进程） | Linux + NCCL | 官方已支持（`inference_script_multi_gpu_pro.sh`），官方数据为两张 5090 + SageAttention 可让 Pro 实时；这是最推荐的路线 |
| 同上，Windows | Windows | NCCL 不支持 Windows；gloo 后端对 GPU 张量的集合通信支持很有限，xfuser 也是按 NCCL 实现的，实际不可用 |
| 单进程手写双卡序列并行（每层 attention 交换 K/V） | Windows | 理论可行，但 4090 不支持显卡间 P2P/NVLink，每步每层的 K/V 都要经 PCIe + 内存中转，通信开销可能吃掉大部分收益；需要改官方模型代码，工作量大、收益不确定 |
| DiT 与 VAE 分放两张卡做流水线 | Windows | 自回归依赖：下一块的运动帧来自上一块解码再编码的结果，两张卡无法重叠计算，基本没有收益 |

在 Linux 上接入官方 USP 的实现思路（留给后续）：
1. 新增 `SequenceParallelWorker`：用 `torchrun --nproc_per_node=2` 启动一组进程，按官方 `get_pipeline(world_size=2, ...)` 初始化；
2. rank0 负责和主进程通信（沿用现有的 cmd_q/res_q 协议，或改成 socket），每条命令用 `dist.broadcast_object_list`
   广播给 rank1（参考 `SoulX-LiveAct/demo.py` 的 `control_loop_rank0 / control_loop_rank_other`）；
   长时间空闲时要定期发心跳，避免 NCCL 超时；
3. 两个 rank 都执行 `generate`，只有 rank0 把帧返回主进程；
4. 在 `WorkerPool` 里把这一组进程当成一个 worker（占两张卡），`gpu.mode: sequence_parallel` 时启用。

## 待办

- [ ] worker 进程崩溃后自动重启，并重建它上面的会话
- [ ] 未预加载的模型改为后台加载，加载期间直播不停（目前切换时同步加载会补帧）
- [ ] RTMP / WebRTC / 虚拟摄像头输出端
- [ ] 接口鉴权（目前任何能访问端口的人都能控制）、HTTPS
- [ ] 离线任务结果持久化与定期清理（目前在内存里，`live_data/offline/` 文件不会自动删除）
- [ ] 多形象预加载与秒切（官方 `prepare_params` 支持目录 + `reset_person_name`）
- [ ] 基于真实模型调参：`idle` 策略、`speech.target_rms_db`、`live.safety_factor`
- [ ] Linux 下接入官方 USP，实现 `gpu.mode: sequence_parallel`
