"""弹幕源接口。实现跑在弹幕子进程里（live/danmaku/process.py），通过回调把事件和状态交给子进程主循环。

新增一个平台：
    @SOURCES.register("kuaishou")
    def _create(opts, emit, status):
        from live.danmaku.kuaishou import KuaishouSource      # 懒加载第三方依赖
        return KuaishouSource(opts, emit, status)
再把名字加进 schema 里 danmaku.source 的 choices。见 docs/live/DEVELOPMENT.md「新增弹幕平台」。
"""
from __future__ import annotations

import threading
from typing import Callable

from live.core.events import LiveEvent
from live.core.registry import Registry

EmitFn = Callable[[LiveEvent], None]
StatusFn = Callable[[dict], None]

SOURCES = Registry("弹幕源")


class DanmakuSource:
    """run() 阻塞直到 stop() 或连接结束（返回后由 DanmakuManager 按退避策略重启子进程）。

    opts：子进程启动参数（DanmakuManager._child_opts 组装：live_id、gender_map、combo_timeout_s、
          mock/replay 参数、cookie 文件路径……）
    emit(ev)：产出一个统一事件；status(dict)：更新状态（state / room_id / logged_in / error …，增量合并）
    """

    name = "base"

    def __init__(self, opts: dict, emit: EmitFn, status: StatusFn):
        self.opts = opts
        self.emit = emit
        self.status = status
        self._stop = threading.Event()

    def run(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def set_sniff(self, on: bool) -> None:
        """协议探针开关（只有真实平台源需要）。"""

    def tick(self) -> None:
        """子进程每 0.5 秒调用一次（连击超时结算等）。"""


@SOURCES.register("mock")
def _create_mock(opts, emit, status):
    from live.danmaku.mock import MockSource
    return MockSource(opts, emit, status)


@SOURCES.register("replay")
def _create_replay(opts, emit, status):
    from live.danmaku.replay import ReplaySource
    return ReplaySource(opts, emit, status)


@SOURCES.register("douyin")
def _create_douyin(opts, emit, status):
    from live.danmaku.douyin.adapter import DouyinSource   # 只有子进程会走到这里：vendor 代码不进主进程
    return DouyinSource(opts, emit, status)
