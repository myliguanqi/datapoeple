# CLAUDE.md —— 给 AI 协作者的项目指引

## 项目是什么

SoulX-FlashHead（官方 1.3B 数字人模型，Lite/Pro）+ 自研的**实时数字人带货直播服务层**（`live/`、`web/`）：
实时出画（mock / 真实推理）、抖音弹幕（只读）→ 导演层互动 → 主持人写词（模板 / LLM + 产品知识库）→ 合规过滤 → 火山 TTS → 数字人开口；
讲解稿轮播讲品、视频素材插播、直播场景页 `/scene` 给 OBS；所有参数 Web 上设置并热更新。
第二阶段（P0–P8）已完成，任务书与进度表：`docs/tasks/phase2-live-commerce/README.md`；还没在真实环境验证的事项列在 `README_LIVE.md`。

## 必读

- `README_LIVE.md`：功能、安装、配置、已验证 / 需要真实环境验证的事项
- `docs/live/ARCHITECTURE.md`：§0 总览；进程/线程、块与时钟、音画同步、模型切换；§9 AppContext 与事件总线主题；§10 弹幕、§11 TTS/LLM、§12 互动与主持人、§13 知识库与讲品、§14 视频与场景
- `docs/live/API.md`：REST / WebSocket 接口、二进制帧协议、`/ws/console` 主题、场景事件
- `docs/live/DEVELOPMENT.md`：代码地图（按"想改什么"找文件）、扩展示例（弹幕平台 / TTS / LLM / 文档格式 / 检索后端 / 触发器 / 配置项）、测试、待办

## 硬性约定

1. **不改官方代码** `flash_head/`；**不改** `vendor/` 下第三方源码（定制写在适配层；抖音改签名时整体替换 `vendor/douyin_fetcher/`）。
2. **主进程不 import torch**；GPU 推理只在 `live/worker/` 子进程里。新 Runner 方法要加进 `live/worker/__init__.py` 的 `RUNNER_METHODS`，MockRunner 同步实现。弹幕抓取在独立子进程。
3. **配置项**先加到 `live/settings/schema.py`（`F(key, type, default, 中文 label, help, group=, apply=hot|restart)`，Web 设置页自动生成），
   再按需在 `live/configs/*.yaml` 覆盖；相对路径统一相对项目根目录。缓存了对象的模块实现 `apply_settings` 并在 `AppContext.start()` 里 `settings.register(...)`。
4. **解耦**：每类能力 = 接口（`base.py`）+ 实现 + `Registry` 注册；服务只在 `live/app_context.py` 构造并注入；模块间只走事件总线 / 注入的接口 / HTTP。
   新的总线主题要给 Web 看就加进 `live/feed.py` 的 `FEED_TOPICS`。
5. **密钥**只放 `.env`（入库的是 `.env.example`，只经 `live/settings/env.py` 读）、cookie 放 `secrets/`；`live_data/` 是运行时数据。三者都在 `.gitignore`。日志不打印密钥。
6. **测试不调用计费接口**（火山 TTS / LLM / Embedding 一律 mock）。真实接口自测（如 `python -m live.tts.volc`）需用户明确同意。
7. **所有要说出口的文本**（模板、LLM、知识库回答、讲解稿、人工插话）都过 `live/interaction/compliance.py`；产品信息只来自知识库资料，资料没有就说「帮您确认 / 看商品详情 / 问客服」；
   知识库、讲解稿、弹幕内容进 LLM 时视为不可信输入。场景页 AI 标识常驻、不能关闭（`compliance.ai_disclosure` 不允许为空）。
8. **上传文件**（文档、视频、图片、形象）限制类型与大小，文件名改写为随机 ID 存盘。
9. 改了行为就同步改文档（`README_LIVE.md`、`docs/live/*.md`），文档与代码不一致视为没做完。
10. 提交：每阶段一个或几个提交；提交信息说清做了什么；**不 push**，除非用户要求。远程：`origin` = 用户私有仓库 datapoeple，`upstream` = 官方仓库。

## 运行与测试（Windows，conda 环境 `flashhead`：`D:\anaconda\envs\flashhead\python.exe`，Python 3.10，命令前加 `PYTHONIOENCODING=utf-8`）

```bat
python -m pytest tests/live -q                            :: 单元/集成 199 项（不起服务、不起 worker、不调计费接口；P6 的需要 ffmpeg）
python live_server.py --config live/configs/mock.yaml     :: 无模型、无显卡调试（mock 推理 + mock TTS），另开终端跑下面的 e2e
python tests/live/test_e2e_mock.py                        :: 端到端 33 项（端口 8000，或设 LIVE_BASE）
python tests/live/test_e2e_commerce.py                    :: 带货互动 16 项
python tests/live/test_e2e_products.py                    :: 讲品 + 知识库 15 项
python tests/live/test_e2e_scene.py                       :: 视频素材 + 直播场景 15 项（需 ffmpeg）
python tests/live/test_runner_logic.py                    :: 真实推理后端逻辑 18 项（需 torch+CUDA，不需要权重）
```
- 四个 e2e **按上面的顺序跑**（或间隔 2 分钟以上）：模拟注入事件后 2 分钟内导演仍认为"在播"，会出冷场话术和送礼回踩，影响紧接着跑的 `test_e2e_mock.py`。
  起服务前确认 `live_data/settings.json` 的覆盖里没有 `danmaku.enabled=true`（Web 上连过弹幕就会写进去，重启后自动连模拟源，污染 e2e）。
- 测试与 e2e 跑完要清理写进 `live_data/` 的测试数据（产品、资料、素材、测试观众），设置用 reset 恢复（原来没被 Web 覆盖的键不要写一个等于默认值的覆盖）；
  模拟 / mock / 回放事件默认不写长期记忆（`interaction.memory.record_test_events`），别破坏这条。
- 测试视频用 ffmpeg 现场生成、不入库；二进制夹具 < 200KB，`.gitattributes` 已把 pdf/视频/图片标为 binary。
- 时间相关逻辑注入 `live/core/clock.py` 的 `FakeClock`；主持人测试用 `tests/live/fakes.py` 的假会话。
- 页面检查：Edge 无头 `msedge --headless=new --remote-debugging-port=9222 --user-data-dir=<scratchpad>`，chrome-devtools MCP 连上后执行
  `tests/live/page_sweep.js`（1366×768 与设备模拟 390 宽：无横向溢出、无 JS 报错）；改了 web JS 跑 `node --check`。
- 停服务：找占用 8000 端口的进程 `taskkill /PID <pid> /T /F`（连 worker / 弹幕子进程一起杀，避免残留）。

## 已知坑

- `diffusers` 必须 `<0.36`（新版要求 huggingface-hub ≥1.x，与 `transformers==4.57.3` 冲突）；`protobuf<4`（mediapipe 0.10.9）；`numpy<2`、`opencv-python<4.12`。新依赖加进 `requirements_live.txt` 并注明用途，装完 `pip check`。
- Windows 不装 `xfuser`/`nvidia-nccl-cu12`；`live/worker/compat.py` 会注入 xfuser 桩。单路画面双卡并行（sequence_parallel）未实现。
- Pro 模型单张 4090 约 10.8 FPS，跟不上 25fps 实时直播；直播用 Lite，Pro 适合离线。
- Windows 默认 sleep 精度 15ms，服务启动时已调 `timeBeginPeriod(1)`。
- 网页布局：网格列用 `minmax(0,1fr)`、文件输入 `min-width:0`，否则会横向溢出（踩过）。
- 浏览器自动播放策略：没有用户手势时 AudioContext 处于 suspended，播放器按音频时钟出帧，所以画面也不动；`/?view=1`、`/scene` 点一下页面才开始，OBS 浏览器源不受限。
- 讲解稿 / 话术里写到「唯一」「第一」「100%」等会被合规替换或删除（测试文本也会）；序数词、时间词在 `compliance.allow_phrases` 里放行。
- Bash 工具里 heredoc 内容含三引号 / 复杂引号会报 "unexpected EOF"：长脚本先写到 scratchpad 文件再执行；git-bash 里 curl 直接带中文 JSON 可能不是 UTF-8，用 Python requests。
- sqlite3 支持 FTS5（检索后端默认 fts，jieba 预分词）；ffmpeg/ffprobe 要在 PATH。

## 参考项目

`D:\jobdemo\douyin_zhibo`（AI 游戏直播解说）：弹幕适配层、导演层互动规则、长期记忆、火山 TTS、LLM 客户端都从这里移植，
映射表见 `docs/tasks/phase2-live-commerce/02-reference-douyin_zhibo.md`。**不要从它的 `secrets.yaml` / cookie 文件复制任何密钥。**
