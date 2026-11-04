"""流式输出按句切分：LLM 一边吐字，一边把已经成句的部分交给 TTS（降低首句延迟）。"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

_END = "。！？!?；;…\n"
_SOFT = "，,、：:"


def iter_sentences(chunks: Iterable[str], max_chars: int = 40, min_chars: int = 4) -> Iterator[str]:
    """chunks 是增量文本片段；遇到句末标点就产出一句；过长（> max_chars）时在最后一个逗号处断开。
    太短的句子（< min_chars，如"好！"）并到下一句，避免 TTS 碎句。"""
    buf = ""
    carry = ""
    for piece in chunks:
        if not piece:
            continue
        buf += piece
        while True:
            idx = next((i for i, ch in enumerate(buf) if ch in _END), -1)
            if idx < 0:
                if len(buf) > max_chars:
                    cut = max((buf.rfind(c, 0, max_chars) for c in _SOFT), default=-1)
                    if cut > 0:
                        sent, buf = buf[:cut + 1], buf[cut + 1:]
                        out = (carry + sent).strip()
                        carry = ""
                        if out:
                            yield out
                        continue
                break
            # 连续的句末标点（"！！"、"？！"）算同一句
            end = idx
            while end + 1 < len(buf) and buf[end + 1] in _END:
                end += 1
            sent, buf = buf[:end + 1], buf[end + 1:]
            out = (carry + sent).strip()
            if len(re.sub(r"\s", "", out)) < min_chars:
                carry = out
                continue
            carry = ""
            if out:
                yield out
    rest = (carry + buf).strip()
    if rest:
        yield rest
