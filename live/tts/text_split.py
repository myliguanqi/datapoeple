"""长文本切句：按句末标点切，过长再按逗号切，过短的并到下一句。

一句一合成，第一句合成完就能开始播，后面的句子在播放期间继续合成，降低首包延迟。
"""
from __future__ import annotations

import re

_SENT_END = re.compile(r"(?<=[。！？!?；;…\n])")
_COMMA = re.compile(r"(?<=[，,、：:])")


def split_sentences(text: str, max_chars: int = 40, min_chars: int = 4) -> list:
    text = re.sub(r"[ \t\r]+", " ", text or "").strip()
    if not text:
        return []
    parts = []
    for sent in _SENT_END.split(text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_chars:
            parts.append(sent)
            continue
        buf = ""
        for piece in _COMMA.split(sent):
            if buf and len(buf) + len(piece) > max_chars:
                parts.append(buf)
                buf = ""
            buf += piece
            while len(buf) > max_chars:   # 没有标点的超长段，硬切
                parts.append(buf[:max_chars])
                buf = buf[max_chars:]
        if buf.strip():
            parts.append(buf.strip())
    merged = []
    for p in parts:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] += p
        else:
            merged.append(p)
    if len(merged) >= 2 and len(merged[-1]) < min_chars:
        merged[-2] += merged.pop()
    return merged
