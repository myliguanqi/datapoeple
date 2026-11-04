"""P1：设置接口、写接口鉴权、控制台 WebSocket、TTS 热切换（不启动 worker 进程）。"""
import json

import pytest


def test_settings_get_put_and_hot_apply(make_client):
    c, ctx = make_client()
    d = c.get("/api/settings").json()
    assert any(f["key"] == "speech.target_rms_db" for f in d["fields"])
    r = c.put("/api/settings", json={"changes": {"speech.target_rms_db": -16, "gpu.devices": [0]}})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == ["speech.target_rms_db"] and r.json()["restart"] == ["gpu.devices"]
    assert ctx.cfg["speech"]["target_rms_db"] == -16
    d = c.get("/api/settings").json()
    f = next(x for x in d["fields"] if x["key"] == "gpu.devices")
    assert f["pending_restart"] and f["value"] == [0] and f["running"] == [0, 1]


def test_settings_validation_400(make_client):
    c, _ = make_client()
    r = c.put("/api/settings", json={"changes": {"stream.jpeg_quality": 5}})
    assert r.status_code == 400 and "stream.jpeg_quality" in r.json()["errors"]


def test_settings_history_rollback_export_import_reset(make_client):
    c, ctx = make_client()
    c.put("/api/settings", json={"changes": {"speech.fade_ms": 11}})
    c.put("/api/settings", json={"changes": {"speech.fade_ms": 22}})
    hist = c.get("/api/settings/history").json()
    assert [h["version"] for h in hist][:2] == [2, 1]
    assert c.post("/api/settings/rollback/1").status_code == 200 and ctx.cfg["speech"]["fade_ms"] == 11
    assert c.post("/api/settings/rollback/99").status_code == 404
    exp = c.get("/api/settings/export").json()
    assert exp["overrides"] == {"speech.fade_ms": 11}
    assert c.post("/api/settings/reset", json={}).status_code == 200 and ctx.cfg["speech"]["fade_ms"] == 15
    assert c.post("/api/settings/import", json={"overrides": exp["overrides"]}).status_code == 200
    assert ctx.cfg["speech"]["fade_ms"] == 11


def test_secrets_status_and_reload_env(make_client, env_file):
    c, ctx = make_client(env_text="LLM_API_KEY=sk-abcdef\n")
    st = {s["key"]: s for s in c.get("/api/settings/secrets-status").json()}
    assert st["LLM_API_KEY"]["configured"] and "value" not in st["LLM_API_KEY"]
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("LLM_API_KEY=sk-zzz\nVOLC_TTS_API_KEY=v\n")
    r = c.post("/api/settings/reload-env").json()
    assert set(r["changed"]) == {"LLM_API_KEY", "VOLC_TTS_API_KEY"}
    assert "sk-zzz" not in json.dumps(r)


# ---------------------------------------------------------------- 鉴权
def test_no_token_local_can_write_remote_forbidden(make_client):
    c, _ = make_client(host="127.0.0.1")
    assert c.put("/api/settings", json={"changes": {"speech.fade_ms": 12}}).status_code == 200
    remote, _ = make_client(host="192.168.1.20")
    assert remote.get("/api/settings").status_code == 200                       # 读不受限
    r = remote.put("/api/settings", json={"changes": {"speech.fade_ms": 13}})
    assert r.status_code == 403
    assert remote.post("/api/sessions/default/speak", json={"text": "x"}).status_code == 403


def test_token_required_when_set(make_client):
    c, _ = make_client(host="127.0.0.1", env_text="ADMIN_TOKEN=secret-token\n")
    assert c.put("/api/settings", json={"changes": {"speech.fade_ms": 12}}).status_code == 401
    assert c.put("/api/settings", json={"changes": {"speech.fade_ms": 12}},
                 headers={"X-Admin-Token": "wrong"}).status_code == 401
    ok = c.put("/api/settings", json={"changes": {"speech.fade_ms": 12}}, headers={"X-Admin-Token": "secret-token"})
    assert ok.status_code == 200
    remote, _ = make_client(host="10.0.0.5", env_text="ADMIN_TOKEN=secret-token\n")
    assert remote.put("/api/settings", json={"changes": {"speech.fade_ms": 14}},
                      headers={"X-Admin-Token": "secret-token"}).status_code == 200
    assert c.get("/api/system").json()["admin"]["token_set"] is True


# ---------------------------------------------------------------- 控制台 WS + TTS 热切换
def test_console_ws_receives_settings_changed(make_client):
    c, ctx = make_client()
    with c.websocket_connect("/ws/console") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["topic"] == "hello" and "recent" in hello["data"]
        c.put("/api/settings", json={"changes": {"speech.fade_ms": 21}})
        msg = json.loads(ws.receive_text())
        assert msg["topic"] == "settings.changed" and msg["data"]["keys"] == ["speech.fade_ms"]


def test_tts_engine_hot_switch(make_client):
    c, ctx = make_client()
    assert ctx.tts.name == "mock"
    r = c.put("/api/settings", json={"changes": {"tts.engine": "edge"}})
    assert r.status_code == 200
    ctx.bus.flush()
    assert ctx.tts.name == "edge"
    assert c.get("/api/system").json()["services"]["tts"]["engine"] == "edge"
    c.put("/api/settings", json={"changes": {"tts.engine": "mock"}})
    ctx.bus.flush()
    assert ctx.tts.name == "mock"
    assert c.get("/api/tts/voices").json()["engine"] == "mock"


def test_tts_voice_hot(make_client):
    c, ctx = make_client()
    c.put("/api/settings", json={"changes": {"tts.voice": "zh-CN-YunxiNeural"}})
    assert ctx.tts.engine.default_voice == "zh-CN-YunxiNeural"


@pytest.mark.parametrize("path", ["/", "/web/app.js", "/web/style.css", "/web/player.js"])
def test_static_pages(make_client, path):
    c, _ = make_client()
    assert c.get(path).status_code == 200
