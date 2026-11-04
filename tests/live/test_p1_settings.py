"""P1：设置中心（三层合并、校验、持久化、版本回滚、热更新通知）与 .env。"""
import json
import os

import yaml

from live.config import DEFAULTS, load_config
from live.settings import schema as S
from live.settings.env import Env
from live.settings.store import SettingsCenter


def _center(tmp_path, yaml_data=None, cli=None):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    y = tmp_path / "c.yaml"
    y.write_text(yaml.safe_dump({"server": {"data_dir": str(data_dir)}, **(yaml_data or {})}), encoding="utf-8")
    return SettingsCenter.from_files(str(y), cli), str(y), str(data_dir)


def test_defaults_generated_from_schema():
    assert DEFAULTS == S.defaults_tree()
    for f in S.FIELDS:
        assert f.label and f.group in S.GROUP_IDS
        S.validate(f, f.default)                    # 默认值本身必须合法


def test_three_layer_merge_order(tmp_path):
    sc, ypath, data_dir = _center(tmp_path, {"speech": {"fade_ms": 30}, "stream": {"jpeg_quality": 70}})
    assert sc.source("speech.fade_ms") == "yaml" and sc.get("speech.fade_ms") == 30
    assert sc.source("speech.max_gain_db") == "default"
    assert sc.update({"speech.fade_ms": 40})["ok"]
    assert sc.source("speech.fade_ms") == "runtime" and sc.get("speech.fade_ms") == 40
    # 重启：运行时覆盖仍然生效，且优先于 yaml
    sc2 = SettingsCenter.from_files(ypath)
    assert sc2.get("speech.fade_ms") == 40 and sc2.get("stream.jpeg_quality") == 70
    # 启动参数优先于运行时覆盖，且 Web 上不能改
    sc3 = SettingsCenter.from_files(ypath, {"speech": {"fade_ms": 5}})
    assert sc3.get("speech.fade_ms") == 5 and sc3.source("speech.fade_ms") == "cli"
    assert not sc3.update({"speech.fade_ms": 6})["ok"]


def test_validation_errors_are_per_field_and_atomic(tmp_path):
    sc, _, _ = _center(tmp_path)
    r = sc.update({"speech.fade_ms": 10, "stream.jpeg_quality": 500, "idle.mode": "xx", "nope": 1,
                   "server.data_dir": "/tmp"})
    assert not r["ok"]
    assert set(r["errors"]) == {"stream.jpeg_quality", "idle.mode", "nope", "server.data_dir"}
    assert "不能大于" in r["errors"]["stream.jpeg_quality"]
    assert sc.get("speech.fade_ms") == DEFAULTS["speech"]["fade_ms"]   # 整批不生效
    assert not os.path.exists(sc.runtime_path)


def test_type_coercion():
    f = S.get_field("speech.fade_ms")
    assert S.validate(f, "20") == 20
    for bad in ("1.5", True, [1]):
        try:
            S.validate(f, bad)
            raise AssertionError(bad)
        except S.ValidationError:
            pass
    assert S.validate(S.get_field("models.compile"), True) == "true"
    assert S.validate(S.get_field("gpu.devices"), ["0", 1]) == [0, 1]
    assert S.validate(S.get_field("speech.target_rms_db"), None) is None
    assert S.validate(S.get_field("live.use_face_crop"), "true") is True


def test_cross_field_check(tmp_path):
    sc, _, _ = _center(tmp_path)
    r = sc.update({"idle.mode": "file"})
    assert not r["ok"] and "idle.file" in r["errors"]


def test_atomic_persist_and_history_rollback(tmp_path):
    sc, _, _ = _center(tmp_path)
    for v in (10, 20, 30):
        assert sc.update({"speech.fade_ms": v})["ok"]
    with open(sc.runtime_path, encoding="utf-8") as f:
        assert json.load(f)["overrides"]["speech.fade_ms"] == 30
    assert not os.path.exists(sc.runtime_path + ".tmp")
    hist = sc.history()
    assert [h["version"] for h in hist] == [3, 2, 1] and hist[0]["current"]
    r = sc.rollback(1)
    assert r["ok"] and sc.get("speech.fade_ms") == 10
    assert sc.rollback(999)["ok"] is False


def test_history_keeps_last_n(tmp_path):
    sc, _, _ = _center(tmp_path)
    sc.keep_history = 5
    for v in range(12):
        sc.update({"speech.fade_ms": v})
    assert len(sc.history()) == 5 and sc.history()[0]["version"] == 12


def test_changed_event_only_carries_changed_hot_keys(tmp_path):
    sc, _, _ = _center(tmp_path)
    events = []
    sc.bus.subscribe("settings.changed", lambda t, p: events.append(p))
    r = sc.update({"speech.target_rms_db": -16, "gpu.devices": [0, 1], "speech.fade_ms": DEFAULTS["speech"]["fade_ms"]})
    assert r["applied"] == ["speech.target_rms_db"] and r["restart"] == ["gpu.devices"]
    sc.bus.flush()
    assert events[-1]["keys"] == ["speech.target_rms_db"]
    assert events[-1]["old"] == {"speech.target_rms_db": -20.0} and events[-1]["new"] == {"speech.target_rms_db": -16.0}
    # restart 项不改运行中的 cfg，只标 pending
    assert sc.get("gpu.devices") == DEFAULTS["gpu"]["devices"] and "gpu.devices" in sc.pending_restart()
    # 相同值再存一次：不发事件
    n = len(events)
    sc.update({"speech.target_rms_db": -16})
    sc.bus.flush()
    assert len(events) == n


def test_register_reconfigurable_prefix(tmp_path):
    sc, _, _ = _center(tmp_path)
    calls = []

    class M:
        def apply_settings(self, view, changed):
            calls.append((changed, view.get("speech.fade_ms")))

    sc.register(M(), ["speech"])
    sc.update({"stream.jpeg_quality": 60})
    sc.update({"speech.fade_ms": 33})
    sc.bus.flush()
    assert calls == [({"speech.fade_ms"}, 33)]


def test_path_fields_normalized(tmp_path):
    sc, _, _ = _center(tmp_path)
    wav = tmp_path / "idle.wav"
    wav.write_bytes(b"x")
    assert sc.update({"idle.file": "assets/idle.wav"})["ok"]
    assert os.path.isabs(sc.get("idle.file"))


def test_reset_and_export_import(tmp_path):
    sc, _, _ = _center(tmp_path)
    sc.update({"speech.fade_ms": 50, "stream.jpeg_quality": 60})
    exp = sc.export()
    assert exp["overrides"] == {"speech.fade_ms": 50, "stream.jpeg_quality": 60}
    assert sc.reset(["speech.fade_ms"])["ok"] and sc.get("speech.fade_ms") == DEFAULTS["speech"]["fade_ms"]
    assert sc.get("stream.jpeg_quality") == 60
    assert sc.reset()["ok"] and sc.get("stream.jpeg_quality") == DEFAULTS["stream"]["jpeg_quality"]
    assert sc.import_(exp["overrides"])["ok"] and sc.get("speech.fade_ms") == 50
    assert not sc.import_({"speech.fade_ms": "x"})["ok"]


def test_from_cfg_compat(tmp_path):
    cfg = load_config(None, {"server": {"data_dir": str(tmp_path)}, "stream": {"jpeg_quality": 66}})
    sc = SettingsCenter.from_cfg(cfg)
    assert sc.source("stream.jpeg_quality") == "yaml" and sc.cfg is cfg
    sc.update({"stream.jpeg_quality": 77})
    assert cfg["stream"]["jpeg_quality"] == 77           # 原地改共享 cfg


def test_describe_shape(tmp_path):
    sc, _, _ = _center(tmp_path)
    d = sc.describe()
    keys = {f["key"] for f in d["fields"]}
    assert "speech.target_rms_db" in keys and len(d["groups"]) == len(S.GROUPS)
    f = next(x for x in d["fields"] if x["key"] == "gpu.devices")
    assert f["apply"] == "restart" and f["label"]


# ---------------------------------------------------------------- .env
def test_env_load_reload_and_status(tmp_path):
    from live.core.bus import EventBus
    p = tmp_path / ".env"
    p.write_text("LLM_API_KEY=sk-123456\nLLM_MODEL=m1\n", encoding="utf-8")
    bus = EventBus()
    got = []
    bus.subscribe("env.changed", lambda t, pl: got.append(pl))
    env = Env(str(p), bus=bus)
    assert env.get("LLM_API_KEY") == "sk-123456"
    st = {s["key"]: s for s in env.status()}
    assert st["LLM_API_KEY"]["configured"] and "value" not in st["LLM_API_KEY"]   # 密钥不回显
    assert st["LLM_MODEL"]["value"] == "m1" and not st["VOLC_TTS_API_KEY"]["configured"]
    p.write_text("LLM_API_KEY=sk-999\nLLM_MODEL=m1\n", encoding="utf-8")
    assert env.reload() == ["LLM_API_KEY"]
    bus.flush()
    assert got[-1] == {"keys": ["LLM_API_KEY"]}
    assert "sk-999" not in json.dumps(env.status())


def test_env_process_environment_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "from-env")
    env = Env(str(tmp_path / "missing.env"))
    assert env.get("LLM_MODEL") == "from-env" and env.source("LLM_MODEL") == "env"
