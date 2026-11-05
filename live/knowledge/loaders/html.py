"""HTML：BeautifulSoup（内置 html.parser，不依赖 lxml），去掉 script/style/nav/header/footer/aside 等噪声，
h1–h6 → heading_path，块级元素各成一行，表格行转成「列1：列2」或逗号分隔。"""
from __future__ import annotations

import re

from live.knowledge.loaders.base import Loader, LoaderError, check_not_empty
from live.knowledge.models import Document, Section

NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg", "template",
              "button", "select", "input")
BLOCK_TAGS = ("p", "div", "section", "article", "main", "li", "dt", "dd", "blockquote", "pre", "figcaption", "caption",
              "br", "hr", "ul", "ol", "dl", "table")
_H = "\x01H"


class HtmlLoader(Loader):
    name = "html"
    exts = (".html", ".htm")

    def load(self, path: str, title: str | None = None) -> Document:
        return self.parse(self.read_text(path), self.default_title(path, title))

    def parse(self, html: str, title: str) -> Document:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as e:                       # noqa: BLE001  bs4 很宽容，基本不会到这里
            raise LoaderError(f"HTML 解析失败: {e}") from None
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for t in soup(NOISE_TAGS):
            t.decompose()
        for t in soup.find_all(["head", "title", "meta", "link"]):
            t.decompose()
        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            tr.replace_with(soup.new_string("\n" + (f"{cells[0]}：{cells[1]}" if len(cells) == 2 else "，".join(cells)) + "\n"))
        for lv in range(1, 7):
            for hd in soup.find_all(f"h{lv}"):
                hd.replace_with(soup.new_string(f"\n{_H}{lv}\x02{hd.get_text(' ', strip=True)}\n"))
        for b in soup.find_all(BLOCK_TAGS):
            b.insert_before("\n")
            b.insert_after("\n")
        text = soup.get_text()
        doc_title = None
        stack: list = []
        sections: list = []
        buf: list = []

        def flush():
            body = "\n".join(buf).strip()
            if body:
                sections.append(Section(heading_path=[doc_title or page_title or title] + [h for _, h in stack], text=body))
            buf.clear()

        for line in text.split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                continue
            if line.startswith(_H):
                lv, head = int(line[2]), line[4:].strip()
                flush()
                if lv == 1 and doc_title is None:
                    doc_title, stack = head, []
                    continue
                while stack and stack[-1][0] >= lv:
                    stack.pop()
                stack.append((lv, head))
                continue
            buf.append(line)
        flush()
        return check_not_empty(Document(title=doc_title or page_title or title, sections=sections))
