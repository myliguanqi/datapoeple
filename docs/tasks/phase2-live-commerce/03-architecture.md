# 03 架构设计

## 1. 总体数据流

```
 抖音直播间 WSS                     Web 控制台 / 外部系统
      │                                   │ REST / WS
      ▼                                   ▼
┌──────────────────┐  LiveEvent  ┌────────────────────────── 主进程 ───────────────────────────────┐
│ 弹幕子进程         │ ─────────▶ │ EventBus                                                         │
│ DouyinAdapter     │  mp.Queue  │   ├─▶ EventFeed（环形缓冲 → /ws/console 推给 Web 弹幕面板）       │
│ （或 MockSource）  │            │   ├─▶ Recorder（可选：JSONL 录制）                                │
└──────────────────┘            │   ├─▶ ViewerMemory（长期记忆）                                    │
                                │   └─▶ InteractionEngine：RoomState（保鲜窗口）→ Director.tick    │
                                │            │ Cue（priority, directive, situation, fallback）      │
                                │            ▼                                                     │
                                │        CueQueue（优先级 + 过期丢弃）                               │
                                │            │                                                     │
                                │            ▼                                                     │
                                │        Host 主持人（编排：互动 / 讲品轮播 / 视频插播）              │
                                │          ├─ Composer：模板 or LLM（+ KnowledgeBase 检索）写台词    │
                                │          ├─ Compliance：违禁词/极限词过滤                         │
                                │          ├─▶ LiveSession.speak(...) ──▶ TTS(火山+缓存) ──▶ 数字人  │
                                │          └─▶ SceneState（产品卡片 / 视频播放 / 布局）               │
                                │                                                                  │
                                │ SettingsCenter（三层合并 + 校验 + 持久化）── settings.changed ──▶ 各模块 │
                                └──────────────────────────────────────────────────────────────────┘
                                         │ AV 帧 + 场景事件（/ws/sessions/{sid}）
                                         ▼
                                  场景页 /scene（OBS 浏览器源）：数字人画面 + 产品卡片 + 视频 + 字幕
```

## 2. 解耦原则（硬性）

1. **接口 + 实现 + 注册**：每类能力定义一个抽象基类（`base.py`），实现放同目录，`registry.py` 按配置名创建。
   新增实现 = 新文件 + 注册一行，不改调用方。适用：弹幕源、TTS、LLM、检索后端、文档解析器、话术编写器（Composer）。
2. **模块间只走三种通道**：事件总线（异步广播）、显式接口调用（同步，依赖注入构造参数）、HTTP API（给 Web/外部）。
   禁止模块互相 import 对方的内部实现或全局变量。
3. **依赖注入**：在 `live/app_context.py`（新增）里统一构造所有服务并互相注入，FastAPI 路由从 `AppContext` 取服务。
   测试时可以替换任意服务为 mock。
4. **配置不硬编码**：所有阈值/文案/开关进设置中心（带 schema），模块通过 `apply_settings()` 接收变更。
5. **领域数据和平台数据分开**：`LiveEvent` 是平台无关的统一模型；抖音特有字段放 `extra`。

## 3. 统一事件模型（`live/core/events.py`）

```python
@dataclass
class ViewerProfile:
    user_id: str                 # 平台内唯一 ID（字符串）
    nickname: str = ""
    gender: str = "unknown"      # male | female | unknown
    wealth_level: int = 0        # 财富等级（抖音 pay_grade.level）
    wealth_name: str = ""
    wealth_diamonds: int = 0     # 累计送出钻石
    fans_club: dict | None = None   # {"name","level","status"}
    user_level: int = 0
    followers: int | None = None
    follow_status: int | None = None
    verified: bool = False
    avatar_url: str = ""         # 只用于展示，不落盘
    badges: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)   # 平台特有字段（short_id、display_id、sec_uid、age_range…）

@dataclass
class LiveEvent:
    type: str                    # chat | gift | like | member | follow | share | fansclub | room_stat | rank
                                 # | room_stats | room_notice | product_change | stream_end | system
    platform: str                # douyin | mock | replay
    room_id: str
    ts: float                    # 墙钟秒
    msg_id: str = ""             # 去重
    user: ViewerProfile | None = None
    data: dict = field(default_factory=dict)    # 各类型字段，见下
    in_room: dict = field(default_factory=dict) # {"consume": int, "gift_count": int}（public_area_common）
    raw_method: str = ""

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "LiveEvent": ...
```

`data` 约定（按 type）：
- chat：`{"content", "is_emoji"}`
- gift：`{"gift_id", "gift_name", "unit_diamonds", "count", "total_diamonds", "combo_id", "is_final", "to_user"}`（`is_final` 为连击结算后的那一条；中间包 `is_final=false`，互动引擎只处理 final）
- like：`{"count", "total"}`
- member：`{"member_count", "enter_type"}`
- follow / share：`{"action", "share_type"}`
- fansclub：`{"kind": "join" | "upgrade", "content"}`
- room_stat：`{"online", "total_pv", "popularity"}`
- rank：`{"ranks": [{"user_id","nickname","score"}]}`
- product_change：`{"products": [...], "toast"}`

`LiveEvent` 必须可 JSON 序列化（用于子进程传输、录制回放、Web 推送）。

## 4. 进程模型

| 进程 | 内容 | 说明 |
|---|---|---|
| 主进程 | FastAPI、设置中心、事件总线、互动引擎、主持人、知识库、LLM/TTS 调用、会话时钟 | 不 import torch |
| GPU worker（每卡一个） | FlashHead 推理 | 现有，不变 |
| 弹幕子进程（每个直播间一个） | DouyinLiveWebFetcher + 适配层 | **新增**。隔离 AGPL 代码、全局 monkeypatch、chdir、JS 引擎；崩溃自动重启；改房间号 = 重启子进程 |

弹幕子进程 ↔ 主进程：`multiprocessing` spawn，`event_q`（子→主，`LiveEvent.to_dict()`）、`ctrl_q`（主→子：stop、set_sniff），
外加状态心跳（连接中/已连接/游客态/登录态/最近一条消息时间/错误）。主进程 `DanmakuManager` 负责启动、监控、退避重连、热切换。

## 5. 设置中心与热更新（`live/settings/`）

### 5.1 三层合并
`代码默认值（schema 里的 default）` ← `live/configs/live.yaml`（部署配置）← `live_data/settings.json`（Web 写入的运行时覆盖）。
密钥只来自 `.env`（`python-dotenv` 加载），不进上述三层。

### 5.2 Schema
每个设置项定义一次（`live/settings/schema.py`），包含：`key`（点路径，如 `interaction.gift.big_value`）、`type`
（int/float/bool/str/text/enum/list/secret_ref）、`default`、`min/max/choices`、`label`（中文）、`help`、`group`（Web 分组）、
`apply: "hot" | "restart"`、`secret`（只显示是否已配置）。现有 `live/config.py` 的 `DEFAULTS` 迁移进 schema（保留 `load_config` 兼容层，避免大改）。

### 5.3 变更流程
```
PUT /api/settings  {changes: {"interaction.gift.big_value": 20, "danmaku.live_id": "xxx"}}
  → schema 校验（类型/范围/枚举）→ 失败返回逐项错误
  → 写 live_data/settings.json（原子替换）+ 版本快照（保留最近 20 份，可回滚）
  → bus.publish("settings.changed", {"keys": [...], "old": {...}, "new": {...}})
  → 订阅者按 key 前缀热应用；apply=restart 的项只保存并在 Web 标"需重启"
```
模块实现 `Reconfigurable` 协议：`def apply_settings(self, settings: SettingsView, changed: set[str]) -> None`，只处理自己前缀的键。
示例：`danmaku.live_id` 变 → `DanmakuManager` 重启子进程；`tts.volc.voice_type` 变 → 重建 TTS 引擎（缓存键自动变）；
`interaction.*` 变 → 导演更新阈值/触发器表；`persona.*` 变 → Composer 重新加载人设。

### 5.4 .env
`live/settings/env.py`：启动时加载 `.env`；`POST /api/settings/reload-env` 重新加载并发布 `env.changed`（LLM/TTS/Embedding 客户端据此重建）。
Web 只显示每个密钥"已配置/未配置"和来源，不回显值。

## 6. 事件总线（`live/core/bus.py`）

线程安全的进程内 pub/sub：`subscribe(topic_pattern, handler, *, queue_size=1000, name)`，每个订阅者一个有界队列 + 工作线程，
慢订阅者丢最旧的并计数（不阻塞发布者）。`publish(topic, payload)` 非阻塞。

| 主题 | 发布者 | 订阅者 |
|---|---|---|
| `live.event` | DanmakuManager | RoomState、ViewerMemory、EventFeed、Recorder |
| `danmaku.status` | DanmakuManager | EventFeed（Web 状态灯） |
| `interaction.cue` | Director | CueQueue、EventFeed（决策日志） |
| `host.action` | Host | EventFeed（"说了什么/为什么"） |
| `session.event` | 会话的内部 sink（speak_done、playback 等） | Host |
| `scene.changed` | SceneState | 会话 hub（推给场景页）、EventFeed |
| `settings.changed` / `env.changed` | SettingsCenter | 所有 Reconfigurable |
| `kb.ingest` | KnowledgeBase | EventFeed（索引进度） |

## 7. 互动引擎（`live/interaction/`）

```
state.py      RoomState：on_event() 入保鲜队列；snapshot() → RoomSnapshot（recent_chat/gift/member/follow/like、online、
              online_delta、gift_top、rank、returning 标记）；墙钟；连击礼物只认 is_final
director.py   Director：tick(snapshot) → Cue | None；信号派生（平台 + 带货）、触发器、冷却、能量、发声间隔、同拍合并
policies/     commerce.yaml（触发器表）、persona_commerce.md（人设）、templates.yaml（兜底话术，可选拆分）
memory.py     ViewerMemory：record(event)、profile(user_id)、known_before(user_id)、flush()
queue.py      CueQueue：push(cue)、get(timeout)、peek_priority()、purge_stale()；max_stale_s 可热更新
intent.py     意图识别：rules（关键词表，可在 Web 编辑）→ product_question / purchase_intent / chitchat / command / spam；
              可选 LLM 分类兜底
compliance.py Compliance：filter(text) → (clean_text, hits)；词库（广告法极限词、平台禁语、自定义）在 Web 编辑
engine.py     InteractionEngine：订阅 live.event，驱动 RoomState + Director（1Hz tick + 关键事件立即 tick）
```

`Cue` 在参考项目基础上增加：`id`、`kind`（interaction | product | video | manual）、`user`（ViewerProfile）、`question`（产品提问原文）、
`product_id`、`created_at`、`max_stale_s`、`interruptible`。

## 8. 主持人编排（`live/host/host.py`）

Host 是唯一"决定数字人下一句说什么"的地方。每个直播会话一个 Host（绑定 `LiveSession`）。

```
循环（独立线程）：
  1. 如果正在播报（当前请求未收到 speak_done，且未超时）：
       - 若 CueQueue 里最高优先级 ≥ settings.host.interrupt_priority 且当前播报 interruptible（如讲品）
         → session.interrupt()，记下讲品断点，转 2
       - 否则等待（条件变量，被 speak_done / 新 cue 唤醒）
  2. cue = CueQueue.get()（已丢弃过期的）
     有 cue → text = Composer.compose(cue)（模板 or LLM+知识库；有超时，超时用 cue.fallback_text）
             → Compliance.filter → session.speak(text, request_id=cue.id, speed=cue.speed)
  3. 没有 cue：
       - 讲品开启 → ProductScript.next_segment()（断点续讲、按时长轮换产品）→ speak（interruptible=True）
       - 到了视频节点 → SceneState.play_video(...)（视频带原声时暂停讲品，结束后恢复）
       - 否则空闲（数字人照常空闲生成，不冷场由 idle 触发器负责）
  4. 每一步发布 host.action（含 cue 来源、触发器名、最终文本、耗时）
```

延迟优化：LLM 支持流式时，Composer 按句产出，Host 用"追加式请求"逐句交给会话（需要在 `LiveSession` 增加
`begin_request / append / end_request`，保证 `speak_done` 在最后一句播完才发，见 04 P4）。

人工干预：Web 上手动"说一句"走 `kind=manual` 的 cue（最高优先级可配），"暂停互动/暂停讲品"开关为热更新设置。

## 9. 知识库与产品（`live/knowledge/`、`live/products/`）

```
knowledge/
  loaders/   base.py（Loader 接口：load(path) -> Document{title, sections[{heading_path, text, page}]}）
             markdown.py、html.py（BeautifulSoup，去 script/style/nav）、pdf.py（pypdf，按页）
  chunker.py 按标题/段落切块，目标 300–500 字、重叠 50 字；chunk 元数据：doc_id、product_id、heading_path、page
  index/     base.py（Retriever 接口：add(chunks)、delete(doc_id)、search(query, k, product_id) -> [Hit]）
             fts.py（SQLite FTS5 + jieba 预分词；⚠ 先验证本机 sqlite3 支持 fts5，不支持则用 rank_bm25 纯 Python 实现）
             embedding.py（可选：OpenAI 兼容 /embeddings，向量存 sqlite/npy，余弦）；hybrid.py（分数融合）
  store.py   文档元数据（sqlite：docs、chunks）、原文件存 live_data/kb/files/
  service.py KnowledgeBase：ingest(file, product_id) 异步任务（发布 kb.ingest 进度）、list/delete/reindex、search、answer(question, product_id)
products/
  models.py  Product{id, name, price_text, selling_points[], script_segments[], doc_ids[], video_ids[], enabled, order, duration_s}
  store.py   JSON/sqlite 持久化，CRUD
  script.py  ProductScript：generate(product)（LLM 基于知识库生成分段讲解稿 → 合规过滤 → 待人工确认）、next_segment()、断点续讲、轮换
```

RAG 回答提示词要点（放 `policies/` 里可编辑）：人设 + "只根据【资料】回答；资料没有就说'这个我帮您确认一下，可以看商品详情或问客服'" +
口语化、≤ `interaction.answer_max_chars` 字 + 合规约束（不用极限词、不承诺功效/收益）+ 当前产品名 + 提问者称呼（按性别）。

## 10. 场景与视频（`live/media/`、`web/scene.*`）

- `media/store.py`：视频库（上传到 `live_data/media/`，ffprobe 取时长/分辨率，生成封面），关联产品。
- `media/scene.py`：`SceneState`（每会话一个）：`layout`（`dh_full` | `dh_with_card` | `video_full` | `video_pip` | `split`）、
  `product_card`（名称/价格/卖点，来自当前产品）、`video`（`{media_id, url, started_at, mode, audio: "video"|"mute", loop}`）、`subtitle`（开关/样式）。
  变更发布 `scene.changed`，经会话的 WsHub 作为 JSON 事件 `{"type":"scene", ...}` 推给所有场景页。
- 场景页 `web/scene.html`：全屏画布（默认 720×1280，可设置），层叠：背景图 → 视频层（`<video>`）→ 数字人画布（复用 `player.js`）→ 产品卡片 → 字幕。
  视频带原声时数字人音频压低（ducking），视频结束上报 `scene.video_ended`（WS 回传）→ Host 恢复讲品。
- 现有 `/?view=1` 保留为"只有数字人"的简化场景，`/scene` 为完整直播画面；OBS 浏览器源用 `/scene?session=default`。
- 服务端合成 + RTMP 直推为后续可选项（不在本阶段）。

## 11. 存储（全部在 `live_data/`，已 gitignore）

```
live_data/
  settings.json            Web 写入的设置覆盖
  settings_history/        最近 20 份快照
  viewers.sqlite           长期记忆（或 viewers.json，沿用参考项目格式也可）
  kb/kb.sqlite             文档、切块、FTS 索引
  kb/files/                原始文档
  products.json            产品库
  media/                   视频与封面
  events/YYYYMMDD_<room>.jsonl   弹幕录制（可选）
  cache/tts/               TTS 合成缓存
  uploads/、offline/        现有
secrets/douyin_cookie.txt  抖音 cookie（gitignore；路径可在 .env 改）
.env                       密钥（gitignore）；.env.example 入库
```

## 12. 新目录结构（在现有 `live/` 基础上新增）

```
live/
  app_context.py           统一构造与注入所有服务
  core/        events.py  bus.py  registry.py  clock.py（可注入假时钟，测试用）
  settings/    schema.py  store.py  env.py  view.py
  api/         system.py  sessions.py  offline.py  settings.py  danmaku.py  interaction.py
               knowledge.py  products.py  media.py  scene.py  console_ws.py
  danmaku/     base.py  manager.py  mock.py  replay.py  recorder.py
               douyin/  adapter.py  process.py  parse.py（protobuf → LiveEvent 的纯函数，便于单测）
  interaction/ state.py  director.py  memory.py  queue.py  intent.py  compliance.py  engine.py  policies/
  host/        host.py  composer.py  session_tap.py
  llm/         base.py  openai_compat.py  mock.py
  knowledge/   见 §9
  products/    见 §9
  media/       store.py  scene.py
  tts/         现有 + volc.py  cache.py
  server.py    瘦身为 app 工厂：创建 AppContext、挂载 api/*、静态文件
vendor/douyin_fetcher/     原样复制
web/                       见 05
tests/live/                现有 + 新增（见 07）
tests/fixtures/            样例文档（md/html/pdf）、录制的弹幕 JSONL、假 SSE 响应
```

## 13. 新增 API 一览（细节在实现时写进 docs/live/API.md）

| 分组 | 接口 |
|---|---|
| 设置 | `GET /api/settings`（schema + 当前值，密钥脱敏）、`PUT /api/settings`、`POST /api/settings/reset`、`GET /api/settings/history`、`POST /api/settings/rollback/{version}`、`GET/POST /api/settings/export|import`、`POST /api/settings/reload-env`、`GET /api/settings/secrets-status` |
| 弹幕 | `GET /api/danmaku/status`、`POST /api/danmaku/connect`（live_id 可选，默认取设置）、`POST /api/danmaku/disconnect`、`GET /api/danmaku/events?limit=&type=`、`POST /api/danmaku/simulate`（注入事件）、`POST /api/danmaku/mock/start|stop`、`POST /api/danmaku/replay`、`PUT /api/danmaku/cookie`（只写） |
| 观众 | `GET /api/viewers?q=&limit=`、`GET /api/viewers/{user_id}` |
| 互动 | `GET /api/interaction/triggers`、`PUT /api/interaction/triggers/{name}`、`GET /api/interaction/queue`、`GET /api/interaction/log?limit=`、`POST /api/interaction/pause|resume` |
| 主持 | `GET /api/host/status`、`POST /api/host/say`（人工插话）、`POST /api/host/product/{id}/activate`、`POST /api/host/script/pause|resume|next` |
| 知识库 | `POST /api/kb/docs`（上传，可带 product_id）、`GET /api/kb/docs`、`DELETE /api/kb/docs/{id}`、`POST /api/kb/reindex`、`GET /api/kb/search?q=&product_id=&k=`、`POST /api/kb/answer`（`{question, product_id, speak:false}`） |
| 产品 | `GET/POST /api/products`、`GET/PUT/DELETE /api/products/{id}`、`POST /api/products/{id}/script/generate`、`PUT /api/products/{id}/script` |
| 视频 | `POST /api/media`（上传）、`GET /api/media`、`DELETE /api/media/{id}` |
| 场景 | `GET /api/sessions/{sid}/scene`、`PUT /api/sessions/{sid}/scene`（布局/卡片/字幕）、`POST /api/sessions/{sid}/scene/video`（`{media_id, mode, audio}`）、`POST /api/sessions/{sid}/scene/video/stop` |
| 控制台 WS | `WS /ws/console`：推送 live.event（节流后）、danmaku.status、interaction.cue、host.action、kb.ingest、settings.changed |

写操作接口（PUT/POST/DELETE）要求请求头 `X-Admin-Token`（`.env` 的 `ADMIN_TOKEN`；未设置则仅允许 127.0.0.1 访问写接口）。
