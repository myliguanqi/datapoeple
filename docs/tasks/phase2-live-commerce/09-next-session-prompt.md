# 09 新窗口接手提示词（P5–P8）

在 `D:\ProgramData\SoulX-FlashHead` 打开 Claude Code，把下面整段粘进去。

````text
请完整阅读 docs/tasks/phase2-live-commerce/ 下的全部文档（从 README.md 开始，进度表和「用户答复」都在里面）、根目录 CLAUDE.md、README_LIVE.md、docs/live/*.md。
P0–P4 已完成并提交（git log 可见）。你的任务是按 04-plan.md 继续做剩下的 P5 → P6 → P7 → P8，执行规则不变：
每个阶段先列出要改的文件 → 实现 → 跑该阶段新测试 + 全部原有测试 → 更新 README 进度表和受影响的文档（README_LIVE.md、docs/live/*.md）→ git commit（不 push）。
遇到文档中标「⚠ 需用户确认」的事项先停下来问我。不要调用任何会计费的真实接口（TTS/LLM/Embedding），测试一律用 mock。

【已确定的事，不用再问】
- 直播画面比例：竖屏 9:16（720×1280），P6 场景页按此设计。
- 我暂时没有真实产品资料和产品视频：P5/P6 用 tests/fixtures/ 自造样例（md/html/pdf 各一份、几秒的测试视频用 ffmpeg 现场生成、不入库；pdf 夹具提交前确认 <200KB）。
- LLM / 火山凭证我在别的项目实测过；真要用可从 D:\jobdemo\douyin_zhibo 取，但只能写进本地 .env（gitignore），不进仓库、不打日志；任何会计费的调用先问我。
- 平台规则确认、抖音登录 cookie、Node.js：我以后再提供，真实直播间实测不在这次范围内（P8 只把待实测清单写进 README_LIVE.md）。

【环境】
- Python 用 conda 环境 flashhead：D:\anaconda\envs\flashhead\python.exe（3.10，已装全量依赖含 torch 2.7.1+cu128；numpy 必须 <2）。新依赖加进 requirements_live.txt 并注明用途，装完跑 pip check。
- 测试命令（加 PYTHONIOENCODING=utf-8）：
  python -m pytest tests/live -q                         # 当前 140 项
  python live_server.py --config live/configs/mock.yaml   # 另起，后台跑
  python tests/live/test_e2e_mock.py                      # 原有 33 项
  python tests/live/test_e2e_commerce.py                  # 带货全链路 16 项
  python tests/live/test_runner_logic.py                  # 原有 18 项
- sqlite3 支持 FTS5（检索后端默认 fts，jieba 预分词）；ffmpeg/ffprobe 在 PATH。
- 停服务：找占用 8000 端口的进程 taskkill /PID <pid> /T /F（要连 worker/弹幕子进程一起杀，避免残留）。
- 页面检查：用 Edge 无头 + 远程调试端口 9222（msedge --headless=new --remote-debugging-port=9222 --user-data-dir=<scratchpad>），chrome-devtools MCP 就能连；检查 1366×768 与窄屏 scrollWidth <= innerWidth、无 JS 报错。
- Bash 工具里 heredoc 内容含三引号/复杂引号会报 "unexpected EOF"：长脚本先用 Write 写到 scratchpad 再执行。

【已有的接入点（P5/P6 直接用，别重写）】
- live/host/composer.py：Composer(retrieve=...) 钩子，retrieve(cue) 返回 {"context", "refs", "enough", "fallback_text"}；product_question / purchase_intent 会调用；资料不足时用 fallback_text（"帮您确认/看商品详情/问客服"），不让 LLM 编。
- live/host/host.py：Host.script 接口（P5 的 ProductScript 实现）：active() / next_segment()->Cue(kind="product", interruptible=True) / on_interrupted(cue)（断点续讲，不前进）/ on_done(cue) / status() / skip()；/api/host/script/next 依赖 skip()。讲品中低于 host.min_priority_during_script 的互动会被跳过。
- live/app_context.py：host_context() 目前返回 {}，P5 要返回当前产品 {"product_name": ...}（话术里的 {product} 占位符用它）；新服务都在 AppContext.start() 里构造并 settings.register(...) 热更新。
- 设置项一律先加到 live/settings/schema.py（带中文 label/help、hot/restart），Web 设置页自动生成；knowledge.* / products.* / scene.* 还没加（06 §2.5、§2.6）；compliance.ai_disclosure 已存在——P6 场景页必须常驻显示且不能关闭。
- 所有要说出口的文本（含讲解稿、LLM 生成的讲解稿）都过 live/interaction/compliance.py。
- Web：原生 ES modules（web/app.js 外壳、web/ui.js 组件、web/pages/*.js），products.js / media.js 目前是占位页；/ws/console 已推 settings/danmaku/live.event/interaction.cue/host.action，新增主题（kb.ingest、scene.changed）加进 live/feed.py 的 FEED_TOPICS。

【注意】
- 不改 flash_head/ 和 vendor/；主进程不 import torch。
- 测试和 e2e 跑完要清理写进 live_data/ 的测试数据、恢复改过的设置（参考 test_e2e_commerce.py 末尾的做法）；模拟/mock/回放事件默认不写长期记忆，别破坏这条。
- 上传文件限制类型与大小、文件名重写为随机 ID（08 §6）；知识库与讲解稿内容进 LLM 时视为不可信输入。
- P8 要把根目录 CLAUDE.md 更新为最新约定，并按 07 §4 人工验收清单（mock 模式）逐条走一遍，结果写进进度表；"需要真实环境验证"的事项汇总到 README_LIVE.md。

先从 P5 开始。
````
