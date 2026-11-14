"""一次性迁移：人设模板化之前的数据 → 模板。

旧版本把人设散在全局设置里（live_data/settings.json）：persona.*、interaction.triggers、interaction.intent.*_keywords、
knowledge.no_answer_text；产品在 live_data/products.json。新版本这些键已从 schema 删除（SettingsCenter 会忽略未知键），
所以必须在它们被丢掉之前读原始文件：

1. 有旧覆盖 → 以出厂「带货主播」为底叠加这些覆盖，生成用户模板「带货主播（迁移）」，设为当前；
   settings.json 先备份成 settings.json.pre-persona.bak，再去掉这些键；
2. products.json → programs/<人设 id>.json（迁移出来的模板，没有就归默认人设），原文件改名 products.json.bak；
3. 旧版单一知识库 live_data/kb/{kb.sqlite,files} → live_data/kb/<人设 id>/（新版每人设一个目录）；
4. 写标记 personas/.migrated_v1，只迁一次。
"""
from __future__ import annotations

import json
import os
import shutil

from loguru import logger

from live.personas.models import normalize, validate
from live.personas.store import _ID_RE, load_builtin

MARKER = ".migrated_v1"
_PERSONA_KEYS = {"persona.anchor_name": "anchor_name", "persona.shop_name": "shop_name", "persona.prompt": "prompt",
                 "persona.rag_prompt": "rag_prompt", "persona.script_prompt": "script_prompt",
                 "persona.honorific": "honorific", "persona.big_honorific": "big_honorific"}
_INTENT_KEYS = {"interaction.intent.product_keywords": "product_question",
                "interaction.intent.purchase_keywords": "purchase_intent"}
LEGACY_KEYS = set(_PERSONA_KEYS) | set(_INTENT_KEYS) | {"interaction.triggers", "interaction.intent.spam_keywords",
                                                        "knowledge.no_answer_text"}


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def legacy_persona(legacy: dict) -> dict:
    """出厂 commerce + 旧覆盖 → 模板 dict（还没有 id）。"""
    p = load_builtin("commerce")
    for key, field in _PERSONA_KEYS.items():
        if key in legacy and legacy[key] is not None:
            p[field] = legacy[key]
    for key, intent in _INTENT_KEYS.items():
        if key in legacy:
            for it in p["intents"]:
                if it["name"] == intent:
                    it["keywords"] = list(legacy[key] or [])
    if "interaction.intent.spam_keywords" in legacy:
        p["spam_keywords"] = list(legacy["interaction.intent.spam_keywords"] or [])
    if legacy.get("knowledge.no_answer_text"):
        p["knowledge"]["no_answer_text"] = legacy["knowledge.no_answer_text"]
    for name, ov in (legacy.get("interaction.triggers") or {}).items():
        if isinstance(ov, dict) and name in p["triggers"]:
            p["triggers"][name] = {**p["triggers"][name], **ov}
    p.update(builtin=False, source="commerce", name="带货主播（迁移）",
             description="从旧版本的「主播人设」「触发器」「意图关键词」设置迁移过来的带货人设。")
    return p


def migrate_legacy(data_dir: str, store, default_id: str = "commerce") -> dict:
    """返回 {"persona_id": 迁移出来的模板 id 或 None, "notes": [...]}。"""
    out = {"persona_id": None, "notes": []}
    marker = os.path.join(store.user_dir, MARKER)
    if os.path.exists(marker):
        return out
    spath = os.path.join(data_dir, "settings.json")
    raw = _read_json(spath)
    wrapped = isinstance(raw.get("overrides"), dict)
    overrides = raw["overrides"] if wrapped else raw
    legacy = {k: v for k, v in (overrides or {}).items() if k in LEGACY_KEYS}
    if legacy:
        p = legacy_persona(legacy)
        p = normalize(p)
        err = validate(p)
        if err:                                      # 旧覆盖里可能有坏值：保留身份/提示词，触发器回出厂
            logger.warning(f"[人设迁移] 旧设置生成的模板不合法（{err}），触发器改用出厂值")
            p["triggers"] = load_builtin("commerce")["triggers"]
            p = normalize(p)
            if validate(p):
                p = load_builtin("commerce")
                p["name"] = "带货主播（迁移）"
                p["description"] = "旧设置无法完整迁移，已使用出厂带货人设。"
        p["id"] = store.new_id()
        p["builtin"] = False
        p["source"] = "commerce"
        store.save(p)
        apath = os.path.join(store.user_dir, "active.json")
        with open(apath + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"active": p["id"]}, f)
        os.replace(apath + ".tmp", apath)
        shutil.copy2(spath, spath + ".pre-persona.bak")
        remaining = {k: v for k, v in (overrides or {}).items() if k not in LEGACY_KEYS}
        if wrapped:
            raw["overrides"] = remaining
            _write_json(spath, raw)
        else:
            _write_json(spath, remaining)
        out["persona_id"] = p["id"]
        out["notes"].append(f"旧设置 {sorted(legacy)} → 人设「{p['name']}」（{p['id']}）；原 settings.json 备份为 settings.json.pre-persona.bak")
    target = out["persona_id"] or default_id or "commerce"
    if not _ID_RE.match(target or "") or not store.exists(target):
        target = "commerce"
    prod = os.path.join(data_dir, "products.json")
    if os.path.isfile(prod):
        dst = os.path.join(data_dir, "programs", f"{target}.json")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            shutil.copy2(prod, dst)
            out["notes"].append(f"产品库 products.json → programs/{target}.json")
        os.replace(prod, prod + ".bak")
    kb = os.path.join(data_dir, "kb")
    legacy_entries = ("kb.sqlite", "kb.sqlite-wal", "kb.sqlite-shm", "files")
    old_layout = any(os.path.exists(os.path.join(kb, n)) for n in legacy_entries)
    dst_kb = os.path.join(kb, target)
    if old_layout:
        if os.path.exists(dst_kb):
            out["notes"].append(f"知识库 {dst_kb} 已存在，旧版 live_data/kb 根目录下的文件未移动（请人工确认）")
        else:
            os.makedirs(dst_kb, exist_ok=True)
            moved = []
            for name in legacy_entries:
                src = os.path.join(kb, name)
                if os.path.exists(src):
                    shutil.move(src, os.path.join(dst_kb, name))
                    moved.append(name)
            out["notes"].append(f"知识库 live_data/kb 的 {moved} → live_data/kb/{target}/")
    os.makedirs(store.user_dir, exist_ok=True)
    tmp_marker = marker + ".tmp"
    with open(tmp_marker, "w", encoding="utf-8") as f:
        f.write("\n".join(out["notes"]) or "nothing to migrate")
    os.replace(tmp_marker, marker)
    for n in out["notes"]:
        logger.info(f"[人设迁移] {n}")
    return out
