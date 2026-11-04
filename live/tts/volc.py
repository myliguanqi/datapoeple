"""火山引擎 大模型语音合成 V3 —— 声音复刻（HTTP SSE 单向流式）。照抄参考项目 douyin_zhibo/expression/tts_client.py 的核心：
鉴权头、speed → speech_rate 映射、SSE 解析；适配本项目 TTSEngine 接口（返回 float32 16k）与 .env。

鉴权（.env，见 .env.example）：
  新控制台  VOLC_TTS_API_KEY            → 请求头 X-Api-Key
  旧控制台  VOLC_APP_ID + VOLC_ACCESS_TOKEN → X-Api-App-Key + X-Api-Access-Key
  另有 X-Api-Resource-Id（seed-icl-2.0 等，tts.volc.resource_id 或 .env VOLC_RESOURCE_ID）、X-Api-Request-Id（uuid）
音色：tts.volc.voice_type 或 .env VOLC_VOICE_TYPE（声音复刻 S_xxx）。V3 声音复刻不下发 emotion（情绪由音色定调）。

⚠ 一旦合成即计费。测试一律用假响应；只保留手动自测（需要你同意再跑）：
    python -m live.tts.volc "测试一句"
它会打印 HTTP 状态、X-Tt-Logid、前几条原始 SSE 事件，并把音频写到 live_data/tts_selftest.wav。
"""
from __future__ import annotations

import base64
import json
import uuid

import numpy as np
from loguru import logger

from live.audio_utils import pcm16_to_float, resample
from live.tts.base import TTSEngine

OK_CODES = (None, 0, 3000, 20000000)


class VolcTTSError(RuntimeError):
    pass


def speech_rate(speed: float, speed_min: float = 0.5, speed_max: float = 2.0) -> int:
    """speed（语速比，1.0=正常）→ V3 speech_rate（整数，0=正常，正=快），夹到 [-50, 100]。"""
    speed = max(speed_min, min(speed_max, float(speed)))
    return max(-50, min(100, int(round((speed - 1.0) * 100))))


def read_sse_pcm(lines, logid: str = "", sniff: int = 0) -> bytes:
    """解析 SSE 事件流，累积裸 PCM 字节。事件形如 `data: {json}`，音频在 data / audio / audio_data 字段（base64）；
    code 不在 OK_CODES 的事件告警跳过；非 JSON 行忽略。"""
    chunks = bytearray()
    for raw in lines:
        if not raw:
            continue
        line = raw.decode("utf-8", "ignore").strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
        if sniff > 0:
            logger.info(f"[sniff] {line[:300]}")
            sniff -= 1
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        code = obj.get("code")
        if code not in OK_CODES:
            logger.warning(f"火山 SSE 事件错误 code={code} msg={obj.get('message')} logid={logid}")
            continue
        for k in ("data", "audio", "audio_data"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                try:
                    chunks.extend(base64.b64decode(v))
                except (ValueError, TypeError):
                    pass
                break
    return bytes(chunks)


class VolcTTS(TTSEngine):
    name = "volc"
    billable = True

    def __init__(self, cfg: dict, env=None, session=None):
        super().__init__(cfg)
        import requests
        self.env = env
        self.session = session or requests.Session()
        self.sniff = 0

    # ---- 每次读配置 / .env：Web 改音色、.env 重新加载后立即生效 ----
    def _env(self, key: str) -> str:
        return (self.env.get(key) if self.env is not None else None) or ""

    @property
    def opts(self) -> dict:
        return self.cfg["tts"]["volc"]

    @property
    def speaker(self) -> str:
        return self.opts.get("voice_type") or self._env("VOLC_VOICE_TYPE")

    @property
    def resource_id(self) -> str:
        return self.opts.get("resource_id") or self._env("VOLC_RESOURCE_ID") or "seed-icl-2.0"

    @property
    def has_credentials(self) -> bool:
        return bool(self._env("VOLC_TTS_API_KEY")) or bool(self._env("VOLC_APP_ID") and self._env("VOLC_ACCESS_TOKEN"))

    @property
    def available(self) -> bool:
        return self.has_credentials and bool(self.speaker)

    @property
    def unavailable_reason(self) -> str | None:
        if not self.has_credentials:
            return "火山 TTS 未配置（.env 缺 VOLC_TTS_API_KEY）"
        if not self.speaker:
            return "火山 TTS 缺音色 ID（.env 的 VOLC_VOICE_TYPE 或设置 tts.volc.voice_type）"
        return None

    @property
    def default_voice(self) -> str:   # type: ignore[override]
        return self.speaker

    @property
    def cache_signature(self) -> str:
        return f"volc|{self.resource_id}|{self.opts.get('sample_rate')}"

    def headers(self) -> dict:
        h = {"Content-Type": "application/json", "X-Api-Resource-Id": self.resource_id,
             "X-Api-Request-Id": str(uuid.uuid4())}
        key = self._env("VOLC_TTS_API_KEY")
        if key:
            h["X-Api-Key"] = key
        else:
            h["X-Api-App-Key"] = self._env("VOLC_APP_ID")
            h["X-Api-Access-Key"] = self._env("VOLC_ACCESS_TOKEN")
        return h

    def payload(self, text: str, speaker: str, speed: float) -> dict:
        return {"user": {"uid": "flashhead_live"},
                "req_params": {"text": text, "speaker": speaker,
                               "audio_params": {"format": "pcm", "sample_rate": int(self.opts["sample_rate"]),
                                                "speech_rate": speech_rate(speed)}}}

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0, emotion: str | None = None) -> np.ndarray:
        reason = self.unavailable_reason
        if reason:
            raise VolcTTSError(reason)
        text = (text or "").strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        speaker = voice if voice and voice.startswith("S_") else self.speaker
        rate = int(self.opts["sample_rate"])
        with self.session.post(self.opts["base_url"], headers=self.headers(), json=self.payload(text, speaker, speed),
                               timeout=float(self.opts["timeout_s"]), stream=True) as r:
            logid = r.headers.get("X-Tt-Logid", "")
            if r.status_code != 200:
                raise VolcTTSError(f"火山合成 HTTP {r.status_code} logid={logid} {r.text[:300]}")
            pcm = read_sse_pcm(r.iter_lines(), logid, sniff=self.sniff)
        if not pcm:
            raise VolcTTSError(f"火山合成没有返回音频 logid={logid}")
        return resample(pcm16_to_float(pcm), rate, self.sr)

    def voices(self) -> list:
        sp = self.speaker
        return [{"id": sp, "name": f"火山声音复刻 {sp}"}] if sp else []


def _selftest() -> None:   # pragma: no cover  手动自测，会计费
    import os
    import sys

    from live.audio_utils import write_wav
    from live.config import PROJECT_ROOT, load_config
    from live.settings.env import Env

    text = sys.argv[1] if len(sys.argv) > 1 else "大家好，欢迎来到直播间！"
    cfg = load_config(os.path.join(PROJECT_ROOT, "live", "configs", "live.yaml"))
    tts = VolcTTS(cfg, Env(os.path.join(PROJECT_ROOT, ".env")))
    print(f"可用: {tts.available}  原因: {tts.unavailable_reason or '—'}  音色: {tts.speaker or '—'}  "
          f"resource_id: {tts.resource_id}  鉴权: {'X-Api-Key' if tts._env('VOLC_TTS_API_KEY') else 'App-Key'}")
    print(f"端点: {tts.opts['base_url']}  采样率: {tts.opts['sample_rate']}")
    if not tts.available:
        return
    tts.sniff = 5
    pcm = tts.synthesize(text)
    out = os.path.join(cfg["server"]["data_dir"], "tts_selftest.wav")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    write_wav(out, pcm, tts.sr)
    print(f"✓ {pcm.size / tts.sr:.2f}s 音频 → {out}")


if __name__ == "__main__":   # pragma: no cover
    _selftest()
