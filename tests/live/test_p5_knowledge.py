"""P5：知识库——三种文档解析、切块、FTS5/BM25/混合检索、按产品过滤、删除与重建索引、入库进度事件、上传限制。"""
import importlib.util
import os

import pytest

from live.core.bus import EventBus
from live.knowledge import LOADERS
from live.knowledge.chunker import chunk_document, split_text
from live.knowledge.index.embedding import HashEmbedder
from live.knowledge.loaders.base import LoaderError
from live.knowledge.models import Document, Section
from live.knowledge.service import KnowledgeBase, UploadRejected

from fakes import wait_for

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FX = os.path.join(ROOT, "tests", "fixtures", "kb")
PRODUCTS = {"cup": {"id": "cup", "name": "暖暖保温杯 500ml"}, "tee": {"id": "tee", "name": "清风纯棉T恤"}}


def _pdf_builder():
    spec = importlib.util.spec_from_file_location("kb_fixtures", os.path.join(FX, "make_fixtures.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_pdf


def load(name):
    return LOADERS.create(os.path.splitext(name)[1]).load(os.path.join(FX, name))


# ---------------------------------------------------------------- 解析
def test_markdown_headings_and_table():
    doc = load("product_a.md")
    assert doc.title == "暖暖保温杯 500ml"
    heads = {tuple(s.heading_path) for s in doc.sections}
    assert ("暖暖保温杯 500ml", "价格与优惠") in heads and ("暖暖保温杯 500ml", "规格参数") in heads
    spec = next(s for s in doc.sections if s.heading_path[-1] == "规格参数").text
    assert "容量：500 毫升" in spec and "项目" not in spec and "---" not in spec      # 两列表格：行转「列1：列2」，表头去掉
    price = next(s for s in doc.sections if s.heading_path[-1] == "价格与优惠").text
    assert price.startswith("日常价 129 元") and "- " not in price                    # 列表符号去掉
    assert all(s.page is None for s in doc.sections)


def test_html_strips_noise_keeps_structure():
    doc = load("product_b.html")
    assert doc.title == "清风纯棉T恤"
    text = "\n".join(s.text for s in doc.sections)
    for noise in ("不应进入知识库", "window.tracking", "font-family"):
        assert noise not in text
    sizes = next(s for s in doc.sections if s.heading_path[-1] == "尺码表").text
    assert "M 码，160-168cm，52-60kg" in sizes
    assert next(s for s in doc.sections if s.heading_path[-1] == "价格").text == "原价 89 元，直播间专享价 59 元，两件再减 10 元。"


def test_pdf_pages_and_headings():
    doc = load("policy.pdf")
    assert doc.pages == 2 and doc.title == "好物小店售后与发货政策"
    by_head = {s.heading_path[-1]: s for s in doc.sections}
    assert by_head["一、发货时间"].page == 1 and "48 小时内发货" in by_head["一、发货时间"].text
    assert by_head["三、退换货"].page == 2 and "七天无理由退货" in by_head["三、退换货"].text


def test_loader_errors(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("#  \n\n   \n", encoding="utf-8")
    with pytest.raises(LoaderError, match="没有可用的文字"):
        LOADERS.create(".md").load(str(empty))
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(LoaderError, match="损坏"):
        LOADERS.create(".pdf").load(str(bad))
    scanned = tmp_path / "scan.pdf"
    scanned.write_bytes(_pdf_builder()([[], []]))           # 有页面没有文字层（像扫描件）
    with pytest.raises(LoaderError, match="扫描版"):
        LOADERS.create(".pdf").load(str(scanned))
    gbk = tmp_path / "gbk.txt"
    gbk.write_bytes("保修一年，七天无理由。".encode("gbk"))
    assert "保修一年" in LOADERS.create(".txt").load(str(gbk)).sections[0].text


# ---------------------------------------------------------------- 切块
def test_chunk_length_and_overlap():
    para = "".join(f"第{i}句讲的是保温杯的第{i}个特点。" for i in range(60))
    chunks = split_text(para, chunk_chars=120, overlap=20)
    assert len(chunks) > 5 and all(len(c) <= 120 for c in chunks)
    for a, b in zip(chunks, chunks[1:]):
        assert b.startswith(a[-20:])                         # 相邻块重叠 20 字
    doc = Document(title="T", sections=[Section(["T", "甲"], "短段落一。"), Section(["T", "乙"], "短段落二。")])
    cs = chunk_document(doc, "d1", "p1", 400, 50)
    assert [c.heading for c in cs] == ["T / 甲", "T / 乙"]   # 不跨章节合并
    assert all(c.product_id == "p1" and c.doc_id == "d1" for c in cs)


# ---------------------------------------------------------------- 检索
@pytest.fixture(params=["fts", "bm25"])
def kb(request, mock_cfg):
    mock_cfg["knowledge"]["backend"] = request.param
    bus = EventBus()
    events = []
    bus.subscribe("kb.ingest", lambda _t, p: events.append(p))
    kb = KnowledgeBase(mock_cfg, bus, warmup=False, product_lookup=PRODUCTS.get)
    for name, pid in (("product_a.md", "cup"), ("product_b.html", "tee"), ("policy.pdf", "")):
        d = kb.ingest_path(os.path.join(FX, name), pid)
        assert kb.wait(d["id"])["status"] == "ready"
    kb.events = events
    yield kb
    kb.close()
    bus.close()


def top(r):
    return r["hits"][0] if r["hits"] else {}


@pytest.mark.parametrize("q,pid,heading,fact", [
    ("这个保温杯多少钱", "cup", "价格与优惠", "到手价 99 元"),
    ("咖啡保质期多久", "cup", "赠品", "保质期 12 个月"),
    ("我165穿什么码", "tee", "尺码表", "160-168cm"),
    ("多久发货", "tee", "一、发货时间", "48 小时"),          # 公共资料（售后政策）对每个产品都可检索
    ("是什么材质的", "cup", "规格参数", "316 不锈钢"),
])
def test_chinese_queries_hit_right_section(kb, q, pid, heading, fact):
    r = kb.search(q, pid)
    assert r["enough"], r
    assert top(r)["heading"].endswith(heading) and fact in top(r)["text"]


def test_product_filter_and_global(kb):
    r = kb.search("价格多少钱", "cup")
    assert r["hits"] and all(h["product_id"] in ("cup", "") for h in r["hits"])
    assert "99 元" in top(r)["text"]
    r = kb.search("价格多少钱", "tee")
    assert "59 元" in top(r)["text"] and all(h["product_id"] in ("tee", "") for h in r["hits"])
    only = kb.search("退货运费", "cup", include_global=False)
    assert all(h["product_id"] == "cup" for h in only["hits"])
    assert any(h["product_id"] == "" for h in kb.search("退货运费", "cup")["hits"])


def test_not_enough_when_material_missing(kb):
    assert not kb.search("敏感肌能用吗", "cup")["enough"]
    assert not kb.search("能不能刷信用卡分期", "cup")["enough"]
    assert kb.search("能放微波炉吗", "cup")["enough"]          # 资料写了「不能放进微波炉」


def test_delete_and_reindex(kb, mock_cfg):
    doc = next(d for d in kb.list_docs() if d["name"] == "product_a.md")
    n_before = doc["chunks"]
    mock_cfg["knowledge"]["chunk_chars"] = 100
    mock_cfg["knowledge"]["chunk_overlap"] = 10
    assert next(d for d in kb.list_docs() if d["id"] == doc["id"])["stale"]     # 改了切块参数 → 提示重建
    kb.reindex(doc["id"])
    assert kb.wait(doc["id"])["status"] == "ready"
    assert not next(d for d in kb.list_docs() if d["id"] == doc["id"])["stale"]
    assert kb.store.get_doc(doc["id"])["chunks"] >= n_before
    assert "99 元" in top(kb.search("多少钱", "cup")).get("text", "")
    kb.delete(doc["id"])
    assert all(h["doc_id"] != doc["id"] for h in kb.search("多少钱 到手价", None)["hits"])
    assert not os.path.exists(kb.store.file_path(doc))
    after = kb.search("这个保温杯多少钱", "cup")
    assert all("99 元" not in h["text"] for h in after["hits"]) and not after["enough"]


def test_ingest_events_and_status(kb):
    seq = [e["status"] for e in kb.events if e.get("name") == "product_a.md"]
    assert seq == ["queued", "parsing", "indexing", "ready"]
    ready = next(e for e in kb.events if e.get("name") == "policy.pdf" and e["status"] == "ready")
    assert ready["chunks"] >= 4 and ready["product_id"] == ""
    st = kb.status()
    assert st["docs"] == 3 and st["by_status"] == {"ready": 3} and st["chunks"] > 10


def test_upload_limits_and_dedupe(mock_cfg):
    kb = KnowledgeBase(mock_cfg, warmup=False)
    try:
        with pytest.raises(UploadRejected, match="不支持"):
            kb.ingest("run.exe", b"MZ...")
        with pytest.raises(UploadRejected, match="空"):
            kb.ingest("a.md", b"")
        mock_cfg["knowledge"]["max_doc_mb"] = 1
        with pytest.raises(UploadRejected, match="太大"):
            kb.ingest("big.md", b"x" * (1024 * 1024 + 1))
        d = kb.ingest("../../evil name.md", "# 标题\n\n内容一句。".encode("utf-8"))
        assert d["name"] == "evil name.md"                                   # 只保留文件名部分
        assert os.path.basename(kb.store.file_path(d)) == d["id"] + ".md"     # 存盘文件名是随机 ID
        kb.wait(d["id"])
        again = kb.ingest("copy.md", "# 标题\n\n内容一句。".encode("utf-8"))
        assert again["duplicate"] and again["id"] == d["id"]
        broken = kb.ingest("broken.pdf", b"%PDF-1.4 garbage")
        assert kb.wait(broken["id"])["status"] == "failed" and "损坏" in kb.store.get_doc(broken["id"])["error"]
    finally:
        kb.close()


def test_duplicate_content_in_two_scopes_deduped(kb):
    kb.wait(kb.ingest_path(os.path.join(FX, "product_a.md"), "")["id"])      # 同一份资料又作为公共资料传了一次
    texts = [h["text"] for h in kb.search("这个保温杯多少钱", "cup")["hits"]]
    assert len(texts) == len(set(texts)) and "到手价 99 元" in texts[0]


def test_backend_switch_rebuilds(kb, mock_cfg):
    before = top(kb.search("七天无理由退货", None))["chunk_id"]
    mock_cfg["knowledge"]["backend"] = "bm25" if kb.retriever.name == "fts" else "fts"
    kb.switch_backend()
    assert kb.retriever.name == mock_cfg["knowledge"]["backend"]
    assert top(kb.search("七天无理由退货", None))["chunk_id"] == before


def test_hybrid_with_fake_embedder(mock_cfg):
    mock_cfg["knowledge"]["backend"] = "hybrid"
    mock_cfg["knowledge"]["embedding"]["enabled"] = True
    emb = HashEmbedder()
    kb = KnowledgeBase(mock_cfg, warmup=False, embedder=emb, product_lookup=PRODUCTS.get)
    try:
        d = kb.ingest_path(os.path.join(FX, "product_a.md"), "cup")
        kb.wait(d["id"])
        assert emb.calls >= 1 and kb.retriever.stats()["vector"]["vectors"] == kb.store.get_doc(d["id"])["chunks"]
        r = kb.search("这个保温杯多少钱", "cup")
        assert r["enough"] and "99 元" in top(r)["text"] and top(r)["backend"].startswith("hybrid")
        calls = emb.calls
        kb.switch_backend()                                       # 重建只补缺的向量，不重复计费
        assert emb.calls == calls
    finally:
        kb.close()
    mock_cfg["knowledge"]["embedding"]["enabled"] = False
    kb = KnowledgeBase(mock_cfg, warmup=False, product_lookup=PRODUCTS.get)
    try:
        assert "向量" in (kb.status()["warning"] or "")
        d = kb.ingest_path(os.path.join(FX, "product_a.md"), "cup")
        kb.wait(d["id"])
        assert "99 元" in top(kb.search("多少钱", "cup"))["text"]      # 向量不可用时等同关键词检索
    finally:
        kb.close()


def test_restart_requeues_unfinished(mock_cfg):
    kb = KnowledgeBase(mock_cfg, warmup=False)
    d = kb.ingest_path(os.path.join(FX, "product_a.md"))
    kb.wait(d["id"])
    kb.store.update_doc(d["id"], status="indexing")               # 模拟入库做到一半服务重启
    kb.close()
    kb2 = KnowledgeBase(mock_cfg, warmup=False)
    try:
        assert wait_for(lambda: kb2.store.get_doc(d["id"])["status"] == "ready")
        assert kb2.search("到手价", None)["hits"]
    finally:
        kb2.close()
