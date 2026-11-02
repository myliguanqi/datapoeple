# 接口文档

默认地址 `http://<host>:8000`。所有 JSON 接口出错时返回 `{"detail": "原因"}` 和对应 HTTP 状态码。
FastAPI 自带的交互式文档：`/docs`。

## 网页

| 地址 | 说明 |
|---|---|
| `/` | 完整控制台。参数：`session=`、`autoconnect=1`、`latency=0.3`、`subs=0` |
| `/?view=1&session=default` | 纯画面模式：全屏画面 + 字幕，自动连接、断线重连，给 OBS 浏览器源用 |
| `/web/player.js` | 播放器模块（ES module），可在自己的页面里复用，用法见文件头注释 |

## 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system?refresh=false` | 版本、后端、多卡模式、每个 worker 的状态/显卡/已加载模型/错误、会话列表。`refresh=true` 会向 worker 实时查询显存 |
| GET | `/api/tts/voices` | `{"engine", "default", "voices": [{"id", "name"}]}` |

## 直播会话

服务启动后（`live.auto_start: true`）会自动创建 `default` 会话。

| 方法 | 路径 | 请求 | 说明 |
|---|---|---|---|
| GET | `/api/sessions` | | 所有会话状态 |
| POST | `/api/sessions` | JSON `{session_id?, model?, avatar?, seed?, use_face_crop?, gpu_id?}` | 新建会话。`avatar` 是服务器上的图片路径（网页上传请建好后再调 `/avatar`）；`gpu_id` 指定显卡 |
| GET | `/api/sessions/{sid}` | | 会话状态（见下） |
| DELETE | `/api/sessions/{sid}` | | 关闭会话 |
| POST | `/api/sessions/{sid}/speak` | JSON `{text, voice?, interrupt?, request_id?}` | 文本走 TTS 后说出。`interrupt=true` 先打断当前的。返回 `{request_id, queued}` |
| POST | `/api/sessions/{sid}/audio` | multipart `file`（wav/mp3/...）, `text?`, `interrupt?` | 直接用一段音频驱动（外部 TTS / 录音）。返回 `{request_id, seg_id, seconds}` |
| POST | `/api/sessions/{sid}/interrupt` | | 打断：清空未合成文本和未生成语音。返回 `{dropped_segments, dropped_frames}` |
| POST | `/api/sessions/{sid}/model` | JSON `{model: "lite"\|"pro"}` | 切换模型，切换完成后返回 `{model, chunk}` |
| POST | `/api/sessions/{sid}/avatar` | multipart `file`（图片）, `use_face_crop?` | 更换形象（运动状态会重置到新图） |

会话状态字段：

```jsonc
{
  "session_id": "default", "state": "running", "error": null,
  "model": "lite", "gpu_id": 0, "worker": 0,
  "chunk": {"chunk_frames": 24, "chunk_samples": 15360, "chunk_seconds": 0.96, "motion_frames": 9, "fps": 25, ...},
  "speaking": true, "playing_text": "大家好，欢迎来到直播间！",
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
{"type": "hello", "protocol": 1, "fps": 25, "sample_rate": 16000, "session": {...会话状态...}}
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
| `speak_done` | request_id | 一个请求的最后一段播完 |
| `interrupted` | dropped_segments, dropped_frames | 被打断 |
| `model_switching` / `model` | from, to / model, chunk | 开始切换 / 切换完成 |
| `avatar` | avatar | 形象已更换 |
| `warning` | message | 例如生成速度跟不上实时 |
| `error` | where, message, fatal? | TTS/生成/控制失败；fatal=true 表示会话已停止 |
| `closed` | | 会话关闭（随后连接断开） |

所有事件都带 `session_id` 和 `ts`（服务端 Unix 时间）。

### 客户端 → 服务端（可选，与 REST 等价）

```json
{"type": "speak", "text": "...", "voice": null, "interrupt": false, "request_id": null}
{"type": "interrupt"}
{"type": "ping", "ts": 123}
```

分别回 `{"type": "ack", "for": "speak", "request_id": ...}`、`{"type": "ack", "for": "interrupt", ...}`、`{"type": "pong", "ts": 123}`。
