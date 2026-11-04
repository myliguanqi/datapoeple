# 06 配置与密钥

## 1. `.env`（密钥与外部服务地址；不入库，入库的是 `.env.example`）

| 键 | 必填 | 示例 / 默认 | 说明 |
|---|---|---|---|
| `ADMIN_TOKEN` | 建议 | 随机长串 | Web/接口写操作口令；不设则只允许本机执行写操作 |
| `LLM_API_URL` | P4 起需要 | `https://api.deepseek.com/v1` | OpenAI 兼容地址，写到 `/v1` 或完整 `/chat/completions` 均可 |
| `LLM_API_KEY` | 同上 | | |
| `LLM_MODEL` | 同上 | `deepseek-v4-flash`（参考项目在用） | |
| `LLM_TIMEOUT` | 否 | `10` | 秒 |
| `EMBEDDING_API_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | 否 | | 只有开启向量检索才需要；不填则只用关键词检索 |
| `VOLC_TTS_API_KEY` | 用火山时需要 | | 新控制台 API Key |
| `VOLC_APP_ID` / `VOLC_ACCESS_TOKEN` | 否 | | 旧控制台鉴权（新控制台不用） |
| `VOLC_VOICE_TYPE` | 用火山时需要 | `S_xxxxxxxx` | 声音复刻音色 ID（默认值，可在 Web 覆盖） |
| `VOLC_RESOURCE_ID` | 否 | `seed-icl-2.0` | 版本/计费：`seed-icl-1.0` / `seed-icl-1.0-concurr` / `seed-icl-2.0` |
| `DOUYIN_COOKIE_FILE` | 否 | `secrets/douyin_cookie.txt` | 抖音登录 cookie 文件路径（收礼物必需） |

规则：
- 代码只通过 `live/settings/env.py` 读取环境变量，模块不直接 `os.environ`。
- Web 只显示"已配置/未配置"，不回显值；`POST /api/settings/reload-env` 重新加载。
- 日志里不得打印密钥和 cookie（打印长度或前 4 位即可）。

## 2. Web 可设置项（设置中心 schema）

`apply`：**hot** = 保存后立即生效；**restart** = 需重启服务。默认值是建议起点，实现时可按实际调整，但要写进 schema 的 `default` 和 `help`。

### 2.1 直播间与弹幕 `danmaku.*`

| 键 | 类型 | 默认 | apply | 说明 |
|---|---|---|---|---|
| `danmaku.enabled` | bool | false | hot | 是否接弹幕 |
| `danmaku.source` | enum douyin/mock/replay | douyin | hot | 弹幕来源 |
| `danmaku.live_id` | str | "" | hot | 抖音直播间 web_rid（`live.douyin.com/<这串数字>`）；修改即重连 |
| `danmaku.reconnect_delay_s` | float 1–60 | 5 | hot | 断线重连初始间隔（指数退避，上限 60s） |
| `danmaku.combo_timeout_s` | float 0.5–10 | 3 | hot | 连击礼物多久没新包就结算 |
| `danmaku.gender_map` | dict | `{1: male, 2: female}` | hot | 性别枚举映射（实测反了就对调） |
| `danmaku.sniff` | bool | false | hot | 协议探针（排查新消息类型） |
| `danmaku.record` | bool | false | hot | 录制事件到 `live_data/events/*.jsonl` |
| `danmaku.mock.rate_per_min` | int | 30 | hot | mock 源每分钟事件数 |
| `danmaku.mock.mix` | dict | chat 0.35 / member 0.3 / like 0.2 / gift 0.1 / follow 0.05 | hot | mock 事件比例 |
| `danmaku.mock.question_ratio` | float | 0.3 | hot | mock 弹幕里产品提问的比例 |
| `danmaku.feed.max_items` | int | 500 | hot | Web 事件流保留条数 |

### 2.2 互动策略 `interaction.*`（来自参考项目 `config.yaml: director/state_merger`，起步值照搬后再调）

| 键 | 默认 | apply | 说明 |
|---|---|---|---|
| `interaction.enabled` | true | hot | 总开关（关掉后只讲品/人工插话） |
| `interaction.tick_s` | 1.0 | hot | 导演节拍 |
| `interaction.urgent_priority` | 24 | hot | ≥ 此优先级无视发声间隔 |
| `interaction.max_stale_s` | 10 | hot | 队列里的 cue 超过多久没说就丢弃（单个触发器可覆盖） |
| `interaction.window.chat_s / gift_s / member_s / like_s / rank_s` | 4 / 5 / 5 / 6 / 90 | hot | 事件保鲜窗口 |
| `interaction.gift.min_value` | 1 | hot | 礼物 ≥ 此钻石数才感谢 |
| `interaction.gift.tier_m / tier_l` | 10 / 50 | hot | S < M ≤ L 分档阈值（单笔总价值） |
| `interaction.gift.followup_delays_s` | [18, 45] | hot | 送礼后回踩时间点 |
| `interaction.gift.repeat_window_s` | 180 | hot | 连续送礼判定窗口 |
| `interaction.welcome.vip_gift_value` | 50 | hot | 历史累计送礼 ≥ 此值的老观众 = 金主 |
| `interaction.welcome.regular_visits` | 3 | hot | 历史进场 ≥ 此次数 = 熟客 |
| `interaction.welcome.high_grade_level` | 20 | hot | 财富等级 ≥ 此值 = 高等级大佬 |
| `interaction.like.min_count` | 5 | hot | 窗口内点赞 ≥ 此值才点名 |
| `interaction.like.repeat_cooldown_factor` | 3.0 | hot | 同一人霸屏时冷却放大倍数 |
| `interaction.online.surge_min` / `drain_min` / `milestone_step` | 5 / 5 / 50 | hot | 人数猛涨/下滑/破整（投流来人时承接） |
| `interaction.triggers.<name>.enabled/priority/cooldown_s/emotion/directive/fallback/max_stale_s` | 来自 `policies/commerce.yaml` | hot | 单个触发器覆盖；Web 触发器表编辑写这里 |
| `interaction.intent.product_keywords` | 价格、多少钱、尺码、成分、保质期、发货、包邮、售后、退换、怎么买、链接、优惠、券… | hot | 产品提问关键词 |
| `interaction.intent.purchase_keywords` | 要了、拍了、下单、几号链接、怎么拍… | hot | 购买意向关键词 |
| `interaction.intent.llm_classify` | false | hot | 关键词没命中时是否用 LLM 判意图（多一次调用） |

### 2.3 主持人与人设 `host.*`、`persona.*`

| 键 | 默认 | apply | 说明 |
|---|---|---|---|
| `host.enabled` | true | hot | |
| `host.interrupt_priority` | 50 | hot | 新 cue 优先级 ≥ 此值时打断正在讲的讲品段 |
| `host.manual_priority` | 90 | hot | 人工插话的优先级 |
| `host.pause_interaction` / `host.pause_script` | false / false | hot | 控制台上的两个暂停开关 |
| `host.compose_timeout_s` | 6 | hot | 写词超时，超时用兜底话术 |
| `host.reply_max_chars` | 40 | hot | 互动台词最大字数 |
| `host.answer_max_chars` | 70 | hot | 产品问答最大字数 |
| `persona.anchor_name` | "小美" | hot | 主播自称 |
| `persona.shop_name` | "" | hot | 店铺名 |
| `persona.prompt` | `policies/persona_commerce.md` 的内容 | hot | 人设提示词（多行） |
| `persona.honorific` | `{male: 哥, female: 姐, unknown: 家人}` | hot | 称呼后缀（参考 `_suffix_by_gender`） |
| `persona.rag_prompt` | 见 03 §9 | hot | 知识库回答提示词模板 |

### 2.4 语音与大模型 `tts.*`、`llm.*`

| 键 | 默认 | apply | 说明 |
|---|---|---|---|
| `tts.engine` | volc | hot | volc / edge / http / mock |
| `tts.fallback` | [edge, mock] | hot | 首选不可用时的回退顺序 |
| `tts.volc.voice_type` | 取 `.env` 的 `VOLC_VOICE_TYPE` | hot | 覆盖音色 |
| `tts.volc.resource_id` | 取 `.env` 或 seed-icl-2.0 | hot | |
| `tts.volc.sample_rate` | 16000 | hot | ⚠ 需验证；不支持则 24000 + 重采样 |
| `tts.volc.timeout_s` | 10 | hot | |
| `tts.speed` | 1.0 | hot | 默认语速（0.5–2.0） |
| `tts.cache.enabled` / `speed_round` / `mem_max_entries` | true / 0.1 / 1024 | hot | 合成缓存 |
| `tts.voice` | zh-CN-XiaoxiaoNeural | hot | edge 音色（现有） |
| `llm.temperature` | 0.8 | hot | |
| `llm.max_tokens` | 400 | hot | 混合推理模型要留够（参考项目注释） |
| `llm.stream` | true | hot | 流式按句播报 |
| `llm.history_size` | 5 | hot | 防复读记忆条数 |

### 2.5 知识库与讲品 `knowledge.*`、`products.*`

| 键 | 默认 | apply | 说明 |
|---|---|---|---|
| `knowledge.backend` | fts | hot | fts / bm25 / hybrid（hybrid 需 embedding） |
| `knowledge.top_k` | 4 | hot | |
| `knowledge.min_score` | 0 | hot | 低于此分数视为"资料没有" |
| `knowledge.chunk_chars` / `chunk_overlap` | 400 / 50 | restart（需重建索引） | 切块参数，修改后提示重建 |
| `knowledge.embedding.enabled` | false | hot | |
| `products.rotation_enabled` | true | hot | 空闲时讲品 |
| `products.duration_s` | 300 | hot | 每个产品默认讲多久再换下一个（产品可单独设置） |
| `products.segment_gap_s` | 2 | hot | 段间停顿 |
| `products.require_confirmed_script` | true | hot | 只播人工确认过的讲解稿 |

### 2.6 直播画面 `scene.*`

| 键 | 默认 | apply | 说明 |
|---|---|---|---|
| `scene.width` / `scene.height` | 720 / 1280 | hot | 画布尺寸（⚠ 比例需用户确认） |
| `scene.background` | "" | hot | 背景图（上传到素材库） |
| `scene.dh_rect` | `{x:0, y:160, w:720, h:720}` | hot | 数字人位置大小 |
| `scene.pip_rect` | `{x:440, y:60, w:260, h:260}` | hot | 画中画时数字人位置 |
| `scene.card.enabled` / `style` | true / default | hot | 产品卡片 |
| `scene.subtitle.enabled` | true | hot | 字幕 |
| `scene.ducking` | 0.3 | hot | 视频有原声时数字人音量系数 |
| `scene.video.default_mode` / `default_audio` | pip / video | hot | |

### 2.7 合规 `compliance.*`

| 键 | 默认 | apply | 说明 |
|---|---|---|---|
| `compliance.enabled` | true | hot | |
| `compliance.banned_words` | 内置广告法极限词表 + 平台禁语（最、第一、唯一、顶级、国家级、100%、万能、根治、包治、稳赚、加微信…） | hot | 可编辑列表 |
| `compliance.replacements` | `{最好: 很好, 第一: 很受欢迎…}` | hot | 替换表；没有替换的命中词直接删除 |
| `compliance.block_on_hit` | false | hot | true 时命中直接丢弃整句（改用兜底） |
| `compliance.ai_disclosure` | "本直播间为 AI 数字人直播" | hot | 场景页常驻标识文案 |

### 2.8 现有配置的归类

| 现有段 | apply |
|---|---|
| `speech.*`、`idle.*`、`stream.jpeg_quality`、`live.safety_factor/margin_s/max_ahead_chunks/interrupt_drop_queued` | hot |
| `models.*`、`gpu.*`、`server.*`、`live.fps/sample_rate` | restart |
| `offline.max_concurrent` | restart |
