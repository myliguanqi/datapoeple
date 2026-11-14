# 第三阶段 P9：人设模板（带货 / 讲解 / 娱乐）任务书与进度

- 编写 / 完成日期：2026-11-14
- 状态：☑ 已完成（mock 环境；真实环境事项见文末）
- 需求来源：用户反馈「弹幕互动、触发器和主播人设分散；切换人设困难，且总有残留的带货主播人设」
- 设计说明：[docs/live/PERSONAS.md](../../live/PERSONAS.md)

## 1. 目标

把「这个主播是谁、怎么说话、怎么接弹幕、讲什么」收进一个可复制、可切换的**人设模板**，切换后不残留上一个人设的提示词、触发器、意图、节目单或知识库资料。

用户确认的四个决策：

1. **只在停播时允许切换**（没有运行中的会话、弹幕未连接、知识库不忙）。
2. **节目单 + 知识库按人设隔离，视频素材共用**。
3. 出厂**娱乐主播**覆盖：聊天接梗 / 整活、游戏解说 / 陪伴、知识科普 / 问答。
4. 网页做**完整编辑**：提示词、弹幕意图、触发器、称呼、字数、节目单叫法、知识库兜底话术。

## 2. 交付内容

### 2.1 模板与内置人设

```
live/personas/
  models.py       字段默认值、规范化、校验（意图名/触发器/字数/兜底话术）
  store.py        出厂模板（只读）+ 用户模板（live_data/personas/<id>/）
  service.py      当前人设、切换（事务性 + 回滚）、复制、编辑、触发器增删改
  migrate.py      旧设置 / products.json / 单一知识库 → 人设模板
  builtin/
    commerce/     带货主播（从原 policies/ 迁移）
    explainer/    讲解主播（讲解视频、系统介绍、原理科普，不卖货）
    entertainment/娱乐主播（聊天接梗、整活、游戏陪伴、知识问答，不卖货）
```

一个模板 = `persona.yaml` + `prompt.md` + `rag_prompt.md` + `script_prompt.md` + `triggers.yaml`。
节目单在 `live_data/programs/<id>.json`，知识库在 `live_data/kb/<id>/`。

### 2.2 运行时

- `live/interaction/signals.py`：平台固定信号、触发器字段、意图窗口、保留键。
- `IntentClassifier` 按当前人设的 `intents[]` 分类；意图名同时是导演信号。
- `Director` 从 `cfg["persona"]["triggers"]` 重建触发器；平台信号 + 人设意图信号。
- `Cue` 增加 `intent` / `qa` / `first_come` / `search_suffix`；`Composer` 按 `qa` 决定是否检索、用哪种字数上限。
- `KnowledgeBase` 每人设一个目录；`switch_persona()` 先建新 store/检索后端再一次性换上；`busy()` 防止入库中切换。
- `ProductStore.switch_path()` + `ProductScript.reset()`：节目单文件切换但对象引用不变，API/场景/知识库的绑定不失效。
- `SceneManager` 按 `persona.program.card` 显示完整卡片 / 只标题 / 不显示，按 `show_price` 决定价格。
- `LLMService.clear_history()`：切换时清掉旧人设的「最近台词」。

### 2.3 Web

- 新增侧边栏「人设」页：模板卡片、使用、复制、删除、完整编辑（基本信息 / 提示词 / 弹幕意图 / 触发器 / 节目单与知识库）。
- 「弹幕互动 → 触发器」编辑当前人设；出厂模板只读，提示先复制。
- 「节目与知识库」「直播控制台」的条目叫法、场景卡片、价格显示随当前人设变化。
- 「模拟」页按当前人设的意图生成快捷注入按钮。
- 出厂模板只读，但**节目单和知识库不受此限制**：当前用出厂模板也能直接加条目、传资料。

### 2.4 迁移

第一次启动时：

1. 旧 `settings.json` 的 `persona.*` / `interaction.triggers` / 意图关键词 / `knowledge.no_answer_text` → 用户模板「带货主播（迁移）」，原文件备份为 `settings.json.pre-persona.bak`；
2. `products.json` → `programs/<人设 id>.json`；
3. 旧单一知识库 `live_data/kb/{kb.sqlite,files}` → `live_data/kb/<人设 id>/`；
4. 写标记 `live_data/personas/.migrated_v1`，只迁一次。

### 2.5 接口

见 [docs/live/API.md](../../live/API.md)「人设模板」：

- `GET /api/personas`、`GET /api/personas/active`、`GET /api/personas/{id}`
- `POST /api/personas/{id}/activate`（在播 / 弹幕连接 / 入库中 → 409）
- `POST /api/personas/{id}/duplicate`（复制节目单，不复制知识库）
- `PUT /api/personas/{id}`、`DELETE /api/personas/{id}?delete_data=false`
- `GET/POST/PUT/DELETE /api/personas/{id}/triggers...`
- 兼容入口：`/api/interaction/triggers` 读写当前人设（出厂模板 403）
- 新总线主题：`persona.changed`

## 3. 切换语义（无残留）

`POST /api/personas/{id}/activate` 只在停播时允许，依次：

1. 打断当前播报、取消写词线程、清空主持人队列与历史；
2. 重建意图队列、清空决策日志、重置导演能量 / 冷却 / 消费标记 / 回礼回踩 / 榜一 / 破整锁存；
3. 换节目单文件与知识库目录；
4. 清空 LLM 最近台词；
5. 停止正在播放的视频并刷新场景；
6. 发布 `persona.changed`，最后写 `live_data/personas/active.json`。

切换是事务性的：钩子失败会回滚到上一个人设。编辑当前人设走软刷新（重读触发器 / 意图、清旧 cue，但保留导演长期状态）。

## 4. 测试与验收

- `pytest tests/live -q`：**210 项全过**（新增 `test_p9_personas.py` 11 项：内置模板校验、模板读写、切换/复制/编辑、触发器增删改、娱乐意图、先来先答消费、切换清残留、停播校验、节目单/知识库隔离、旧数据迁移）。
- e2e：mock 33 + 带货 16 + 讲品 15 + 场景 15 + 人设切换 10，全部通过；全程未调用 TTS / LLM / Embedding 计费接口。
- 人设 e2e：复制娱乐人设 → 停播切换 → 节目单/知识库为空 → 「讲个笑话」命中 `joke_request`、没有带货意图残留 → 垃圾弹幕不回应 → 切回原人设并删除测试人设。
- 网页：Chrome 无头（CDP）检查人设页三套模板、编辑器五个标签、改基本信息保存、添加触发器；1366 宽与 390 宽无横向溢出、无 JS 报错。

## 5. 已知边界 / 后续可做

- 切换只在停播时允许（按用户要求）；直播中不热切。
- 复制模板会复制节目单文件，**不复制知识库资料**（避免大文件悄悄翻倍），需要在新人设下重新上传。
- 网页暂无「从零新建空白模板」按钮，增加方式是复制现有模板再改。
- 没有按人设独立的 TTS 音色 / 形象 / 推流参数（仍为全局或会话级）。
- 视频素材库共用；不同节目单关联不同视频，不会串台。
- 真实环境未验证：Windows + 真实模型、真实抖音直播间、火山 TTS / LLM 计费调用、OBS 实机；清单见 `README_LIVE.md`「需要你在真实环境验证的事项」。
- 未做：人设导入/导出、模板版本历史、单人设内跨模板复制单个节目条目/知识库文档、多人设同时直播。
