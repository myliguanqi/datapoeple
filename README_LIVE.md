# SoulX-FlashHead 实时直播框架

在官方 SoulX-FlashHead（1.3B，Lite / Pro）之上加的一层**实时数字人直播服务**，不修改官方代码。

| 需求 | 实现 |
|---|---|
| 实时接收画面 | WebSocket 推流（JPEG + 40ms PCM 一帧一包，带序号/时间戳），网页客户端音画同步播放；`/?view=1` 纯画面模式可直接给 OBS 用 |
| 没有 TTS 时画面不能静止 | 模型一直在跑：没有语音就喂"空闲音频"（默认 -55dB 底噪，可换真人呼吸声），持续生成眨眼、微动 |
| 有 TTS 就实时讲话 | 文本 → 切句 → TTS → 写入语音缓冲；下一块直接用语音驱动，空闲 ↔ 说话无缝衔接 |
| 画面不能太僵硬 | 块之间用运动帧接续；空闲底噪替代纯静音；TTS 响度归一化；句首尾淡入淡出（可调参数见下） |
| Lite / Pro 切换 | 直播中随时切，旧模型最后几帧重新编码成新模型的运动帧，动作不跳 |
| 图片 + TTS 生成视频 | 离线任务接口：图片 + 文本（或音频）→ mp4，可异步轮询，也可同步直接返回文件 |
| 双显卡 | 每张卡一个 worker 进程：直播用 0 号卡、离线用 1 号卡，或两张卡各跑一路直播。**单路画面两卡合算（序列并行）在 Windows 上未实现，见下文** |
| 先不下载模型也能开发 | `mock` 后端：不需要模型、显卡、torch，整条链路（时钟、协议、网页、离线任务、多卡调度）都能跑 |

---

## 目录

```
live/                       服务层（Python 包）
  server.py                 FastAPI 入口：REST + WebSocket + 静态网页
  manager.py                会话管理
  session.py                直播会话：TTS 线程 / 生成线程（按需提前调度）/ 播放时钟线程
  speech_buffer.py          待播语音缓冲（按 seg_id 分段）
  idle.py                   空闲音频源（noise / zeros / file）
  sinks.py                  输出端接口 + WebSocket 推流（二进制帧协议定义在这里）
  offline.py                离线视频任务
  pool.py                   worker 进程池（每卡一个）、RPC、分卡调度
  audio_utils.py            ffmpeg 解码、响度归一化、淡入淡出
  config.py                 配置项与默认值（所有可调参数都在这里有注释）
  configs/live.yaml         正式配置（Windows + 双 4090）
  configs/mock.yaml         无模型调试配置
  tts/                      TTS 引擎：edge（默认）/ http（对接自建 TTS）/ mock
  worker/                   推理子进程
    process.py              子进程主循环（按优先级执行命令）
    flashhead_runner.py     真实 FlashHead 推理（lite/pro、多会话状态切换、模型切换接续）
    mock_runner.py          假推理（参考图微动 + 按音量画嘴）
    compat.py               Windows 兼容：xfuser 桩、torch.compile 自动检测
web/                        网页客户端（无需构建，直接由服务端提供）
  index.html  app.js  player.js  style.css
docs/live/
  ARCHITECTURE.md           架构、线程模型、调度与音画同步原理
  API.md                    REST / WebSocket 接口与二进制协议
  DEVELOPMENT.md            二次开发：加 TTS、加 RTMP/WebRTC 输出、多卡单路加速方案、待办
tests/live/
  test_e2e_mock.py          端到端测试（起 mock 服务后运行，33 项）
  test_runner_logic.py      真实推理后端逻辑测试（需 torch+CUDA，不需要权重，18 项）
live_server.py              启动脚本
start_live.bat / start_live_mock.bat
requirements_live.txt
```

---

## 快速开始

### 1. 无模型调试（mock）

只需要服务层依赖（任意 Python ≥ 3.10），不需要显卡：

```bat
pip install fastapi "uvicorn[standard]" python-multipart pyyaml numpy opencv-python loguru edge-tts
python live_server.py --config live/configs/mock.yaml
```

浏览器打开 <http://127.0.0.1:8000/>，点「连接」：能看到参考图在微动（左上角显示 `MOCK LITE IDLE`），
输入文字点「说话」，嘴会按合成音张合，字幕同步显示。mock 模拟了两张卡：default 直播在 GPU0，离线任务在 GPU1。

> mock 配置默认用 `tts.engine: mock`（不联网的合成音）。想听真人声，把 `live/configs/mock.yaml` 里改成 `edge`。
> 推送音频、离线视频、edge-tts 需要 `ffmpeg` 在 PATH 里。

### 2. 正式运行（Windows + 双 RTX 4090）

```bat
conda create -n flashhead python=3.10 -y
conda activate flashhead
pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements_live.txt
```

**不要**在 Windows 上 `pip install -r requirements.txt`：其中的 `xfuser`、`nvidia-nccl-cu12` 是 Linux 多卡用的，
装不上也不需要。框架会自动给 xfuser 注入单卡桩模块（`live/worker/compat.py`）。

可选加速（都是可选的，不装也能跑，只是慢一些）：

| 组件 | 作用 | Windows 安装 |
|---|---|---|
| triton-windows | 启用 `torch.compile`（`models.compile: auto` 自动检测） | `pip install "triton-windows<3.4"`（版本需与 torch 2.7 对应） |
| SageAttention | 注意力加速（官方 Pro 实时依赖它） | 找对应 Python/torch/CUDA 版本的社区 Windows wheel |
| flash-attn 2 | 注意力加速 | 同上，社区 Windows wheel |

都没装时注意力用 PyTorch 自带的 SDPA，`/api/system` 里每个 worker 的 `attention` 字段会显示实际用的实现。

下载模型（国内先 `set HF_ENDPOINT=https://hf-mirror.com`）：

```bat
pip install "huggingface_hub[cli]"
huggingface-cli download Soul-AILab/SoulX-FlashHead-1_3B --local-dir models/SoulX-FlashHead-1_3B
huggingface-cli download facebook/wav2vec2-base-960h --local-dir models/wav2vec2-base-960h
```

启动：

```bat
start_live.bat
:: 或
python live_server.py --config live/configs/live.yaml
```

启动后两个 worker 逐个加载 Lite + Pro 并预热（开了 compile 时第一次要几分钟），
在 `/api/system` 或网页「系统」卡片里能看到状态，加载完自动创建 `default` 直播会话。

### 3. 纯画面模式 / OBS 推流

`http://127.0.0.1:8000/?view=1&session=default` 只显示画面（带字幕），打开即自动连接，断线自动重连。
可以直接在 OBS 里加「浏览器源」填这个地址（宽高 512×512，勾选「通过 OBS 控制音频」），
再由 OBS 推流到 B 站 / 视频号 / 快手，或用 OBS 虚拟摄像头接抖音直播伴侣。
URL 参数：`session=` 会话、`subs=0` 不显示字幕、`latency=0.3` 客户端缓冲秒数、`autoconnect=1`（完整界面也自动连接）。

> 普通浏览器要求用户点击后才能出声，纯画面模式下点一下页面即可；OBS 浏览器源允许自动播放，不需要点。

### 4. 用代码接入

```python
import requests
requests.post("http://127.0.0.1:8000/api/sessions/default/speak",
              json={"text": "欢迎来到直播间！", "interrupt": False})
```

画面接收见 `docs/live/API.md`（WebSocket 二进制协议，附 Python 接收示例）。

---

## 关键配置（`live/configs/live.yaml`，完整列表与注释见 `live/config.py`）

| 配置 | 默认 | 说明 |
|---|---|---|
| `models.preload` | `[lite, pro]` | 启动时加载的模型。两个都加载才能秒切；没预加载的模型切换时现加载，期间画面会停住 |
| `models.sample_steps.pro` | 4 | Pro 单卡跟不上实时可改 2（官方蒸馏模型支持 2 步，速度未实测） |
| `models.compile` | auto | 有 triton 就开 torch.compile |
| `gpu.devices` | `[0, 1]` | 每张卡一个 worker 进程 |
| `gpu.live_devices` / `offline_devices` | `[0]` / `[1]` | 直播和离线分卡，互不抢算力 |
| `gpu.stagger_start` | true | worker 逐个加载，降低内存峰值（内存 12G 必须开） |
| `live.safety_factor` / `margin_s` | 1.3 / 0.15 | 提前生成量 = 生成耗时 × safety_factor + margin。越小延迟越低，越容易补帧 |
| `live.max_ahead_chunks` | 2 | 最多领先播放几个块 |
| `idle.mode` / `noise_db` / `file` | noise / -55 | 空闲音频。画面发僵先调这里 |
| `speech.target_rms_db` | -20 | TTS 响度归一化目标。嘴张得太小就调大（如 -16），太夸张就调小 |
| `tts.engine` | edge | edge / http / mock |

### 画面僵硬怎么调

1. **空闲**：先试 `idle.mode: noise` 的 `noise_db` 在 -65 ~ -45 之间；效果最好的通常是录一段 10~30 秒
   真人不说话时的环境声/呼吸声，`idle.mode: file` 循环播放。
2. **说话**：`speech.target_rms_db` 控制嘴型幅度；`speech.sentence_gap_ms` 控制句间停顿（太短会显得一直在动嘴）。
3. **画质与动作**：Pro 比 Lite 自然，但单张 4090 跟不上实时（见下表）；可以直播用 Lite、离线用 Pro。
4. **参考图**：正脸、嘴自然闭合、光线均匀的半身/头肩照效果最好；人脸太小可勾「人脸裁剪」。

---

## 性能与双卡说明

官方数据（README）：Lite 单卡 4090 **96 FPS**；Pro 单卡 4090 **10.8 FPS**，两张 5090 + SageAttention 才能实时（25 FPS）。
直播要求 ≥ 25 FPS，所以：

| 场景 | 双 4090 / Windows 上 |
|---|---|
| Lite 直播 | 单卡实时，有富余（官方说 4090 可 3 路并发） |
| Pro 直播 | 单卡约 10.8 FPS，**跟不上实时**，画面会周期性补帧卡顿。可试 `sample_steps.pro: 2` |
| Pro 离线视频 | 没问题（不要求实时） |
| 两路 Lite 直播 | 每卡一路（网页「系统」→ 新建会话，指定 GPU） |

当前双卡模式是 **multi_instance**：两张卡同时工作，但各自负责不同的会话或任务。
**单路画面用两张卡一起算**（官方 `torchrun` + xfuser 序列并行）依赖 NCCL，Windows 不支持，
`gpu.mode: sequence_parallel` 目前只是占位（会回退为 multi_instance 并打印警告）。
可行方案和工作量分析见 `docs/live/DEVELOPMENT.md`「多卡单路加速」。

网页右上角的 **RTF**（生成耗时 / 块时长）< 1 才能实时；> 1 时服务端会推 warning 事件。

---

## 已验证 / 未验证

- ✅ mock 后端端到端测试 33 项全部通过：空闲 25fps 不断流、说话/字幕/说完事件、打断、Lite↔Pro 切换不断流、
  换形象、推送音频、WebSocket 控制消息、离线任务（异步 + 同步，mp4 音视频等长）、离线任务调度到第二张卡、第二路直播。
- ✅ 真实浏览器（Edge）里网页客户端出画、出字幕、说话状态正确，完整界面和纯画面模式都验证过；客户端 25fps、无补帧/丢包。
- ✅ edge-tts 实测可用（首句约 1.5s）。
- ✅ 在 Windows（本机 RTX 4070，torch 2.11）上：不装 xfuser 能导入官方模型代码；
  runner 用"官方 prepare_params/generate + 微型假模型"跑通 18 项逻辑测试（`tests/live/test_runner_logic.py`）。
- ⚠️ **真实模型推理（`flashhead_runner.py`）没有用真权重跑过**（按要求没有下载模型）。
  第一次接真模型时请重点看：权重加载、`torch.compile` 在 Windows 上是否可用、Lite/Pro 实际 RTF、空闲动作是否自然，
  步骤见 `docs/live/DEVELOPMENT.md`「拿到模型后第一件事」。

## 文档

- [架构与原理](docs/live/ARCHITECTURE.md)
- [接口文档](docs/live/API.md)
- [二次开发指南](docs/live/DEVELOPMENT.md)
