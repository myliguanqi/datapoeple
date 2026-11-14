"""人设模板的数据结构：默认值、规范化、校验。

一个模板 = persona.yaml（结构化字段）+ prompt.md / rag_prompt.md / script_prompt.md（提示词）+ triggers.yaml（触发器表）。
解析后是一个普通 dict（就是运行时的 cfg["persona"]），各模块按键读：
  id name description builtin source
  anchor_name shop_name honorific big_honorific
  prompt rag_prompt script_prompt
  intents[] spam_keywords intent_llm_hint
  knowledge{no_answer_text} limits{reply_max_chars, answer_max_chars}
  program{item_label, show_price, card, default_item_text, closing_line}
  triggers{name: spec}
**人设相关的文案只写在模板里**，代码里不写任何带货 / 人设话术（CLAUDE.md 约定）。
"""
from __future__ import annotations

import copy
import re

from live.interaction.signals import INTENT_WINDOWS, PLATFORM_SIGNALS, RESERVED_INTENTS, SNAPSHOT_KEYS, TRIGGER_FIELDS

PROMPT_FILES = {"prompt": "prompt.md", "rag_prompt": "rag_prompt.md", "script_prompt": "script_prompt.md"}
YAML_KEYS = ("name", "description", "anchor_name", "shop_name", "honorific", "big_honorific", "intents", "spam_keywords",
             "intent_llm_hint", "knowledge", "limits", "program")
QA_MODES = ("strict", "context", "off")
CARD_MODES = ("product", "title", "off")
WINDOWS = INTENT_WINDOWS
INTENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
NAME_MAX = 30
TEXT_MAX = 20000

DEFAULT_PERSONA = {
    "name": "未命名人设",
    "description": "",
    "anchor_name": "主播",
    "shop_name": "",
    "honorific": {"male": "哥", "female": "姐", "unknown": ""},
    "big_honorific": {"male": "大哥", "female": "大姐", "unknown": "朋友"},
    "prompt": "",
    "rag_prompt": "",
    "script_prompt": "",
    "intents": [],
    "spam_keywords": [],
    "intent_llm_hint": "",
    "knowledge": {"no_answer_text": "{asker}，这个我还不确定，帮您确认一下哦！"},
    "limits": {"reply_max_chars": None, "answer_max_chars": None},
    "program": {"item_label": "节目", "show_price": False, "card": "title", "default_item_text": "今天的内容",
                "closing_line": ""},
    "triggers": {},
}
DEFAULT_INTENT = {"name": "", "label": "", "keywords": [], "qa": "off", "first_come": False, "window": "question_s",
                  "search_suffix": "", "enabled": True}


def _str_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n,，]", v)]
    return [str(x).strip() for x in v if str(x).strip()]


def _opt_int(v):
    if v in (None, "", "null"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return v                                   # 留给 validate 报中文错误，不在这里抛 500


def normalize_intent(d: dict) -> dict:
    out = {**DEFAULT_INTENT, **{k: v for k, v in (d or {}).items() if k in DEFAULT_INTENT}}
    out["name"] = str(out["name"] or "").strip()
    out["label"] = str(out["label"] or out["name"]).strip()
    out["keywords"] = _str_list(out["keywords"])
    qa = out["qa"]
    out["qa"] = "strict" if qa is True else ("off" if qa in (False, None, "") else str(qa))
    out["first_come"] = bool(out["first_come"])
    out["enabled"] = bool(out["enabled"])
    out["window"] = str(out["window"] or "question_s")
    out["search_suffix"] = str(out["search_suffix"] or "").strip()
    return out


def normalize(d: dict) -> dict:
    """补齐默认值、规范类型（不校验合法性，见 validate）。返回新 dict。"""
    src = copy.deepcopy(d or {})
    out = copy.deepcopy(DEFAULT_PERSONA)
    for k in ("id", "builtin", "source"):
        if k in src:
            out[k] = src[k]
    for k in ("name", "description", "anchor_name", "shop_name", "prompt", "rag_prompt", "script_prompt", "intent_llm_hint"):
        if k in src and src[k] is not None:
            out[k] = str(src[k])
    for k in ("honorific", "big_honorific"):
        if isinstance(src.get(k), dict):
            out[k] = {g: str(src[k].get(g) or "") for g in ("male", "female", "unknown")}
    if "intents" in src:
        out["intents"] = [normalize_intent(x) for x in (src["intents"] or []) if isinstance(x, dict)]
    if "spam_keywords" in src:
        out["spam_keywords"] = _str_list(src["spam_keywords"])
    for k in ("knowledge", "limits", "program"):
        if isinstance(src.get(k), dict):
            out[k].update({kk: vv for kk, vv in src[k].items() if kk in DEFAULT_PERSONA[k]})
    out["limits"] = {k: _opt_int(out["limits"].get(k)) for k in DEFAULT_PERSONA["limits"]}
    pg = out["program"]
    pg["show_price"] = bool(pg["show_price"])
    if pg["card"] is False:                       # YAML 1.1 把 off 解析成 False
        pg["card"] = "off"
    elif pg["card"] is True:
        pg["card"] = "product"
    for k in ("item_label", "card", "default_item_text", "closing_line"):
        pg[k] = str(pg[k] or "")
    out["knowledge"]["no_answer_text"] = str(out["knowledge"]["no_answer_text"] or "")
    if isinstance(src.get("triggers"), dict):
        out["triggers"] = {str(n): dict(v) for n, v in src["triggers"].items() if isinstance(v, dict)}
    out["builtin"] = bool(out.get("builtin", False))
    return out


def signals_of(p: dict) -> set:
    """这个人设模板里可引用的信号：平台信号 + 本模板所有意图名（禁用意图的触发器保留但不会触发）。"""
    return set(PLATFORM_SIGNALS) | {i["name"] for i in p.get("intents") or [] if i.get("name")}


def validate(p: dict) -> dict:
    """返回 {字段: 中文原因}；空 dict = 合法。p 应先经过 normalize。"""
    err: dict = {}
    name = (p.get("name") or "").strip()
    if not name:
        err["name"] = "人设名称不能为空"
    elif len(name) > NAME_MAX:
        err["name"] = f"人设名称不能超过 {NAME_MAX} 字"
    if not (p.get("anchor_name") or "").strip():
        err["anchor_name"] = "主播自称不能为空"
    for k in ("prompt", "rag_prompt", "script_prompt"):
        if len(p.get(k) or "") > TEXT_MAX:
            err[k] = f"不能超过 {TEXT_MAX} 字"
    if not (p.get("prompt") or "").strip():
        err["prompt"] = "人设提示词不能为空"
    seen = set()
    for i, it in enumerate(p.get("intents") or []):
        key = f"intents[{i}]"
        n = it.get("name") or ""
        if not INTENT_NAME_RE.match(n):
            err[key] = f"意图名 {n!r} 只能用小写字母、数字、下划线，字母开头"
        elif n in PLATFORM_SIGNALS or n in RESERVED_INTENTS or n in SNAPSHOT_KEYS:
            err[key] = f"意图名 {n!r} 和内置信号重名"
        elif n in seen:
            err[key] = f"意图名 {n!r} 重复"
        elif it.get("qa") not in QA_MODES:
            err[key] = f"qa 只能是 {'/'.join(QA_MODES)}"
        elif it.get("window") not in WINDOWS:
            err[key] = f"window 只能是 {'/'.join(WINDOWS)}"
        elif not it.get("keywords"):
            err[key] = f"意图 {n} 至少要有一个关键词"
        seen.add(n)
    pg = p.get("program") or {}
    if pg.get("card") not in CARD_MODES:
        err["program.card"] = f"场景卡片只能是 {'/'.join(CARD_MODES)}"
    if not (pg.get("item_label") or "").strip():
        err["program.item_label"] = "节目条目的叫法不能为空"
    for k, v in (p.get("limits") or {}).items():
        try:
            bad = v is not None and not (10 <= int(v) <= 300)
        except (TypeError, ValueError):
            bad = True
        if bad:
            err[f"limits.{k}"] = "字数上限要在 10~300 之间（留空 = 用全局设置）"
    if any(i.get("qa") == "strict" for i in p.get("intents") or []) and not (p.get("knowledge") or {}).get("no_answer_text", "").strip():
        err["knowledge.no_answer_text"] = "有严格依据资料的意图时，资料不够的兜底话术不能为空"
    sig = signals_of(p)
    for tname, spec in (p.get("triggers") or {}).items():
        key = f"triggers.{tname}"
        unknown = set(spec) - set(TRIGGER_FIELDS)
        if unknown:
            err[key] = f"未知字段 {sorted(unknown)}"
        elif spec.get("when") != "always" and spec.get("when") not in sig:
            err[key] = f"when={spec.get('when')!r} 不是已知信号（平台信号或本人设的意图名）"
        elif not isinstance(spec.get("lines") or [], list):
            err[key] = "lines 必须是列表"
        elif any(not isinstance(x, str) for x in spec.get("lines") or []):
            err[key] = "lines 里每一项都应是文本"
        else:
            try:
                int(spec.get("priority", 0))
                float(spec.get("cooldown_s", 0) or 0)
                float(spec.get("target_energy", 0.3))
            except (TypeError, ValueError):
                err[key] = "priority / cooldown_s / target_energy 必须是数字"
    if not p.get("triggers"):
        err["triggers"] = "至少要有一个触发器"
    return err


def summary(p: dict) -> dict:
    """列表页用的摘要。"""
    return {"id": p.get("id"), "name": p.get("name"), "description": p.get("description"), "builtin": bool(p.get("builtin")),
            "source": p.get("source"), "anchor_name": p.get("anchor_name"),
            "item_label": (p.get("program") or {}).get("item_label"), "triggers": len(p.get("triggers") or {}),
            "intents": [i["name"] for i in p.get("intents") or []]}
