"""pytest 公共夹具。运行：python -m pytest tests/live -q -m "not e2e"

原有的 test_e2e_mock.py / test_runner_logic.py 以及 test_e2e_*.py 是独立脚本（导入即运行、需要先起服务或 torch），
不让 pytest 收集，按 docs/live/DEVELOPMENT.md 里的命令单独跑。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

collect_ignore = ["test_e2e_mock.py", "test_runner_logic.py"]
collect_ignore_glob = ["test_e2e_*.py"]


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    """测试绝不调用计费接口：清掉进程环境里可能存在的真实密钥（.env 也只用 tmp 文件）。"""
    from live.settings.env import ENV_KEYS
    for key, _, _ in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "live_data"
    d.mkdir()
    return str(d)


@pytest.fixture
def mock_cfg(data_dir):
    from live.config import load_config
    return load_config(os.path.join(ROOT, "live", "configs", "mock.yaml"), {"server": {"data_dir": data_dir}})


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("", encoding="utf-8")
    return str(p)


@pytest.fixture
def make_client(mock_cfg, env_file):
    """make_client(host="127.0.0.1", start_workers=False) -> (TestClient, ctx)；不启动 worker 进程。"""
    from fastapi.testclient import TestClient
    from live.server import create_app
    from live.settings.env import Env

    clients = []

    def _make(host: str = "127.0.0.1", start_workers: bool = False, env_text: str | None = None):
        if env_text is not None:
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(env_text)
        app = create_app(mock_cfg, env=Env(env_file), start_workers=start_workers)
        c = TestClient(app, client=(host, 50000))
        c.__enter__()
        clients.append(c)
        return c, app.state.ctx

    yield _make
    for c in clients:
        c.__exit__(None, None, None)
