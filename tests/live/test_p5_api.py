"""P5：知识库 / 产品 / 讲品接口（TestClient，不起 worker、不调计费接口；mock 配置下 LLM 未配置 → 走资料原句与模板）。"""
import os
import time

from fakes import wait_for

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FX = os.path.join(ROOT, "tests", "fixtures", "kb")


def upload(c, names, product_id=""):
    files = [("files", (n, open(os.path.join(FX, n), "rb").read())) for n in names]
    r = c.post("/api/kb/docs", files=files, data={"product_id": product_id})
    assert r.status_code == 200, r.text
    return r.json()


def wait_ready(c, ids):
    assert wait_for(lambda: all(c.get(f"/api/kb/docs/{i}").json()["status"] == "ready" for i in ids), 15)


def test_kb_upload_search_answer_delete(make_client):
    c, ctx = make_client()
    cup = c.post("/api/products", json={"name": "暖暖保温杯 500ml", "price_text": "到手价 99 元"}).json()
    tee = c.post("/api/products", json={"name": "清风纯棉T恤"}).json()
    docs = upload(c, ["product_a.md"], cup["id"]) + upload(c, ["product_b.html"], tee["id"]) + upload(c, ["policy.pdf"])
    wait_ready(c, [d["id"] for d in docs])
    assert all(not os.path.exists(os.path.join(ctx.kb.store.files_dir, d["name"])) for d in docs)   # 存盘名是随机 ID
    lst = c.get("/api/kb/docs").json()
    assert {d["name"] for d in lst} == {"product_a.md", "product_b.html", "policy.pdf"}
    assert [d["name"] for d in c.get("/api/kb/docs", params={"product_id": ""}).json()] == ["policy.pdf"]
    assert c.get(f"/api/kb/docs/{docs[0]['id']}", params={"chunks": True}).json()["chunk_list"]

    r = c.get("/api/kb/search", params={"q": "多少钱", "product_id": cup["id"]}).json()
    assert r["enough"] and "99 元" in r["hits"][0]["text"] and "_hits" not in r
    asks = [("这个保温杯多少钱", cup, "99 元"), ("咖啡保质期多久", cup, "12 个月"), ("能放微波炉吗", cup, "微波炉"),
            ("我165穿什么码", tee, "M 码"), ("多久发货", tee, "48 小时")]
    for q, p, fact in asks:                                        # 回答测试：5 个问题，答案都来自资料
        a = c.post("/api/kb/answer", json={"question": q, "product_id": p["id"], "nickname": "王阿姨", "gender": "female"}).json()
        assert a["source"] == "knowledge" and fact in a["text"] and a["refs"], (q, a)
    a = c.post("/api/kb/answer", json={"question": "敏感肌能用吗", "product_id": cup["id"]}).json()
    assert "帮您确认" in a["text"]

    c.put(f"/api/kb/docs/{docs[2]['id']}", json={"product_id": tee["id"]})           # 公共资料改挂到 T 恤
    assert c.get(f"/api/kb/docs/{docs[2]['id']}").json()["product_id"] == tee["id"]
    assert not c.get("/api/kb/search", params={"q": "多久发货", "product_id": cup["id"]}).json()["enough"]
    assert c.delete(f"/api/kb/docs/{docs[0]['id']}").json()["ok"]
    assert c.delete(f"/api/kb/docs/{docs[0]['id']}").status_code == 404
    assert not c.get("/api/kb/search", params={"q": "这个保温杯多少钱", "product_id": cup["id"]}).json()["enough"]
    assert c.post("/api/kb/reindex", json={}).json()["queued"]
    st = c.get("/api/kb/status").json()
    assert st["active_backend"] == "fts" and st["docs"] == 2
    kinds = [m["data"]["status"] for m in ctx.feed.recent("kb.ingest") if m["data"].get("kind") == "doc"]
    assert "ready" in kinds and "deleted" in kinds                 # 入库进度推到 /ws/console
    assert c.get("/api/system").json()["services"]["knowledge"]["docs"] == 2


def test_kb_upload_rejects(make_client):
    c, ctx = make_client()
    r = c.post("/api/kb/docs", files=[("files", ("x.exe", b"MZ"))], data={"product_id": ""})
    assert r.status_code == 400 and "不支持" in r.json()["detail"]
    r = c.post("/api/kb/docs", files=[("files", ("a.md", b"# a\n\nb"))], data={"product_id": "nope"})
    assert r.status_code == 404
    ok = c.post("/api/kb/docs", files=[("files", ("ok.md", "# 标题\n\n正文一句。".encode())), ("files", ("bad.zip", b"PK"))],
                data={"product_id": ""}).json()
    assert ok[0]["status"] == "queued" and ok[1]["status"] == "rejected"    # 多文件：坏的单独报错，好的照常入库
    assert c.get("/api/kb/search", params={"q": " "}).status_code == 400


def test_products_script_and_host(make_client):
    c, ctx = make_client()
    r = c.post("/api/products", json={"name": ""})
    assert r.status_code == 400
    p = c.post("/api/products", json={"name": "暖暖保温杯 500ml", "price_text": "到手价 99 元", "selling_points": "双层真空\n倒置不漏"}).json()
    q = c.post("/api/products", json={"name": "清风纯棉T恤", "duration_s": 120}).json()
    assert p["selling_points"] == ["双层真空", "倒置不漏"] and p["order"] == 0 and q["order"] == 1
    assert c.post("/api/host/script/next").status_code == 409                  # 还没有讲解稿
    d = upload(c, ["product_a.md"], p["id"])[0]
    wait_ready(c, [d["id"]])
    g = c.post(f"/api/products/{p['id']}/script/generate", json={"segments": 4}).json()
    assert g["source"] == "template" and g["generated"] == 4 and g["error"]       # 没配 LLM：模板草稿
    prod = g["product"]
    assert prod["playable"] == 0 and all(not s["confirmed"] for s in prod["segments"])
    assert not c.get("/api/host/status").json()["script_active"]              # 待确认的不播
    prod = c.post(f"/api/products/{p['id']}/script/confirm", json={}).json()
    assert prod["playable"] == 4
    segs = prod["segments"]
    segs[1]["text"] = "人工改写：{product}的保温效果很好，最好的选择！"
    prod = c.put(f"/api/products/{p['id']}/script", json={"segments": segs}).json()
    assert prod["segments"][1]["source"] == "manual"
    st = c.get("/api/host/status").json()
    assert st["script_active"] and st["script"]["product_name"] == "暖暖保温杯 500ml" and st["script"]["segments"] == 4
    assert c.post("/api/host/script/next").json()["script"]["segment"] == 2
    assert c.post(f"/api/host/product/{q['id']}/activate").json()["script"]["product_name"] == "清风纯棉T恤"
    assert c.get("/api/products").json()["active_id"] == q["id"]
    assert ctx.host_context()["product_name"] == "清风纯棉T恤"                  # 话术 {product} 跟着当前产品
    assert c.post("/api/host/product/next").json()["script"]["product_name"] == "暖暖保温杯 500ml"
    assert c.post("/api/host/product/nope/activate").status_code == 404
    lst = c.get("/api/products").json()
    assert [x["name"] for x in lst["items"]] == ["暖暖保温杯 500ml", "清风纯棉T恤"] and lst["items"][0]["is_current"]
    c.put("/api/products/order", json={"ids": [q["id"], p["id"]]})
    assert [x["name"] for x in c.get("/api/products").json()["items"]] == ["清风纯棉T恤", "暖暖保温杯 500ml"]
    assert c.put(f"/api/products/{q['id']}", json={"duration_s": None}).json()["duration_s"] is None
    assert c.put(f"/api/products/{q['id']}", json={"duration_s": 3}).status_code == 400
    det = c.get(f"/api/products/{p['id']}").json()
    assert det["docs"] == 1 and det["doc_list"][0]["name"] == "product_a.md"
    r = c.delete(f"/api/products/{p['id']}").json()
    assert r["deleted_docs"] == 1 and c.get("/api/kb/docs").json() == []          # 删产品连同它的资料
    assert c.get(f"/api/products/{p['id']}").status_code == 404


def test_settings_schema_has_p5_groups(make_client):
    c, ctx = make_client()
    keys = {f["key"]: f for f in c.get("/api/settings").json()["fields"]}
    for k in ("knowledge.backend", "knowledge.top_k", "knowledge.min_coverage", "knowledge.chunk_chars", "products.rotation_enabled",
              "products.duration_s", "products.segment_gap_s", "products.require_confirmed_script",
              "host.use_llm", "host.reply_max_chars", "host.answer_max_chars"):
        assert k in keys and keys[k]["apply"] == "hot", k
    assert keys["personas.default"]["apply"] == "restart"
    assert not any(k.startswith("persona.") and k != "personas.default" for k in keys)   # 人设内容在模板里，不在全局设置
    assert keys["knowledge.backend"]["group"] == "knowledge"
    r = c.put("/api/settings", json={"changes": {"knowledge.backend": "bm25"}}).json()
    assert r["ok"] and "knowledge.backend" in r["applied"]
    assert wait_for(lambda: ctx.kb.retriever.name == "bm25", 5)                 # 热切换检索后端
    time.sleep(0.1)
