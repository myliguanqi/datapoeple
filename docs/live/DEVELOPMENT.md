# 二次开发指南

## 代码地图（按"想改什么"找文件）

| 想改的 | 文件 |
|---|---|
| 新增/修改配置项 | `live/settings/schema.py`（加一行 `F(key, type, default, label, help, group=, apply=hot\|restart, min/max/choices)`，`DEFAULTS` 与 Web 设置表单都由它生成），再在 `live/configs/*.yaml` 覆盖 |
| 设置的合并 / 校验 / 持久化 / 回滚 | `live/settings/store.py`（SettingsCenter）；`.env` 密钥 `live/settings/env.py` |
| 服务的构造与注入 | `live/app_context.py`（AppContext）|
| HTTP / WebSocket 接口 | `live/api/*.py`（按功能分文件：system、sessions、offline、settings、danmaku、interaction、host、knowledge、products、media、console_ws），`live/server.py` 只是 app 工厂 + 写操作鉴权中间件 |
| 事件总线 / 统一事件模型 / 注册表 / 可注入时钟 | `live/core/bus.py`、`events.py`、`registry.py`、`clock.py` |
| 控制台推送（/ws/console）与节流 | `live/feed.py` |
| 弹幕接入 | `live/danmaku/`：`base.py` 接口与注册、`manager.py` 子进程管理、`process.py` 子进程入口、`mock.py`、`replay.py`、`recorder.py`、`cookie.py`；抖音 `douyin/parse.py`（纯函数 + 连击聚合）、`douyin/adapter.py`（接管 vendor fetcher）|
| 抖音 fetcher（第三方，原样复制，**不改**） | `vendor/douyin_fetcher/`；抖音改签名连不上时用上游 DouyinLiveWebFetcher 同名文件整体覆盖 |
| 互动规则（触发器表、话术、人设） | `live/interaction/policies/commerce.yaml`、`persona_commerce.md`（出厂值；Web 上改的写进设置覆盖） |
| 导演 / 局面 / 长期记忆 / 队列 / 意图 / 合规 | `live/interaction/director.py`、`state.py`、`memory.py`、`queue.py`、`intent.py`、`compliance.py`；`engine.py` 串起来 |
| 主持人（谁先说、写词、打断讲品、人工插话） | `live/host/host.py`、`composer.py`、`session_tap.py` |
| 知识库（解析、切块、检索、资料够不够、摘句） | `live/knowledge/`：`loaders/`（md/html/pdf）、`chunker.py`、`text.py`（分词、查询构造、覆盖度、摘句）、`index/`（fts / bm25 / embedding + hybrid）、`store.py`（kb.sqlite）、`service.py`（KnowledgeBase：入库、检索、retrieve 钩子、回答测试）；注册表在 `__init__.py` |
| 视频素材、直播场景（布局、产品卡片、视频播放、讲品让路） | `live/media/store.py`（MediaStore：ffprobe、封面、media.json）、`live/media/scene.py`（SceneManager）；场景页 `web/scene.html` + `web/scene.js` |
| 产品库、讲品轮播、讲解稿生成 | `live/products/`：`models.py`、`store.py`（products.json）、`script.py`（ProductScript）、`generator.py`；提示词 `live/interaction/policies/rag_prompt.md`、`script_prompt.md`（出厂值，Web 上改 `persona.rag_prompt` / `persona.script_prompt`） |
| 直播调度、空闲/说话、打断、追加式请求、事件 | `live/session.py` |
| 空闲音频策略 | `live/idle.py` |
| TTS | `live/tts/`：引擎（edge / http / mock / volc 火山）在 `__init__.py` 的 `TTS_ENGINES` 注册；`service.py` 回退链 + 每日上限 + 热切换；`cache.py` 合成缓存 |
| LLM | `live/llm/`：`base.py` 接口、`openai_compat.py`、`mock.py`、`__init__.py` 的 `LLM_PROVIDERS` 注册、`service.py`（统计 / 上限 / 防复读）、`text.py`（流式按句切分） |
| 每日计费用量 | `live/core/usage.py`（`live_data/usage.json`） |
| 输出端（RTMP、WebRTC、虚拟摄像头……） | `live/sinks.py` 的 `FrameSink` |
| 真实模型推理 | `live/worker/flashhead_runner.py` |
| 显卡调度 / 多进程 RPC | `live/pool.py`、`live/worker/process.py` |
| 离线视频 | `live/offline.py` |
| 网页 | `web/app.js`（外壳：路由、顶栏徽章、口令、/ws/console）、`web/api.js`（fetch 封装）、`web/ui.js`（公共组件）、`web/pages/*.js`（每页一个模块）、`web/player.js`（播放器，可单独复用）。原生 ES modules，无构建、无 CDN |

约定：
- **主进程不 import torch**。需要 GPU 的逻辑全部放进 Runner，经 RPC 调用。
- 模块之间只通过**事件总线**、**构造时注入的接口**、**HTTP API** 通信，不互相 import 内部实现或全局变量。
- 密钥只从 `live/settings/env.py` 的 `Env` 读，模块不直接读 `os.environ`；日志里不打印密钥（`env.mask()` 只给长度）。
- 每类能力 = 接口（`base.py`）+ 实现 + 注册（`live/core/registry.py` 的 `Registry`），新增实现不改调用方。
- Runner 新增可远程调用的方法，要加到 `live/worker/__init__.py` 的 `RUNNER_METHODS` 白名单，MockRunner 也要实现一份。
- 播放线程里调用的 `FrameSink.on_frame` 必须很快返回，耗时操作自己开线程。
- 所有相对路径都相对项目根目录（`config._normalize` 统一转成绝对路径）。

## 测试

```bat
:: 0) 单元 / 集成测试（pytest，不起服务、不起 worker 进程、不调用任何计费接口）
python -m pytest tests/live -q -m "not e2e"

:: 1) 端到端（mock，不需要模型/显卡）：先起服务，再跑测试
python live_server.py --config live/configs/mock.yaml
python tests/live/test_e2e_mock.py          :: 依赖 requests websockets；33 项

:: 1b) 带货全链路（同样先起 mock 服务）：模拟弹幕 → 导演 → 主持人 → 播报顺序 / 人工插话 / 暂停
python tests/live/test_e2e_commerce.py      :: 16 项
:: 1c) 讲品 + 知识库（同样先起 mock 服务）：上传样例资料 → 回答测试 → 生成并确认讲解稿 → 讲品 → 产品提问打断 → 断点续讲 → 跳段
python tests/live/test_e2e_products.py      :: 15 项
:: 1d) 视频素材 + 直播场景（同样先起 mock 服务，需要 ffmpeg）：上传 → 画中画原声播放（讲品让路）→ 场景页回传播完 → 续讲 → 静音全屏按时长结束
python tests/live/test_e2e_scene.py         :: 15 项

:: 2) 真实推理后端的逻辑测试（需要 torch+CUDA+官方依赖，不需要权重）
python tests/live/test_runner_logic.py      :: 18 项
```

pytest 测试放 `tests/live/test_*.py`；夹具在 `tests/live/conftest.py`（`make_client()` 起一个不带 worker 进程的 FastAPI TestClient，
可指定来源 IP 和 `.env` 内容）。原有的两个脚本和 `test_e2e_*.py` 不让 pytest 收集，按上面的命令单独跑。
时间相关逻辑用 `live/core/clock.py` 的 `FakeClock` 注入，不要 `sleep`。主持人相关测试用 `tests/live/fakes.py` 的假会话（`FakeSession`）。
知识库测试夹具在 `tests/fixtures/kb/`：`product_a.md`（保温杯）、`product_b.html`（T 恤，带噪声标签）、`policy.pdf`（两页售后政策，
由同目录 `make_fixtures.py` 不依赖第三方库生成），内容都是自造的。视频测试（`test_p6_media.py`、`test_e2e_scene.py`）用 ffmpeg 现场生成
几秒的 testsrc 视频，不入库；没有 ffmpeg 时 P6 的 pytest 跳过。`.gitattributes` 把 pdf/mp4/图片标成二进制（`core.autocrlf` 会改坏没有 NUL 字节的 PDF）。

页面走查：Edge 无头（`msedge --headless=new --remote-debugging-port=9222 --user-data-dir=<临时目录>`）打开 `http://127.0.0.1:8000/`，
用 DevTools 协议执行 `tests/live/page_sweep.js` 里的函数（chrome-devtools MCP 的 `evaluate_script(sourcePath=…)` 即可），
1366×768 和设备模拟 390 宽各跑一次：返回亮 / 暗主题下每个页面、每个标签的横向溢出与 JS 报错（无头窗口最窄 496，390 宽要用设备模拟）。
改了网页 JS 后跑一遍 `node --check web/*.js web/pages/*.js`。

场景页的人工检查：Edge 无头 + `--remote-debugging-port`，打开 `/scene?session=default&debug=1`（`window.__scene` 暴露当前快照和播放器），
调 `POST /api/sessions/default/scene/video` 切不同 mode 后截图。

e2e 脚本**按 1 → 1b → 1c → 1d 的顺序跑**（或两次之间隔 2 分钟以上）：模拟注入事件后 2 分钟内导演仍认为"在播"，
会出冷场话术和送礼回踩，紧接着跑 `test_e2e_mock.py` 会让"空闲帧不带说话标记"一项失败。各 e2e 脚本跑完会删掉自己建的产品/资料/测试观众，
并把改过的设置恢复原样（原来没被 Web 覆盖的键用 reset 去掉覆盖）。
另外注意：Web 上「连接」弹幕会把 `danmaku.enabled=true`（和来源）写进 `live_data/settings.json`，服务重启后会自动连上——跑 e2e 之前确认
设置覆盖里没有它（「直播间设置 → 恢复默认」或 `POST /api/settings/reset {"keys": ["danmaku.enabled"]}`），否则开机就有模拟事件。

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

## 扩展：新增配置项 / 让模块支持热更新

1. 在 `live/settings/schema.py` 对应分组加一行，例如
   `F("speech.fade_ms", "int", 15, "句首尾淡入淡出（毫秒）", "避免爆音", min=0, max=200, group="avatar")`；
   默认 `apply=hot`，只有在启动时才读、改了必须重启的项写 `apply=RESTART`。路径类加 `path=True`（相对路径相对项目根目录）。
2. 模块里用这个值：
   - 能"每次用的时候读 `cfg[...]`"就这样做——热更新自动生效，不用写别的；
   - 缓存了对象（客户端、文件、模型）的，实现 `apply_settings(self, settings, changed)`，在 `AppContext.start()` 里
     `self.settings.register(obj, ["前缀"])`。`changed` 只含该前缀下真正变化的 hot 键。
3. Web 设置页自动出现这一项（标签、说明、范围、即时生效/需重启徽章都来自 schema），不用改前端。

## 扩展：调互动策略 / 新增触发器

大多数调整不用写代码：
- 改优先级、冷却、话术、是否用 LLM：Web「弹幕互动 → 触发器」，或在 `policies/commerce.yaml` 改出厂值；
- 改阈值（礼物分档、欢迎分级、点赞门槛、人数猛涨……）：Web「直播间设置 → 互动策略」；
- 改关键词（产品提问、购买意向、垃圾弹幕）：同上 `interaction.intent.*`；改违禁词：「合规词库」。

新增一个触发器（基于已有信号），在 `commerce.yaml` 加一段即可：

```yaml
gift_milestone_thanks:
  when: gift_l                 # 已有信号，见 director.SIGNALS
  priority: 64
  emotion: 感激高能
  cooldown_s: 120
  use_llm: false
  directive: "……给 LLM 的指令……"
  lines:
    - "感谢{gift_user}的{gift_count}个{gift_name}！"     # 占位符见 Director._fmt
```

新增一个**信号**：在 `RoomState`（需要新的事件窗口时）和 `Director._derive()` 里算出一个布尔值放进返回的 dict，名字加进
`director.SIGNALS`；如果它消费某类当事事件（防重复念），在 `_CONSUMES` 里登记。用假时钟写测试，照 `tests/live/test_p4_director.py`。

## 扩展：新增文档格式 / 检索后端

文档解析器：实现 `Loader.load(path, title) -> Document`（`sections` 里每段带 `heading_path` 和可选 `page`），解析失败抛 `LoaderError`（中文原因，会显示在 Web 资料表里）：

```python
# live/knowledge/loaders/docx.py
from live.knowledge.loaders.base import Loader, LoaderError, check_not_empty
from live.knowledge.models import Document, Section

class DocxLoader(Loader):
    name = "docx"
    exts = (".docx",)
    def load(self, path, title=None):
        import docx                                  # 第三方依赖在这里懒加载，并加进 requirements_live.txt
        try:
            d = docx.Document(path)
        except Exception as e:
            raise LoaderError(f"Word 文件无法解析（{type(e).__name__}）") from None
        doc_title, head, sections = self.default_title(path, title), None, []
        for p in d.paragraphs:
            if p.style.name.startswith("Heading"):
                head = p.text.strip()
            elif p.text.strip():
                sections.append(Section(heading_path=[doc_title] + ([head] if head else []), text=p.text.strip()))
        return check_not_empty(Document(title=doc_title, sections=sections))
```

在 `live/knowledge/__init__.py` 的 `_register_loaders()` 里把 `DocxLoader` 加进元组（按 `exts` 注册），网页上传框的 `accept` 加上 `.docx`。

检索后端：实现 `Retriever`（`add / delete / rebuild / search(query, k, product_ids) -> [Hit]`，可选 `set_product`），
`query.terms` 是检索词、`query.text` 是原问题；`product_ids=None` 表示全部，否则只返回这些产品（`""` = 公共资料）的块：

```python
# live/knowledge/index/my_vector_db.py
from live.knowledge.index.base import Retriever
from live.knowledge.models import Hit

class MyVectorDB(Retriever):
    name = "myvdb"
    def add(self, chunks): ...              # 写入外部向量库，chunk.id / doc_id / product_id 作为元数据
    def delete(self, doc_id): ...
    def rebuild(self, chunks): ...          # 清空后重写（切换后端、重建索引时调用）
    def search(self, query, k, product_ids=None):
        rows = ...                           # 外部库检索，按 product_ids 过滤
        chunks = self.store.get_chunks([r.id for r in rows])
        return [Hit(chunk=chunks[r.id], score=r.score, backend=self.name) for r in rows if r.id in chunks]
```

在 `live/knowledge/__init__.py` 用 `RETRIEVERS.register("myvdb", lambda store, **kw: MyVectorDB(store))` 注册，schema 的
`knowledge.backend` choices 加 `"myvdb"`。"资料够不够"（覆盖度）、摘句、提示词组装都在 `KnowledgeBase` 里，与后端无关。
调检索效果一般不用写代码：改 `knowledge.synonyms`（同义词）、`knowledge.min_coverage`（覆盖度门槛）、`knowledge.top_k`，
在「产品与知识库 → 测试 → 检索测试」里看信息点、命中片段和覆盖度。

## 扩展：新增弹幕平台

弹幕源跑在子进程里，只要实现 `DanmakuSource` 并注册，Manager、事件总线、Web 都不用改：

```python
# live/danmaku/kuaishou.py
from live.core.events import LiveEvent, ViewerProfile
from live.danmaku.base import DanmakuSource

class KuaishouSource(DanmakuSource):
    name = "kuaishou"
    def run(self):                               # 阻塞；返回后 DanmakuManager 按退避策略重启子进程
        self.status({"state": "connecting", "live_id": self.opts["live_id"]})
        client = connect_somehow(self.opts["live_id"])        # 平台 SDK / 协议
        self.status({"state": "connected"})
        for msg in client.messages():
            if self.stopped:
                break
            if msg.kind == "comment":
                u = ViewerProfile(user_id=str(msg.uid), nickname=msg.name, gender="unknown")
                self.emit(LiveEvent(type="chat", platform="kuaishou", room_id=msg.room, user=u,
                                    data={"content": msg.text, "is_emoji": False}))
    def stop(self):
        super().stop()
        # 关闭连接，让 run() 尽快返回
```

```python
# live/danmaku/base.py 末尾注册（懒加载第三方依赖，主进程不导入）
@SOURCES.register("kuaishou")
def _create_kuaishou(opts, emit, status):
    from live.danmaku.kuaishou import KuaishouSource
    return KuaishouSource(opts, emit, status)
```

再在 `live/settings/schema.py` 里给 `danmaku.source` 的 `choices` 加上 `"kuaishou"`。要点：
- 礼物要么直接产出 `is_final=True` 的结算事件，要么复用 `GiftComboAggregator`（在 `tick()` 里调 `flush()`）；
- 时间 `ts` 用墙钟；同一条消息重推要按 `msg_id` 去重；隐私字段（生日、手机号）不要放进 `ViewerProfile`；
- 单元测试照 `tests/live/test_p2_parse.py`：把平台消息解析写成纯函数，用构造的消息测字段映射。

## 扩展：新增 TTS 引擎

```python
# live/tts/my_tts.py
import numpy as np
from live.tts.base import TTSEngine
from live.audio_utils import decode_audio_bytes

class MyTTS(TTSEngine):
    name = "my"
    billable = True            # 按调用计费就设 True，受 tts.daily_max_chars 保护
    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0,
                   emotion: str | None = None) -> np.ndarray:
        audio_bytes = call_my_service(text, voice or self.default_voice, speed)   # 任意格式；失败直接抛异常
        return decode_audio_bytes(audio_bytes, sr=self.sr, ffmpeg=self.ffmpeg)  # -> float32 16k
```

再在 `live/tts/__init__.py` 注册一行 `TTS_ENGINES.register("my", lambda cfg, env=None: MyTTS(cfg))`，
并把 `"my"` 加进 schema 里 `tts.engine` / `tts.fallback` 的 choices，配置 `tts.engine: my`。
需要密钥就在 `.env` 加键（并登记到 `live/settings/env.py` 的 `ENV_KEYS`），构造时接收 `env`，每次调用现读 `env.get(...)`。
缓存、回退、每日上限、热切换都由 `TTSService` 统一处理，引擎里不用管。若引擎的关键参数会影响音频，覆盖 `cache_signature`。

火山 TTS 手动自测（**会计费，确认后再跑**）：`python -m live.tts.volc "测试一句"`，打印 HTTP 状态、`X-Tt-Logid`、前几条原始 SSE 事件，
音频写到 `live_data/tts_selftest.wav`。

## 扩展：新增 LLM 接口

```python
# live/llm/my_llm.py
from live.llm.base import LLMClient, LLMError

class MyLLM(LLMClient):
    name = "my"
    def __init__(self, cfg, env):
        self.cfg, self.env = cfg, env
    @property
    def unavailable_reason(self):
        return None if self.env.get("MY_LLM_KEY") else "MY_LLM_KEY 未配置"
    def chat(self, messages, *, stream=False, temperature=None, max_tokens=None, timeout=None, json_mode=False):
        ...   # 非流式返回 str；流式返回逐片段的迭代器；失败抛 LLMError
```

在 `live/llm/__init__.py` 注册 `LLM_PROVIDERS.register("my", lambda cfg, env: MyLLM(cfg, env))`，schema 的 `llm.provider` choices 加 `"my"`。
测试用 `live/llm/mock.py` 的 `MockLLM`（规则回复、延迟/超时、报错、流式分片，`calls` 记录收到的 messages）。
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

第二阶段已内置（互动引擎 + 主持人，见 ARCHITECTURE §12）：弹幕 → 导演 → 主持人用模板或 LLM 写词 → 追加式请求逐句播报，
`.env` 配好 `LLM_*` 即可。若要在外部系统里自己做：

```
弹幕/评论 → 自己的 LLM → POST /api/host/say（走主持人：过合规、进决策日志）或 POST /api/sessions/{sid}/speak（直接说）
                                  ↑
监听 WebSocket 的 speak_done 事件，知道上一句什么时候说完，再决定下一条
```

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

第二阶段（弹幕互动 / 火山 TTS / LLM / 知识库 / 视频场景 / Web 设置）的功能已完成，真实环境待验证的事项见 `README_LIVE.md`
「需要你在真实环境验证的事项」。下面是还没做的：

- [ ] 抖音真实直播间实测后补字段表「实测」列、核对性别枚举 / 连击规律 / 商品消息 method 名（需用户提供 cookie、确认平台规则）
- [ ] 「数字人与系统」页显示服务日志尾部（任务书 05 §4.6 标为可选）
- [ ] 服务端画面合成 + RTMP 直推（现在由场景页 + OBS 采集）
- [ ] 讲解稿按产品定时插播视频（现在是每轮讲解稿的开头/结尾，`products.video_slot`），以及外部系统（投流）触发播放的定时任务
- [ ] e2e 脚本之间的"在播"状态互相影响（见上文「测试」），可以给模拟注入加一个"测试会话结束"接口清掉导演状态
- [ ] worker 进程崩溃后自动重启，并重建它上面的会话
- [ ] 未预加载的模型改为后台加载，加载期间直播不停（目前切换时同步加载会补帧）
- [ ] RTMP / WebRTC / 虚拟摄像头输出端
- [x] 接口鉴权：写操作需 `ADMIN_TOKEN`，未设置时只允许本机（第二阶段 P1）
- [ ] HTTPS
- [ ] 离线任务结果持久化与定期清理（目前在内存里，`live_data/offline/` 文件不会自动删除）
- [ ] 多形象预加载与秒切（官方 `prepare_params` 支持目录 + `reset_person_name`）
- [ ] 基于真实模型调参：`idle` 策略、`speech.target_rms_db`、`live.safety_factor`
- [ ] Linux 下接入官方 USP，实现 `gpu.mode: sequence_parallel`
