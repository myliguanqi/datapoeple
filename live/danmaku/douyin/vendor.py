"""定位 vendor/douyin_fetcher（原样复制的 DouyinLiveWebFetcher，AGPL-3.0，LICENSE 随附）。

vendor 代码用顶层包名 `protobuf`、`liveMan`、`ac_signature` 互相导入，所以要把它的目录放进 sys.path。
只在弹幕子进程和单元测试里调用，主进程不加载 vendor 代码。
"""
from __future__ import annotations

import os
import sys

from live.config import PROJECT_ROOT

FETCHER_DIR = os.environ.get("FLASHHEAD_FETCHER_DIR") or os.path.join(PROJECT_ROOT, "vendor", "douyin_fetcher")
SIGN_JS = os.path.join(FETCHER_DIR, "sign.js")
ABOGUS_JS = os.path.join(FETCHER_DIR, "a_bogus.js")


def ensure_path() -> None:
    if FETCHER_DIR not in sys.path:
        sys.path.insert(0, FETCHER_DIR)


def pb():
    """vendor 的 betterproto 消息类模块（protobuf/douyin.py）。"""
    ensure_path()
    from protobuf import douyin
    return douyin
