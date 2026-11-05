# CLAUDE.md —— 给 AI 协作者的项目指引

## 项目是什么

SoulX-FlashHead（官方 1.3B 数字人模型，Lite/Pro）+ 自研的**实时数字人直播服务层**（`live/`、`web/`）。
当前正在做第二阶段：抖音弹幕互动 + 火山 TTS + LLM + 产品知识库 + 视频场景 + Web 设置热更新。
**任务书：`docs/tasks/phase2-live-commerce/`（先读 README.md，按 04-plan.md 分阶段执行，进度表在 README.md）。**

## 必读

- `README_LIVE.md`：功能、安装、配置、已验证/未验证
- `docs/live/ARCHITECTURE.md`：进程/线程模型、块与时钟、按需提前调度、音画同步、模型切换接续
- `docs/live/API.md`：REST/WS 接口与二进制帧协议
- `docs/live/DEVELOPMENT.md`：扩展点、测试、待办

## 硬性约定

1. **不改官方代码** `flash_head/`；**不改** `vendor/` 下第三方源码（定制写在适配层）。
2. **主进程不 import torch**；GPU 推理只在 `live/worker/` 子进程里。新 Runner 方法要加进 `live/worker/__init__.py` 的 `RUNNER_METHODS`，MockRunner 同步实现。
3. 配置项先加到 schema/DEFAULTS（带中文注释或 help），再在 yaml 覆盖；相对路径统一相对项目根目录。
4. 密钥只放 `.env`（入库的是 `.env.example`）、cookie 放 `secrets/`；`live_data/` 是运行时数据。三者都在 `.gitignore`。日志不打印密钥。
5. **测试不调用计费接口**（TTS/LLM/Embedding 用 mock）。真实接口自测需用户明确同意。
6. 改了行为就同步改文档（`README_LIVE.md`、`docs/live/*.md`），文档与代码不一致视为没做完。
7. 提交：每阶段一个或几个提交；提交信息说清做了什么；**不 push**，除非用户要求。远程：`origin` = 用户私有仓库 datapoeple，`upstream` = 官方仓库。

## 运行与测试（Windows）

```bat
python live_server.py --config live/configs/mock.yaml     :: 无模型、无显卡调试（mock 推理 + mock TTS）
python tests/live/test_e2e_mock.py                        :: 端到端 33 项（需先起上面的服务，端口 8000，或设 LIVE_BASE）
python tests/live/test_e2e_commerce.py                    :: 带货全链路 16 项（同上，先起服务）
python tests/live/test_e2e_products.py                    :: 讲品 + 知识库 15 项（同上）
python tests/live/test_e2e_scene.py                       :: 视频素材 + 直播场景 15 项（同上，需 ffmpeg；四个 e2e 按这个顺序跑）
python tests/live/test_runner_logic.py                    :: 真实推理后端逻辑 18 项（需 torch+CUDA，不需要权重）
```
新增的单元/集成测试用 pytest：`python -m pytest tests/live -q`。

## 已知坑

- `diffusers` 必须 `<0.36`（新版要求 huggingface-hub ≥1.x，与 `transformers==4.57.3` 冲突）；`protobuf<4`（mediapipe 0.10.9）。
- Windows 不装 `xfuser`/`nvidia-nccl-cu12`；`live/worker/compat.py` 会注入 xfuser 桩。单路画面双卡并行（sequence_parallel）未实现。
- Pro 模型单张 4090 约 10.8 FPS，跟不上 25fps 实时直播；直播用 Lite，Pro 适合离线。
- Windows 默认 sleep 精度 15ms，服务启动时已调 `timeBeginPeriod(1)`。
- 网页布局：网格列用 `minmax(0,1fr)`、文件输入 `min-width:0`，否则会横向溢出（踩过）。
- 浏览器自动播放策略：没有用户手势时 AudioContext 处于 suspended；`/?view=1` 纯画面模式点一下页面才出声，OBS 浏览器源不受限。

## 参考项目

`D:\jobdemo\douyin_zhibo`（AI 游戏直播解说）：弹幕适配层、导演层互动规则、长期记忆、火山 TTS、LLM 客户端都从这里移植，
映射表见 `docs/tasks/phase2-live-commerce/02-reference-douyin_zhibo.md`。**不要从它的 `secrets.yaml` / cookie 文件复制任何密钥。**
