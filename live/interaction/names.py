"""昵称 → 口播称呼（移植参考项目 director._short_name / _person_part / _honor_by_gender）。

  short_name("诗江传媒…王少", "male") → "王少哥"（剥机构前缀，叫人不叫机构）
  short_name("多次婉拒砂糖桔", "female") → "多次姐"（取前两个汉字 + 后缀）
  short_name("六哥牛杂", "male") → "六哥"（已自带称呼尾不再加）
  short_name("用户12345") → "用户"+后缀；纯英文/数字昵称同理
后缀与尊称来自设置 persona.honorific / persona.big_honorific，可按需改。
"""
from __future__ import annotations

import re

_HAN = re.compile(r"[一-鿿]")
_ADDRESS_TAIL = set("哥姐弟妹宝酱神王爷爹娘姨叔伯婶")
_ORG_WORDS = ("传媒", "文化", "科技", "工作室", "官方", "团队", "集团", "公司", "商贸", "珠宝", "直播间", "电商", "严选",
              "优选", "旗舰店", "小店", "影视", "玉器", "茶业")
_SEP_RE = re.compile(r"[…⋯·・丨|/\\\-—–_~～〜\s.。,，、*＊★☆✨()（）【】\[\]《》「」{}]+")

DEFAULT_SUFFIX = {"male": "哥", "female": "姐", "unknown": ""}
DEFAULT_BIG = {"male": "大哥", "female": "大姐", "unknown": "家人"}


def person_part(name: str) -> str:
    segs = [s for s in _SEP_RE.split(name) if s]
    if len(segs) > 1:
        for seg in reversed(segs):
            if _HAN.search(seg) and not seg.endswith(_ORG_WORDS):
                return seg
        return segs[-1]
    seg = segs[0] if segs else name
    cut = 0
    for w in _ORG_WORDS:
        i = seg.rfind(w)
        if i >= 0:
            cut = max(cut, i + len(w))
    rest = seg[cut:]
    return rest if cut and _HAN.search(rest) else seg


def short_name(name, gender: str | None = "unknown", suffixes: dict | None = None) -> str:
    suffix = (suffixes or DEFAULT_SUFFIX).get(gender or "unknown", "")
    name = str(name or "").strip()
    han = _HAN.findall(person_part(name))
    if name.startswith("用户") or not han:
        return "用户" + suffix if name else "这位朋友"
    core = "".join(han[:2])
    if core[-1] in _ADDRESS_TAIL:
        return core
    return core + suffix


def honor(gender: str | None, big: dict | None = None) -> str:
    return (big or DEFAULT_BIG).get(gender or "unknown") or (big or DEFAULT_BIG).get("unknown", "家人")
