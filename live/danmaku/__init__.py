"""弹幕接入：所有弹幕源（抖音 / 模拟 / 回放）都跑在独立子进程里，统一产出 LiveEvent（live/core/events.py）。

  base.py      DanmakuSource 接口 + SOURCES 注册表
  douyin/      抖音：parse.py（protobuf → LiveEvent 纯函数 + 连击聚合）、adapter.py（接管 vendor fetcher）
  mock.py      模拟源（可配速率/比例/性别/财富等级/产品提问，也可跑确定性脚本）
  replay.py    回放录制的 JSONL
  process.py   子进程入口
  manager.py   DanmakuManager（主进程）：启动/监控/退避重连/热切换/发布 live.event 与 danmaku.status
  recorder.py  JSONL 录制（主进程，订阅 live.event）
  cookie.py    抖音登录 cookie 的解析与读写（只写不读）
"""
