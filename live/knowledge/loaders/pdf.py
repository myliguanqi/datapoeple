"""PDF：pypdf 按页提取文字（保留页码）。扫描版（没有文字层）不做 OCR，直接报错提示。

PDF 没有结构化标题，用启发式识别：「一、发货时间」「3.2 保修」这类不带句末标点的短行当小标题（跨页延续）。"""
from __future__ import annotations

import re

from live.knowledge.loaders.base import Loader, LoaderError, check_not_empty
from live.knowledge.models import Document, Section

_HEAD = re.compile(r"^(第[一二三四五六七八九十百\d]+[章节条部分]|[一二三四五六七八九十]+[、.．]|\d+(\.\d+)*[、.．\s])\s*\S")


class PdfLoader(Loader):
    name = "pdf"
    exts = (".pdf",)

    def load(self, path: str, title: str | None = None) -> Document:
        import logging

        import pypdf
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        try:
            reader = pypdf.PdfReader(path)
            if reader.is_encrypted and not reader.decrypt(""):
                raise LoaderError("PDF 有密码，无法读取")
            pages = list(reader.pages)
        except LoaderError:
            raise
        except Exception as e:                          # noqa: BLE001  pypdf 的各种解析异常
            raise LoaderError(f"PDF 文件损坏或无法解析（{type(e).__name__}）") from None
        meta_title = ""
        try:
            meta_title = (reader.metadata.title or "").strip() if reader.metadata else ""
        except Exception:                               # noqa: BLE001
            pass
        doc_title = self.default_title(path, title if title else (meta_title or None))
        sections = []
        head = None
        for i, page in enumerate(pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:                           # noqa: BLE001  单页坏了跳过
                text = ""
            buf: list = []
            for ln in text.split("\n"):
                ln = re.sub(r"[ \t]+", " ", ln).strip()
                if not ln:
                    continue
                if len(ln) <= 24 and _HEAD.match(ln) and ln[-1] not in "。！？；，,.;:：":
                    if buf:
                        sections.append(Section(heading_path=[doc_title] + ([head] if head else []), text="\n".join(buf), page=i))
                        buf = []
                    head = ln
                    continue
                buf.append(ln)
            if buf:
                sections.append(Section(heading_path=[doc_title] + ([head] if head else []), text="\n".join(buf), page=i))
        if not sections:
            raise LoaderError("PDF 里没有可提取的文字（可能是扫描版，本系统不做 OCR）")
        return check_not_empty(Document(title=doc_title, sections=sections, pages=len(pages)))
