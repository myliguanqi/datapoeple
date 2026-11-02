"""主进程侧的 worker 池：启动每张卡的子进程、RPC 调用、按用途挑选显卡。

多卡策略（gpu.mode）：
  single          只用 devices[0]
  multi_instance  每张卡一个独立 worker（Windows 可用，不需要 NCCL）。
                  直播会话和离线任务分摊到不同的卡上，两张卡同时工作；
                  卡多时也可以同时开多个直播间。
  sequence_parallel  单路画面用两张卡一起算（官方 xfuser USP，需要 NCCL，只能在 Linux 上跑）。
                  本框架尚未实现，见 docs/live/DEVELOPMENT.md「多卡单路加速」。
"""
from __future__ import annotations

import itertools
import multiprocessing as mp
import queue
import threading
import time
from concurrent.futures import Future

from loguru import logger

from live.worker.process import worker_main

PRIORITY_LIVE = 0       # 直播会话的生成/控制
PRIORITY_CONTROL = 1
PRIORITY_OFFLINE = 10   # 离线视频任务


class WorkerError(RuntimeError):
    """worker 里执行方法抛出的异常（带远端 traceback）。"""


class WorkerUnavailable(RuntimeError):
    """worker 未就绪或已退出。"""


class WorkerHandle:
    def __init__(self, index: int, gpu_id: int, cfg: dict, ctx):
        self.index = index
        self.gpu_id = gpu_id
        self.cfg = cfg
        self.ctx = ctx
        self.cmd_q = ctx.Queue()
        self.res_q = ctx.Queue()
        self.proc = None
        self.state = "init"           # init | starting | ready | error | dead | stopped
        self.error = None
        self.info: dict = {}
        self.ready_event = threading.Event()
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.live_sessions: set = set()
        self.jobs: set = set()

    def start(self) -> None:
        self.state = "starting"
        self.proc = self.ctx.Process(
            target=worker_main, args=(self.index, self.gpu_id, self.cfg, self.cmd_q, self.res_q),
            name=f"flashhead-worker{self.index}", daemon=True,
        )
        self.proc.start()
        threading.Thread(target=self._read_loop, name=f"worker{self.index}-reader", daemon=True).start()
        logger.info(f"worker{self.index} 启动中 (gpu {self.gpu_id}, pid {self.proc.pid})")

    def wait_ready(self, timeout: float | None = None) -> bool:
        self.ready_event.wait(timeout)
        return self.state == "ready"

    def _read_loop(self) -> None:
        while True:
            try:
                req_id, ok, payload = self.res_q.get(timeout=1.0)
            except queue.Empty:
                if self.proc is not None and not self.proc.is_alive():
                    self._on_exit()
                    return
                continue
            except (EOFError, OSError):
                self._on_exit()
                return
            if req_id == "__ready__":
                if ok:
                    self.state, self.info = "ready", payload
                    logger.info(f"worker{self.index} 就绪: {payload.get('gpu_name')} 模型={list(payload.get('models', {}))}")
                else:
                    self.state, self.error = "error", payload
                    logger.error(f"worker{self.index} 启动失败:\n{payload}")
                self.ready_event.set()
                continue
            with self._lock:
                fut = self._pending.pop(req_id, None)
            if fut is None:
                continue
            if ok:
                fut.set_result(payload)
            else:
                fut.set_exception(WorkerError(payload))

    def _on_exit(self) -> None:
        if self.state not in ("stopped", "error"):
            self.state = "dead"
            self.error = self.error or f"worker 进程退出 (exitcode={self.proc.exitcode})"
            logger.error(f"worker{self.index} {self.error}")
        self.ready_event.set()
        with self._lock:
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            fut.set_exception(WorkerUnavailable(self.error or "worker 已退出"))

    def call(self, method: str, priority: int = PRIORITY_CONTROL, **kwargs) -> Future:
        if self.state != "ready":
            raise WorkerUnavailable(f"worker{self.index} 状态为 {self.state}: {self.error or ''}")
        fut: Future = Future()
        req_id = next(self._ids)
        with self._lock:
            self._pending[req_id] = fut
        self.cmd_q.put((priority, req_id, method, kwargs))
        return fut

    def refresh_info(self, timeout: float = 5.0) -> dict:
        try:
            self.info = self.call("info", priority=PRIORITY_CONTROL).result(timeout)
        except Exception:
            pass
        return self.info

    def stop(self, timeout: float = 10.0) -> None:
        if self.proc is None:
            return
        if self.state == "ready":
            try:
                self.call("shutdown", priority=-1).result(timeout)
            except Exception:
                pass
        self.state = "stopped"
        self.proc.join(timeout)
        if self.proc.is_alive():
            self.proc.terminate()

    def describe(self) -> dict:
        return {
            "index": self.index,
            "gpu_id": self.gpu_id,
            "state": self.state,
            "error": self.error,
            "info": self.info,
            "live_sessions": sorted(self.live_sessions),
            "jobs": sorted(self.jobs),
        }


class WorkerPool:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        gpu = cfg["gpu"]
        if gpu["mode"] == "sequence_parallel":
            # TODO(多卡单路加速)：见 docs/live/DEVELOPMENT.md。目前回退为每卡独立实例。
            logger.warning("gpu.mode=sequence_parallel 尚未实现（需要 Linux + NCCL），回退为 multi_instance")
            gpu["mode"] = "multi_instance"
        self.ctx = mp.get_context("spawn")
        self.workers = [WorkerHandle(i, d, cfg, self.ctx) for i, d in enumerate(gpu["devices"])]
        self.started = threading.Event()

    def start(self) -> None:
        """启动所有 worker。stagger_start=True 时逐个启动（等上一个加载完），降低内存峰值。"""
        timeout = self.cfg["gpu"]["worker_start_timeout"]
        for w in self.workers:
            w.start()
            if self.cfg["gpu"]["stagger_start"]:
                w.wait_ready(timeout)
        for w in self.workers:
            w.wait_ready(timeout)
        self.started.set()

    def wait_any_ready(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.time() + timeout
        while deadline is None or time.time() < deadline:
            if any(w.state == "ready" for w in self.workers):
                return True
            if all(w.state in ("error", "dead", "stopped") for w in self.workers):
                return False
            time.sleep(0.2)
        return False

    def pick(self, purpose: str = "live", gpu_id: int | None = None) -> WorkerHandle:
        """挑一个 worker：live 优先 live_devices、离线优先 offline_devices，再按负载最小。"""
        ready = [w for w in self.workers if w.state == "ready"]
        if gpu_id is not None:
            ready = [w for w in ready if w.gpu_id == gpu_id]
        if not ready:
            raise WorkerUnavailable("没有就绪的 worker（模型还在加载，或加载失败，看 /api/system）")
        pref = self.cfg["gpu"]["live_devices" if purpose == "live" else "offline_devices"]
        cands = [w for w in ready if pref is None or w.gpu_id in pref] or ready
        if purpose == "live":
            return min(cands, key=lambda w: (len(w.live_sessions), len(w.jobs), w.index))
        return min(cands, key=lambda w: (len(w.jobs), len(w.live_sessions), w.index))

    def describe(self) -> list:
        return [w.describe() for w in self.workers]

    def stop(self) -> None:
        for w in self.workers:
            w.stop()
