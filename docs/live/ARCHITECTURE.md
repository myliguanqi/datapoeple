# 架构与原理

## 1. 进程与线程

```
┌──────────────────────────── 主进程（不导入 torch）────────────────────────────┐
│ FastAPI / uvicorn（asyncio 事件循环）                                          │
│   REST /api/...   WebSocket /ws/sessions/{sid}   静态网页 /                     │
│                                                                               │
│ SessionManager ─┬─ LiveSession "default" ──────────────────────────────┐       │
│                 │    TTS 线程 ─▶ SpeechBuffer ─▶ 生成线程 ─▶ 帧队列 ─▶ 播放线程 ─▶ sinks
│                 │                                  │ RPC                  (WsHub 等)
│                 └─ LiveSession "room2" ...         │                              │
│ OfflineManager ── 离线任务线程 ─────────────────────┤ RPC                          │
│ WorkerPool ─── WorkerHandle#0 ────────────────────┤                              │
│             └─ WorkerHandle#1 ────────────────────┘                              │
└──────────────────────────────┼──────────────────────────────┼─────────────────┘
          multiprocessing Queue │ (priority, req_id, method, kwargs)
┌──────────────────────────────▼───────┐   ┌──────────────────▼──────────────────┐
│ worker0 子进程  CUDA_VISIBLE_DEVICES=0 │   │ worker1 子进程  CUDA_VISIBLE_DEVICES=1 │
│ FlashHeadRunner / MockRunner          │   │ 同左                                  │
│  pipes = {lite, pro}（各一个官方       │   │                                      │
│  FlashHeadPipeline）                   │   │                                      │
│  sessions = {sid: 会话状态}            │   │                                      │
└───────────────────────────────────────┘   └──────────────────────────────────────┘
```

主进程里的服务由 `live/app_context.py` 的 **AppContext** 统一构造、互相注入（见 §9），FastAPI 路由按功能拆在 `live/api/*.py`，
从 `request.app.state.ctx` 取服务；`live/server.py` 只是 app 工厂。

- **主进程不碰 GPU**：只做调度、TTS、时钟、编码、网络。一张卡出问题（OOM、驱动异常）只会让对应 worker 退出，
  服务本身和另一张卡不受影响。
- **一张卡一个进程**：Windows 没有 NCCL，用不了官方 torchrun 多卡；每卡独立进程既能让两张卡同时工作，
  也避开了 Python GIL 对两路推理的互相拖慢。
- **worker 单线程串行**：每取一条命令前，先把积压的命令按 `priority` 排序。直播 generate 是 0，离线任务是 10，
  所以同卡上离线任务不会插到直播前面（但正在执行的那一块要等它算完，这也是建议直播和离线分卡的原因）。

## 2. 块（chunk）

FlashHead 是自回归逐块生成的（见官方 `generate_video.py --audio_encode_mode stream`）：

| | Lite | Pro |
|---|---|---|
| VAE 时间压缩 | 8（LTX） | 4（Wan） |
| 运动帧 motion_frames = (2-1)×压缩+1 | 9 | 5 |
| 每块新帧 = 33 - motion_frames | 24 帧 = 0.96s | 28 帧 = 1.12s |
| 每块音频 = 帧数 × 640 | 15360 采样 | 17920 采样 |

每次 `generate` 输入一块音频，worker 把它追加到这个会话自己的 **8 秒音频滑窗** 里，
对整个滑窗跑 wav2vec，取最后 33 帧的特征（每帧带前后各 2 帧上下文），生成 33 帧后丢掉前面的运动帧，
返回新的 24/28 帧。**返回的帧和输入的这块音频严格一一对应**（第 i 帧 ↔ 第 i 个 640 采样），
音画同步就建立在这个对应关系上。

块与块之间靠"上一块最后几帧重新编码成的运动帧 latent"接续，所以：
- 空闲 → 说话 → 空闲 只是喂的音频内容变了，模型状态是连续的，不会跳；
- 不管有没有语音，生成都不能停，否则画面就静止了 —— 这就是"空闲音频"存在的原因。

## 3. 直播会话的三个线程（`live/session.py`）

### TTS 线程
文本 → `split_sentences`（句末标点切分，超过 `max_sentence_chars` 再按逗号切）→ 逐句 TTS
→ 响度归一化（`speech.target_rms_db`）+ 首尾淡入淡出 → 作为一段 `Segment(seg_id, text, pcm)` 写入 `SpeechBuffer`。
第一句合成完就能开始播，后面的句子在播放期间继续合成。

### 生成线程：按需提前调度
目标：语音尽量晚地确定（降低延迟），又不能让播放队列断（不补帧）。

```
lead = 生成耗时EMA × safety_factor + margin_s
等待，直到  已缓冲帧时长 ≤ lead  且  已缓冲 < max_ahead_chunks 块
取一块音频：SpeechBuffer 里有多少语音就取多少（从块开头连续填），不足的部分用 IdleSource 补
worker.generate(...)  → 帧
帧 + 对应音频 + 每帧所属 seg_id → 播放队列
```

生成完成时队列里还剩 `lead - 生成耗时 ≈ 生成耗时×0.3 + 0.15s` 的缓冲，正好接上。
切模型、换形象这类控制命令也在这个线程里、两块之间执行，天然与生成串行。

**延迟**（文本进来到听到声音）≈ TTS 首句耗时 + 等到下一个"取音频"时刻（0 ~ 1 块）+ 生成一块 + 剩余缓冲
+ 客户端抖动缓冲。mock 下（TTS 瞬时、lite 生成 0.29s/块）服务端出帧延迟实测 1.3~1.5s；
真实环境再加 edge-tts 首句约 1.5s。

### 播放线程：节拍器
按 `1/fps` 的绝对时间表（不累积误差）每次取一帧 + 对应的 640 个采样，打包成 `AVFrame` 分发给所有 sink。
- 队列空（显卡跟不上）：重复上一帧 + 静音，标记 `filler`，输出不断流；
- 卡顿超过 1 秒：重新对齐节拍，不追帧；
- Windows 下启动时调用 `timeBeginPeriod(1)`，把 sleep 精度从 15ms 提到 1ms；
- 根据帧上的 seg_id 变化发 `playback start/end`、`speak_done` 事件。

## 4. 音画同步

- **服务端**：帧和音频在生成时就锁在一起（每帧 640 采样），播放线程按帧发出，一个包里同时有画面和它那 40ms 的声音。
- **客户端**（`web/player.js`）：音频包按到达顺序用 WebAudio 无缝排队（`AudioBufferSourceNode.start(nextTime)`），
  每个包的画面记下"它的音频开始播放的时间"；渲染循环用 `getOutputTimestamp()` 得到当前真正在发声的时间，
  显示不晚于该时间的最新一帧。画面跟着声音走，网络抖动只影响延迟，不影响同步。
- 抖动缓冲 `latency`（网页上的「缓冲」滑块）：缓冲见底会重新留出 latency；积压超过 latency+0.6s 会丢包追实时。

## 5. Lite / Pro 切换接续

`FlashHeadRunner.switch_model`：
1. 保存这个会话在旧模型上的状态，旧模型不再为它占位；
2. 新模型对同一张参考图 `prepare_params`；
3. 取旧模型最后输出的 `motion_frames` 帧（像素，worker 每块都保留最后 16 帧），
   用**新模型的 VAE** 编码成运动帧 latent，替换掉 prepare_params 默认的"参考图首帧"。

所以切换后的第一块会从旧画面的最后姿态接着动，而不是跳回参考图的姿态。8 秒音频滑窗也原样保留（两个模型用同一个 wav2vec）。
`models.preload` 里没有的模型会在切换时现加载，期间生成线程阻塞，画面会补帧（停住），所以建议两个都预加载。

## 6. 同一个模型服务多个会话

官方 `FlashHeadPipeline` 把权重和会话状态放在同一个对象上（参考图 latent、运动帧、随机数发生器……）。
runner 为每个模型记录"当前装在 pipeline 上的是哪个会话"，切到别的会话时保存/恢复 `STATE_KEYS` 里的属性。
`tests/live/test_runner_logic.py` 验证过：多个会话交替生成时，每个会话的输出与它单独生成时逐位一致。
这让同一张卡可以同时跑直播 + 离线任务，或多路 Lite 直播（官方说 4090 可 3 路 Lite 实时）。

## 7. 多卡调度（`live/pool.py`）

- `pick("live")`：优先 `gpu.live_devices`，在其中挑直播会话最少的卡；
- `pick("offline")`：优先 `gpu.offline_devices`，挑离线任务最少的卡；
- 都不可用时退回任意就绪的卡；创建会话时也可以用 `gpu_id` 指定。

## 8. 失败处理

| 情况 | 行为 |
|---|---|
| worker 启动失败（缺模型、CUDA 不可用） | 状态 `error`，错误原文在 `/api/system` 与网页「系统」卡片；其他 worker 正常 |
| 单次 generate 抛异常 | 会话记 `gen_errors`，推 `error` 事件，0.5s 后重试，画面用补帧顶住 |
| worker 进程退出 | 所有挂起的调用失败，会话置为 `error` 并停止；需要重建会话（自动重启 worker 是待办项） |
| TTS 失败 | 跳过这一句，推 `error` 事件，后面的句子照常 |
| 客户端慢 | 每个 WebSocket 客户端最多积压 `stream.client_queue_frames` 帧，超出丢最旧的，不影响其他客户端 |

## 9. 主进程服务与解耦（第二阶段）

```
AppContext（live/app_context.py）
  ├─ SettingsCenter（live/settings/store.py）── 运行配置 cfg（所有模块共享同一个 dict）
  │     └─ EventBus（live/core/bus.py）  settings.changed / env.changed / live.event / …
  ├─ Env（live/settings/env.py）        .env 密钥，只给"已配置/未配置"
  ├─ TTSService（live/tts/service.py）   内部引擎可热切换
  ├─ WorkerPool / SessionManager / OfflineManager（现有）
  └─ EventFeed（live/feed.py）         总线 → /ws/console（Web 控制台推送）
```

模块之间只走三种通道：**事件总线**（异步广播）、**显式接口调用**（构造时注入）、**HTTP API**（给 Web / 外部系统）。

### 9.1 事件总线

`EventBus.subscribe(pattern, handler, queue_size=1000)`：每个订阅者一个有界队列 + 一个工作线程，
`publish()` 永不阻塞；慢订阅者队列满了丢最旧的并计数（`bus.stats()` 可看）；handler 抛异常只记日志，不影响别人。
主题用 fnmatch 匹配（`live.*`、`*`）。

| 主题 | 发布者 | 订阅者 |
|---|---|---|
| `settings.changed` | SettingsCenter | 所有 Reconfigurable、EventFeed |
| `env.changed` | Env.reload | LLM/TTS 客户端（P3 起）、EventFeed |
| `live.event` | DanmakuManager（子进程事件 / 模拟注入） | EventFeed（节流后推 Web）、Recorder（JSONL 录制）、互动引擎与长期记忆（P4） |
| `danmaku.status` | DanmakuManager（状态变化 + 每 5 秒） | EventFeed（Web 状态灯） |

### 9.2 设置中心与热更新

三层合并（后者覆盖前者）：`live/settings/schema.py` 的默认值 ← `live/configs/*.yaml` ← `live_data/settings.json`（Web 写入）
← 启动参数（`--port` 等，Web 上不能改）。每个设置项在 schema 里只定义一次：类型、默认、范围、中文说明、Web 分组、`hot`/`restart`。
`live/config.py` 的 `DEFAULTS` 由 schema 生成，`load_config()` 保留给 worker 和测试。

```
PUT /api/settings {changes}
  → 逐项校验（类型 / 范围 / 枚举 / 跨字段）→ 有错整批不生效，返回逐项错误
  → 写 live_data/settings.json（临时文件 + os.replace）+ 版本快照 live_data/settings_history/（最近 20 份）
  → hot 项：原地改共享的 cfg → 发布 settings.changed {keys, restart_keys, old, new, version}
  → restart 项：只保存，GET /api/settings 里标 pending_restart
```

热更新的两种方式：
- 模块每次使用时读 cfg（`speech.*`、`live.safety_factor/margin_s/max_ahead_chunks`、`stream.*`、`tts.voice`、
  `tts.max_sentence_chars`）——改完下一句/下一块就生效；
- 缓存了对象的模块实现 `Reconfigurable.apply_settings(view, changed)`，经 `SettingsCenter.register(obj, 前缀)` 订阅：
  `TTSService`（`tts.engine`、`tts.edge.*` → 重建引擎）、`SessionManager`（`idle.*` → 各会话重建空闲音频源，新配置不可用时保留旧的并推 error 事件）。

### 9.3 写操作鉴权

所有 `/api/` 下的 POST/PUT/PATCH/DELETE 由 `server.py` 的中间件统一检查（新加的写接口自动受保护）：
`.env` 设了 `ADMIN_TOKEN` 时必须带请求头 `X-Admin-Token`，否则 401；没设时只允许本机（127.0.0.1 / ::1），其他来源 403。
画面 WebSocket 里的 speak/interrupt 控制消息同样要求写权限（`?token=`），只看画面不受限。

## 10. 弹幕接入（第二阶段 P2）

```
                 ┌─────────────── 弹幕子进程（每个直播间一个，multiprocessing spawn）───────────────┐
抖音 WSS ──▶     │ vendor/douyin_fetcher（原样）← EventDouyinFetcher（adapter.py）：接管消息分发、       │
                 │   注入登录 cookie、心跳静音、协议探针 → parse.py 纯函数 → GiftComboAggregator → 去重  │
 或 Mock/Replay  │ → out_q: ("event", LiveEvent.to_dict()) / ("status", {...})                          │
                 └──────────────────────────────▲───────────────┬──────────────────────────────────────┘
                                   ctrl_q: stop / set_sniff      │ out_q
┌──────────────────────── 主进程 ───────────────────────────────┴───────────────────────────────────────┐
│ DanmakuManager：起/停子进程、退避重启（reconnect_delay_s 起翻倍，上限 60s，连上清零）、热切换房间      │
│   └─ bus.publish("live.event") ─▶ EventFeed（点赞合并 / 进场折叠 / 在线人数限频）─▶ /ws/console      │
│                                ├─▶ Recorder（danmaku.record 打开时写 live_data/events/*.jsonl）      │
│                                └─▶ 互动引擎（P4）                                                    │
│ 模拟注入 POST /api/danmaku/simulate ─▶ DanmakuManager.inject()（不经过子进程）                        │
└───────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

为什么放子进程：vendor 代码（AGPL-3.0）、对 `websocket.WebSocketApp` 的全局 monkeypatch（注入 cookie）、JS 引擎
（`sign.js` 走 mini_racer 内置 V8，`a_bogus.js` 走 PyExecJS + Node.js）都隔离在里面，崩溃、卡死不影响直播主进程；
改房间号 = 重启子进程。vendor 原样不改：`sign.js` 的绝对路径是在子进程里把 `liveMan.generateSignature` 换成带
`script_file` 的 partial，`a_bogus.js` 通过构造参数传绝对路径，不 `chdir`。

### 10.1 统一事件模型（`live/core/events.py`）

所有平台都转成 `LiveEvent{type, platform, room_id, ts(墙钟), msg_id, user: ViewerProfile, data, in_room, raw_method}`，
可 JSON 序列化（子进程传输、录制回放、Web 推送都用 `to_dict()`）。`ViewerProfile` 含 user_id、昵称、性别（male/female/unknown）、
财富等级/名称/累计钻石、粉丝团 {name, level, status}、用户等级、粉丝数、关注状态、认证、头像（只展示不落盘）、徽章；
平台特有字段（抖音号、sec_uid、年龄段、城市、签名、升级进度……）放 `extra`。**生日、手机号不提取。**

| type | data |
|---|---|
| chat | `content, is_emoji` |
| gift | `gift_id, gift_name, unit_diamonds, count, total_diamonds, combo_id, is_final, comboable, to_user, raw{…}` |
| like | `count, total`（抖音点赞的 user 往往没有 id） |
| member | `member_count, enter_type, action` |
| follow / share | `action, share_type, share_target, follow_count` |
| fansclub | `kind: join / upgrade, content` |
| room_stat | `online`（`online_user_for_anchor` 解析，"1.2万"→12000）`, total_pv, popularity, total_user` |
| rank | `ranks: [{user_id, nickname, score}]` |
| room_stats / room_notice / product_change / stream_end | 见 `live/core/events.py` 文件头 |

`in_room`：`{consume: 本场消费, gift_count: 本场送礼次数}`（抖音 public_area_common）。

### 10.2 连击礼物

同一连击抖音会推多条。`GiftComboAggregator` 按 `combo_id = 用户:礼物:group_id(或 trace_id)` 聚合：不可连击的礼物直接结算；
连击中产出 `is_final=False` 的进度事件（Web 不显示）；`repeat_end=1` 或 `danmaku.combo_timeout_s`（默认 3s）没有新包时结算一次
`is_final=True`（数量取最大值）；结算后 5 秒内同一连击的重复推送丢弃。**互动引擎只处理 `is_final=True` 的礼物。**
单次推送的数量 = max(combo_count, repeat_count)，group_count > 1 时再乘 group_count —— ⚠ 推送规律需要上播实测核对。
