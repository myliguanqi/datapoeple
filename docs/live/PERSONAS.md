# 人设模板（P9）

一个**人设模板**把「这个主播是谁、怎么说话、怎么接弹幕、讲什么」全部打包在一个目录里，切换人设 = 换一整套行为，
不需要再在多个设置页之间拼凑，也不会残留上一个人设的提示词、触发器、意图、节目单或知识库资料。

## 一个人设拥有什么

| 内容 | 说明 |
|---|---|
| 身份与称呼 | 名称、描述、主播自称、店铺名、观众称呼后缀、礼物尊称 |
| 提示词 | `prompt.md`（LLM 写词系统提示）、`rag_prompt.md`（知识库回答规则）、`script_prompt.md`（讲解稿生成提示） |
| 弹幕意图 | 关键词表、意图名（同时是导演信号）、问答模式、先来先答、保鲜窗口、检索追加词 |
| 触发器 | 完整触发器表（when / 优先级 / 情绪 / 能量 / 冷却 / 话术 / LLM 指令 / 合并规则） |
| 知识库兜底 | 资料不够时说的「帮您确认」类话术 |
| 字数上限 | 互动台词 / 知识库回答（留空 = 用全局设置） |
| 节目单叫法 | 条目叫法（商品 / 讲解主题 / 节目）、是否显示价格、场景卡片模式、默认条目文案、结尾引导语 |
| 节目单 | 每人设一个 `live_data/programs/<人设 id>.json`（带货是商品，讲解是主题，娱乐可为空） |
| 知识库 | 每人设一个 `live_data/kb/<人设 id>/`（自己的 `kb.sqlite` + `files/`） |
| 视频素材 | **共用** `live_data/media/`；不同节目单关联不同视频，不会串台 |

## 目录结构

```
live/personas/
  models.py                 字段默认值、规范化、校验
  store.py                  出厂模板 + 用户模板读写
  service.py                PersonaService：当前人设、切换、复制、编辑、触发器增删改
  migrate.py                旧版本设置 / products.json / 单一知识库 → 人设模板
  builtin/
    commerce/              带货主播（只读）
      persona.yaml  prompt.md  rag_prompt.md  script_prompt.md  triggers.yaml
    explainer/             讲解主播（只读）
    entertainment/         娱乐主播（只读）
live_data/personas/<id>/   复制出来的用户模板（可编辑）
live_data/programs/<id>.json
live_data/kb/<id>/
```

出厂模板 id 固定为 `commerce` / `explainer` / `entertainment`；用户模板 id 由程序生成（`u` + 8 位十六进制），
不用用户输入的名字当目录名（防路径穿越）。

## persona.yaml 字段

```yaml
name: 带货主播                 # 网页显示名，≤ 30 字
description: 一句话说明
anchor_name: 小美              # 主播自称，话术里的 {anchor}
shop_name: ""                  # 话术里的 {shop_line} = 「，店铺「xx」」；空则没有
honorific: {male: 哥, female: 姐, unknown: ""}          # 昵称简称后缀
big_honorific: {male: 大哥, female: 大姐, unknown: 朋友}  # 礼物答谢尊称

intents:
  - name: product_question     # 小写字母/数字/下划线；同时是触发器 when
    label: 产品提问            # 网页显示名
    keywords: [多少钱, 价格, 尺码, 发货]
    qa: strict                 # strict | context | off
    first_come: true           # 先来先答（答完从队列移除），否则取最新一条
    window: question_s         # chat_s | question_s | gift_s | member_s | follow_s
    search_suffix: ""          # 检索时追加的词，如购买意向加「价格 优惠 下单」
    enabled: true
spam_keywords: [加微信, 代刷]   # 命中的弹幕不回应
intent_llm_hint: "..."         # 开 interaction.intent.llm_classify 时给 LLM 的分类说明

knowledge:
  no_answer_text: "{asker}，这个问题主播帮您确认一下……"   # qa=strict 且资料不够时说
limits:
  reply_max_chars: null        # 留空 = host.reply_max_chars
  answer_max_chars: null       # 留空 = host.answer_max_chars
program:
  item_label: 商品             # 网页和话术里的条目叫法
  show_price: true             # 是否显示/使用价格
  card: product                # product（完整卡片）| title（只显示名称）| off（不显示）
  default_item_text: 今天的好物 # 没有当前条目时 {product} 的兜底
  closing_line: "喜欢{product}的家人们……"   # 生成讲解稿时追加；可空
```

### 问答模式 `qa`

| 值 | 行为 |
|---|---|
| `strict` | 走当前人设的知识库；资料够就只依据资料回答，不够就说 `knowledge.no_answer_text`，不让 LLM 编 |
| `context` | 走知识库；资料够就把资料给 LLM 参考，不够也照常回应（用触发器话术兜底） |
| `off` | 不检索；直接接梗 / 用触发器话术 |

有 `strict` 意图时 `no_answer_text` 不能为空。意图名不能和平台信号、`chitchat/spam/noise/always`、导演快照键（`gift` / `chat` / `member` 等）重名。

## triggers.yaml

每一条触发器：

```yaml
gift_m:
  when: gift_m          # 平台信号，或本人设某个意图的 name；always = 恒真兜底
  priority: 60          # 高 → 低；≥ interaction.urgent_priority 不受发声间隔限制；≥ host.interrupt_priority 会打断讲解段
  emotion: 感激高能      # 中间情绪标签
  target_energy: 0.8    # 命中后把能量顶到多高（影响语速和发声间隔）
  cooldown_s: 0         # 同一触发器最短间隔
  max_stale_s: 60       # 排队太久就不说（不填用 interaction.max_stale_s）
  use_llm: false        # false = 直接用 lines，零成本零延迟
  role: who             # who（点名人）/ what（场面）/ 空；同拍合并用
  merge_clause: "欢迎{member_user}"   # 合并子句
  merge_with: [viewer_surge]          # 可合并的触发器名
  directive: "..."      # 给 LLM 的指令
  lines:                # 兜底话术，随机挑一条（避开最近说过的）
    - "感谢{gift_user}送的{gift_count}个{gift_name}！"
```

平台信号见 `live/interaction/signals.py` 的 `PLATFORM_SIGNALS`。常用占位符：

`{anchor}` `{shop}` `{shop_or_anchor}` `{product}` / `{item}` `{item_label}` `{intent}` `{intent_label}`
`{gift_user}` `{gift_honor}` `{gift_name}` `{gift_count}` `{gift_total}`
`{member_user}` `{member_honor}` `{member_visits}` `{member_gift_value}` `{member_grade_tag}`
`{follow_user}` `{follow_honor}` `{fans_user}` `{share_user}` `{like_user}` `{like_honor}` `{like_count}`
`{chat_user}` `{chat_honor}` `{chat_content}` `{asker}` `{asker_honor}` `{question}`
`{online}` `{online_delta}` `{milestone}` `{viewer_name}` `{gift_king_user}` …

## 内置模板

| id | 名称 | 适用 | 节目单 | 知识库 |
|---|---|---|---|---|
| `commerce` | 带货主播 | 商品讲解、价格机制、购买意向引导 | 商品，显示价格，完整卡片 | 商品资料 + 公共资料 |
| `explainer` | 讲解主播 | 讲解视频、系统介绍、原理/参数科普，不卖货 | 讲解主题，不显示价格，标题卡片 | 主题资料 + 公共资料 |
| `entertainment` | 娱乐主播 | 聊天接梗 / 整活 / 游戏陪伴 / 知识问答，不卖货 | 可为空，不显示卡片 | 自己的知识资料（可选） |

出厂模板只读；网页「人设」页点「复制」生成用户模板后编辑。**节目单和知识库不受模板只读限制**：
即使当前用出厂模板，也可以直接在产品页/知识库页添加条目和资料（它们属于当前人设）。

## 切换规则

只允许**停播**时切换：

- 没有 `starting` / `running` 的直播会话；
- 弹幕未连接（`state` 为 stopped/finished）；
- 知识库没有正在入库的文档。

不满足时 `POST /api/personas/{id}/activate` 返回 **409** 和原因。切换会依次：

1. 打断当前播报，取消写词线程，清空互动队列和主持人历史；
2. 重建意图队列、清空决策日志、重置导演能量/冷却/消费标记/回礼回踩/榜一/破整锁存；
3. 换 `programs/<id>.json`（节目单）和 `kb/<id>/`（知识库）；清空 LLM 最近台词；
4. 停止正在播放的视频并刷新场景；
5. 发布 `persona.changed`，最后写 `live_data/personas/active.json`。

编辑当前人设（提示词 / 意图 / 触发器 / 话术）立即生效：重读触发器、重建意图队列、清空旧 cue，但不清导演的长期状态。

## 旧版本迁移（一次性）

启动时若发现旧数据，`live/personas/migrate.py` 会：

1. 读 `live_data/settings.json` 里旧的人设键（`persona.*`、`interaction.triggers`、意图关键词、`knowledge.no_answer_text`），
   以出厂 `commerce` 为底生成用户模板「带货主播（迁移）」，设为当前；原设置备份为 `settings.json.pre-persona.bak`，
   这些键从设置覆盖里移除（其他设置原样保留）；
2. `live_data/products.json` → `live_data/programs/<人设 id>.json`，原文件改名为 `.bak`；
3. 旧版单一知识库 `live_data/kb/{kb.sqlite,files}` → `live_data/kb/<人设 id>/`；
4. 写标记 `live_data/personas/.migrated_v1`，只迁一次。

## 新增一个出厂模板

1. 在 `live/personas/builtin/<id>/` 建 `persona.yaml` + 三个 `.md` + `triggers.yaml`（可复制现有模板改）；
2. 在 `live/personas/store.py` 的 `BUILTIN_ORDER` 里加 id（决定网页顺序；不加也会被发现，排在最后）；
3. 用 `python -c "from live.personas.store import load_builtin; from live.personas.models import validate; print(validate(load_builtin('<id>')))"` 校验；
4. 补一条合规测试：`pytest tests/live/test_p4_intent_compliance.py::test_policy_lines_are_compliant`；
5. 重启服务，在「人设」页复制后编辑。

## 网页与接口

- 「人设」页：列表、切换、复制、删除、完整编辑（基本信息 / 提示词 / 弹幕意图 / 触发器 / 节目单与知识库）。
- 「弹幕互动 → 触发器」页编辑的是当前人设（出厂模板会提示先复制）。
- 接口清单见 [API.md](API.md)「人设模板」一节；运行时结构见 [ARCHITECTURE.md](ARCHITECTURE.md) §12/§13；
  任务书、验收记录和已知边界见 [第三阶段 P9 任务书](../tasks/phase3-persona/README.md)。
