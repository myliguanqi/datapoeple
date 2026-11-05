"""中文分词与查询构造（jieba）、覆盖度、把资料片段整理成能念出口的句子。

- index_tokens(text)：建索引用，jieba 搜索引擎模式（"保温杯" → 保温 / 保温杯），小写，去标点；
- build_query(q, synonyms, product_name)：问题 → 检索词 + 信息点分组。去掉疑问词/语气词等停用词，
  去掉产品名本身（"这个保温杯多少钱"里"保温杯"不是信息点），按同义词表扩展（"多少钱" → 价格 / 售价 / 到手价）；
- coverage(groups, text)：资料覆盖了几个信息点（判断"资料没有"，不让 LLM 编）；
- extract_answer(...)：没有 LLM 时从命中片段里挑最相关的句子直接念（去掉 Markdown/表格符号）。
jieba 第一次分词要加载词典（约 1 秒），KnowledgeBase 启动时在后台线程预热。
"""
from __future__ import annotations

import logging
import re
import threading

from live.knowledge.models import Query

_jieba = None
_jieba_lock = threading.Lock()

STOPWORDS = frozenset("""
的 了 吗 呢 啊 吧 呀 哦 嘛 么 呗 哈 嗯 哇 啦 喔 噢 诶 是 有 没 没有 不 也 都 就 还 和 跟 与 及 或 在 给 让 被 把 对 从
我 你 他 她 它 我们 你们 他们 咱 咱们 您 大家 主播 老板 宝宝们 家人们
这 那 这个 那个 这款 那款 这种 那种 这些 那些 这里 那里 这边 那边 这么 那么 此
介绍 讲讲 说说 讲一讲 说一说 详细 具体 情况
什么 啥 怎么 怎么样 怎样 如何 咋 咋样 为什么 为啥 哪 哪个 哪些 哪里 哪儿 几 几个 几种 多少 多久 多长 多大 多么
能 能不能 可以 可不可以 会 会不会 要 想 请问 问 问下 问一下 一下 一个 一些 吗吗 呢呢 请 谢谢 麻烦 帮 帮忙 告诉 知道
吧吧 呀呀 啊啊 嘛嘛 了了 现在 今天 一般 大概 到底 真的 是不是 有没有 好不好 行不行 对不对 么么
""".split())
_PUNCT = re.compile(r"^[\W_]+$", re.UNICODE)
_SPLIT_SENT = re.compile(r"(?<=[。！？!?；;\n])")
_MD_CLEAN = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),             # 图片
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),         # 链接 → 文字
    (re.compile(r"[*_`~#>]+"), ""),
    (re.compile(r"^\s*[-+]\s+", re.M), ""),
    (re.compile(r"\s*\|\s*"), "，"),
    (re.compile(r"[\t ]{2,}"), " "),
]


def _get_jieba():
    global _jieba
    if _jieba is None:
        with _jieba_lock:
            if _jieba is None:
                import jieba
                jieba.setLogLevel(logging.WARNING)
                jieba.initialize()
                _jieba = jieba
    return _jieba


def warmup() -> None:
    _get_jieba()


def _norm(tok: str) -> str:
    return tok.strip().lower()


def index_tokens(text: str) -> list:
    jb = _get_jieba()
    out = []
    for tok in jb.cut_for_search(text or ""):
        tok = _norm(tok)
        if tok and not _PUNCT.match(tok):
            out.append(tok)
    return out


def _strip_modal(tok: str) -> str:
    """"能保 / 能放 / 可用" 这类"情态词 + 动词"的粘连词，去掉前面的情态字。"""
    if len(tok) == 2 and tok[0] in "能可会要" and tok[1] not in "力量够":
        return tok[1]
    return tok


def build_query(q: str, synonyms: dict | None = None, product_name: str = "") -> Query:
    jb = _get_jieba()
    text = (q or "").strip()
    low = text.lower()
    name = (product_name or "").lower()
    name_toks = {_norm(t) for t in jb.cut_for_search(name)} if name else set()
    groups: list = []                                        # [(组, 是否产品名里的词)]
    covered = []                                             # 同义词表命中的原词（其子词不再单独成组）
    for key, alts in (synonyms or {}).items():
        k = str(key).lower()
        if k and k in low:
            alts = alts if isinstance(alts, (list, tuple)) else [alts]
            groups.append(([k] + [str(a).lower() for a in alts if str(a).strip()], bool(name) and k in name))
            covered.append(k)
    for tok in jb.lcut(text):
        tok = _strip_modal(_norm(tok))
        if not tok or _PUNCT.match(tok) or tok in STOPWORDS:
            continue
        if any(tok in k for k in covered) or any(tok in g for g, _ in groups):
            continue
        alts = [tok] + [_norm(s) for s in jb.cut_for_search(tok) if _norm(s) != tok and len(_norm(s)) > 1]
        groups.append((alts, bool(name) and (tok in name or tok in name_toks)))
    # 产品名里的词（"这个保温杯多少钱"的"保温杯"）不是信息点；但问题只剩这些词时（"保温能保多久"）它们就是信息点
    info = [g for g, in_name in groups if not in_name] or [g for g, _ in groups]
    terms = []
    for g in info:
        for t in g:
            for piece in (index_tokens(t) if len(t) > 2 else [t]):
                if piece not in terms and piece not in STOPWORDS:
                    terms.append(piece)
    if not terms:                                            # 只问"介绍一下"：用产品名检索（覆盖度记 1）
        terms = [t for t in name_toks if t not in STOPWORDS and len(t) > 1]
    return Query(text=text, terms=terms, groups=info)


_NUM = re.compile(r"^\d+(\.\d+)?$")
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~～至到]\s*(\d+(?:\.\d+)?)")


def _alt_in(a: str, low: str) -> bool:
    if not a:
        return False
    if a in low:
        return True
    if _NUM.match(a):                                        # "我165穿什么码" ↔ "160-168cm"
        v = float(a)
        return any(float(x) <= v <= float(y) for x, y in _RANGE.findall(low))
    return False


def coverage(groups: list, text: str) -> tuple[float, list]:
    """返回 (覆盖率, 命中的组首词)。没有信息点（只问"介绍一下"）时覆盖率记为 1。"""
    if not groups:
        return 1.0, []
    low = (text or "").lower()
    hit = [g[0] for g in groups if any(_alt_in(a, low) for a in g)]
    return len(hit) / len(groups), hit


def speakable(text: str) -> str:
    """资料片段 → 口播文本：去 Markdown/表格符号、合并空白。"""
    t = text or ""
    for pat, rep in _MD_CLEAN:
        t = pat.sub(rep, t)
    t = re.sub(r"，{2,}", "，", t)
    t = re.sub(r"^[，\s]+|[，\s]+$", "", t, flags=re.M)
    return re.sub(r"\n+", "\n", t).strip()


def sentences(text: str) -> list:
    out = []
    for line in speakable(text).split("\n"):
        for s in _SPLIT_SENT.split(line):
            s = s.strip()
            if len(s) >= 2:
                out.append(s)
    return out


def extract_answer(groups: list, texts: list, max_chars: int) -> str:
    """从命中片段（按相关度排好）里挑覆盖信息点最多的一句；同一片段里还有能补上**新**信息点的句子就按原文顺序补上（≤ max_chars）。
    问题没有信息点（"介绍一下"）时返回空串，交给 LLM 或触发器话术。"""
    if not groups:
        return ""
    per = [sentences(t) for t in texts]
    cands = []
    for rank, sents in enumerate(per):
        for pos, s in enumerate(sents):
            hit = coverage(groups, s)[1]
            if hit:
                cands.append((-len(hit), rank, pos, s, set(hit)))
    if not cands:
        return ""
    cands.sort(key=lambda c: c[:3])
    best = cands[0]
    picked = [(best[2], best[3])]
    seen = set(best[4])
    total = len(best[3])
    for c in cands[1:]:
        if c[1] != best[1] or not (c[4] - seen) or total + len(c[3]) > max_chars:
            continue
        picked.append((c[2], c[3]))
        seen |= c[4]
        total += len(c[3])
    text = "".join(s if s[-1] in "。！？!?；;" else s + "。" for _, s in sorted(picked))
    if len(text) > max_chars:
        cut = max(text.rfind(c, 0, max_chars) for c in "，。；")
        text = text[:cut + 1] if cut > max_chars // 3 else text[:max_chars]
    return text
