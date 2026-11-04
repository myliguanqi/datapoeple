"""P3：OpenAI 兼容 LLM（假 HTTP，不访问真实接口）、流式按句、超时回退、无 key、.env 重载、每日上限、MockLLM。"""
import json

import pytest

from live.core.usage import UsageMeter
from live.llm.base import LLMError, LLMUnavailable, extract_json
from live.llm.mock import MockLLM
from live.llm.openai_compat import OpenAICompatLLM, normalize_url
from live.llm.service import LLMService
from live.llm.text import iter_sentences
from live.settings.env import Env


class Resp:
    def __init__(self, status=200, payload=None, lines=None, text=""):
        self.status_code = status
        self._payload = payload
        self._lines = lines or []
        self.text = text or json.dumps(payload or {})
        self.closed = False

    def json(self):
        return self._payload

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class Sess:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, stream=False):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout, "stream": stream})
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def env_of(tmp_path, text):
    p = tmp_path / "llm.env"
    p.write_text(text, encoding="utf-8")
    return Env(str(p))


GOOD_ENV = "LLM_API_URL=https://api.example.com/v1\nLLM_API_KEY=sk-test\nLLM_MODEL=m1\nLLM_TIMEOUT=7\n"


def sse(*pieces, reasoning=False):
    out = []
    if reasoning:
        out.append(b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}')
    for p in pieces:
        out.append(("data: " + json.dumps({"choices": [{"delta": {"content": p}}]}, ensure_ascii=False)).encode())
        out.append(b"")
    out.append(b"data: [DONE]")
    return out


def test_normalize_url():
    assert normalize_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1/chat/completions"
    assert normalize_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1/chat/completions"
    assert normalize_url("https://x/api/v3/chat/completions") == "https://x/api/v3/chat/completions"
    assert normalize_url("") == ""


def test_non_stream_request_shape(mock_cfg, tmp_path):
    sess = Sess(Resp(payload={"choices": [{"message": {"content": "你好呀"}}]}))
    llm = OpenAICompatLLM(mock_cfg, env_of(tmp_path, GOOD_ENV), session=sess)
    assert llm.chat([{"role": "user", "content": "hi"}], json_mode=True) == "你好呀"
    c = sess.calls[0]
    assert c["url"] == "https://api.example.com/v1/chat/completions"
    assert c["headers"]["Authorization"] == "Bearer sk-test" and c["json"]["model"] == "m1"
    assert c["json"]["temperature"] == mock_cfg["llm"]["temperature"] and c["json"]["max_tokens"] == mock_cfg["llm"]["max_tokens"]
    assert c["json"]["response_format"] == {"type": "json_object"} and c["timeout"] == (7.0, 7.0)


def test_stream_pieces_and_sentences(mock_cfg, tmp_path):
    sess = Sess(Resp(lines=sse("感谢小王", "大哥的玫瑰！", "这款杯子", "保温十二小时。", "喜欢的", "点小黄车")))
    llm = OpenAICompatLLM(mock_cfg, env_of(tmp_path, GOOD_ENV), session=sess)
    pieces = list(llm.chat([{"role": "user", "content": "x"}], stream=True))
    assert "".join(pieces) == "感谢小王大哥的玫瑰！这款杯子保温十二小时。喜欢的点小黄车"
    sess2 = Sess(Resp(lines=sse("感谢小王", "大哥的玫瑰！", "这款杯子", "保温十二小时。", "喜欢的", "点小黄车", reasoning=True)))
    llm2 = OpenAICompatLLM(mock_cfg, env_of(tmp_path, GOOD_ENV), session=sess2)
    sents = list(iter_sentences(llm2.chat([{"role": "user", "content": "x"}], stream=True)))
    assert sents == ["感谢小王大哥的玫瑰！", "这款杯子保温十二小时。", "喜欢的点小黄车"]   # 推理内容不进正文


def test_iter_sentences_edge_cases():
    assert list(iter_sentences(["好！", "我们继续讲。"])) == ["好！我们继续讲。"]             # 太短的并到下一句
    long = "这是一个没有句号但是特别特别长的句子，" * 3
    out = list(iter_sentences([long], max_chars=20))
    assert all(len(s) <= 25 for s in out) and "".join(out) == long.strip()
    assert list(iter_sentences(["真的吗？！", "太好了"])) == ["真的吗？！", "太好了"]


def test_errors_and_empty(mock_cfg, tmp_path):
    import requests
    env = env_of(tmp_path, GOOD_ENV)
    with pytest.raises(LLMError, match="HTTP 429"):
        OpenAICompatLLM(mock_cfg, env, Sess(Resp(429, text="rate limited"))).chat([])
    with pytest.raises(LLMError, match="Timeout"):
        OpenAICompatLLM(mock_cfg, env, Sess(requests.Timeout("read timed out"))).chat([])
    with pytest.raises(LLMError, match="空内容"):
        OpenAICompatLLM(mock_cfg, env, Sess(Resp(payload={"choices": [{"message": {"content": ""}}]}))).chat([])
    with pytest.raises(LLMError, match="空内容"):
        list(OpenAICompatLLM(mock_cfg, env, Sess(Resp(lines=sse(reasoning=True)))).chat([], stream=True))


def test_no_key_unavailable(mock_cfg, tmp_path):
    svc = LLMService(mock_cfg, env_of(tmp_path, "LLM_API_URL=https://x/v1\n"))
    assert not svc.available and "LLM_API_KEY" in svc.reason and "LLM_MODEL" in svc.reason
    with pytest.raises(LLMUnavailable):
        svc.chat([{"role": "user", "content": "hi"}])
    assert svc.status()["available"] is False


def test_env_reload_makes_available(mock_cfg, tmp_path):
    p = tmp_path / ".env"
    p.write_text("", encoding="utf-8")
    env = Env(str(p))
    svc = LLMService(mock_cfg, env)
    assert not svc.available
    p.write_text(GOOD_ENV, encoding="utf-8")
    env.reload()
    svc.on_env_changed(payload={"keys": ["LLM_API_URL", "LLM_API_KEY", "LLM_MODEL"]})
    assert svc.available and svc.status()["model"] == "m1"


def test_service_stats_stream_and_failures(mock_cfg, tmp_path):
    usage = UsageMeter(str(tmp_path / "u.json"))
    mock = MockLLM(rules=[("多少钱", "到手价九十九元。详情看小黄车！")])
    svc = LLMService(mock_cfg, env_of(tmp_path, ""), usage, client=mock)
    out = "".join(svc.chat([{"role": "user", "content": "这个多少钱"}], stream=True))
    assert out == "到手价九十九元。详情看小黄车！" and svc.stats["last_ttft_ms"] is not None
    assert mock.calls[-1]["messages"][-1]["content"] == "这个多少钱" and usage.today("llm_calls") == 1
    mock.fail = "boom"
    with pytest.raises(LLMError):
        svc.chat([{"role": "user", "content": "x"}], stream=False)
    assert svc.stats["failures"] == 1 and svc.stats["last_error"] == "boom"


def test_timeout_via_mock(mock_cfg, tmp_path):
    svc = LLMService(mock_cfg, env_of(tmp_path, ""), client=MockLLM(delay_s=2.0))
    with pytest.raises(LLMError, match="超时"):
        svc.chat([{"role": "user", "content": "x"}], stream=False, timeout=0.05)


def test_daily_call_limit(mock_cfg, tmp_path):
    mock_cfg["llm"]["daily_max_calls"] = 2
    svc = LLMService(mock_cfg, env_of(tmp_path, ""), UsageMeter(None), client=MockLLM(default="好"))
    svc.chat([], stream=False)
    svc.chat([], stream=False)
    assert not svc.available and "上限" in svc.reason
    with pytest.raises(LLMUnavailable):
        svc.chat([], stream=False)


def test_history_note_spoken(mock_cfg, tmp_path):
    mock_cfg["llm"]["history_size"] = 3
    svc = LLMService(mock_cfg, env_of(tmp_path, ""), client=MockLLM())
    for t in ["a", "a", "b", "c", "d"]:
        svc.note_spoken(t)
    assert svc.recent_lines() == ["b", "c", "d"]


def test_extract_json():
    assert extract_json('```json\n{"text": "你好"}\n```') == {"text": "你好"}
    assert extract_json('好的：{"text": "x", "speed": 1.1} 以上') == {"text": "x", "speed": 1.1}
    assert extract_json("不是json") is None


def test_provider_switch_and_status_api(make_client):
    c, ctx = make_client()
    st = c.get("/api/system").json()["services"]["llm"]
    assert st["provider"] == "openai" and st["available"] is False
    c.put("/api/settings", json={"changes": {"llm.provider": "mock"}})
    ctx.bus.flush()
    st = c.get("/api/system").json()["services"]["llm"]
    assert st["provider"] == "mock" and st["available"] is True
