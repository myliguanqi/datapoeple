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
| 抖音弹幕接入（第二阶段） | 独立子进程接抖音直播间（只读），弹幕/礼物/点赞/进场/关注/分享/粉丝团/在线人数/贡献榜全字段解析（性别、财富等级、粉丝团、本场消费…）；连击礼物结算一次；断线退避重连；Web 上改房间号即热切换；模拟弹幕源与 JSONL 录制/回放 |
| 所有参数网页可改（第二阶段） | 「直播间设置」页按 schema 自动生成，标「即时生效 / 需重启」；导出/导入/历史回滚；写操作需口令 |

---

## 目录

```
live/                       服务层（Python 包）
  server.py                 FastAPI app 工厂 + 写操作鉴权中间件
  app_context.py            统一构造、注入所有服务（AppContext）
  api/                      HTTP / WebSocket 路由（system、sessions、offline、settings、console_ws）
  core/                     统一事件模型 events、事件总线 bus、实现注册表 registry、可注入时钟 clock
  settings/                 设置中心：schema（所有配置项的单一来源）、store（三层合并 + 热更新 + 版本回滚）、env（.env 密钥）
  feed.py                   控制台推送（事件总线 → /ws/console，点赞/进场节流）
  danmaku/                  弹幕接入：manager（子进程管理）、process（子进程入口）、mock、replay、recorder、cookie；
    douyin/                 抖音：parse.py（protobuf → 统一事件，纯函数 + 连击聚合）、adapter.py（接管 vendor fetcher）
  manager.py                会话管理
  session.py                直播会话：TTS 线程 / 生成线程（按需提前调度）/ 播放时钟线程
  speech_buffer.py          待播语音缓冲（按 seg_id 分段）
  idle.py                   空闲音频源（noise / zeros / file）
  sinks.py                  输出端接口 + WebSocket 推流（二进制帧协议定义在这里）
  offline.py                离线视频任务
  pool.py                   worker 进程池（每卡一个）、RPC、分卡调度
  audio_utils.py            ffmpeg 解码、响度归一化、淡入淡出
  config.py                 load_config()（DEFAULTS 由 settings/schema.py 生成）
  configs/live.yaml         正式配置（Windows + 双 4090）
  configs/mock.yaml         无模型调试配置
  tts/                      TTS 引擎：edge（默认）/ http（对接自建 TTS）/ mock；service.py 可热切换的统一入口
  worker/                   推理子进程
    process.py              子进程主循环（按优先级执行命令）
    flashhead_runner.py     真实 FlashHead 推理（lite/pro、多会话状态切换、模型切换接续）
    mock_runner.py          假推理（参考图微动 + 按音量画嘴）
    compat.py               Windows 兼容：xfuser 桩、torch.compile 自动检测
vendor/douyin_fetcher/      第三方 DouyinLiveWebFetcher（AGPL-3.0，原样复制不改；抖音改签名时整体替换）
web/                        网页客户端（原生 ES modules，无需构建，直接由服务端提供）
  index.html  app.js（外壳/路由）  api.js  ui.js  player.js  style.css
  pages/                    每页一个模块：console danmaku products media settings system offline
docs/live/
  ARCHITECTURE.md           架构、线程模型、调度与音画同步原理
  API.md                    REST / WebSocket 接口与二进制协议
  DEVELOPMENT.md            二次开发：加 TTS、加 RTMP/WebRTC 输出、多卡单路加速方案、待办
tests/live/
  test_e2e_mock.py          端到端测试（起 mock 服务后运行，33 项）
  test_runner_logic.py      真实推理后端逻辑测试（需 torch+CUDA，不需要权重，18 项）
  test_*.py                 pytest 单元/集成测试（python -m pytest tests/live -q -m "not e2e"）
live_server.py              启动脚本
start_live.bat / start_live_mock.bat
requirements_live.txt
.env.example                密钥模板（复制为 .env 填写；.env 不入库）
```

---

## 快速开始

### 1. 无模型调试（mock）

只需要服务层依赖（任意 Python ≥ 3.10），不需要显卡：

```bat
conda create -n flashhead python=3.10 -y
conda activate flashhead
pip install fastapi "uvicorn[standard]" python-multipart pydantic pyyaml "numpy<2" "opencv-python<4.12" loguru edge-tts requests python-dotenv
python live_server.py --config live/configs/mock.yaml
```

（第二阶段的知识库、弹幕、测试依赖在 `requirements_live.txt` 的「真实模型推理」一段之前，mock 调试只需要装前面这几段。）

浏览器打开 <http://127.0.0.1:8000/>，在「直播控制台」点「连接」：能看到参考图在微动（左上角显示 `MOCK LITE IDLE`），
在「人工插话」输入文字点「说」，嘴会按合成音张合，字幕同步显示。mock 模拟了两张卡：default 直播在 GPU0，离线任务在 GPU1。

网页按功能分页（左侧栏）：直播控制台 / 弹幕互动 / 产品与知识库 / 视频素材 / 直播间设置 / 数字人与系统 / 离线视频。
**「直播间设置」页可以改所有配置项**：标「即时生效」的保存后立刻生效（不用重启），标「需重启」的保存后提示重启；
支持导出/导入、历史版本回滚（最近 20 份）、恢复默认。Web 上的修改保存在 `live_data/settings.json`，优先级高于 yaml。

### 密钥与写操作口令（`.env`）

把 `.env.example` 复制为 `.env` 再填（`.env` 已在 `.gitignore`）。`ADMIN_TOKEN` 设了之后，所有写操作（说话、改设置、建会话……）
都要带口令（网页会弹框要一次）；**不设则只有本机能执行写操作**，局域网里的其他电脑只能看。
改了 `.env` 不用重启：「直播间设置 → 密钥与凭证 → 重新加载 .env」（或「数字人与系统」页右上角）。

- **火山 TTS**：填 `VOLC_TTS_API_KEY`、`VOLC_VOICE_TYPE`（声音复刻音色 S_xxx）、`VOLC_RESOURCE_ID`（seed-icl-2.0 等），`tts.engine: volc`。
  没填时自动回退 edge-tts（顶栏 TTS 徽章显示「回退」）。重复的句子命中合成缓存（`live_data/cache/tts/`）不再计费。
- **大模型**：填 `LLM_API_URL`（如 `https://api.deepseek.com/v1`）、`LLM_API_KEY`、`LLM_MODEL`。不填也能跑，台词全部走模板兜底。

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

### 3. 接抖音弹幕（只读）

1. 依赖：`requirements_live.txt` 里「抖音弹幕」一段（betterproto、websocket-client、PyExecJS、mini_racer）；**本机要装 Node.js**（`a_bogus.js` 走 PyExecJS 调 Node）。
2. 「弹幕互动」页：来源选「抖音直播间」，填房间号（`live.douyin.com/` 后面那串数字），点「连接」。改房间号再点连接即热切换。
3. **收礼物要登录 cookie**：游客模式抖音不推礼物。登录 live.douyin.com 后在浏览器开发者工具复制任意请求的 cURL（或请求头里的 cookie 行），
   在「弹幕互动 → 粘贴 cookie」或「直播间设置 → 密钥与凭证」粘贴。只保存在本机 `secrets/douyin_cookie.txt`（已 gitignore），网页只写不读，约 2 个月过期。
4. 没有直播间时：来源选「模拟弹幕」点连接（速率、各类比例、产品提问占比、性别比例、财富等级分布在设置里调），或在「模拟」页一键注入各类事件；
   打开「录制事件」会写 `live_data/events/*.jsonl`，可在「模拟」页回放。

只接收消息，不发弹幕、不点赞、不私信；只用于自己的直播间。抖音对 AI 数字人直播有标识等规范，开播前请查阅平台最新规则。

### 4. 纯画面模式 / OBS 推流

`http://127.0.0.1:8000/?view=1&session=default` 只显示画面（带字幕），打开即自动连接，断线自动重连。
可以直接在 OBS 里加「浏览器源」填这个地址（宽高 512×512，勾选「通过 OBS 控制音频」），
再由 OBS 推流到 B 站 / 视频号 / 快手，或用 OBS 虚拟摄像头接抖音直播伴侣。
URL 参数：`session=` 会话、`subs=0` 不显示字幕、`latency=0.3` 客户端缓冲秒数、`autoconnect=1`（完整界面也自动连接）。

> 普通浏览器要求用户点击后才能出声，纯画面模式下点一下页面即可；OBS 浏览器源允许自动播放，不需要点。

### 5. 用代码接入

```python
import requests
requests.post("http://127.0.0.1:8000/api/sessions/default/speak",
              json={"text": "欢迎来到直播间！", "interrupt": False})
```

画面接收见 `docs/live/API.md`（WebSocket 二进制协议，附 Python 接收示例）。

---

## 关键配置（`live/configs/live.yaml`，完整列表、范围与说明见 `live/settings/schema.py` 或网页「直播间设置」）

优先级：schema 默认值 < yaml < 网页修改（`live_data/settings.json`）< 启动参数（`--port` 等）。

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
| `tts.engine` | volc | volc（火山声音复刻，`.env` 配凭证；没配时自动按 `tts.fallback` 回退 edge → mock）/ edge / http / mock |
| `tts.speed` / `tts.cache.*` / `tts.daily_max_chars` | 1.0 / 开 / 0 | 默认语速；合成缓存（重复句不再请求）；计费 TTS 每日字数上限（0 = 不限） |
| `llm.*` | | 温度、max_tokens、流式、防复读条数、每日调用上限；地址/密钥/模型在 `.env` 的 `LLM_API_URL` / `LLM_API_KEY` / `LLM_MODEL` |

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

- ✅ 第二阶段 P3（火山 TTS + 缓存、LLM 客户端）：pytest 26 项（假 SSE 响应：正常/多块/错误码/空音频/非 JSON 行、新旧控制台鉴权头、
  speech_rate 边界、24k→16k 重采样、回退链与单句回退、每日字数上限落盘、缓存命中/换音色失效/重启后磁盘命中/失败不缓存；
  LLM URL 规范化、非流式/流式（忽略推理内容）/按句切分、HTTP 错误/超时/空回复、无 key 不可用、.env 重载后可用、每日调用上限）。**没有访问任何真实计费接口。**
- ⚠️ 火山 TTS 与 LLM 没有用真实凭证跑过（你说在另一个项目实测过）：首次上线请跑 `python -m live.tts.volc "测试一句"`（会计费）
  确认 16k 采样率可用、音色正确；LLM 在「数字人与系统」页看可用性与首字耗时。
- ✅ 第二阶段 P2（弹幕接入）：pytest 30 项（抖音 13 类消息全字段解析含隐私字段不提取、"1.2万"人数解析、自定义性别映射、连击聚合 5 例、
  子进程启动/崩溃自动重启/改设置 10 秒内热切换/停止无残留、抖音源在子进程里导入 vendor 成功、录制→回放顺序与倍速、接口与 /ws/console 节流）；
  Edge 无头验证弹幕页与控制台弹幕流（模拟源 + 快捷注入，标签显示 ♂/♀、💎财富等级、🎫粉丝团）。
- ⚠️ **抖音真实直播间未实测**（等你确认平台规则、提供登录 cookie 后再连）：要核对字段表「实测」列、性别枚举是否反、连击推送规律
  （combo_count / repeat_count / group_count 含义）、关注与分享的区分、带货房间商品消息的 method 名。方法：连自己的直播间，打开「协议探针」和「录制事件」。
- ✅ 第二阶段 P1（设置中心、事件总线、接口拆分、写操作鉴权、新网页外壳）：pytest 36 项通过；重构后原有 e2e 33 项仍全部通过；
  Edge 无头浏览器验证：各页面在 1366×768 与窄屏下无横向溢出、无 JS 报错；网页上把 `speech.target_rms_db` 从 -30 改到 -14，
  下一句播报实测响度从 -32.5 dB 变为 -16.5 dB（即时生效），改 `gpu.devices` 显示「已保存，待重启」。
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
- [第二阶段任务书（进行中）：弹幕互动 + 火山 TTS + LLM + 知识库 + 视频场景 + Web 设置热更新](docs/tasks/phase2-live-commerce/README.md)
