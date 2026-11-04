"""合规过滤：所有要说出口的文本（模板、LLM、讲解稿、人工插话）都过 filter()。

- compliance.banned_words：广告法极限词、功效/收益承诺、站外导流等；命中有替换（compliance.replacements）的换掉，没替换的删掉；
- compliance.allow_phrases：含违禁词但意思正常的短语（第一时间、第一次、最近……）先保护起来不替换；
- compliance.block_on_hit：命中直接返回空串（调用方改用兜底话术，兜底话术本身也要再过一遍——那次不 block，只替换/删除）；
- 匹配不区分大小写（加V / 加v）；替换后清理多余的标点与空白。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class FilterResult:
    text: str
    hits: list = field(default_factory=list)
    blocked: bool = False

    def to_dict(self) -> dict:
        return {"text": self.text, "hits": list(self.hits), "blocked": self.blocked}


class Compliance:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def opts(self) -> dict:
        return self.cfg["compliance"]

    def filter(self, text: str, allow_block: bool = True) -> FilterResult:
        text = text or ""
        if not self.opts.get("enabled", True) or not text:
            return FilterResult(text)
        protected = {}
        for i, phrase in enumerate(sorted((p for p in self.opts.get("allow_phrases") or [] if p), key=len, reverse=True)):
            token = f"\u0000{i}\u0000"
            if phrase in text:
                text = text.replace(phrase, token)
                protected[token] = phrase
        repl = self.opts.get("replacements") or {}
        hits = []
        # 长词先换（"全网最低" 先于 "最低"）
        for word in sorted({w for w in (self.opts.get("banned_words") or []) if w}, key=len, reverse=True):
            pat = re.compile(re.escape(word), re.IGNORECASE)
            if pat.search(text):
                hits.append(word)
                text = pat.sub(lambda _m, w=word: repl.get(w, ""), text)
        for token, phrase in protected.items():
            text = text.replace(token, phrase)
        if hits:
            text = re.sub(r"[ \t]{2,}", " ", text)
            text = re.sub(r"([，,、])\s*[，,、]+", r"\1", text)
            text = re.sub(r"^[，,、\s]+", "", text).strip()
            if allow_block and self.opts.get("block_on_hit"):
                return FilterResult("", hits, blocked=True)
        return FilterResult(text, hits)
