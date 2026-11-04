"""通用 HTTP TTS：对接 GPT-SoVITS / CosyVoice / IndexTTS 等自建服务。

只要服务"请求一句文本 -> 返回一段音频（wav/mp3/...）"就能接。配置里字符串中的
{text} 和 {voice} 会被替换，例如 GPT-SoVITS api_v2：

tts:
  engine: http
  http:
    url: http://127.0.0.1:9880/tts
    method: GET
    params: {text: "{text}", text_lang: zh, ref_audio_path: D:/ref.wav,
             prompt_text: 参考音频的文字, prompt_lang: zh, media_type: wav}
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import numpy as np

from live.audio_utils import decode_audio_bytes
from live.tts.base import TTSEngine


def _render(obj, text: str, voice: str):
    if isinstance(obj, str):
        return obj.replace("{text}", text).replace("{voice}", voice)
    if isinstance(obj, dict):
        return {k: _render(v, text, voice) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render(v, text, voice) for v in obj]
    return obj


class HttpTTS(TTSEngine):
    name = "http"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.opts = cfg["tts"]["http"]

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0,
                   emotion: str | None = None) -> np.ndarray:
        voice = voice or self.default_voice
        o = self.opts
        url = _render(o["url"], text, voice)
        params = _render(o.get("params") or {}, text, voice)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = dict(o.get("headers") or {})
        body = None
        method = (o.get("method") or "GET").upper()
        if o.get("json") is not None:
            body = json.dumps(_render(o["json"], text, voice)).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
            method = "POST"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=o.get("timeout", 60)) as resp:
            data = resp.read()
        return decode_audio_bytes(data, sr=self.sr, ffmpeg=self.ffmpeg)
