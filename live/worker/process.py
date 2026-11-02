"""worker 子进程入口。主进程用 multiprocessing(spawn) 启动，每张显卡一个。

消息格式：
  主 -> worker  cmd_q: (priority, req_id, method, kwargs)      priority 越小越先执行
  worker -> 主  res_q: (req_id, ok, payload)                    payload = 返回值 或 错误文本
  启动完成时发送 ("__ready__", ok, info_or_error)

worker 单线程串行执行命令。每取一条命令前，先把队列里积压的命令全部取出按优先级排序，
保证直播会话的 generate（priority=0）能插到离线任务（priority=10）前面。
"""
from __future__ import annotations

import heapq
import os
import queue
import sys
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def create_runner(cfg: dict, gpu_id: int):
    backend = cfg["models"]["backend"]
    if backend == "mock":
        from live.worker.mock_runner import MockRunner
        return MockRunner(cfg, gpu_id)
    if backend == "flashhead":
        from live.worker.flashhead_runner import FlashHeadRunner
        return FlashHeadRunner(cfg, gpu_id)
    raise ValueError(f"未知 models.backend: {backend}")


def worker_main(index: int, gpu_id: int, cfg: dict, cmd_q, res_q) -> None:
    # 必须在任何 CUDA 调用之前设置：这个进程只看得到一张卡，代码里统一用 "cuda"
    if cfg["models"]["backend"] != "mock":
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    os.chdir(PROJECT_ROOT)   # 官方代码有相对路径（flash_head/configs/...）

    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format=f"<green>{{time:HH:mm:ss}}</green> | {{level:<7}} | worker{index}/gpu{gpu_id} | {{message}}")

    from live.worker import RUNNER_METHODS

    try:
        runner = create_runner(cfg, gpu_id)
        for m in cfg["models"]["preload"]:
            runner.load_model(m)
        res_q.put(("__ready__", True, runner.info()))
        logger.info("worker 就绪")
    except BaseException as e:  # noqa: BLE001  启动失败要把原因带回主进程
        logger.exception("worker 启动失败")
        res_q.put(("__ready__", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
        return

    heap: list = []
    while True:
        if not heap:
            heapq.heappush(heap, cmd_q.get())
        while True:
            try:
                heapq.heappush(heap, cmd_q.get_nowait())
            except queue.Empty:
                break
        _prio, req_id, method, kwargs = heapq.heappop(heap)
        if method == "shutdown":
            res_q.put((req_id, True, None))
            break
        if method not in RUNNER_METHODS:
            res_q.put((req_id, False, f"未知方法: {method}"))
            continue
        try:
            res_q.put((req_id, True, getattr(runner, method)(**kwargs)))
        except BaseException as e:  # noqa: BLE001
            logger.error(f"{method} 失败: {type(e).__name__}: {e}")
            res_q.put((req_id, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                break
