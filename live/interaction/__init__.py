"""互动引擎（移植参考项目 douyin_zhibo 的平台层导演；人设模板决定意图、信号与触发器）。

  names.py       昵称简称（X哥 / X姐）、按性别尊称（来自当前人设）
  cue.py         Cue：导演产出的"这一刻该说什么"（含意图名 / qa 模式）
  signals.py     平台固定信号、触发器字段、意图窗口（人设模板不必 import 导演）
  state.py       RoomState：事件保鲜窗口、回头客、本场礼物榜、在线人数、按人设意图分流 → RoomSnapshot
  director.py    Director：派生平台信号 + 人设意图信号 → 当前人设的触发器表 → 冷却 / 能量 / 发声间隔 / 同拍合并
  memory.py      ViewerMemory：跨场长期记忆（建档门槛、known_before）
  queue.py       CueQueue：单槽优先级队列 + 过期丢弃
  intent.py      弹幕意图：当前人设的关键词表 → 意图名；垃圾 / 无意义 / 闲聊兜底
  compliance.py  合规过滤：极限词替换、平台禁语
  engine.py      InteractionEngine：订阅 live.event，驱动 RoomState + Director，发布 interaction.cue，入队，决策日志
"""
