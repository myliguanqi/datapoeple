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
