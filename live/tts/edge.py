"""edge-tts：免费在线 TTS（需要能访问微软服务），不占显卡。"""
from __future__ import annotations

import asyncio

import numpy as np

from live.audio_utils import decode_audio_bytes
from live.tts.base import TTSEngine

# 网络不通时前端下拉框的兜底列表
FALLBACK_VOICES = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural", "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural", "zh-CN-YunxiaNeural", "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural", "zh-HK-HiuMaanNeural", "zh-TW-HsiaoChenNeural",
    "en-US-JennyNeural", "en-US-GuyNeural",
]


class EdgeTTS(TTSEngine):
    name = "edge"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import edge_tts  # noqa: F401  提前报缺依赖
        opts = cfg["tts"].get("edge") or {}
        self.rate = opts.get("rate", "+0%")
        self.volume = opts.get("volume", "+0%")
        self.pitch = opts.get("pitch", "+0Hz")
        self._voices = None

    @property
    def cache_signature(self) -> str:
        return f"edge|{self.rate}|{self.volume}|{self.pitch}"

    def _rate_for(self, speed: float) -> str:
        if abs(float(speed) - 1.0) < 1e-3:
            return self.rate
        return f"{int(round((float(speed) - 1.0) * 100)):+d}%"   # 指定了语速就按语速换算（覆盖 tts.edge.rate）

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0,
                   emotion: str | None = None) -> np.ndarray:
        import edge_tts

        async def _run() -> bytes:
            comm = edge_tts.Communicate(text, voice or self.default_voice,
                                        rate=self._rate_for(speed), volume=self.volume, pitch=self.pitch)
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        mp3 = asyncio.run(_run())
        if not mp3:
            return np.zeros(0, dtype=np.float32)
        return decode_audio_bytes(mp3, sr=self.sr, ffmpeg=self.ffmpeg)

    def voices(self) -> list:
        if self._voices is None:
            try:
                import edge_tts
                items = asyncio.run(edge_tts.list_voices())
                self._voices = [
                    {"id": v["ShortName"], "name": f'{v["ShortName"]} ({v.get("Gender", "")})'}
                    for v in items if v["Locale"].startswith(("zh-", "en-US"))
                ]
            except Exception:
                return [{"id": v, "name": v} for v in FALLBACK_VOICES]
        return self._voices
