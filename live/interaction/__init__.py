"""互动引擎（移植参考项目 douyin_zhibo 的平台层导演，去掉游戏层，改成带货语境）。

  names.py       昵称简称（X哥 / X姐）、按性别尊称
  cue.py         Cue：导演产出的"这一刻该说什么"
  state.py       RoomState：事件保鲜窗口、回头客、本场礼物榜、在线人数变化 → RoomSnapshot
  director.py    Director：派生信号 → 触发器（policies/commerce.yaml + Web 覆盖）→ 冷却 / 能量 / 发声间隔 / 同拍合并
  memory.py      ViewerMemory：跨场长期记忆（建档门槛、known_before）
  queue.py       CueQueue：单槽优先级队列 + 过期丢弃
  intent.py      弹幕意图：产品提问 / 购买意向 / 闲聊 / 垃圾
  compliance.py  合规过滤：极限词替换、平台禁语
  engine.py      InteractionEngine：订阅 live.event，驱动 RoomState + Director，发布 interaction.cue，入队，决策日志
"""
