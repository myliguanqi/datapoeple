# 接口文档

默认地址 `http://<host>:8000`。所有 JSON 接口出错时返回 `{"detail": "原因"}` 和对应 HTTP 状态码。
FastAPI 自带的交互式文档：`/docs`。

## 鉴权（写操作）

所有 `/api/` 下的 **POST / PUT / PATCH / DELETE** 都要求写权限：

| `.env` 的 `ADMIN_TOKEN` | 规则 |
|---|---|
| 已设置 | 请求头 `X-Admin-Token: <口令>`，不对返回 **401** |
| 未设置 | 只允许本机（127.0.0.1 / ::1）调用，其他来源返回 **403** |

GET 接口与画面推流不受限（局域网里其他人能看画面，但不能改）。画面 WebSocket 里的 `speak` / `interrupt` 控制消息同样需要写权限，
口令放 URL：`/ws/sessions/default?token=<口令>`（本机且未设口令时不需要）；`hello` 消息里的 `can_write` 表示这条连接能否发控制消息。
网页客户端在收到 401 时会弹框要口令，存在浏览器本地。

## 网页

| 地址 | 说明 |
|---|---|
| `/` | 完整控制台（侧边栏分页，hash 路由：`#/console` 直播控制台、`#/danmaku` 弹幕互动、`#/products` 产品与知识库、`#/media` 视频素材、`#/settings` 直播间设置、`#/system` 数字人与系统、`#/offline` 离线视频）。参数：`session=`、`autoconnect=1`、`latency=0.3`、`subs=0` |
| `/?view=1&session=default` | 纯画面模式：全屏画面 + 字幕，自动连接、断线重连（只有数字人） |
| `/scene?session=default` | **直播场景页**（OBS 浏览器源，宽高 = `scene.width × scene.height`，默认 720×1280）：背景 + 视频 + 数字人 + 产品卡片 + 字幕 + 常驻 AI 标识；参数 `w=&h=`（覆盖画布尺寸）、`debug=1`（布局框和状态）、`latency=0.3`、`preview=1`（静音预览） |
| `/web/player.js` | 播放器模块（ES module），可在自己的页面里复用，用法见文件头注释 |

## 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system?refresh=false` | 版本、后端、多卡模式、每个 worker 的状态/显卡/已加载模型/错误、会话列表。`refresh=true` 会向 worker 实时查询显存。另含 `services`（各服务状态）：`tts` `{requested, engine(实际在用), chain, error(首选不可用原因), warning(超每日上限等), calls, failures, fallbacks, last_ms, last_engine, last_error, cache{enabled, hits, miss, rate, mem_entries}, today_chars{引擎: 字数}, daily_max_chars}`；`llm` `{provider, model, available, reason, calls, failures, last_ms, last_ttft_ms, last_error, today_calls, daily_max_calls}`；`danmaku` 同 `/api/danmaku/status`、`interaction`（互动引擎计数）、`knowledge` 同 `/api/kb/status`、`admin.token_set`、`settings.{version, pending_restart}` |
| GET | `/api/tts/voices` | `{"engine", "default", "voices": [{"id", "name"}]}` |

## 设置中心

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/settings` | | `{version, groups:[{id,label}], fields:[…], pending_restart:[键]}`。每个 field：`key type label help group apply(hot\|restart) min max choices nullable item_type item_choices default value(保存后的值) running(运行中的值) source(default\|yaml\|runtime\|cli) pending_restart locked` |
| PUT | `/api/settings` | `{"changes": {"speech.target_rms_db": -16, …}}` | 校验通过后保存并热应用。返回 `{ok, applied:[即时生效的键], restart:[需重启的键], version, pending_restart}`；校验失败 **400** `{"detail", "errors": {键: 原因}}`，整批不生效 |
| POST | `/api/settings/reset` | `{"keys": [...]}` 或 `{}` | 去掉 Web 覆盖（不给 keys = 全部），回到配置文件/默认值 |
| GET | `/api/settings/history` | | 最近 20 份版本 `[{version, ts, source, keys, current}]` |
| POST | `/api/settings/rollback/{version}` | | 恢复该版本时的 Web 覆盖；版本不存在 404 |
| GET | `/api/settings/export` | | `{version, overrides}` |
| POST | `/api/settings/import` | `{"overrides": {...}}` | 整体替换 Web 覆盖层 |
| POST | `/api/settings/reload-env` | | 重新读 `.env`，返回 `{changed:[键名], secrets:[…]}`，并发布 `env.changed` |
| GET | `/api/settings/secrets-status` | | `.env` 各键 `[{key, secret, desc, configured, source(file\|env\|null), value(仅非密钥项)}]`，**密钥值永不返回** |

## 弹幕

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/danmaku/status` | | `{state(stopped / starting / connecting / connected / backoff / disconnected / error / finished), enabled, source, live_id, room_id, logged_in, pid, events, restarts, last_event_ts, next_retry_in, error, cookie{configured, length, age_days, has_sessionid}, counters{类型: 数量}, last_online}` |
| POST | `/api/danmaku/connect` | `{live_id?, source?}` | 写入设置 `danmaku.enabled=true`（+ 房间号/来源）并（重新）连接；source=douyin 且没有房间号时 400 |
| POST | `/api/danmaku/disconnect` | | `danmaku.enabled=false`，停掉子进程 |
| GET | `/api/danmaku/events?limit=100&type=chat,gift` | | 最近的原始事件（未节流，`LiveEvent.to_dict()` 列表，旧的在前） |
| POST | `/api/danmaku/simulate` | `{type, nickname?, user_id?, gender?, wealth_level?, fans_level?, content?, gift_name?, unit_diamonds?, count?, online?, online_delta?, kind?, data?}` | 模拟注入一条事件（platform=simulate，不经过子进程）。chat 不给 content 时随机一句产品提问；room_stat 可用 `online_delta` 在当前在线人数上加减（人数猛涨/下滑） |
| POST | `/api/danmaku/mock/start` | `{rate_per_min?}` | 切到模拟弹幕源并连接 |
| POST | `/api/danmaku/mock/stop` | | 停止模拟弹幕源 |
| POST | `/api/danmaku/replay` | `{file, speed?, loop?}` | 回放 JSONL（只给文件名时在 `live_data/events/` 里找） |
| GET | `/api/danmaku/recordings` | | 录制文件列表 `[{name, path, size, mtime}]` |
| PUT | `/api/danmaku/cookie` | `{cookie}` | 保存抖音登录 cookie（支持整条 curl / `cookie:` 请求头 / `k=v; k=v`），**只写不读**，返回 `{configured, length, updated_at, age_days, has_sessionid}`；正在连抖音时自动重连 |
| DELETE | `/api/danmaku/cookie` | | 删除本机保存的 cookie |

## 互动与主持人

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/interaction/triggers` | | 生效中的触发器表（出厂 + 覆盖），按优先级从高到低：`{name, when, priority, emotion, target_energy, cooldown_s, max_stale_s, use_llm, role, merge_clause, merge_with, directive, lines, enabled, overridden:[改过的字段], default:{出厂值}, last_fired}` |
| PUT | `/api/interaction/triggers/{name}` | `{enabled?, priority?, cooldown_s?, emotion?, target_energy?, directive?, lines?, max_stale_s?, use_llm?}` | 修改一个触发器（写入设置 `interaction.triggers`，立即生效、可在设置历史里回滚）；未知触发器 404，priority 不在 0~100 时 400 |
| POST | `/api/interaction/triggers/{name}/reset` | | 恢复出厂 |
| GET | `/api/interaction/queue` | | 排队中的 cue `[{id, event, priority, kind, waited_s, stale_s, text, reason}]` |
| GET | `/api/interaction/log?limit=100` | | 决策日志（新的在前）：`{id, ts, event, kind, priority, reason, source{msg_id, nickname, content}, status(queued/composing/speaking/spoken/interrupted/dropped/failed/timeout/empty), text, text_source(template/llm/knowledge/manual/script), fallback_text, compliance_hits, compose_ms, latency_ms, request_id, duration_s, note, refs(知识库依据的片段)}` |
| POST | `/api/interaction/pause` / `resume` | | 暂停 / 恢复互动（设置 `host.pause_interaction`；暂停时清空队列） |
| GET | `/api/host/status` | | `{enabled, session_id, session_ok, current{cue_id, event, kind, priority, reason, text, text_source, request_id, interruptible, elapsed_s}, queue[前 10 条], queue_size, pause_interaction, pause_script, script_active, video_blocking(正在放带原声的视频、讲品让路), script, history[最近 10 条]}`；`script` = `{product_id, product_name, segment(下一段/正在讲第几段，从 1 起), segments(可播段数), speaking, next_text, elapsed_s, duration_s, spoken, rotations, inactive_reason, rotation[{id, name}]}` |
| POST | `/api/host/say` | `{text, interrupt?}` | 人工插话：优先级 `host.manual_priority`（默认 90），过合规；`interrupt=true` 先打断当前播报。返回 `{cue_id, priority}` |
| POST | `/api/host/interrupt` | | 打断主持人当前播报 |
| POST | `/api/host/script/pause` / `resume` | | 暂停 / 恢复讲品（设置 `host.pause_script`） |
| POST | `/api/host/script/next` | | 跳到下一段（正在讲讲品段时先打断它）；没有可讲的讲解稿 409（`detail` 说明原因）。返回 `/api/host/status` |
| POST | `/api/host/product/{id}/activate` | | 设为当前讲解的产品（正在讲的讲品段被打断，立即换；话术 `{product}` 与知识库检索范围跟着换）；没有该产品 404 |
| POST | `/api/host/product/next` / `prev` | | 在启用的产品里切到下一个 / 上一个；没有启用的产品 409 |

## 产品与知识库

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/products` | | `{items[产品], active_id, script(同 host status 的 script)}`，按轮播顺序。产品：`{id, name, price_text, selling_points[], segments[{id, text, confirmed, source(manual/llm/template), hits[], updated_at}], video_ids[], enabled, order, duration_s(null=默认), notes, created_at, updated_at, docs(资料数), playable(可播段数), is_current}` |
| POST | `/api/products` | `{name, price_text?, selling_points?(列表或换行分隔), enabled?, duration_s?, notes?}` | 新建（排在最后）；名称空 / 超 60 字 / 时长不在 30~7200 → 400 |
| GET | `/api/products/{id}` | | 产品 + `doc_list`（它的资料） |
| PUT | `/api/products/{id}` | 同上任意字段 | 修改；`duration_s: null` = 用 `products.duration_s` |
| DELETE | `/api/products/{id}?delete_docs=true` | | 删除产品；默认连同它的资料一起删（`delete_docs=false` 则资料转为公共资料）。返回 `{ok, deleted_docs}` |
| PUT | `/api/products/order` | `{ids: [...]}` | 轮播顺序（没列出的排在后面） |
| PUT | `/api/products/{id}/script` | `{segments: [{id?, text, confirmed}]}` | 保存讲解稿（≤ 50 段、每段 ≤ 500 字）；文本改过的段来源记为 manual |
| POST | `/api/products/{id}/script/generate` | `{segments?: 2~12, replace_unconfirmed?: true}` | 用知识库生成讲解稿（LLM；不可用时模板草稿），每段过合规、标待确认；默认替换旧的待确认段、保留已确认段。返回 `{product, generated, source(llm/template), error(为什么没用 LLM), refs(用到的资料块数), ms}` |
| POST | `/api/products/{id}/script/confirm` | `{ids?: [...], confirmed?: true}` | 确认（或取消确认）指定段，不给 ids = 全部 |
| GET | `/api/kb/status` | | `{backend(设置的), active_backend(实际在用), fts5, warning, docs, by_status{状态: 数量}, chunks, stale_docs(切块参数改过待重建), index{…}, supported[扩展名], max_doc_mb}` |
| GET | `/api/kb/docs?product_id=` | | 文档列表：不带参数 = 全部；`product_id=`（空）= 公共资料；`product_id=xxx` = 该产品。每项 `{id, name, ext, product_id, size, sha1, title, pages, chunks, chars, status(queued/parsing/indexing/ready/failed), error, chunk_chars, chunk_overlap, created_at, updated_at, stale}` |
| POST | `/api/kb/docs` | multipart：`files`（可多个；md/markdown/txt/html/htm/pdf）、`product_id`（空 = 公共资料） | 上传并排队入库，立即返回各文件的记录（`status=queued`；内容与已有文档相同则返回已有记录并带 `duplicate: true`；单个文件不合格的那项 `status=rejected` + `error`；全部不合格 400）。存盘文件名是随机 ID；进度经 `/ws/console` 的 `kb.ingest` 推送 |
| GET | `/api/kb/docs/{id}?chunks=false` | | 文档详情；`chunks=true` 时带 `chunk_list[{id, seq, heading, text, page, …}]` |
| PUT | `/api/kb/docs/{id}` | `{product_id}` | 改关联产品（空 = 公共资料） |
| DELETE | `/api/kb/docs/{id}` | | 删除文档、切块、索引和原文件 |
| POST | `/api/kb/reindex` | `{doc_id?}` | 重新解析 + 切块 + 建索引（改了切块参数后用）；不给 doc_id = 全部并重建索引。返回 `{queued: [doc_id]}` |
| GET | `/api/kb/search?q=&product_id=&k=&include_global=true` | | 检索测试：`{query, product_id, terms(检索词), groups(信息点及同义写法), coverage, matched, enough(资料够不够), backend, ms, hits[{chunk_id, doc_id, doc_name, product_id, heading, page, text, score, backend, matched}]}`；不带 product_id = 全部资料 |
| POST | `/api/kb/answer` | `{question, product_id?, speak?: false, interrupt?: false, nickname?, gender?}` | 回答测试：走直播里同一条路径（检索 → LLM 或资料原句 → 合规），返回 `{text, sentences, source(knowledge/llm/template), refs, hits(合规命中), ms, note, product_id}`；`speak=true` 时把这句交给主持人说（人工插话优先级），多返回 `cue_id`。不给 product_id 用当前讲解的产品 |

写词时的知识库规则见 ARCHITECTURE §13：资料够就用资料（LLM 写或念原句），不够就说「帮您确认 / 看商品详情 / 问客服」。

## 视频素材与直播场景

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/media?kind=video\|image` | | 素材列表（新的在前）：`{id, name, ext, kind(video/image), size, mime, width, height, duration_s, has_audio, codec, audio_codec, tags, created_at, url, cover_url, product_ids, is_background}` |
| POST | `/api/media` | multipart：`files`（可多个；mp4/webm/mov/m4v/png/jpg/jpeg/webp）、`product_id?`（上传的视频同时关联到这个产品） | 边收边写盘，大小上限 `scene.media_max_mb`；ffprobe 认不出的、没有画面的拒绝。单个文件不合格的那项 `status=rejected` + `error`；全部不合格 400 |
| GET | `/api/media/{id}` | | 详情 |
| GET | `/api/media/{id}/file` | | 文件本身（支持 Range，`<video>` 可拖进度；GET 不需要口令，OBS 可直接加载） |
| GET | `/api/media/{id}/cover` | | 视频封面 jpg（没有 404） |
| PUT | `/api/media/{id}` | `{name?, tags?, product_ids?}` | 改名 / 标签 / 关联产品（关联写在产品的 `video_ids` 上） |
| DELETE | `/api/media/{id}?force=false` | | 被产品引用或正被用作背景时 409（`detail` 说明被谁用）；`force=true` 删除并解除关联、清空背景；正在播放的会被停掉 |
| GET | `/api/sessions/{sid}/scene` | | 场景快照：`{session_id, version, layout(dh_full/dh_with_card/video_full/video_pip/split), reason, canvas{width, height, background{color}\|{image}\|null, dh_rect, pip_rect, card_rect}, card{enabled, style, product{id, name, price_text, selling_points[≤3]}}, video{play_id, media_id, name, url, mode, audio, loop, duration_s, has_audio, width, height, started_at, source(manual/script)}\|null, subtitle{enabled}, ducking, disclosure, blocks_script, ts}`。会话还没建也能查（场景状态与会话独立） |
| PUT | `/api/sessions/{sid}/scene` | `{card_visible?, subtitle?}` | 临时开关产品卡片 / 字幕（不改设置） |
| POST | `/api/sessions/{sid}/scene/video` | `{media_id, mode?(full/pip/split), audio?(video/mute), loop?}` | 立即播放（没给的用 `scene.video.default_*`）；返回新快照。素材不存在 404，是图片或参数不对 400 |
| POST | `/api/sessions/{sid}/scene/video/stop` | | 停止视频，回到数字人 |

## 观众（长期记忆）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/viewers?q=&limit=50` | `{stats{users, donors, known_at_boot}, items[{user_id, name, gender, visits, gift_count, gift_value, chat_count, followed, last_gift, gifts, max_wealth_level, fans_level, last_question, first_seen, last_seen, known_before}]}`，最近互动的在前 |
| GET | `/api/viewers/{user_id}` | 一位观众的画像；没建档 404 |
| DELETE | `/api/viewers/{user_id}` | 删除这位观众的记录 |

只有发过弹幕 / 送过礼 / 关注过 / 加粉丝团的观众才建档；模拟注入、mock 源、回放的事件默认不写长期记忆（`interaction.memory.record_test_events`）。

## 控制台推送：`/ws/console`（只读）

连接后先收到 `{"topic": "hello", "data": {"recent": {主题: [最近的消息…]}}}`，之后每条消息形如
`{"topic": "settings.changed", "ts": 1730000000.0, "data": {...}}`。当前推送的主题：`settings.changed`、`env.changed`、`danmaku.status`、`live.event`、`interaction.cue`（`Cue.to_dict()`）、
`host.action`（决策日志条目，字段同 `/api/interaction/log`，状态每变一次推一次）、
`kb.ingest`（`{kind: "doc", doc_id, name, product_id, status(queued/parsing/indexing/ready/failed/deleted/moved), progress, chunks, error, ms}`
或索引重建 `{kind: "index", status: "rebuilt", backend}`）、`products.changed`（`{reason(create/update/script/order/delete/active), product_id, active_id}`）、
`media.changed`（`{reason(add/update/delete), media_id}`）、`scene.changed`（`{session_id, reason, scene: 快照}`，reason 见下表 `scene` 事件）。

`live.event` 推给 Web 前节流：一般事件 `data` 就是 `LiveEvent.to_dict()`；点赞合并为
`{"type": "like_batch", "count", "events", "users": [{nickname, count}], "total", "ts"}`，普通进场合并为
`{"type": "member_batch", "count", "users": [ViewerProfile…最多 12 个], "ts"}`（财富等级 ≥ 15 或有粉丝团的进场单独推）；
`room_stat` 最多每 2 秒一条；连击中的礼物包（`is_final=false`）不推。要未节流的原始事件用 `GET /api/danmaku/events`。客户端可发 `{"type": "ping"}`，回 `{"topic": "pong"}`。

## 直播会话

服务启动后（`live.auto_start: true`）会自动创建 `default` 会话。

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/sessions` | | 所有会话状态 |
| POST | `/api/sessions` | JSON `{session_id?, model?, avatar?, seed?, use_face_crop?, gpu_id?}` | 新建会话。`avatar` 是服务器上的图片路径（网页上传请建好后再调 `/avatar`）；`gpu_id` 指定显卡 |
| GET | `/api/sessions/{sid}` | | 会话状态（见下） |
| DELETE | `/api/sessions/{sid}` | | 关闭会话 |
| POST | `/api/sessions/{sid}/speak` | JSON `{text, voice?, interrupt?, request_id?, speed?}` | 文本走 TTS 后说出。`interrupt=true` 先打断当前的；`speed` 语速比 0.5~2.0（不给用设置 `tts.speed`）。返回 `{request_id, queued}` |
| POST | `/api/sessions/{sid}/audio` | multipart `file`（wav/mp3/...）, `text?`, `interrupt?` | 直接用一段音频驱动（外部 TTS / 录音）。返回 `{request_id, seg_id, seconds}` |
| POST | `/api/sessions/{sid}/interrupt` | | 打断：清空未合成文本和未生成语音。返回 `{dropped_segments, dropped_frames, request_ids}`（被打断的请求不会再发 speak_done） |
| POST | `/api/sessions/{sid}/model` | JSON `{model: "lite"\|"pro"}` | 切换模型，切换完成后返回 `{model, chunk}` |
| POST | `/api/sessions/{sid}/avatar` | multipart `file`（图片）, `use_face_crop?` | 更换形象（运动状态会重置到新图） |

会话状态字段：

```jsonc
{
  "session_id": "default", "state": "running", "error": null,
  "model": "lite", "gpu_id": 0, "worker": 0,
  "chunk": {"chunk_frames": 24, "chunk_samples": 15360, "chunk_seconds": 0.96, "motion_frames": 9, "fps": 25, ...},
  "speaking": true, "playing_text": "大家好，欢迎来到直播间！", "playing_request_id": "a1b2c3", "open_requests": 1,
  "buffered_s": 0.52,          // 已生成未播放
  "speech_pending_s": 3.1,     // 已合成未生成的语音
  "tts_queue": 0,              // 未开始合成的请求数
  "gen_time_ema_s": 0.31, "rtf": 0.32, "realtime": true,   // rtf = 生成耗时/块时长，<1 才能实时
  "chunks": 120, "speech_chunks": 30, "frames_out": 2880, "fillers": 0, "gen_errors": 0
}
```

### Python 示例

```python
import requests
B = "http://127.0.0.1:8000"
r = requests.post(f"{B}/api/sessions/default/speak", json={"text": "欢迎来到直播间！"}).json()
requests.post(f"{B}/api/sessions/default/model", json={"model": "pro"})
with open("reply.wav", "rb") as f:
    requests.post(f"{B}/api/sessions/default/audio", files={"file": f}, data={"text": "外部TTS", "interrupt": "true"})
requests.post(f"{B}/api/sessions/default/interrupt")
```

## 离线视频

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| POST | `/api/offline/jobs` | multipart：`image?`（不传用默认形象）, `text?`, `audio?`, `model?`, `voice?`, `seed?`, `use_face_crop?`, `wait?` | `text` 与 `audio` 至少一个，有 `audio` 时忽略 `text`。默认返回任务 JSON；`wait=true` 时等生成完直接返回 mp4 |
| GET | `/api/offline/jobs` | | 任务列表（新的在前） |
| GET | `/api/offline/jobs/{job_id}` | | `{status: queued\|tts\|generating\|encoding\|done\|failed, progress 0~1, message, worker, duration_s, video_url}` |
| GET | `/api/offline/jobs/{job_id}/video` | | 下载 mp4（h264 + aac） |

```bash
curl -F image=@examples/girl.png -F "text=你好，这是测试视频。" -F model=pro -F wait=true \
     http://127.0.0.1:8000/api/offline/jobs -o out.mp4
```

## WebSocket：`/ws/sessions/{sid}`

连接后服务端先发一条 JSON：

```json
{"type": "hello", "protocol": 1, "fps": 25, "sample_rate": 16000, "can_write": true, "session": {...会话状态...}}
```

之后持续推送两种消息：**二进制帧包**（每秒 fps 个）和 **JSON 事件**（文本消息）。

### 二进制帧包（protocol 1，小端）

| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | char[4] | magic | `FHL1` |
| 4 | u8 | type | 1 = 音视频帧 |
| 5 | u8 | flags | bit0 说话中；bit1 补帧（生成跟不上，重复上一帧） |
| 6 | u16 | reserved | |
| 8 | u32 | seq | 帧序号，会话内从 0 递增；跳号说明客户端太慢被丢帧了 |
| 12 | f64 | pts | 秒，= seq / fps |
| 20 | u32 | seg_id | 正在播放的语音段，0 = 空闲；对应 `segment` 事件里的文字，可做字幕 |
| 24 | u32 | jpeg_len | |
| 28 | u32 | audio_len | 字节数；int16 单声道 PCM，采样率见 hello（16000 → 每帧 640 采样 = 1280 字节） |
| 32 | | jpeg 数据，紧接 audio 数据 | |

Python 接收示例：

```python
import asyncio, json, struct, numpy as np, cv2, websockets
HDR = struct.Struct("<4sBBHIdIII")

async def main():
    async with websockets.connect("ws://127.0.0.1:8000/ws/sessions/default", max_size=None) as ws:
        while True:
            msg = await ws.recv()
            if isinstance(msg, str):
                print("事件", json.loads(msg)); continue
            magic, typ, flags, _, seq, pts, seg, jl, al = HDR.unpack_from(msg)
            img = cv2.imdecode(np.frombuffer(msg, np.uint8, jl, 32), cv2.IMREAD_COLOR)   # BGR
            pcm = np.frombuffer(msg, "<i2", al // 2, 32 + jl)                             # 640 个采样
            # 画面 img 和声音 pcm 属于同一个 40ms，直接一起送给播放器/推流器

asyncio.run(main())
```

### JSON 事件

| type | 字段 | 时机 |
|---|---|---|
| `speak_queued` | request_id, text | 文本请求入队 |
| `segment` | seg_id, request_id, text, seconds | 一句话合成完成、进入待播 |
| `playback` | state=start\|end, seg_id, request_id, text | 这一段开始/结束播放（服务端出帧时刻） |
| `speak_done` | request_id | 一个请求的最后一段播完（追加式请求：end 之后且最后一段播完；被打断的请求不发） |
| `interrupted` | dropped_segments, dropped_frames, request_ids | 被打断 |
| `model_switching` / `model` | from, to / model, chunk | 开始切换 / 切换完成 |
| `avatar` | avatar | 形象已更换 |
| `warning` | message | 例如生成速度跟不上实时 |
| `error` | where, message, fatal? | TTS/生成/控制失败；fatal=true 表示会话已停止 |
| `closed` | | 会话关闭（随后连接断开） |
| `scene` | reason, scene（同 `GET /api/sessions/{sid}/scene`） | 场景变化：`video_started` / `video_ended`（播完，场景页回传或服务端按时长）/ `video_stopped` / `video_deleted` / `patched` / `settings` / `product` |

所有事件都带 `session_id` 和 `ts`（服务端 Unix 时间）。

### 客户端 → 服务端（可选，与 REST 等价）

```json
{"type": "speak", "text": "...", "voice": null, "interrupt": false, "request_id": null, "speed": null}
{"type": "interrupt"}
{"type": "ping", "ts": 123}
{"type": "scene.video_ended", "play_id": "..."}
```

分别回 `{"type": "ack", "for": "speak", "request_id": ...}`、`{"type": "ack", "for": "interrupt", ...}`、`{"type": "pong", "ts": 123}`。
没有写权限时 speak / interrupt 回 `{"type": "error", "message": "没有写权限…"}`（见上文「鉴权」）。
`scene.video_ended` 是场景页回报"这个视频播完了"，**不需要写权限**、没有回复，只结束 play_id 对得上的视频。
