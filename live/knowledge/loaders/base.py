"""文档解析器接口：load(path) -> Document{title, sections[{heading_path, text, page}]}。

新增格式：写一个 Loader 子类（exts 声明扩展名），在 live/knowledge/__init__.py 的 LOADERS 注册一行。
解析失败（空文档、损坏文件、扫描版 PDF）抛 LoaderError，原因是给人看的中文。
"""
from __future__ import annotations

import os

from live.knowledge.models import Document


class LoaderError(ValueError):
    pass


class Loader:
    name = "base"
    exts: tuple = ()

    def load(self, path: str, title: str | None = None) -> Document:
        raise NotImplementedError

    @staticmethod
    def read_text(path: str) -> str:
        with open(path, "rb") as f:
            data = f.read()
        for enc in ("utf-8-sig", "gb18030"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        raise LoaderError("文件编码无法识别（请存成 UTF-8）")

    @staticmethod
    def default_title(path: str, title: str | None) -> str:
        return title or os.path.splitext(os.path.basename(path))[0]


def check_not_empty(doc: Document) -> Document:
    doc.sections = [s for s in doc.sections if s.text.strip()]
    if not doc.sections:
        raise LoaderError("文档里没有可用的文字内容")
    return doc
