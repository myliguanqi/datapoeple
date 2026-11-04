"""P3：火山 TTS（假 SSE 响应，不访问真实接口）、语速映射、重采样、回退链、每日字数上限、合成缓存。"""
import os

import numpy as np
import pytest

from live.core.usage import UsageMeter
from live.settings.env import Env
from live.tts import create_tts
from live.tts.service import TTSAllFailed, TTSService
from live.tts.volc import VolcTTS, VolcTTSError, read_sse_pcm, speech_rate

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "volc_sse")


def lines(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read().split(b"\n")


class FakeResp:
    def __init__(self, status=200, body_lines=(), text=""):
        self.status_code = status
        self.headers = {"X-Tt-Logid": "logid-123"}
        self._lines = list(body_lines)
        self.text = text

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, stream=False):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def env_with(tmp_path):
    def make(text):
        p = tmp_path / "volc.env"
        p.write_text(text, encoding="utf-8")
        return Env(str(p))
    return make


def test_speech_rate_mapping_bounds():
    assert speech_rate(1.0) == 0 and speech_rate(1.2) == 20 and speech_rate(0.8) == -20
    assert speech_rate(0.5) == -50 and speech_rate(0.1) == -50          # 夹到 speed_min
    assert speech_rate(2.0) == 100 and speech_rate(3.0) == 100


def test_read_sse_variants():
    assert len(read_sse_pcm(lines("normal.txt"))) == 2 * 1600 * 2
    assert len(read_sse_pcm(lines("multi.txt"))) == 6 * 1600 * 2
    assert len(read_sse_pcm(lines("error.txt"))) == 1600 * 2               # 错误码的事件跳过
    assert read_sse_pcm(lines("empty.txt")) == b""
    assert len(read_sse_pcm(lines("nonjson.txt"))) == 1600 * 2             # 非 JSON 行忽略，audio 字段也认


def test_headers_new_and_old_console(mock_cfg, env_with):
    t = VolcTTS(mock_cfg, env_with("VOLC_TTS_API_KEY=k1\nVOLC_VOICE_TYPE=S_abc\n"))
    h = t.headers()
    assert h["X-Api-Key"] == "k1" and h["X-Api-Resource-Id"] == "seed-icl-2.0" and h["X-Api-Request-Id"]
    assert "X-Api-App-Key" not in h
    old = VolcTTS(mock_cfg, env_with("VOLC_APP_ID=app\nVOLC_ACCESS_TOKEN=tok\nVOLC_VOICE_TYPE=S_abc\nVOLC_RESOURCE_ID=seed-icl-1.0\n"))
    h2 = old.headers()
    assert h2["X-Api-App-Key"] == "app" and h2["X-Api-Access-Key"] == "tok" and "X-Api-Key" not in h2
    assert h2["X-Api-Resource-Id"] == "seed-icl-1.0"
    mock_cfg["tts"]["volc"]["resource_id"] = "seed-icl-1.0-concurr"     # Web 设置覆盖 .env
    assert old.headers()["X-Api-Resource-Id"] == "seed-icl-1.0-concurr"


def test_synthesize_16k_payload_and_voice(mock_cfg, env_with):
    sess = FakeSession(FakeResp(body_lines=lines("normal.txt")))
    t = VolcTTS(mock_cfg, env_with("VOLC_TTS_API_KEY=k\nVOLC_VOICE_TYPE=S_abc\n"), session=sess)
    pcm = t.synthesize("大家好", speed=1.2)
    assert pcm.dtype == np.float32 and pcm.size == 3200 and 0.2 < np.abs(pcm).max() < 0.35
    body = sess.calls[0]["json"]
    assert body["req_params"]["speaker"] == "S_abc" and body["req_params"]["text"] == "大家好"
    assert body["req_params"]["audio_params"] == {"format": "pcm", "sample_rate": 16000, "speech_rate": 20}
    t.synthesize("换个音色", voice="S_other")
    assert sess.calls[1]["json"]["req_params"]["speaker"] == "S_other"
    mock_cfg["tts"]["volc"]["voice_type"] = "S_web"                     # Web 覆盖 .env 的音色
    t.synthesize("再来")
    assert sess.calls[2]["json"]["req_params"]["speaker"] == "S_web"


def test_synthesize_24k_resampled_to_16k(mock_cfg, env_with):
    mock_cfg["tts"]["volc"]["sample_rate"] = 24000
    t = VolcTTS(mock_cfg, env_with("VOLC_TTS_API_KEY=k\nVOLC_VOICE_TYPE=S_abc\n"),
                session=FakeSession(FakeResp(body_lines=lines("multi.txt"))))
    pcm = t.synthesize("测试")
    assert pcm.size == round(6 * 1600 * 16000 / 24000)                 # 9600 个 24k 采样 → 6400 个 16k 采样


def test_errors(mock_cfg, env_with):
    env = env_with("VOLC_TTS_API_KEY=k\nVOLC_VOICE_TYPE=S_abc\n")
    with pytest.raises(VolcTTSError, match="HTTP 401"):
        VolcTTS(mock_cfg, env, session=FakeSession(FakeResp(401, text="unauthorized"))).synthesize("x")
    with pytest.raises(VolcTTSError, match="没有返回音频"):
        VolcTTS(mock_cfg, env, session=FakeSession(FakeResp(body_lines=lines("empty.txt")))).synthesize("x")
    no = VolcTTS(mock_cfg, env_with(""))
    assert not no.available and "VOLC_TTS_API_KEY" in no.unavailable_reason
    with pytest.raises(VolcTTSError):
        no.synthesize("x")
    no_voice = VolcTTS(mock_cfg, env_with("VOLC_TTS_API_KEY=k\n"))
    assert not no_voice.available and "音色" in no_voice.unavailable_reason


# ---------------------------------------------------------------- TTSService
def _service(cfg, env, session=None, usage=None):
    def factory(c, e, engine=None):
        if engine == "volc":
            return VolcTTS(c, e, session=session)
        return create_tts(c, e, engine=engine)
    return TTSService(cfg, env, usage, factory=factory)


def test_fallback_when_volc_not_configured(mock_cfg, env_with):
    mock_cfg["tts"].update(engine="volc", fallback=["mock"])
    svc = _service(mock_cfg, env_with(""))
    st = svc.status()
    assert st["requested"] == "volc" and st["engine"] == "mock" and "未配置" in st["error"]
    assert svc.synthesize("你好").size > 0


def test_env_reload_switches_to_volc(mock_cfg, tmp_path):
    p = tmp_path / ".env"
    p.write_text("", encoding="utf-8")
    env = Env(str(p))
    mock_cfg["tts"].update(engine="volc", fallback=["mock"])
    svc = _service(mock_cfg, env, FakeSession(FakeResp(body_lines=lines("normal.txt"))))
    assert svc.name == "mock"
    p.write_text("VOLC_TTS_API_KEY=k\nVOLC_VOICE_TYPE=S_abc\n", encoding="utf-8")
    env.reload()
    svc.on_env_changed(payload={"keys": ["VOLC_TTS_API_KEY", "VOLC_VOICE_TYPE"]})
    assert svc.name == "volc" and svc.status()["error"] is None


def test_runtime_fallback_per_sentence(mock_cfg, env_with):
    mock_cfg["tts"].update(engine="volc", fallback=["mock"], runtime_fallback=True)
    mock_cfg["tts"]["cache"]["enabled"] = False
    sess = FakeSession(FakeResp(500, text="server error"))
    svc = _service(mock_cfg, env_with("VOLC_TTS_API_KEY=k\nVOLC_VOICE_TYPE=S_abc\n"), sess)
    assert svc.name == "volc"
    pcm = svc.synthesize("这一句火山失败了")
    assert pcm.size > 0 and svc.stats["fallbacks"] == 1 and svc.stats["failures"] == 1
    assert svc.stats["last_engine"] == "mock" and "500" in svc.stats["last_error"]
    mock_cfg["tts"]["runtime_fallback"] = False
    with pytest.raises(TTSAllFailed):
        svc.synthesize("不回退就跳过")


def test_daily_char_limit(mock_cfg, env_with, tmp_path):
    mock_cfg["tts"].update(engine="volc", fallback=["mock"], daily_max_chars=10)
    mock_cfg["tts"]["cache"]["enabled"] = False
    sess = FakeSession(FakeResp(body_lines=lines("normal.txt")))
    usage = UsageMeter(str(tmp_path / "usage.json"))
    svc = _service(mock_cfg, env_with("VOLC_TTS_API_KEY=k\nVOLC_VOICE_TYPE=S_abc\n"), sess, usage)
    svc.synthesize("一二三四五六七八")                                   # 8 字，走火山
    assert len(sess.calls) == 1 and usage.today("tts_chars:volc") == 8
    svc.synthesize("九十十一十二")                                       # 再 6 字超上限 → 回退
    assert len(sess.calls) == 1 and svc.stats["last_engine"] == "mock" and "上限" in svc.warning
    usage.flush()
    assert UsageMeter(str(tmp_path / "usage.json")).today("tts_chars:volc") == 8   # 落盘，重启不清零


def test_cache_hit_invalidation_and_persist(mock_cfg, env_with):
    mock_cfg["tts"].update(engine="volc", fallback=["mock"])
    env = env_with("VOLC_TTS_API_KEY=k\nVOLC_VOICE_TYPE=S_abc\n")
    sess = FakeSession(FakeResp(body_lines=lines("normal.txt")))
    svc = _service(mock_cfg, env, sess)
    a = svc.synthesize("欢迎来到直播间", speed=1.0)
    b = svc.synthesize("欢迎来到直播间", speed=1.04)                     # 同一语速档 → 命中
    assert len(sess.calls) == 1 and np.allclose(a, b, atol=1e-4) and svc.cache.hits == 1
    svc.synthesize("欢迎来到直播间", speed=1.3)                          # 语速档不同 → 重新合成
    assert len(sess.calls) == 2
    mock_cfg["tts"]["volc"]["voice_type"] = "S_new"                     # 换音色 → 键变
    svc.synthesize("欢迎来到直播间")
    assert len(sess.calls) == 3
    svc2 = _service(mock_cfg, env, FakeSession(FakeResp(500)))          # 新实例（模拟重启）：磁盘缓存命中
    assert svc2.synthesize("欢迎来到直播间").size == a.size and svc2.cache.hits == 1


def test_failures_not_cached(mock_cfg, env_with):
    mock_cfg["tts"].update(engine="volc", fallback=["mock"], runtime_fallback=False)
    env = env_with("VOLC_TTS_API_KEY=k\nVOLC_VOICE_TYPE=S_abc\n")
    sess = FakeSession(FakeResp(500), FakeResp(body_lines=lines("normal.txt")))
    svc = _service(mock_cfg, env, sess)
    with pytest.raises(TTSAllFailed):
        svc.synthesize("先失败")
    assert svc.synthesize("先失败").size == 3200 and len(sess.calls) == 2


def test_engine_switch_via_settings(make_client):
    c, ctx = make_client()
    assert ctx.tts.name == "mock"
    c.put("/api/settings", json={"changes": {"tts.engine": "volc", "tts.fallback": ["mock"]}})
    ctx.bus.flush()
    st = c.get("/api/system").json()["services"]["tts"]
    assert st["requested"] == "volc" and st["engine"] == "mock" and st["error"]
