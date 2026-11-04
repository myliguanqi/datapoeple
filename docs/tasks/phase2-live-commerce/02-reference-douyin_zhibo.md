# 02 参考项目分析：D:\jobdemo\douyin_zhibo

参考项目是一个"无人值守 AI 游戏直播解说"（Python 3.9），三层：感知（OCR）→ 决策（导演）→ 表达（LLM + TTS + 播放）。
我们**只要平台层**（弹幕/礼物/用户/互动/TTS/LLM），不要游戏层（OCR、推羊PK 逻辑、自动点击）。
执行前请自己再读一遍参考项目的 `ARCHITECTURE.md` 和 `CLAUDE.md`（"易踩坑"一节很有价值）。

## 1. 照抄 / 移植 / 改写 映射表

| 参考文件 | 本项目去向 | 处理方式 |
|---|---|---|
| `vendor/douyin_fetcher/`（整个目录） | `vendor/douyin_fetcher/` | **原样复制**，不改源码。AGPL-3.0，LICENSE 随附 |
| `danmaku/danmaku_source.py` | `live/danmaku/douyin/adapter.py`（解析）+ `live/danmaku/douyin/process.py`（子进程运行） | **移植改写**：保留 `EventDouyinFetcher` 子类接管 `_wsOnMessage`、cookie 注入、`_pb_dump` 探针、`_parse_cn_count`；**扩充为全字段解析**（§3）；去掉 `os.chdir`（改用绝对路径）；输出统一事件模型 |
| `decision/state_merger.py` | `live/interaction/state.py` | 移植：事件保鲜窗口（墙钟）、本场回头客 `_seen_members`、本场礼物榜 `_session_gifts`/`gift_top`、贡献榜名单；去掉 OCR 相关 |
| `decision/director.py` | `live/interaction/director.py` | 移植**平台层**：`_derive_platform`、触发器构建 `_build_triggers`、优先级/冷却/能量/发声间隔、同拍合并、礼物分档/回踩/榜一易主、欢迎分级、`_honor_by_gender`/`_short_name`；删除游戏信号；新增带货信号（§4.4） |
| `game_profiles/sheep_pk/triggers.yaml`（平台触发器部分） | `live/interaction/policies/commerce.yaml` | **改写**：保留结构和优先级思路，directive/兜底话术改成带货语境 |
| `game_profiles/sheep_pk/persona.md` | `live/interaction/policies/persona_commerce.md` | 改写为带货主播人设，保留合规红线 |
| `decision/user_store.py` | `live/interaction/memory.py` | 移植：建档门槛、`known_before`、节流落盘、原子替换；字段扩充（财富等级、粉丝团等级） |
| `live_main.py` 的 `CueQueue` | `live/interaction/queue.py` | 移植：优先级队列 + `max_stale_s` 过期丢弃 + 同级 FIFO |
| `expression/tts_client.py` | `live/tts/volc.py` | **照抄**核心（鉴权头、`_speech_rate`、`_read_sse_pcm`），适配本项目 `TTSEngine` 接口与 `.env`（§6） |
| `expression/tts_cache.py` | `live/tts/cache.py` | **照抄**思路：装饰器、两级缓存、键含 provider/speaker/语速桶/文本 |
| `expression/llm_client.py` | `live/llm/openai_compat.py` | 改写为通用 `chat(messages)`（非流式+流式）、`_extract_json` 容错保留；凭证改读 `.env` |
| `config.py` 的 `reload_config` / `get_secret` | `live/settings/` | 思路参考：热重载 + 密钥与配置分离；本项目改为三层设置中心 + 事件通知（见 03 §5） |
| `logger.py`、`perception/*`、`game_profiles/*` 其余、`auto_click` | —— | 不需要 |

## 2. 参考项目的弹幕适配层要点（必须保留的经验）

1. **接管消息分发**：父类 `_wsOnMessage` 用 `except: pass` 吞掉所有异常，且只解 gzip。适配层重写：gzip 解压失败就按未压缩解析；
   解析异常打 warning；`need_ack` 时回 ack；按 `_DISPATCH` 表分发。
2. **登录 cookie**：游客模式**收不到礼物**（`WebcastGiftMessage` 只推给登录态连接），其他消息都能收到。
   参考项目用模块级 monkeypatch `websocket.WebSocketApp`，只对 URL 含 `douyin`/`webcast` 的连接注入 cookie；HTTP 侧给 `session.headers["Cookie"]`。
   本项目把弹幕放进独立子进程后，monkeypatch 只影响该子进程，更安全。cookie 解析支持整条 curl / 请求头 / 纯串三种格式（`_extract_cookie`）。
3. **sign.js / a_bogus.js 路径**：fetcher 用相对路径加载 JS，参考项目靠 `os.chdir(FETCHER_DIR)`（进程级副作用）。
   本项目：子进程里 chdir 也可以，但更好的做法是把 `abogus_file` 传绝对路径，并在子类里重写签名调用用绝对路径读取 `sign.js`。
4. **心跳**：重写 `_sendHeartbeat` 去掉每 5 秒一次的 print。
5. **探针**：`_SNIFF` 开关打开时，记录"首次出现的 method"、裸解未接消息、礼物关键词诱捕。默认关。
6. **在线人数**：真实在线在 `RoomUserSeqMessage.online_user_for_anchor`（字符串，如 "1.2万"），不是 `total`；用 `_parse_cn_count` 解析。
7. **性别枚举**：1=男、2=女、0=未知（⚠ 参考项目注释提醒上播后要用已知性别的观众核对，反了就对调）。
8. **下播**：`ControlMessage.status == 3`。
9. 抖音改签名导致连不上时：用上游 DouyinLiveWebFetcher 的同名文件覆盖 `vendor/douyin_fetcher/`，适配层不动。

## 3. 抖音弹幕可拿到的字段全表

来源：`vendor/douyin_fetcher/protobuf/douyin.proto`（betterproto 生成的 Python 字段名是 snake_case，如 `nick_name`、`pay_grade`、
`total_diamond_count`、`url_list_list`；以 `protobuf/douyin.py` 为准）。**"实测"列要在 P2 用真实直播间核对后填写**——
协议里有字段不代表服务端会下发（手机号、生日、城市等隐私字段对其他观众基本为空）。

### 3.1 用户 `User`（几乎每种消息都带）

| 字段 | 含义 | 提取到 ViewerProfile | 备注 |
|---|---|---|---|
| `id` / `id_str` | 用户 ID | `user_id`（统一转字符串） | 长期记忆的主键 |
| `short_id`、`display_id`、`sec_uid` | 抖音号/展示 ID/安全 ID | `short_id`、`display_id`、`sec_uid` | |
| `nick_name` | 昵称 | `nickname` | |
| `gender` | 性别 1男2女0未知 | `gender`: "male"/"female"/"unknown" | 需实测核对枚举 |
| `level` | 用户等级 | `user_level` | |
| `pay_grade.level` | **财富等级**（观众榜"钻石X"的 X） | `wealth_level` | 参考项目据此派生 high_grade_member |
| `pay_grade.name` / `next_name` | 等级名 / 下一级名 | `wealth_name` | |
| `pay_grade.total_diamond_count` | 累计送出钻石 | `wealth_diamonds` | |
| `pay_grade.now_diamond` / `next_diamond` / `upgrade_need_consume` | 升级进度 | `wealth_progress`（dict） | 可用于"差一点升级"话术，谨慎使用 |
| `fans_club.data.club_name` / `level` / `user_fans_club_status` | **粉丝团**：团名/等级/状态 | `fans_club`: {name, level, status} | 判断是否本房粉丝团成员 |
| `follow_info.follower_count` / `following_count` / `follow_status` | 粉丝数/关注数/是否关注主播 | `followers`、`following`、`follow_status` | follow_status 是否表示"关注了本主播"需实测 |
| `avatar_thumb.url_list_list[0]` | 头像 URL | `avatar_url` | Web 面板显示用，**不落盘** |
| `verified` | 是否认证 | `verified` | |
| `badge_image_list`、`medal`、`real_time_icons_list` | 徽章/勋章 | `badges`（只留名称/URL 列表） | |
| `age_range` | 年龄段 | `age_range` | 多半为 0，需实测 |
| `city`、`signature` | 城市/签名 | 仅当非空时提取 | 隐私：不落盘 |
| `birthday`、`telephone` | 生日/手机 | **不提取** | 隐私红线，即使有值也丢弃 |

另外 Chat/Member/Social/Gift 消息带 `public_area_common`：`user_consume_in_room`（本场消费）、`user_send_gift_cnt_in_room`（本场送礼次数）
→ 放到事件的 `in_room` 字段。

### 3.2 各消息类型

| method | 统一事件 `type` | 关键字段 | 备注 |
|---|---|---|---|
| `WebcastChatMessage` | `chat` | `content`、`event_time`、`user` | |
| `WebcastEmojiChatMessage` | `chat`（`is_emoji=true`） | `default_content`、`emoji_id` | 内容为空时用 "[表情]" |
| `WebcastGiftMessage` | `gift` | `gift.name`、`gift.id`、`gift.diamond_count`（单价）、`combo_count`、`repeat_count`、`group_count`、`total_count`、`repeat_end`、`group_id`、`trace_id`、`to_user`、`send_time` | **需登录 cookie**；连击去重见 §5 |
| `WebcastLikeMessage` | `like` | `count`（本次）、`total`（累计） | user 往往无 id → 不进长期记忆 |
| `WebcastMemberMessage` | `member` | `member_count`、`enter_type`、`action`、`user` | 量大，日志只进文件 |
| `WebcastSocialMessage` | `follow` 或 `share` | `action`、`share_type`、`follow_count` | 参考项目全当关注；需实测区分 action/share_type |
| `WebcastFansclubMessage` | `fansclub` | `type`（1 升级 / 2 加入）、`content`、`user` | |
| `WebcastRoomUserSeqMessage` | `room_stat` | `online_user_for_anchor`（真在线）、`total_pv_for_anchor`、`popularity`、`ranks_list` | |
| `WebcastRoomRankMessage` | `rank` | `ranks_list[].user`、`score_str` | 冷场点名用 |
| `WebcastRoomStatsMessage` | `room_stats` | `display_long`、`display_value`、`total` | 参考项目未接，可接 |
| `WebcastRoomMessage` | `room_notice` | `content`、`system_top_msg` | 系统提示 |
| `WebcastControlMessage` | `stream_end`（status=3） | `status` | |
| `WebcastRoomStreamAdaptationMessage` | —— | | 噪音，丢弃 |
| `WebcastProductChangeMessage`（⚠ method 名需抓包确认） | `product_change` | `update_product_info_list`、`update_toast` | 带货房间商品上下架，协议里有定义，参考项目未接 |

所有事件统一加：`ts`（墙钟秒）、`room_id`（`common.room_id`）、`msg_id`（`common.msg_id`，去重用）、`platform="douyin"`、`raw_method`。

## 4. 参考项目的互动处理机制（要移植的思想）

### 4.1 核心理念
"**激情是算出来的，不是 LLM 即兴**"：规则决定"此刻该回应什么、用什么情绪、多高优先级"，LLM 只把指令写成口语；
LLM 慢了/挂了用兜底话术，绝不冷场。带货场景同理：规则决定先谢礼还是先答问题，LLM + 知识库负责把话说对。

### 4.2 数据流
弹幕事件 → `state_merger.on_event`（按类型进保鲜队列、回头客标记、本场礼物累计、喂长期记忆）→ 定时 `tick` 生成快照 →
`director.tick`（派生信号 → 按优先级挑触发器 → 冷却/节流判断 → 同拍合并 → 产出 `Cue{event, emotion, energy, speed, priority, directive, situation, fallback_text}`）
→ `CueQueue`（优先级、过期丢弃）→ 语音 worker（LLM 写词 → TTS → 播放，串行）。

### 4.3 参考项目的平台触发器（优先级供起步参考，带货场景需重新调）

| 触发器 | 信号 | 优先级 | 说明 |
|---|---|---|---|
| `gift_l` / `gift_m` / `gift_s` | 礼物按单笔总价值分档（S<10≤M<50≤L 钻） | 63 / 60 / 59 | 必报"谁+几个+什么礼物"，L 档最高规格 |
| `repeat_gift` | 同一人 3 分钟内连续送 | 61 | 重点维护 |
| `gift_king_dethroned` | 本场礼物榜一易主 | 57 | 只播事实排名，不承诺权益 |
| `has_new_follow` | 关注 | 50 | |
| `has_new_chat`（chat_reply） | 有内容的弹幕 | 47 | 先接住话，再给动作 |
| `gift_followup` | 送礼后 18s/45s 回踩 | 46 | 谢完不断，隔一会再抬一次 |
| `vip_member` | 往场金主回归（历史送礼 ≥ vip_gift_value 且 known_before） | 45 | |
| `high_grade_member` | 进场者财富等级 ≥ high_grade_level（默认 20） | 44 | 首次来的大佬也认 |
| `regular_member` | 往场熟客（历史进场 ≥ regular_visits） | 23 | |
| `returning_member` | 本场来过又回来 | 22 | |
| `viewer_surge` | 在线净涨 ≥ online_surge_min | 22 | **投流来人时承接** |
| `has_new_member` | 普通进场 | 20 | 轻点名，不刷屏 |
| `has_like` | 窗口内点赞 ≥ like_min_count | 18 | 同一人霸屏时冷却放大 |
| `viewer_drain` | 在线下滑 | 9 | 不说"人少"，给留下的理由 |
| `online_milestone` | 在线破整 | 7 | |
| `idle_interact` | 有在线观众名单 | 2 | 冷场点名 |
| `idle` | always | 0 | 平静期碎碎念 |

其他机制：`urgent_priority`（≥ 此值无视发声间隔）、能量衰减与动态发声间隔、`like_repeat_cooldown_factor`、
同拍合并（如"礼物+关注"合成一句）、`_handled_*_ts` 防同一事件被两个触发器重复念、模板占位符（`{member_user}`、`{gift_name}`…）。

### 4.4 带货场景需要新增的信号（参考项目没有）

| 信号 | 判定 | 建议优先级 | 动作 |
|---|---|---|---|
| `product_question` | 弹幕命中产品意图（价格/规格/尺码/成分/保质期/发货/售后/怎么买/链接/优惠…）或 LLM 分类为提问 | 高于普通弹幕（如 52） | 知识库检索 + LLM 回答 |
| `purchase_intent` | "要了/怎么拍/几号链接/下单" | 高（如 55） | 引导去购物车 + 当前机制（合规） |
| `product_rotation` | 空闲且讲品开启 | 低（如 3） | 播讲解稿下一段（断点续讲） |
| `video_slot` | 讲品流程到了某产品视频节点 / 定时 | 中低（如 5） | 场景播放产品视频 |
| `chitchat` | 普通弹幕 | 沿用 chat_reply | LLM 闲聊式回应，拉回产品 |

### 4.5 长期记忆规则（照搬）
- 只对**发过弹幕/送过礼/关注过**的人建档；纯进房不落盘（只更新已建档者的 visits）。
- `known_before`：进程启动前就在库里的人才算熟客/金主；本场新来的人下一场才"升级"。
- like 事件无 user_id，不入库。落盘节流（默认 30s）+ 原子替换；退出时 flush。
- 本项目额外记录：最高财富等级、粉丝团等级、最近一次提问的产品（用于"上次问的 XX 今天有货"之类话术，可选）。

## 5. 已知坑（执行时逐条处理）

| 坑 | 处理 |
|---|---|
| 游客模式收不到礼物 | 必须带登录 cookie；Web 状态显示"登录态/游客态"；cookie 约 2 个月过期，礼物突然收不到时提示重抓 |
| 连击礼物多次推送 | 同一连击会推多条（`combo_count` 递增，`repeat_end=1` 结束）。按 `group_id`/`trace_id`+用户 聚合，**结束时结算一次**；超时（如 3s 无新包）也结算。⚠ 需实测确认，先在 P2 打开探针录一段真实数据 |
| `chdir` 与全局 monkeypatch | 放子进程隔离；能用绝对路径就不用 chdir |
| PyExecJS 需要 Node.js | `a_bogus.js` 走 Node；`sign.js` 走 mini_racer 自带 V8。缺 Node 时 `get_room_status` 失败，但不影响收消息 |
| 依赖版本 | vendor 的 requirements：`betterproto==2.0.0b6`、`websocket-client==1.7.0`、`PyExecJS==1.5.1`、`mini_racer==0.12.4`、`requests`。本项目环境是 Python 3.10，先在 P0 验证可装；**不要**让它们升级/降级本项目已锁定的包（`protobuf<4` 给 mediapipe） |
| 性别枚举可能反 | 上播后用已知性别的号核对，配置项 `danmaku.gender_map` 可对调 |
| 抖音改签名 | 整体替换 vendor 目录；适配层只依赖 `DouyinLiveWebFetcher` 的少量方法（列在适配层文件头注释里） |
| 弹幕量大 | 进场/点赞量很大：日志分级（进场只进文件）；Web 面板做节流（合并点赞、进场折叠）；事件总线有界队列 |

## 6. 火山 TTS 接入要点（照抄 `expression/tts_client.py`）

- 端点：`https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse`（V3 HTTP SSE 单向流式，声音复刻）。
- 鉴权头：新控制台 `X-Api-Key`；旧控制台回退 `X-Api-App-Key` + `X-Api-Access-Key`；另有 `X-Api-Resource-Id`（`seed-icl-2.0` 等）、`X-Api-Request-Id`（uuid）。
- 请求体：`{"user":{"uid":...},"req_params":{"text","speaker","audio_params":{"format":"pcm","sample_rate":...,"speech_rate":...}}}`。
  本项目数字人要 16k 单声道：**优先直接请求 `sample_rate: 16000`**（⚠ 需验证服务端支持；不支持就请求 24000 再重采样到 16k）。
- SSE 解析：逐行 `data: {json}`，取 `data`/`audio`/`audio_data` 的 base64 累积 PCM；`code` 不在 `(None, 0, 3000, 20000000)` 时告警跳过。
- `speed → speech_rate`：`int(round((speed-1)*100))`，夹到 [-50, 100]。V3 声音复刻不下发 emotion。
- **一旦合成即计费**：测试用 mock；只保留 `python -m live.tts.volc "测试一句"` 手动自测（打印 HTTP 状态、`X-Tt-Logid`、前几条 SSE）。
- 缓存：照搬 `CachingTTSClient`（换音色/后端自动失效；失败不缓存），缓存目录 `live_data/cache/tts/`。

## 7. LLM 客户端要点（改写 `expression/llm_client.py`）

- OpenAI 兼容：`POST {LLM_API_URL}/chat/completions`，`Authorization: Bearer {LLM_API_KEY}`。
  `LLM_API_URL` 允许写到 `/v1` 或完整 `/chat/completions`，代码里规范化。
- 保留：`_extract_json`（去 ```json 围栏）、`history` 防复读（`note_spoken`）、超时回退。
- 新增：流式（SSE `data:` 行、`[DONE]`），按句切分后逐句交给 TTS；`max_tokens`、`temperature`、`timeout` 可热更新。
- 参考项目用 DeepSeek（`deepseek-v4-flash`，注释提到混合推理模型 max_tokens 要留够）——这是用户已有可用的模型，作为 `.env.example` 的示例值。
