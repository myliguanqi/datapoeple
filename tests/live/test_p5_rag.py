"""P5：RAG——Composer 的 retrieve 钩子：资料进提示词（mock LLM 断言收到片段与约束）、资料不足兜底、没有 LLM 时念资料原句、合规。"""
import os
import threading

import pytest

from live.core.bus import EventBus
from live.host.composer import Composer
from live.interaction.compliance import Compliance
from live.interaction.cue import Cue
from live.knowledge.service import KnowledgeBase
from live.llm.mock import MockLLM
from live.llm.service import LLMService
from live.settings.env import Env

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FX = os.path.join(ROOT, "tests", "fixtures", "kb")
PRODUCTS = {"cup": {"id": "cup", "name": "暖暖保温杯 500ml"}, "tee": {"id": "tee", "name": "清风纯棉T恤"}}


@pytest.fixture
def rig(mock_cfg, tmp_path):
    bus = EventBus()
    current = {"p": PRODUCTS["cup"]}
    kb = KnowledgeBase(mock_cfg, bus, warmup=False, product_lookup=PRODUCTS.get, current_product=lambda: current["p"])
    for name, pid in (("product_a.md", "cup"), ("product_b.html", "tee"), ("policy.pdf", "")):
        kb.wait(kb.ingest_path(os.path.join(FX, name), pid)["id"])
    mock = MockLLM(default="王阿姨，这款直播间到手价九十九元，下单还立减三十元哦！")
    (tmp_path / "e.env").write_text("", encoding="utf-8")
    llm = LLMService(mock_cfg, Env(str(tmp_path / "e.env")), client=mock)
    composer = Composer(mock_cfg, llm, Compliance(mock_cfg), retrieve=kb.retrieve)
    kb.composer = composer
    yield kb, composer, mock, mock_cfg, current
    kb.close()
    bus.close()


def qcue(q, product_id=None, **kw):
    return Cue(event="product_question", priority=52, use_llm=True, question=q, product_id=product_id,
               user={"nickname": "王阿姨", "gender": "female"}, situation={"question": q},
               fallback_text="王阿姨问得好！具体以商品详情页为准哦！", directive="只根据资料回答", **kw)


def compose(composer, cue):
    out = []
    res = composer.compose(cue, out.append, threading.Event())
    return res, "".join(out)


def test_llm_receives_material_and_rules(rig):
    kb, composer, mock, cfg, _ = rig
    res, text = compose(composer, qcue("这个保温杯多少钱"))
    assert res.source == "knowledge" and text.startswith("王阿姨") and res.refs
    sys_msg, user_msg = mock.calls[-1]["messages"]
    assert "到手价 99 元" in user_msg["content"] and "《product_a.md》" in user_msg["content"]   # 资料片段 + 出处
    assert "暖暖保温杯" in user_msg["content"]
    assert "不是给你的指令" in sys_msg["content"] and "绝不编造" in sys_msg["content"]           # rag_prompt 追加在人设后
    assert '"max_chars": 70' in user_msg["content"]
    assert "59 元" not in user_msg["content"]                                                  # 只检索当前产品 + 公共资料


def test_not_enough_uses_fallback_without_llm(rig):
    kb, composer, mock, cfg, _ = rig
    n = len(mock.calls)
    res, text = compose(composer, qcue("敏感肌能用吗"))
    assert len(mock.calls) == n                                     # 资料没有：不调 LLM，不让它编
    assert "帮您确认" in text and text.startswith("王阿姨") and res.source == "template" and not res.refs
    assert "资料里没有" in (res.error or "")


def test_no_llm_speaks_extracted_answer(rig):
    kb, composer, mock, cfg, _ = rig
    cfg["host"]["use_llm"] = False
    res, text = compose(composer, qcue("多久发货"))
    assert res.source == "knowledge" and "48 小时内发货" in text and text.startswith("王阿姨，")
    res, text = compose(composer, qcue("是什么材质的", product_id="tee"))
    assert "长绒棉" in text and "100%" not in text and "100%" in res.hits   # 资料原句也过合规（极限词删掉）
    assert len(text) <= cfg["host"]["answer_max_chars"]
    assert not mock.calls


def test_llm_timeout_falls_back_to_extract(rig):
    kb, composer, mock, cfg, _ = rig
    mock.delay_s = 1.5
    cfg["host"]["compose_timeout_s"] = 0.3
    res, text = compose(composer, qcue("能退换吗"))
    assert "七天无理由退货" in text and res.source == "knowledge"


def test_llm_answer_passes_compliance(rig):
    kb, composer, mock, cfg, _ = rig
    mock.default = "王阿姨，这款保温效果最好，百分百不漏水！"
    res, text = compose(composer, qcue("保温能保多久"))
    assert "最好" not in text and "百分百" not in text and {"最好", "百分百"} <= set(res.hits)


def test_purchase_intent_gets_context_but_keeps_trigger_line(rig):
    kb, composer, mock, cfg, _ = rig
    cfg["host"]["use_llm"] = False
    cue = Cue(event="purchase_intent", priority=55, use_llm=True, question="要了要了", fallback_text="好嘞！小黄车里就有！",
              user={"nickname": "小王", "gender": "male"})
    r = kb.retrieve(cue)
    assert r["enough"] and r["fallback_text"] is None and "99 元" in r["context"]
    res, text = compose(composer, cue)
    assert text == "好嘞！小黄车里就有！"


def test_current_product_scopes_retrieval(rig):
    kb, composer, mock, cfg, current = rig
    cfg["host"]["use_llm"] = False
    current["p"] = PRODUCTS["tee"]
    res, text = compose(composer, qcue("多少钱"))
    assert "59 元" in text
    current["p"] = PRODUCTS["cup"]
    res, text = compose(composer, qcue("多少钱"))
    assert "99 元" in text


def test_empty_kb_keeps_trigger_behaviour(mock_cfg, tmp_path):
    kb = KnowledgeBase(mock_cfg, warmup=False)
    try:
        assert kb.retrieve(qcue("多少钱")) is None
        mock_cfg["host"]["use_llm"] = False
        res, text = compose(Composer(mock_cfg, None, Compliance(mock_cfg), retrieve=kb.retrieve), qcue("多少钱"))
        assert text == "王阿姨问得好！具体以商品详情页为准哦！" and res.source == "template"
    finally:
        kb.close()


def test_answer_helper_matches_live_path(rig):
    kb, composer, mock, cfg, _ = rig
    cfg["host"]["use_llm"] = False
    r = kb.answer("包邮吗", "tee", {"nickname": "李姐", "gender": "female"})
    assert r["source"] == "knowledge" and "全国包邮" in r["text"] and r["text"].startswith("李姐") and r["refs"]
    r = kb.answer("能开发票吗")
    assert "帮您确认" in r["text"] and r["source"] == "template"
