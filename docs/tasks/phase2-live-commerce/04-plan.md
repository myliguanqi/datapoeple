# 04 分阶段实施计划

每个阶段：① 列出要改/新增的文件 → ② 实现 → ③ 跑本阶段测试 + 原有测试 → ④ 更新文档与 README 进度表 → ⑤ `git commit`。
阶段内的"测试"指 `tests/live/` 下的自动化测试（详见 07）；"验收"是人工可观察的结果。

测试命令约定（Windows，项目根目录）：
```bat
python -m pytest tests/live -q -m "not e2e"          :: 单元/集成测试（新增测试用 pytest 写）
python live_server.py --config live/configs/mock.yaml  :: 另开一个终端
python tests/live/test_e2e_mock.py                     :: 原有端到端（33 项），以及新增的 e2e 脚本
python tests/live/test_runner_logic.py                 :: 原有 runner 逻辑测试（需 torch 环境）
```

---

## P0 准备（半天）

任务：
1. 阅读本目录全部文档、根目录 `CLAUDE.md`、`README_LIVE.md`、`docs/live/*.md`；浏览参考项目 02 §1 列出的文件。
2. 新增依赖到 `requirements_live.txt`（标注用途）：`python-dotenv`、`jieba`、`pypdf`、`beautifulsoup4`、`rank_bm25`（FTS5 不可用时的备选）、
   `pytest`、`httpx`（测试 FastAPI）、弹幕：`betterproto==2.0.0b6`、`websocket-client==1.7.0`、`PyExecJS==1.5.1`、`mini_racer==0.12.4`、`requests`。
   在一个干净的 Python 3.10 虚拟环境里验证能装、且不改动 `protobuf<4`、`huggingface-hub<1.0` 等已锁定版本。
3. 新增 `.env.example`（键名见 06 §1，全部留空或示例值）；`.gitignore` 增加 `.env`、`secrets/`、`live_data/`（已有）。
4. 验证 `python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('create virtual table t using fts5(x)')"` 是否成功，结果写进 README 进度表备注（决定检索后端）。
5. 跑通原有测试作为基线。

验收：依赖可装；基线测试全过；`.env.example` 存在且不含任何真实值。
提交：`chore: prepare phase2 deps and env template`

---

## P1 基础设施（1.5–2 天）

任务：
1. `live/core/events.py`（LiveEvent、ViewerProfile，含 to_dict/from_dict）、`live/core/bus.py`（EventBus，见 03 §6）、`live/core/registry.py`、`live/core/clock.py`。
2. 设置中心 `live/settings/`：schema（把现有 `live/config.py` 的 DEFAULTS 迁入并补充 06 §2 的新项）、三层合并、校验、原子持久化、
   版本快照与回滚、`.env` 加载与重载、`Reconfigurable` 协议、`SettingsView`（只读视图，按前缀取值）。
   保留 `live.config.load_config()` 兼容层，让现有 worker/会话代码不用大改。
3. `live/app_context.py`：构造 EventBus、SettingsCenter、WorkerPool、SessionManager、OfflineManager、TTS 等，后续阶段在这里加服务。
4. 把 `live/server.py` 的路由拆到 `live/api/*.py`（system、sessions、offline），行为不变；新增 `api/settings.py`、`api/console_ws.py`。
5. 管理口令：写接口校验 `X-Admin-Token`（`.env` 的 `ADMIN_TOKEN`），未配置时只允许本机访问写接口（原有 e2e 测试从 127.0.0.1 调用，必须仍通过）。
6. 现有可热更新项接入：`speech.*`、`idle.*`、`live.safety_factor/margin_s/max_ahead_chunks`、`stream.jpeg_quality`、`tts.engine/voice` 改为订阅 `settings.changed`。
7. 新 Web 外壳（见 05）：侧边栏 + hash 路由 + 公共组件（卡片、表格、徽章、表单、toast、确认框）+ 设置页（schema 自动生成表单）+ 口令输入；
   把现有控制台功能迁入"直播控制台""数字人与系统""离线视频"页，功能不减。

测试：settings 校验/合并/持久化/回滚/热更新通知；bus 订阅与丢弃；`.env` 重载；写接口鉴权；原有 33+18 项。
验收：Web 设置页能改 `speech.target_rms_db` 并立即影响下一句播报；改 `gpu.devices` 显示"需重启"。
提交：`feat(core): event bus, settings center with hot reload, api routers, new web shell`

---

## P2 弹幕接入（2 天）

许可证事项用户已确认忽略（08 §1），直接开始。

任务：
1. 复制 `D:\jobdemo\douyin_zhibo\vendor\douyin_fetcher` → `vendor/douyin_fetcher/`（原样，含 LICENSE/README/protobuf/JS）。
2. `live/danmaku/base.py`：`DanmakuSource` 接口（start/stop/status，事件回调）；`registry` 注册 `douyin`、`mock`、`replay`。
3. `live/danmaku/douyin/parse.py`：**纯函数** `parse_message(method, payload_bytes) -> list[LiveEvent]` 和 `user_to_profile(user) -> ViewerProfile`，
   按 02 §3 提取全字段；隐私字段不提取；连击礼物聚合器 `GiftComboAggregator`（按 combo/group 键聚合，结束标记或超时结算，输出 `is_final`）。
4. `live/danmaku/douyin/adapter.py`：从参考项目移植 `EventDouyinFetcher`（接管分发、cookie 注入、心跳静音、探针 `_SNIFF`），
   内部调用 `parse.py`；JS 文件用绝对路径。
5. `live/danmaku/douyin/process.py`：子进程入口；`live/danmaku/manager.py`：`DanmakuManager`（启动/停止/退避重连/状态心跳/热切换房间/
   cookie 状态检测"登录态 or 游客态"/发布 `live.event` 与 `danmaku.status`），实现 `Reconfigurable`（`danmaku.*`）。
6. `live/danmaku/mock.py`：可配置的模拟源（事件速率、各类型比例、性别比例、财富等级分布、提问样本库——含产品问题）；
   `live/danmaku/recorder.py`（JSONL 录制，开关在设置里）、`live/danmaku/replay.py`（按原时间间隔或倍速回放）。
7. `EventFeed`：环形缓冲（默认 500 条）+ 节流（点赞合并、进场折叠）→ `/ws/console`；`api/danmaku.py`（03 §13）。
8. Web「弹幕互动」页（见 05 §4.2）：状态灯、连接/断开、房间号、事件流（用户标签）、类型过滤、模拟注入面板、cookie 粘贴（只写）。
9. 文档：`docs/live/ARCHITECTURE.md` 增加弹幕子进程与事件模型；`API.md` 增加弹幕接口；`DEVELOPMENT.md` 增加"新增弹幕平台"示例。

测试：`parse.py` 用 betterproto 构造各类消息字节做单测（字段映射、性别映射、财富等级、粉丝团、在线人数中文数字、隐私字段不出现）；
连击聚合（多包 → 一次 final、超时结算）；manager 子进程崩溃重启（用 mock 源子进程）；replay 回放顺序与时间；`/ws/console` 收到事件。
验收：mock 源下弹幕页实时滚动、标签正确；（用户确认后）连自己的直播间核对 02 §3 的"实测"列并把结果补进该表。
提交：`feat(danmaku): douyin fetcher in subprocess with full-field parsing, mock and replay`

---

## P3 火山 TTS + LLM（1 天）

任务：
1. `live/tts/volc.py`：照抄参考项目 `tts_client.py` 核心（02 §6），实现本项目 `TTSEngine`；凭证读 `.env`；输出 float32 16k；`python -m live.tts.volc "文本"` 手动自测。
2. 扩展 `TTSEngine.synthesize(text, voice=None, speed=1.0, emotion=None)`（向后兼容现有 edge/http/mock）；`LiveSession.speak` 支持 `speed`。
3. `live/tts/cache.py`：照抄 `CachingTTSClient` 思路，包住任意 TTSEngine；`tts.cache.*` 设置。
4. TTS 选择与回退：`tts.engine` 为 `volc` 但凭证缺失 → 依 `tts.fallback`（默认 `["edge","mock"]`）回退并在 Web 告警。
5. `live/llm/base.py`（`LLMClient`：`chat(messages, *, stream=False, **params) -> str | Iterator[str]`、`available`、`stats`）、
   `openai_compat.py`（02 §7，读 `.env`；URL 规范化；流式 SSE；超时；失败计数）、`mock.py`（可编排的假回复，用于测试）。
6. Web「直播间设置」页的 TTS/LLM 分组；"数字人与系统"页显示 TTS/LLM 可用性、最近耗时、失败次数；"重新加载 .env"按钮。

测试：SSE 解析（正常、多块、错误码、空音频、非 JSON 行）用假响应；speech_rate 映射；缓存命中/失效（换音色）；LLM 非流式/流式/超时/无 key 回退；
`.env` 重载后客户端重建。**不访问真实接口。**
验收：（用户同意后）手动自测火山合成一句并在 Web 上让数字人说出来。
提交：`feat(tts,llm): volcengine tts with cache, openai-compatible llm via .env`

---

## P4 互动引擎 + 主持人（2.5–3 天）

任务：
1. 移植 `state.py`、`director.py`、`memory.py`、`queue.py`（02 §1、§4），去游戏层；`policies/commerce.yaml`、`persona_commerce.md` 按带货语境改写
   （保留优先级体系，新增 02 §4.4 的信号；所有 directive/兜底话术合规）。
2. `intent.py`（关键词规则，可在 Web 编辑词表；可选 LLM 分类）、`compliance.py`（极限词/禁语库 + 替换策略 + 命中日志）。
3. `engine.py`：订阅 `live.event` → RoomState → Director（1Hz + 关键事件立即 tick）→ 发布 `interaction.cue` → CueQueue。
4. `LiveSession` 增加追加式请求：`begin_request(request_id, interruptible) / append(request_id, text) / end_request(request_id)`，
   `speak_done` 在 end 之后且最后一段播完才发；`interrupt()` 返回被打断的 request_id。保持现有 `speak()` 行为不变（内部可复用）。
5. `live/host/`：`session_tap.py`（内部 FrameSink，只收事件，转发为 `session.event`）、`composer.py`（模板渲染 / LLM 写词，含按性别称呼、
   昵称简称、字数限制、超时回退）、`host.py`（03 §8 的循环，含打断讲品、人工插话、暂停/恢复）。
6. 设置项：`interaction.*`（各触发器开关/优先级/冷却、礼物分档阈值、欢迎分级阈值、点赞阈值、保鲜窗口、max_stale_s）、`host.*`（interrupt_priority、
   暂停开关、回复最大字数）、`persona.*`、`compliance.*`，全部热更新。
7. Web：「弹幕互动」页增加决策日志（事件 → 触发器 → 台词 → 耗时）、队列视图、触发器表编辑（开关/优先级/冷却/话术）；控制台页显示"正在说/下一句"。

测试：用假时钟 + mock 事件序列复现参考项目 `verify.py` 的平台类用例（礼物分档、连续送礼、回踩、榜一易主、欢迎分级、known_before、建档门槛、
点赞冷却放大、过期丢弃、同拍合并）；意图识别；合规替换；Host 编排（打断讲品、speak_done 驱动下一句、超时回退）；
e2e（mock 后端）：模拟"高等级进场 + 产品提问 + 大礼物"→ 断言播报顺序与文本来源。
验收：mock 源跑 10 分钟，Web 决策日志合理、无复读、无冷场。
提交：`feat(interaction): director/state/memory port, intent, compliance, host orchestration`

---

## P5 产品与知识库（2–2.5 天）

任务：
1. `live/knowledge/`（03 §9）：loaders（md/html/pdf）、chunker、FTS5 或 BM25 检索、可选 embedding + 混合检索、store、service（异步 ingest + 进度事件）。
2. `live/products/`：模型、存储、讲解稿生成（LLM 基于检索结果，生成后合规过滤，标记"待确认"）、`ProductScript`（逐段、断点续讲、按时长轮换）。
3. RAG：`KnowledgeBase.answer(question, product_id)`（检索 → 提示词 → LLM → 合规）；Composer 对 `product_question` / `purchase_intent` 调用它；
   资料不足时的兜底话术。
4. Host 接入讲品轮播（`product_rotation`），设置项 `products.*`（轮播开关、顺序、每个产品时长、段间停顿）。
5. Web「产品与知识库」页（05 §4.3）：产品列表/编辑、讲解稿编辑与生成、文档上传（拖拽、多文件）、索引状态、检索测试、回答测试（可勾选"让数字人说"）。
6. `tests/fixtures/kb/`：自造一份产品 md、html、pdf（pdf 可用 reportlab 生成或手工准备，提交二进制前确认体积 < 200KB）。

测试：三种 loader 的标题/页码；切块长度与重叠；中文检索命中（价格/保质期/尺码等）；按产品过滤；删除文档后检索不到；
RAG 提示词组装（mock LLM 断言收到了资料片段）；资料不足兜底；讲解稿生成（mock）+ 合规；讲品轮播与断点续讲（假时钟）。
验收：上传样例资料，回答测试里问 5 个问题，答案都来自资料；无弹幕时数字人按讲解稿讲，插入提问后先答再续讲。
提交：`feat(knowledge,products): document ingest, retrieval, rag answers, product script rotation`

---

## P6 视频素材与直播场景（1.5–2 天）

⚠ 开始前确认直播画面比例（默认竖屏 720×1280）。

任务：
1. `live/media/store.py`（上传、ffprobe 元数据、封面、关联产品）、`live/media/scene.py`（SceneState，03 §10）、`api/media.py`、`api/scene.py`。
2. 场景事件经会话 WsHub 推送（`{"type":"scene", ...}`）；场景页回传 `video_ended`。
3. `web/scene.html` + `scene.js`：层叠布局、产品卡片、字幕、视频播放（全屏/画中画/分屏）、音频 ducking、断线重连、URL 参数（session、尺寸、调试框）。
4. Host 接入视频：讲品流程中的视频节点、手动"立即播放"；视频原声播放期间暂停讲品，结束恢复；数字人空闲生成不停。
5. 设置项 `scene.*`（画布尺寸、背景图、数字人位置/缩放、卡片样式、字幕开关、ducking 比例），热更新。
6. Web「视频素材」页（05 §4.4）与控制台页的场景预览（iframe 嵌入 `/scene`）。

测试：媒体上传/元数据（用一个几秒的测试视频，ffmpeg 现场生成，不入库）；SceneState 状态机（播放→结束→恢复）；场景事件推送；
Host 在视频期间不讲品；e2e：命令播放视频后场景页收到 scene 事件（可用 DevTools 协议截图验证，见现有做法）。
验收：OBS 浏览器源加载 `/scene`，能看到数字人+产品卡片；点"播放视频"切到画中画，播完回到数字人。
提交：`feat(media,scene): video library and live scene page with pip/fullscreen playback`

---

## P7 Web UI 整理收尾（1 天）

任务：按 05 逐页走查：布局对齐、空状态、加载态、错误提示、危险操作确认、亮/暗主题、1366×768 与手机宽度无横向溢出；
控制台页信息密度（关键状态一屏可见）；所有表单有说明文字；热更新项与需重启项的视觉区分。
测试：用 Edge 无头 + DevTools 协议截图每个页面（参考现有 `README_LIVE.md` 提到的做法），检查 `scrollWidth <= innerWidth`；JS `node --check`。
提交：`feat(web): polish all pages`

---

## P8 文档同步与整体验收（半天–1 天）

任务：
1. 更新 `README_LIVE.md`（功能表、快速开始加 .env/cookie/知识库/场景页、配置表、已验证/未验证）、`docs/live/ARCHITECTURE.md`（新增模块与数据流）、
   `API.md`（全部新接口与 WS 事件）、`DEVELOPMENT.md`（新增弹幕平台/TTS/LLM/检索后端/触发器的扩展示例，测试命令，待办更新）。
2. 根目录 `CLAUDE.md` 更新为最新约定。
3. 全量测试；按 07 §4 的人工验收清单逐条走一遍（mock 模式），把结果写进 README 进度表。
4. 汇总"需用户在真实环境验证"的事项（真实直播间字段核对、性别枚举、连击规律、火山 16k 采样率、OBS 采集），列在 `README_LIVE.md`。

提交：`docs: sync live docs for phase2`
