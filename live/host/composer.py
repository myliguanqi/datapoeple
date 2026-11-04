"""写词：把导演的 Cue 变成要说出口的台词，逐句交给会话（emit），返回来源与耗时（进决策日志）。

路径：
  - 模板：cue.fallback_text（导演已按性别称呼、昵称简称渲染好），立即可用、零成本；
  - LLM：host.use_llm 且触发器 use_llm 且 LLM 可用时。系统提示 = persona.prompt，用户消息 = JSON（事件、指令、局势、兜底示例、
    最近说过的话、字数上限）；流式时按句产出、边写边说；host.compose_timeout_s 内没出第一句 → 用兜底；超过字数上限后面的丢掉；
  - 每一句都过合规过滤：LLM 的句子命中且 block_on_hit 时丢掉这句（第一句就被拦 → 改用兜底），兜底/人工插话只替换不拦截。
知识库（P5）通过 retrieve(cue) 钩子把资料片段放进用户消息。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger

from live.interaction.compliance import Compliance
from live.interaction.cue import Cue
from live.llm.text import iter_sentences
from live.tts.text_split import split_sentences

QA_EVENTS = {"product_question", "purchase_intent"}


@dataclass
class ComposeResult:
    text: str = ""
    source: str = "template"           # template | llm | knowledge | manual | script
    sentences: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    ms: int = 0
    error: str | None = None
    refs: list = field(default_factory=list)   # 知识库引用（P5）


class Composer:
    def __init__(self, cfg: dict, llm=None, compliance: Compliance | None = None, retrieve: Callable | None = None):
        self.cfg = cfg
        self.llm = llm
        self.compliance = compliance or Compliance(cfg)
        self.retrieve = retrieve           # retrieve(cue) -> {"context": str, "refs": [...], "enough": bool} | None

    # ------------------------------------------------------------------ 工具
    def max_chars(self, cue: Cue) -> int:
        h = self.cfg["host"]
        return int(h["answer_max_chars"] if (cue.event in QA_EVENTS or cue.kind == "product") else h["reply_max_chars"])

    def use_llm(self, cue: Cue) -> bool:
        return (bool(self.cfg["host"]["use_llm"]) and cue.use_llm and cue.kind in ("interaction", "product")
                and self.llm is not None and self.llm.available)

    def system_prompt(self) -> str:
        p = self.cfg["persona"]
        shop = p.get("shop_name") or ""
        return (p.get("prompt") or "").replace("{anchor}", p.get("anchor_name") or "主播").replace(
            "{shop_line}", f"，店铺「{shop}」" if shop else "")

    def messages(self, cue: Cue, max_chars: int, knowledge: dict | None = None) -> list:
        payload = {"event": cue.event, "directive": cue.directive,
                   "situation": {**(cue.situation or {}), "max_chars": max_chars},
                   "example": cue.fallback_text, "recent": self.llm.recent_lines() if self.llm else []}
        if knowledge and knowledge.get("context"):
            payload["资料"] = knowledge["context"]
        return [{"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _filter(self, text: str, allow_block: bool):
        return self.compliance.filter(text, allow_block=allow_block)

    # ------------------------------------------------------------------ 主入口
    def compose(self, cue: Cue, emit: Callable[[str], None], cancel: threading.Event | None = None) -> ComposeResult:
        cancel = cancel or threading.Event()
        t0 = time.perf_counter()
        res = ComposeResult(source={"manual": "manual", "product": "script"}.get(cue.kind, "template"))
        knowledge = None
        if self.retrieve is not None and (cue.event in QA_EVENTS):
            try:
                knowledge = self.retrieve(cue)
            except Exception as e:
                logger.warning(f"知识库检索失败: {e}")
        if knowledge and knowledge.get("refs"):
            res.refs = knowledge["refs"]
        if knowledge is not None and not knowledge.get("enough", True) and knowledge.get("fallback_text"):
            cue.fallback_text = knowledge["fallback_text"]          # 资料不足：用"帮您确认"兜底，不让 LLM 编
        elif self.use_llm(cue):
            ok = self._compose_llm(cue, emit, cancel, res, knowledge)
            if ok:
                res.ms = round((time.perf_counter() - t0) * 1000)
                self._note(res)
                return res
        self._compose_template(cue, emit, cancel, res)
        res.ms = round((time.perf_counter() - t0) * 1000)
        self._note(res)
        return res

    def _note(self, res: ComposeResult) -> None:
        res.text = "".join(res.sentences)
        if self.llm is not None and res.text:
            self.llm.note_spoken(res.text)

    def _compose_template(self, cue: Cue, emit, cancel, res: ComposeResult) -> None:
        f = self._filter(cue.fallback_text or "", allow_block=False)
        res.hits += f.hits
        for sent in split_sentences(f.text, max_chars=int(self.cfg["tts"]["max_sentence_chars"])):
            if cancel.is_set():
                break
            res.sentences.append(sent)
            emit(sent)

    def _compose_llm(self, cue: Cue, emit, cancel, res: ComposeResult, knowledge) -> bool:
        """返回 True 表示 LLM 产出了至少一句；False 表示要回退模板（超时、失败、第一句就被合规拦掉）。"""
        max_chars = self.max_chars(cue)
        msgs = self.messages(cue, max_chars, knowledge)
        stream = bool(self.cfg["llm"]["stream"])
        timeout = float(self.cfg["host"]["compose_timeout_s"])
        out: queue.Queue = queue.Queue()

        def worker():
            try:
                r = self.llm.chat(msgs, stream=stream, timeout=max(timeout, 3.0))
                pieces = r if stream else [r]
                for sent in iter_sentences(pieces, max_chars=int(self.cfg["tts"]["max_sentence_chars"])):
                    if cancel.is_set():
                        break
                    out.put(("sent", sent))
                out.put(("end", None))
            except Exception as e:
                out.put(("error", str(e)))

        threading.Thread(target=worker, name="compose-llm", daemon=True).start()
        deadline = time.monotonic() + timeout
        total = 0
        got_any = False
        while not cancel.is_set():
            wait = deadline - time.monotonic() if not got_any else max(timeout, 10.0)
            if wait <= 0:
                res.error = f"LLM 超过 {timeout:g}s 没给出第一句"
                break
            try:
                kind, val = out.get(timeout=wait)
            except queue.Empty:
                res.error = res.error or (f"LLM 超过 {timeout:g}s 没给出第一句" if not got_any else "LLM 输出中断")
                break
            if kind == "error":
                res.error = val
                break
            if kind == "end":
                break
            f = self._filter(val, allow_block=True)
            res.hits += f.hits
            if f.blocked or not f.text.strip():
                if not got_any:
                    res.error = "第一句命中违禁词被拦截"
                    break
                continue
            if total and total + len(f.text) > max_chars * 1.2:
                break                                         # 超字数：后面的不说了
            total += len(f.text)
            res.sentences.append(f.text)
            got_any = True
            emit(f.text)
        if got_any:
            res.source = "knowledge" if (knowledge and knowledge.get("refs")) else "llm"
        elif res.error:
            logger.info(f"写词回退兜底（{cue.event}）：{res.error}")
        return got_any
