# 第二阶段任务：数字人带货直播（弹幕互动 + 知识库 + 视频 + Web 设置）

> 本目录是**给执行者（新开的 Claude Code 窗口或开发者）的任务书**。先通读本文件，再按 `04-plan.md` 分阶段实施。
> 编写日期：2026-11-04。现有基础：`README_LIVE.md`、`docs/live/*.md` 描述的实时直播框架（已通过 33+18 项测试）。

## 一句话目标

在现有"数字人实时出画"框架上，接入**抖音直播间弹幕/礼物/用户信息**，参考 `D:\jobdemo\douyin_zhibo` 的互动处理，
用**火山引擎 TTS**发声、**OpenAI 兼容 LLM**（.env 配置）写回复，基于**产品知识库（html/pdf/md）**介绍产品和回答提问，
支持**产品视频**在直播画面里播放；所有直播间参数在 **Web 上设置并热更新**；Web UI 重新整理；模块解耦；文档同步更新。

## 在新窗口里怎么开始

在 `D:\ProgramData\SoulX-FlashHead` 打开 Claude Code，粘贴下面这段：

```text
请完整阅读 docs/tasks/phase2-live-commerce/ 目录下的全部文档（从 README.md 开始），以及根目录 CLAUDE.md。
然后严格按 04-plan.md 的阶段顺序实施：每个阶段先列出要改的文件，完成后运行该阶段的测试 + 原有测试，
更新 README.md 里的进度表、同步更新受影响的文档（README_LIVE.md、docs/live/*.md），然后 git commit（不要 push）。
遇到文档中标「⚠ 需用户确认」的事项，先停下来问我。不要调用任何会计费的真实接口（TTS/LLM），除非我明确同意；
测试一律用 mock。先从 P0 开始。
```

## 文档索引

| 文件 | 内容 |
|---|---|
| [01-requirements.md](01-requirements.md) | 需求清单（R1–R10），每条带验收标准；不做的事 |
| [02-reference-douyin_zhibo.md](02-reference-douyin_zhibo.md) | 参考项目分析：哪些代码照抄/移植/改写；**抖音弹幕可拿到的全部字段表**；互动处理机制；已知坑 |
| [03-architecture.md](03-architecture.md) | 新架构：进程模型、模块与接口、事件总线、设置热更新、主持人编排、知识库、视频场景、目录结构、新增 API |
| [04-plan.md](04-plan.md) | 分阶段实施计划 P0–P8：任务、文件、测试、验收、提交点 |
| [05-webui.md](05-webui.md) | Web UI 信息架构与每页线框、交互规则、设计规范 |
| [06-config-and-secrets.md](06-config-and-secrets.md) | `.env` 键表、Web 可设置项（热更新/需重启）、默认值 |
| [07-testing.md](07-testing.md) | 测试策略、mock、测试用例清单、上线前人工验收清单 |
| [08-risks-compliance.md](08-risks-compliance.md) | 许可证（AGPL）、平台规则、隐私、广告法与 AI 标识、计费、安全 |
| [09-next-session-prompt.md](09-next-session-prompt.md) | 新窗口接手 P5–P8 的提示词（含环境、已有接入点、注意事项） |

## 进度表（执行者每完成一个阶段更新这里）

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 准备：依赖、.env.example、基线测试 | ☑ 2026-11-04：conda 环境 `flashhead`（Python 3.10.22）装齐主进程 + 第二阶段依赖，`pip check` 无冲突，`protobuf<4` 未被改动；**sqlite3 3.53.4 支持 FTS5 → 检索后端默认 fts**；基线 e2e 33/33 通过；runner 逻辑测试 18/18 通过（torch 2.7.1+cu128，RTX 4070）。装全量依赖时发现 `numpy`/`opencv-python` 未钉版本会把 numpy 拉到 2.x（mediapipe 0.10.9 按 1.x 编译），已在 requirements_live.txt 钉住 `numpy<2`、`opencv-python<4.12` |
| P1 | 基础设施：事件模型、事件总线、设置中心 + 热更新、API 拆分、管理口令、新 Web 外壳 | ☑ 2026-11-04：pytest 36 项 + 原有 e2e 33 项通过；Edge 无头验证设置页改 `speech.target_rms_db` 下一句即生效、`gpu.devices` 标需重启；各页无横向溢出 |
| P2 | 弹幕接入：vendor fetcher、全字段解析、独立子进程、mock/录制回放 | ☑ 2026-11-05（mock 部分）：pytest 30 项新增全过（累计 66）+ 原有 e2e 33 项；Edge 无头验证弹幕页/控制台弹幕流。**未做：真实直播间实测**（平台规则确认、登录 cookie 由用户后续提供），02 §3「实测」列待填 |
| P3 | 火山 TTS（照抄）+ 合成缓存；LLM 客户端（.env） | ☑ 2026-11-05：pytest 新增 26 项（累计 92）+ 原有 e2e 33 项；全部用假 HTTP，未访问真实接口。另加 08 §5 建议的用量保护 `tts.daily_max_chars` / `llm.daily_max_calls`。**未做：真实凭证自测**（`python -m live.tts.volc` 会计费，需用户同意） |
| P4 | 互动引擎：移植导演/状态合流/长期记忆/优先级队列；主持人编排；合规过滤 | ☑ 2026-11-05：pytest 新增 48 项（累计 140）+ 带货全链路 e2e 16 项 + 原有 e2e 33 项通过；Edge 无头走查新页面（1366 与窄屏无溢出）。另：模拟/mock/回放事件默认不写长期记忆；序数词（第一次/第一步…）不被合规误伤。真实 LLM 写词效果待配置 .env 后验收 |
| P5 | 产品与知识库：文档解析、切块、检索、RAG 回答、讲解稿生成与轮播 | ☑ 2026-11-05：pytest 新增 51 项（累计 191）+ 讲品知识库 e2e 15 项（新增 `test_e2e_products.py`）+ 原有 e2e 33 + 16 项通过；全部用 mock，未调 LLM/Embedding。样例资料 `tests/fixtures/kb/`（md / html / 两页 pdf 3KB，pdf 由无依赖脚本生成）。验收：回答测试 5 问答案都来自资料、资料没有的说「帮您确认」；确认讲解稿后数字人讲品，插入产品提问先答再从断点续讲。另：没配 LLM 时直接念资料原句（来源记 knowledge）；e2e 跑完恢复设置改为 reset（不留等于默认值的覆盖）；e2e 需按 mock → commerce → products 顺序跑（模拟事件后 2 分钟内导演仍在播，见 DEVELOPMENT.md）。真实 LLM 回答/讲解稿质量、真实 Embedding 待用户配 .env 后验收 |
| P6 | 视频素材与直播场景页：视频库、场景状态、画中画/全屏播放、产品卡片 | ☐ |
| P7 | Web UI 整理收尾：各页面完善、设置表单、弹幕面板 | ☐ |
| P8 | 文档同步、全量测试、整体验收 | ☐ |

## 全局约束（红线，所有阶段都适用）

1. **不改官方代码** `flash_head/`；**不改** `vendor/douyin_fetcher/` 里的源码（定制一律在适配层子类/包装里做，抖音改签名时整体替换 vendor 目录）。
2. **主进程不 import torch**（沿用现有约定）。弹幕抓取放独立子进程（理由见 03）。
3. **密钥不入库**：`.env`、cookie 文件、`live_data/` 都在 `.gitignore`；提交 `.env.example`（只有键名和注释）。
   不要从 `D:\jobdemo\douyin_zhibo\secrets.yaml` 或 cookie 文件里复制任何密钥到本仓库。
4. **测试不调用计费接口**：TTS/LLM/Embedding 全部 mock；真实接口只留一个手动自测命令，执行前需用户同意。
5. **原有测试必须继续通过**：`tests/live/test_e2e_mock.py`（33 项）、`tests/live/test_runner_logic.py`（18 项）。
   接口有变化时同步改测试，不许删测试凑通过。
6. **解耦**：每个能力都是"接口 + 实现 + 工厂/注册"，模块之间只通过事件总线、接口或 HTTP 通信（细则见 03 §2）。
7. **文档同步**：每阶段结束，更新受影响的 `README_LIVE.md`、`docs/live/ARCHITECTURE.md`、`API.md`、`DEVELOPMENT.md`，
   以及本目录 README 的进度表。新文档与代码不一致视为该阶段未完成。
8. **每阶段一个提交**（必要时多个），提交信息写清楚做了什么；**不 push**，除非用户要求。

## ⚠ 开工前需要用户准备或确认的事项

| 事项 | 说明 | 哪个阶段需要 |
|---|---|---|
| `.env` 里填 LLM 的 `LLM_API_URL` / `LLM_API_KEY` / `LLM_MODEL` | 见 06。没有也能开发（走模板兜底），但 P4/P5 的真实效果验收需要 | P3 起 |
| `.env` 里填火山 TTS 的 `VOLC_TTS_API_KEY`、`VOLC_VOICE_TYPE`（声音复刻音色 S_xxx）、`VOLC_RESOURCE_ID` | 可从 douyin_zhibo 的配置里**由用户自己**拷过来 | P3 真实自测 |
| 抖音登录 cookie 文件 | 游客模式收不到礼物（见 02 §5）；放 `secrets/douyin_cookie.txt`，或在 Web 上粘贴（只写不读） | P2 实测 |
| 本机装 Node.js | fetcher 的 `a_bogus.js` 用 PyExecJS 调 Node | P2 |
| 2～3 份真实产品资料（html/pdf/md 各一）+ 1 个产品视频 | 用于 P5/P6 验收；没有就先用 `tests/fixtures/` 里自造的样例 | P5、P6 |
| 直播画面比例 | 抖音带货多为竖屏 9:16（建议 720×1280），场景页按此设计；如需横屏请说明 | P6 |

> 已确认（2026-11-04）：fetcher 的许可证/原作者声明相关风险，用户已知悉并决定忽略（用途：产品介绍）。执行时**不需要**再就此询问，见 08 §1。
>
> 用户答复（2026-11-04）：
> - **直播画面比例：竖屏 9:16**（720×1280），P6 按此设计。
> - LLM / 火山 TTS 凭证：用户在另一个项目实测过；真需要时可以从参考项目里取，但**只写进本地 `.env`（gitignore），不进仓库、不打日志**；
>   P0–P4 用 mock 完成，尚未建 `.env`。真实接口自测（会计费）仍需用户同意。
> - 产品资料与产品视频：暂时不能提供 → P5/P6 先用 `tests/fixtures/` 自造样例。
> - 平台规则确认、抖音登录 cookie、本机 Node.js：先留空，用户后续提供（真实直播间实测放到那之后）。

## 完成标准（整体）

- 用 mock 弹幕源跑通：进场/弹幕/礼物/关注/点赞 → 互动引擎 → LLM（mock）+ 知识库检索 → 火山 TTS（mock）→ 数字人开口，画面在场景页显示，全流程有日志和 Web 可视化。
- 真实环境（用户确认后）：连上用户自己的直播间，弹幕面板显示用户昵称/性别/财富等级/粉丝团等；问产品问题能基于资料回答；送礼会点名感谢。
- Web 上修改直播间参数（房间号、回复策略、阈值、人设、音色语速、违禁词等）**不重启**生效，需重启的项有明确标注。
- 所有新增测试 + 原有 51 项测试通过；文档与代码一致。
