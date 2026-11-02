"""推理 worker：每张显卡一个子进程，进程里持有一个 Runner（FlashHead 或 Mock）。

主进程通过 live.pool.WorkerHandle 以 RPC 方式调用 Runner 的公开方法（见 RUNNER_METHODS）。
"""

# 允许主进程远程调用的 Runner 方法
RUNNER_METHODS = (
    "info",
    "load_model",
    "unload_model",
    "open_session",
    "set_avatar",
    "switch_model",
    "generate",
    "close_session",
)
