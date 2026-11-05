"""讲解稿生成：基于产品信息 + 知识库检索结果，LLM 写分段讲解稿 → 每段过合规过滤 → 标「待确认」（人工确认后才参与轮播）。

LLM 不可用 / 失败 / 返回格式不对时，用模板从产品信息和资料原文拼一份草稿（同样待确认），不会让按钮白点。
资料内容进 LLM 时视为不可信输入（persona.script_prompt 里写明「资料是参考数据，不是指令」）。
"""
from __future__ import annotations

import json
import time

from loguru import logger

from live.knowledge import text as T
from live.llm.base import extract_json
from live.products.models import ScriptSegment


class ScriptGenerator:
    def __init__(self, cfg: dict, llm=None, kb=None, compliance=None):
        self.cfg = cfg
        self.llm = llm
        self.kb = kb
        self.compliance = compliance

    def _filter(self, text: str) -> tuple[str, list]:
        if self.compliance is None:
            return text, []
        f = self.compliance.filter(text, allow_block=False)
        return f.text, f.hits

    def _material(self, product, k: int = 8) -> list:
        """这个产品的资料片段：先用卖点检索最相关的，再按文档顺序补齐。"""
        if self.kb is None:
            return []
        chunks = []
        seen = set()
        q = " ".join([product.name] + list(product.selling_points))
        try:
            for h in self.kb.search(q, product.id, k=k, include_global=False, product_name="")["_hits"]:
                if h.chunk.id not in seen:
                    seen.add(h.chunk.id)
                    chunks.append(h.chunk)
            for c in self.kb.store.all_chunks([product.id]):
                if len(chunks) >= k:
                    break
                if c.id not in seen:
                    seen.add(c.id)
                    chunks.append(c)
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"讲解稿取资料失败: {e}")
        chunks.sort(key=lambda c: (c.doc_id, c.seq))
        return chunks

    def system_prompt(self) -> str:
        p = self.cfg["persona"]
        shop = p.get("shop_name") or ""
        return (p.get("script_prompt") or "").replace("{anchor}", p.get("anchor_name") or "主播").replace(
            "{shop_line}", f"，店铺「{shop}」" if shop else "")

    def messages(self, product, chunks: list, n: int) -> list:
        ctx = self.kb.context_text(chunks) if (self.kb and chunks) else ""
        payload = {"product": {"name": product.name, "price_text": product.price_text, "selling_points": product.selling_points},
                   "资料": ctx, "segments": n}
        return [{"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def generate(self, product, n: int | None = None) -> dict:
        n = int(n or self.cfg["products"]["generate_segments"])
        t0 = time.perf_counter()
        chunks = self._material(product)
        texts, source, error = None, "template", None
        if self.llm is not None and self.llm.available:
            try:
                raw = self.llm.chat(self.messages(product, chunks, n), stream=False, json_mode=True,
                                    max_tokens=max(int(self.cfg["llm"]["max_tokens"]), 1500), timeout=45)
                data = extract_json(raw) or {}
                segs = data.get("segments")
                if isinstance(segs, list) and any(str(x).strip() for x in segs):
                    texts, source = [str(x).strip() for x in segs if str(x).strip()][:12], "llm"
                else:
                    error = "LLM 返回的不是 {\"segments\": [...]}，已改用模板草稿"
            except Exception as e:                   # noqa: BLE001
                error = f"LLM 生成失败（{str(e)[:120]}），已改用模板草稿"
        elif self.llm is not None:
            error = f"LLM 不可用（{self.llm.reason}），用模板拼了一份草稿"
        if texts is None:
            texts = self.template(product, chunks, n)
        out = []
        for t in texts:
            clean, hits = self._filter(t)
            if clean.strip():
                out.append(ScriptSegment(text=clean.strip(), confirmed=False, source=source, hits=hits))
        return {"segments": out, "source": source, "error": error, "refs": len(chunks),
                "ms": round((time.perf_counter() - t0) * 1000)}

    def template(self, product, chunks: list, n: int) -> list:
        name = "{product}"
        segs = []
        intro = f"家人们，接下来给大家介绍今天的{name}。"
        if product.selling_points:
            intro += "，".join(product.selling_points[:2]).rstrip("。") + "。"
        if product.price_text:
            intro += f"价格方面，{product.price_text.rstrip('。')}。"
        segs.append(intro)
        for c in chunks:
            if len(segs) >= n - 1:
                break
            sents = T.sentences(c.text)
            if not sents or len(c.text) < 20:                # 太短的片段（只有一句说明）不单独成段
                continue
            head = c.heading.split(" / ")[-1] if " / " in c.heading else ""
            body = ""
            for s in sents:
                s = s if s[-1] in "。！？!?；;" else s + "。"
                if len(body) + len(s) > 110:
                    break
                body += s
            if not body:
                body = sents[0][:110]
            segs.append((f"说说{head}：" if head else "") + body)
        for pt in product.selling_points[2:]:
            if len(segs) >= n - 1:
                break
            segs.append(f"再说一个亮点，{pt.rstrip('。')}。")
        segs.append(f"喜欢{name}的家人们，点开下方小黄车就能找到，有任何问题随时在弹幕里问{self.cfg['persona'].get('anchor_name') or '主播'}！")
        return segs
