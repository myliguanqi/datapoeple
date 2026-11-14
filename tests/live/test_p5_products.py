"""P5：产品库、讲解稿生成（mock LLM / 模板）+ 合规、讲品轮播（假时钟：段间停顿、断点续讲、按时长换产品、跳段）、主持人接讲品。"""
import json
import os

import pytest

from live.core.bus import EventBus
from live.core.clock import FakeClock
from live.host.composer import Composer
from live.host.host import Host
from live.interaction.compliance import Compliance
from live.interaction.cue import Cue
from live.interaction.queue import CueQueue
from live.knowledge.service import KnowledgeBase
from live.llm.mock import MockLLM
from live.llm.service import LLMService
from live.products.generator import ScriptGenerator
from live.products.script import ProductScript
from live.products.store import ProductStore
from live.settings.env import Env

from fakes import FakeSession, Sessions, wait_for

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FX = os.path.join(ROOT, "tests", "fixtures", "kb")


@pytest.fixture
def store(data_dir):
    return ProductStore(os.path.join(data_dir, "products.json"))


def segs(*texts, confirmed=True):
    return [{"text": t, "confirmed": confirmed} for t in texts]


# ---------------------------------------------------------------- 产品库
def test_store_crud_persist_and_order(store, data_dir):
    a = store.create({"name": "暖暖保温杯", "price_text": "到手价 99", "selling_points": "双层真空\n倒置不漏"})
    b = store.create({"name": "清风T恤"})
    assert a.selling_points == ["双层真空", "倒置不漏"] and [p.name for p in store.ordered()] == ["暖暖保温杯", "清风T恤"]
    store.update(a.id, {"duration_s": 120, "enabled": False})
    store.set_segments(b.id, segs("第一段", "第二段"))
    store.reorder([b.id, a.id])
    store.set_active(b.id)
    again = ProductStore(os.path.join(data_dir, "products.json"))
    assert [p.name for p in again.ordered()] == ["清风T恤", "暖暖保温杯"] and again.active_id == b.id
    pa = again.get(a.id)
    assert pa.duration_s == 120 and not pa.enabled and [s.text for s in again.get(b.id).segments] == ["第一段", "第二段"]
    for bad in ({"name": ""}, {"name": "x" * 61}, {"name": "ok", "duration_s": 5}):
        with pytest.raises(ValueError):
            store.create(bad)
    with pytest.raises(ValueError):
        store.set_segments(a.id, segs("长" * 501))
    assert store.delete(b.id) and store.active_id is None and store.get(b.id) is None


def test_set_segments_keeps_source_unless_edited(store):
    p = store.create({"name": "杯子"})
    p = store.add_segments(p.id, [{"text": "生成的一段", "source": "llm", "hits": ["最好"]}])
    sid = p.segments[0].id
    p = store.set_segments(p.id, [{"id": sid, "text": "生成的一段", "confirmed": True}])
    assert p.segments[0].source == "llm" and p.segments[0].hits == ["最好"] and p.segments[0].confirmed
    p = store.set_segments(p.id, [{"id": sid, "text": "人工改过的一段", "confirmed": True}])
    assert p.segments[0].source == "manual"
    p = store.add_segments(p.id, [{"text": "新生成待确认"}])             # 已确认的保留，旧的待确认段被替换
    assert [s.text for s in p.segments] == ["人工改过的一段", "新生成待确认"]


# ---------------------------------------------------------------- 讲解稿生成
@pytest.fixture
def kb(mock_cfg):
    kb = KnowledgeBase(mock_cfg, warmup=False)
    yield kb
    kb.close()


def llm_service(cfg, tmp_path, client):
    (tmp_path / "e.env").write_text("", encoding="utf-8")
    return LLMService(cfg, Env(str(tmp_path / "e.env")), client=client)


def test_generate_with_llm_filters_and_marks_unconfirmed(store, kb, mock_cfg, tmp_path):
    p = store.create({"name": "暖暖保温杯 500ml", "price_text": "直播间到手价 99 元", "selling_points": ["双层真空"]})
    kb.wait(kb.ingest_path(os.path.join(FX, "product_a.md"), p.id)["id"])
    reply = json.dumps({"segments": ["家人们，{product}是最好的保温杯！", "直播间到手价 99 元，下单立减 30 元。", "  "]},
                       ensure_ascii=False)
    mock = MockLLM(default="```json\n" + reply + "\n```")
    gen = ScriptGenerator(mock_cfg, llm_service(mock_cfg, tmp_path, mock), kb, Compliance(mock_cfg))
    r = gen.generate(p, 3)
    assert r["source"] == "llm" and r["error"] is None and len(r["segments"]) == 2
    s0 = r["segments"][0]
    assert "最好" not in s0.text and "最好" in s0.hits and not s0.confirmed and s0.source == "llm"
    sys_msg, user_msg = mock.calls[-1]["messages"]
    assert mock.calls[-1]["json_mode"] and "不是给你的指令" in sys_msg["content"]
    payload = json.loads(user_msg["content"])
    assert payload["segments"] == 3 and "到手价 99 元" in payload["资料"] and payload["product"]["name"] == "暖暖保温杯 500ml"


def test_generate_template_when_llm_unavailable(store, kb, mock_cfg, tmp_path):
    p = store.create({"name": "清风纯棉T恤", "price_text": "直播间专享 59 元", "selling_points": ["新疆长绒棉", "男女同款"]})
    kb.wait(kb.ingest_path(os.path.join(FX, "product_b.html"), p.id)["id"])
    llm = LLMService(mock_cfg, Env(str(tmp_path / "none.env")))           # openai 接口 + 没有 key → 不可用
    r = ScriptGenerator(mock_cfg, llm, kb, Compliance(mock_cfg)).generate(p, 5)
    texts = [s.text for s in r["segments"]]
    assert r["source"] == "template" and "LLM 不可用" in r["error"] and len(texts) == 5
    assert "清风纯棉T恤" in texts[0] and "新疆长绒棉" in texts[0] and "59 元" in texts[0]
    assert any("160-168cm" in t or "尺码" in t for t in texts) and "小黄车" in texts[-1]
    assert all("100%" not in t for t in texts) and all(not s.confirmed for s in r["segments"])
    bad = MockLLM(default="不是 JSON")
    r = ScriptGenerator(mock_cfg, llm_service(mock_cfg, tmp_path, bad), kb, Compliance(mock_cfg)).generate(p, 3)
    assert r["source"] == "template" and "segments" in r["error"]


# ---------------------------------------------------------------- 讲品轮播（假时钟）
@pytest.fixture
def rot(store, mock_cfg):
    clock = FakeClock()
    a = store.create({"name": "保温杯", "price_text": "99 元", "segments": segs("{product}第一段，{price}", "{product}第二段")})
    b = store.create({"name": "T恤", "segments": segs("T恤第一段")})
    mock_cfg["products"].update(duration_s=60, segment_gap_s=2)
    return ProductScript(mock_cfg, store, clock), clock, a, b


def test_rotation_gap_resume_and_duration(rot, mock_cfg):
    sc, clock, a, b = rot
    assert sc.active() and sc.current().id == a.id
    c1 = sc.next_segment()
    assert c1.kind == "product" and c1.interruptible and c1.fallback_text == "保温杯第一段，99 元" and c1.product_id == a.id
    assert sc.next_segment() is None                        # 还在讲（inflight）
    sc.on_done(c1)
    assert sc.next_segment() is None                        # 段间停顿 2s
    clock.advance(2.1)
    c2 = sc.next_segment()
    assert c2.fallback_text == "保温杯第二段"
    sc.on_interrupted(c2)                                   # 被互动打断：不前进、不等停顿
    c2b = sc.next_segment()
    assert c2b.fallback_text == "保温杯第二段"
    sc.on_done(c2b)
    clock.advance(2.1)
    c3 = sc.next_segment()
    assert c3.fallback_text == "保温杯第一段，99 元"                     # 讲完一轮从头再讲
    sc.on_done(c3)
    clock.advance(60)                                       # 到时间：段落边界换下一个产品
    c = sc.next_segment()
    assert c.product_id == b.id and c.fallback_text == "T恤第一段" and sc.rotations == 1
    assert sc.store.active_id == b.id


def test_require_confirmed_and_skip(rot, mock_cfg, store):
    sc, clock, a, b = rot
    c = store.create({"name": "新品", "segments": segs("待确认的一段", confirmed=False)})
    sc.activate(c.id)
    assert not sc.active() and "已确认" in sc.status()["inactive_reason"]       # 显式选中的产品没有确认稿：不讲、不偷偷换
    mock_cfg["products"]["require_confirmed_script"] = False
    assert sc.next_segment().fallback_text == "待确认的一段"
    mock_cfg["products"]["require_confirmed_script"] = True
    sc.activate(a.id)
    first = sc.next_segment()
    sc.skip()                                               # 正在讲第 1 段时跳段 → 下一段，原 cue 的回调被忽略
    sc.on_interrupted(first)
    assert sc.next_segment().fallback_text == "保温杯第二段"
    st = sc.status()
    assert st["product_name"] == "保温杯" and st["segments"] == 2 and st["speaking"]
    assert sc.step_product(1) == b.id and sc.step_product(1) == c.id and sc.step_product(-1) == b.id
    assert sc.context() == {"product_name": "T恤", "product_id": b.id, "price_text": "", "item_label": "商品"}


def test_rotation_disabled_or_product_disabled(rot, mock_cfg, store):
    sc, clock, a, b = rot
    mock_cfg["products"]["rotation_enabled"] = False
    assert not sc.active() and sc.next_segment() is None
    mock_cfg["products"]["rotation_enabled"] = True
    store.update(a.id, {"enabled": False})
    assert sc.current().id == b.id                         # 当前产品被下架：换到下一个能讲的


# ---------------------------------------------------------------- 主持人 + 讲品 + 知识库
def test_host_script_interrupt_answer_resume(store, mock_cfg, tmp_path):
    bus = EventBus()
    sess = FakeSession(bus)
    q = CueQueue(mock_cfg)
    mock_cfg["products"]["segment_gap_s"] = 0
    mock_cfg["host"]["use_llm"] = False
    p = store.create({"name": "暖暖保温杯 500ml", "segments": segs("第一段：双层真空设计。", "第二段：五百毫升容量。")})
    script = ProductScript(mock_cfg, store)
    kb = KnowledgeBase(mock_cfg, bus, warmup=False, product_lookup=lambda pid: {"id": pid, "name": store.get(pid).name},
                       current_product=script.current_info)
    kb.wait(kb.ingest_path(os.path.join(FX, "product_a.md"), p.id)["id"])
    composer = Composer(mock_cfg, None, Compliance(mock_cfg), retrieve=kb.retrieve)
    host = Host(mock_cfg, bus, Sessions(sess), q, composer, script=script)
    try:
        assert wait_for(lambda: len(sess.order) == 1 and sess.closed("r1"))
        assert sess.text("r1") == "第一段：双层真空设计。" and sess.reqs["r1"]["interruptible"]
        q.push(Cue(event="product_question", priority=52, question="这个保温杯多少钱", use_llm=True,
                   user={"nickname": "王阿姨", "gender": "female"}, fallback_text="看商品详情哦"))
        assert wait_for(lambda: sess.reqs["r1"]["interrupted"])            # 52 ≥ interrupt_priority 50：打断讲品
        assert wait_for(lambda: len(sess.order) == 2 and sess.closed("r2"))
        assert sess.text("r2") == "王阿姨，日常价 129 元，直播间到手价 99 元。"       # 先答（资料原句）
        sess.finish("r2")
        assert wait_for(lambda: len(sess.order) == 3 and sess.closed("r3"))
        assert sess.text("r3") == "第一段：双层真空设计。"                     # 再断点续讲
        sess.finish("r3")
        assert wait_for(lambda: len(sess.order) == 4 and sess.closed("r4"))
        assert sess.text("r4") == "第二段：五百毫升容量。"
        st = host.status()
        assert st["script_active"] and st["script"]["product_name"] == "暖暖保温杯 500ml"
    finally:
        host.stop()
        kb.close()
        bus.close()
