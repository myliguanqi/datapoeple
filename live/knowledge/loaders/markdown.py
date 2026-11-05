"""Markdown / 纯文本：# 标题层级 → heading_path；表格行转成「列1：列2」；去掉链接、图片、强调符号。"""
from __future__ import annotations

import re

from live.knowledge.loaders.base import Loader, check_not_empty
from live.knowledge.models import Document, Section

_ATX = re.compile(r"^(#{1,6})(?:\s+(.*?))?\s*#*\s*$")
_SETEXT = re.compile(r"^\s*(=+|-+)\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"<[^>]+>"), ""),
    (re.compile(r"(\*\*|__|\*|_|`|~~)(.+?)\1"), r"\2"),
]


def clean_inline(s: str) -> str:
    for pat, rep in _INLINE:
        s = pat.sub(rep, s)
    return s.strip()


def table_cells(line: str) -> list:
    return [c for c in (clean_inline(x) for x in line.strip().strip("|").split("|")) if c]


def table_row(cells: list) -> str:
    return f"{cells[0]}：{cells[1]}" if len(cells) == 2 else "，".join(cells)


class MarkdownLoader(Loader):
    name = "markdown"
    exts = (".md", ".markdown", ".txt")

    def load(self, path: str, title: str | None = None) -> Document:
        return self.parse(self.read_text(path), self.default_title(path, title), plain=path.lower().endswith(".txt"))

    def parse(self, text: str, title: str, plain: bool = False) -> Document:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        doc_title = None
        stack: list = []                    # [(level, heading)]，不含文档标题（H1）
        sections: list = []
        buf: list = []

        def flush():
            body = "\n".join(x for x in buf if x.strip()).strip()
            if body:
                sections.append(Section(heading_path=[doc_title or title] + [h for _, h in stack], text=body))
            buf.clear()

        def heading(level: int, head: str):
            nonlocal doc_title, stack
            flush()
            if level == 1 and doc_title is None:
                doc_title, stack = head, []
                return
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, head))

        in_fence = False
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            i += 1
            if plain:
                buf.append(line)
                continue
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                buf.append(line)
                continue
            m = _ATX.match(line)
            if m:
                head = clean_inline(m.group(2) or "")
                if head:                                     # 空标题（只有 #）忽略
                    heading(len(m.group(1)), head)
                continue
            s = line.strip()
            if s and _SETEXT.match(nxt) and not s.startswith(("|", "-", "*", ">")):     # setext 标题（下一行 === / ---）
                heading(1 if nxt.strip().startswith("=") else 2, clean_inline(s))
                i += 1
                continue
            if _TABLE_SEP.match(line):
                continue
            if s.startswith("|"):
                cells = table_cells(s)
                if _TABLE_SEP.match(nxt) and len(cells) <= 2:
                    continue                                 # 两列表格的表头（项目 | 参数）没有信息量
                if cells:
                    buf.append(table_row(cells))
                continue
            s = re.sub(r"^>\s?", "", s)
            s = re.sub(r"^([-*+]|\d+[.)])\s+", "", s)
            buf.append(clean_inline(s))
        flush()
        return check_not_empty(Document(title=doc_title or title, sections=sections))
