# 07 测试

## 1. 策略

| 层 | 工具 | 说明 |
|---|---|---|
| 单元 | pytest | 纯函数和小类：弹幕解析、连击聚合、设置校验、意图识别、合规过滤、切块、检索、SSE 解析、导演规则 |
| 集成 | pytest + FastAPI `TestClient`/httpx | 接口 + 服务组合（mock 后端、mock TTS/LLM、mock 弹幕源），不起 GPU |
| 端到端 | 起 mock 服务的脚本（沿用 `test_e2e_mock.py` 写法） | WebSocket 帧流 + 事件 + 播报顺序 |
| 页面 | Edge 无头 + DevTools 协议截图 | 每页截图、无横向溢出、无控制台报错 |

规则：
- **不调用任何真实计费接口**（火山 TTS、LLM、Embedding）。真实接口只在用户同意后手动自测。
- 时间相关逻辑（保鲜窗口、冷却、连击超时、讲品时长、回踩）一律注入假时钟（`live/core/clock.py`），不靠 `sleep`。
- 原有 `test_e2e_mock.py`（33 项）和 `test_runner_logic.py`（18 项）每个阶段都要跑。
- 新增 e2e 用例写成独立脚本（如 `tests/live/test_e2e_commerce.py`），不往原脚本里塞，原脚本只在接口变化时改。

## 2. Mock 与夹具

| 名称 | 位置 | 用途 |
|---|---|---|
| MockDanmakuSource | `live/danmaku/mock.py` | 按比例生成事件；支持脚本化序列（测试用确定性序列） |
| 回放 | `tests/fixtures/events/*.jsonl` | 一段典型直播的事件序列（可先用 mock 生成，P2 实测后用户可提供真实录制，**提交前去掉昵称等可识别信息**） |
| 协议样本 | 测试里用 betterproto 类构造消息字节 | 测 `parse.py`，不依赖真实直播间 |
| MockLLM | `live/llm/mock.py` | 按规则返回/按句流式返回/超时/报错，可断言收到的 messages |
| MockTTS | 现有 `live/tts/mock.py` | |
| 假 SSE | `tests/fixtures/volc_sse/*.txt` | 火山 SSE 响应样本（正常、多块、错误码、空音频）；用 `requests` 的适配器或 monkeypatch 注入 |
| 样例资料 | `tests/fixtures/kb/product_a.md`、`product_b.html`、`policy.pdf` | 知识库测试；内容自造，含价格/保质期/尺码/售后等可问点 |
| 测试视频 | 测试里用 ffmpeg 现场生成几秒的 mp4 | 不入库 |

## 3. 用例清单（按阶段）

### P1 基础设施
- 设置：默认/yaml/覆盖三层合并顺序；类型/范围/枚举校验报错信息；原子写；版本快照与回滚；`settings.changed` 只带变更键；restart 项不触发热应用。
- `.env`：加载、重载后 `env.changed`；密钥接口只返回配置状态。
- EventBus：多订阅者、通配主题、慢订阅者丢弃计数、异常隔离。
- 鉴权：设了 `ADMIN_TOKEN` 时写接口无口令返回 401；未设时本机可写、非本机 403。

### P2 弹幕
- `parse.py`：chat / emoji / gift / like / member / follow / share / fansclub / room_stat（"1.2万"→12000）/ rank / control(status=3) / 未知 method。
- ViewerProfile：性别映射（含自定义 gender_map）、财富等级/名称/钻石、粉丝团、头像 URL、隐私字段（生日/手机）不出现在结果里。
- 连击聚合：多包同一连击 → 一次 final（count 取最终值）；超时结算；不同用户/不同礼物不串。
- DanmakuManager：子进程启动、心跳状态、崩溃自动重启、改 live_id 热切换、stop 干净退出（无残留进程）。
- Recorder/Replay：录制格式可被回放；倍速回放。
- `/ws/console`：收到节流后的事件。

### P3 TTS / LLM
- 火山：请求头（新/旧控制台）、`speech_rate` 映射边界、SSE 多块拼接、错误码跳过、空音频返回 None、16k 输出（或 24k→16k 重采样长度正确）。
- 缓存：命中不再请求；换音色/语速桶键变化；失败不缓存；落盘后重启仍命中。
- 回退：volc 无凭证 → edge → mock。
- LLM：URL 规范化（`/v1` 与完整路径）；非流式；流式按句；超时 → 回退；无 key → available=False；`.env` 重载后生效。

### P4 互动
- 复刻参考项目平台用例（假时钟）：礼物 S/M/L 分档、连续送礼、回踩 18s/45s、榜一易主、vip/high_grade/regular/returning/普通进场分级、
  known_before（本场新人送大礼不算金主）、建档门槛（纯进房不建档）、点赞阈值与同人冷却放大、过期丢弃、同拍合并、`_handled_*` 不重复念。
- 意图：产品提问/购买意向/闲聊/命令/垃圾的关键词判定；Web 修改词表后立即生效。
- 合规：极限词替换、删除、block_on_hit；兜底话术本身也要过合规。
- 追加式请求：begin/append/end 后 speak_done 只发一次且在最后一段之后；打断返回 request_id。
- Host：讲品中来高优先级 cue → 打断 → 回应 → 断点续讲；低优先级 cue 不打断；写词超时用兜底；暂停开关；人工插话最高优先级。
- e2e（mock 全链路）：注入"高等级进场 → 产品提问 → 大礼物 → 普通弹幕"，断言播报顺序、文本来源（模板/LLM/知识库）与 speak_done 事件。

### P5 知识库 / 产品
- loader：md 标题层级、html 去噪、pdf 页码；空文档与损坏文件的报错。
- 切块长度/重叠；中文分词检索（价格、保质期、尺码各一问）；按产品过滤；删除后不可检索；重建索引。
- RAG：mock LLM 收到的 messages 含检索片段与约束；低分时兜底"帮您确认"；回答过合规。
- 讲解稿：生成（mock）→ 待确认 → 确认后才进轮播；轮播按时长换产品；断点续讲。

### P6 场景 / 视频
- 媒体上传与元数据；删除被引用的视频时提示。
- SceneState：play → ended → 恢复；stop；布局切换事件推送。
- Host：视频原声播放期间不讲品，结束后恢复。
- 页面：`/scene` 截图包含数字人画布与产品卡片；播放命令后出现视频层。

### P7 页面
- 每页截图（1366×768、390 宽）无横向溢出（`document.documentElement.scrollWidth <= innerWidth`）、无 JS 报错；`node --check web/**/*.js`。

## 4. 上线前人工验收清单（mock 模式先走一遍，真实环境需用户确认后再走）

1. 启动服务，顶栏 5 个状态徽章正确（无 key 时 TTS 回退、LLM 不可用均有提示）。
2. 弹幕页开 mock 源：事件流滚动，用户标签（性别/财富等级/粉丝团/熟客金主）显示正确；点用户弹出画像。
3. 产品页上传样例资料、生成讲解稿并确认；控制台看到数字人开始讲品。
4. 模拟"产品提问"：几秒内数字人基于资料回答，决策日志显示来源为知识库。
5. 模拟大礼物：打断讲品，点名感谢（报礼物名+数量），随后续讲；18 秒后回踩一次。
6. 设置页改 `interaction.gift.tier_l`、`persona.anchor_name`、`tts.speed`、违禁词：保存后下一句立即体现。
7. 视频页"立即播放（画中画）"：场景页切换布局，播完恢复。
8. OBS 浏览器源加载 `/scene?session=default`（竖屏 720×1280），画面与声音正常。
9. （真实）连自己的直播间：核对字段表"实测"列、性别枚举、礼物（需 cookie）、连击只谢一次；火山 TTS 自测一句。
10. 停服务：无残留子进程（弹幕子进程、GPU worker）。
