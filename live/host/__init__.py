"""主持人：唯一决定"数字人下一句说什么"的地方。

  session_tap.py  挂在会话上的 FrameSink：把会话事件（speak_done、interrupted、playback…）转发到事件总线 session.event
  composer.py     写词：模板 or LLM（流式按句），按性别称呼、字数限制、超时回退兜底、合规过滤
  host.py         编排循环：取 cue → 写词 → 追加式请求逐句交给会话；高优先级打断讲品段；人工插话；暂停开关
"""
