"""HTTP / WebSocket 服务入口（app 工厂）。

    python -m live.server --config live/configs/live.yaml
    python -m live.server --config live/configs/mock.yaml        # 无模型调试

路由按功能放在 live/api/*.py；服务的构造与注入在 live/app_context.py。
接口文档：docs/live/API.md；网页客户端：http://<host>:<port>/
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from live import __version__
from live.api import console_ws, danmaku, host as host_api, interaction, offline, sessions, settings as settings_api, system
from live.api.deps import WRITE_METHODS, check_write
from live.app_context import AppContext
from live.config import PROJECT_ROOT
from live.settings.env import Env
from live.settings.store import SettingsCenter

WEB_DIR = os.path.join(PROJECT_ROOT, "web")


def create_app(cfg: dict | None = None, *, settings: SettingsCenter | None = None, env: Env | None = None,
               start_workers: bool = True) -> FastAPI:
    """cfg 与 settings 二选一：正式启动传 SettingsCenter.from_files(...)；老接口/测试可直接传合并好的 cfg。

    start_workers=False 时不启动 worker 进程（只测接口层用）。
    """
    if settings is None:
        if cfg is None:
            raise ValueError("create_app 需要 cfg 或 settings")
        settings = SettingsCenter.from_cfg(cfg)
    ctx = AppContext(settings, env, start_workers=start_workers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.start(asyncio.get_running_loop())
        yield
        ctx.stop()

    app = FastAPI(title="SoulX-FlashHead Live", version=__version__, lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def write_guard(request: Request, call_next):
        # 所有 /api/ 写操作统一鉴权（见 live/api/deps.py），后续新增的写接口自动受保护
        if request.method in WRITE_METHODS and request.url.path.startswith("/api/"):
            denied = check_write(ctx.admin_token, request.headers.get("x-admin-token"),
                                 request.client.host if request.client else None)
            if denied:
                return JSONResponse({"detail": denied[1]}, status_code=denied[0])
        return await call_next(request)

    for mod in (system, sessions, offline, settings_api, danmaku, interaction, host_api, console_ws):
        app.include_router(mod.router)
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
    return app


def _windows_timer_resolution() -> None:
    """Windows 默认 sleep 精度约 15ms，会让 25fps 节拍抖动；调到 1ms。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="SoulX-FlashHead 实时直播服务")
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "live", "configs", "live.yaml"))
    parser.add_argument("--mock", action="store_true", help="使用 mock 后端（不加载模型）")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--devices", help="覆盖 gpu.devices，例如 0,1")
    parser.add_argument("--env", default=os.path.join(PROJECT_ROOT, ".env"), help=".env 路径")
    args = parser.parse_args()

    overrides: dict = {}
    if args.mock:
        overrides.setdefault("models", {})["backend"] = "mock"
    if args.host:
        overrides.setdefault("server", {})["host"] = args.host
    if args.port:
        overrides.setdefault("server", {})["port"] = args.port
    if args.devices:
        overrides.setdefault("gpu", {})["devices"] = [int(x) for x in args.devices.split(",")]
    settings = SettingsCenter.from_files(args.config, overrides)
    cfg = settings.cfg

    if shutil.which(cfg["server"]["ffmpeg"]) is None and not os.path.exists(cfg["server"]["ffmpeg"]):
        logger.warning(f"找不到 ffmpeg（{cfg['server']['ffmpeg']}）：edge-tts 解码、推送音频、离线视频会失败")
    _windows_timer_resolution()

    env = Env(args.env)
    import uvicorn
    logger.info(f"backend={cfg['models']['backend']} gpu={cfg['gpu']['devices']} mode={cfg['gpu']['mode']} "
                f"tts={cfg['tts']['engine']} 写操作={'需口令' if env.get('ADMIN_TOKEN') else '仅本机'} "
                f"->  http://127.0.0.1:{cfg['server']['port']}/")
    uvicorn.run(create_app(settings=settings, env=env), host=cfg["server"]["host"], port=cfg["server"]["port"],
                log_level="info")


if __name__ == "__main__":
    main()
