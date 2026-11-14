"""P9：人设模板——模板读写、切换、意图/触发器、节目单与知识库隔离、旧数据迁移。"""
import json
import os
import random
import time

import pytest

from live.config import load_config
from live.core.bus import EventBus
from live.core.clock import FakeClock
from live.core.events import LiveEvent, ViewerProfile
from live.interaction.cue import Cue
from live.interaction.director import Director
from live.interaction.engine import InteractionEngine
from live.interaction.memory import ViewerMemory
from live.interaction.queue import CueQueue
from live.personas import models
from live.personas.service import PersonaBusy, PersonaInvalid, PersonaService
from live.personas.store import PersonaStore, builtin_ids, load_builtin

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- 模板本身
def test_builtins_load_and_validate():
    assert {"commerce", "explainer", "entertainment"} <= set(builtin_ids())
    for pid in builtin_ids():
        p = load_builtin(pid)
        assert p["id"] == pid and p["builtin"] is True
        assert models.validate(p) == {}, pid
        assert p["prompt"] and p["triggers"] and p["intents"]
        # 平台信号 + 本模板的意图名都能被触发器引用
        assert {"gift_s", "has_new_member", "viewer_surge"} <= models.signals_of(p)
        assert {i["name"] for i in p["intents"]} <= models.signals_of(p)
    commerce = load_builtin("commerce")
    assert {"product_question", "purchase_intent"} <= {i["name"] for i in commerce["intents"]}
    entertainment = load_builtin("entertainment")
    assert "joke_request" in {i["name"] for i in entertainment["intents"]}
    assert "purchase_intent" not in {i["name"] for i in entertainment["intents"]}


def test_validate_rejects_reserved_and_bad_values():
    p = load_builtin("commerce")
    bad = {**p, "intents": [*p["intents"], {"name": "gift", "keywords": ["x"], "qa": "off", "window": "chat_s"}]}
    assert "gift" in models.validate(models.normalize(bad))["intents[2]"]
    bad = {**p, "intents": [*p["intents"], {"name": "ok_intent", "keywords": [], "qa": "off"}]}
    assert "至少要有一个关键词" in models.validate(models.normalize(bad))["intents[2]"]
    bad = {**p, "limits": {"reply_max_chars": "abc", "answer_max_chars": None}}
    assert "limits.reply_max_chars" in models.validate(models.normalize(bad))
    bad = {**p, "knowledge": {"no_answer_text": ""}}
    assert "knowledge.no_answer_text" in models.validate(bad)


def test_store_user_template_roundtrip(tmp_path):
    st = PersonaStore(str(tmp_path))
    pid = st.new_id()
    p = load_builtin("explainer")
    p.update(id=pid, builtin=False, source="explainer", name="我的讲解")
    st.save(p)
    got = st.load(pid)
    assert got["id"] == pid and got["name"] == "我的讲解" and got["source"] == "explainer"
    assert got["program"]["item_label"] == "讲解主题"
    assert pid in st.user_ids() and pid in st.ids()
    with pytest.raises(PermissionError):
        st.save(load_builtin("commerce"))
    with pytest.raises(PermissionError):
        st.delete("commerce")
    st.delete(pid)
    assert pid not in st.ids()


# ---------------------------------------------------------------- 服务：切换 / 复制 / 编辑
def test_service_activate_duplicate_edit(tmp_path):
    cfg = load_config(os.path.join(ROOT, "live", "configs", "mock.yaml"),
                      {"server": {"data_dir": str(tmp_path)}})
    svc = PersonaService(cfg, EventBus())
    assert svc.active_id == "commerce"
    svc.activate("entertainment")
    assert cfg["persona"]["id"] == "entertainment" and cfg["persona"]["name"] == "娱乐主播"
    assert json.load(open(os.path.join(tmp_path, "personas", "active.json"), encoding="utf-8"))["active"] == "entertainment"

    np = svc.duplicate("explainer", "测试讲解")
    assert np["builtin"] is False and np["source"] == "explainer" and np["program"]["item_label"] == "讲解主题"
    with pytest.raises(PermissionError):
        svc.update("explainer", {"name": "改出厂"})
    intents = [dict(x) for x in np["intents"]]
    intents[0]["keywords"] = ["是什么"]
    upd = svc.update(np["id"], {"name": "测试讲解 2", "anchor_name": "小测", "intents": intents})
    assert upd["name"] == "测试讲解 2" and upd["anchor_name"] == "小测"
    assert upd["intents"][0]["keywords"] == ["是什么"]
    with pytest.raises(PersonaInvalid):
        svc.update(np["id"], {"intents": [{"name": "bad name", "keywords": ["x"], "qa": "off"}]})
    svc.activate(np["id"])
    with pytest.raises(PersonaBusy):
        svc.delete(np["id"])


def test_service_trigger_add_edit_delete(tmp_path):
    cfg = load_config(os.path.join(ROOT, "live", "configs", "mock.yaml"),
                      {"server": {"data_dir": str(tmp_path)}})
    svc = PersonaService(cfg, EventBus())
    p = svc.duplicate("commerce", "触发器测试")
    pid = p["id"]
    svc.add_trigger(pid, "custom_follow", {"when": "has_new_follow", "priority": 41, "lines": ["谢啦{follow_user}"]})
    assert svc.get(pid)["triggers"]["custom_follow"]["priority"] == 41
    with pytest.raises(PersonaInvalid):
        svc.add_trigger(pid, "bad name", {"when": "has_new_follow"})
    svc.update_trigger(pid, "custom_follow", {"priority": 42, "when": "has_new_follow"})
    assert svc.get(pid)["triggers"]["custom_follow"]["priority"] == 42
    svc.delete_trigger(pid, "custom_follow")
    assert "custom_follow" not in svc.get(pid)["triggers"]
    with pytest.raises(PersonaInvalid):
        svc.update_trigger(pid, "follow", {"when": "not_a_signal"})


# ---------------------------------------------------------------- 意图与导演：没有带货残留
class Room:
    def __init__(self, cfg, path):
        cfg["interaction"]["memory"]["record_test_events"] = True
        for name in ("idle", "idle_interact"):
            cfg["persona"]["triggers"].setdefault(name, {})["enabled"] = False
        self.cfg = cfg
        self.clock = FakeClock(1_000_000.0)
        self.bus = EventBus()
        self.mem = ViewerMemory(cfg, path=str(path), clock=self.clock)
        self.queue = CueQueue(cfg, self.clock)
        self.director = Director(cfg, self.clock, rng=random.Random(3))
        self.engine = InteractionEngine(cfg, self.bus, self.queue, self.mem, clock=self.clock,
                                        director=self.director, start_thread=False)
        self.cues = []
        self._n = 0

    def chat(self, nick, text):
        self._n += 1
        ev = LiveEvent(type="chat", platform="mock", room_id="r", ts=self.clock.now(), msg_id=f"m{self._n}",
                       user=ViewerProfile(user_id=f"u{nick}", nickname=nick, gender="female"), data={"content": text})
        self.engine.on_event(ev)
        while True:
            c = self.queue.get(timeout=0)
            if c is None:
                break
            self.cues.append(c)
        return self.cues[-1] if self.cues else None


def test_entertainment_intents_and_no_commerce_residue(mock_cfg, tmp_path):
    mock_cfg["persona"] = load_builtin("entertainment")
    room = Room(mock_cfg, tmp_path / "v.json")
    c = room.chat("小李", "讲个笑话")
    assert c is not None and c.event == "joke_request" and c.intent == "joke_request"
    assert c.qa == "off" and c.fallback_text
    n = len(room.cues)
    c = room.chat("小王", "这个多少钱")
    assert c is not None and c.event == "knowledge_question" and c.qa == "context"
    assert all(x.event not in ("product_question", "purchase_intent") for x in room.cues[n:])
    room.chat("小张", "加微信领红包")
    assert len(room.cues) == n + 1                      # 垃圾弹幕不回应


def test_custom_intent_first_come_consumes(mock_cfg, tmp_path):
    p = load_builtin("explainer")
    p["intents"] = [{"name": "topic_question", "label": "主题提问", "keywords": ["是什么", "为什么"],
                     "qa": "strict", "first_come": True, "window": "question_s", "search_suffix": ""}]
    p["triggers"] = {k: v for k, v in p["triggers"].items() if k in ("topic_question", "idle")}
    p["triggers"]["topic_question"]["priority"] = 80
    p["triggers"]["topic_question"]["cooldown_s"] = 0
    mock_cfg["persona"] = p
    room = Room(mock_cfg, tmp_path / "v.json")
    c1 = room.chat("甲", "这个是什么")
    c2 = room.chat("乙", "为什么这样")
    assert [x.event for x in (c1, c2)] == ["topic_question", "topic_question"]
    assert c1.question == "这个是什么" and c2.question == "为什么这样"


# ---------------------------------------------------------------- API：切换、隔离、忙时拒绝
def test_persona_api_and_isolation(make_client):
    c, ctx = make_client()
    r = c.get("/api/personas")
    assert r.status_code == 200 and {x["id"] for x in r.json()["items"]} >= {"commerce", "explainer", "entertainment"}
    assert r.json()["active_id"] == "commerce"

    # 出厂模板只读
    assert c.put("/api/personas/commerce", json={"name": "x"}).status_code == 403

    # 出厂人设也能管理自己的节目单和知识库
    p = c.post("/api/products", json={"name": "保温杯"}).json()
    doc = c.post("/api/kb/docs", files={"files": ("a.md", "# 价格\n\n到手价 99 元。".encode())},
                 data={"product_id": p["id"]}).json()[0]
    assert wait_for(lambda: c.get(f"/api/kb/docs/{doc['id']}").json()["status"] == "ready", 10)
    assert c.get("/api/products").json()["items"] and c.get("/api/kb/status").json()["docs"] == 1

    # 复制 + 切到讲解人设：节目单和知识库都应该是空的
    made = c.post("/api/personas/explainer/duplicate", json={"name": "我的讲解"}).json()
    assert made["program_copied"] is False
    assert c.post(f"/api/personas/{made['id']}/activate").status_code == 200
    assert ctx.cfg["persona"]["id"] == made["id"]
    assert c.get("/api/products").json()["items"] == []
    assert c.get("/api/kb/docs").json() == [] and c.get("/api/kb/status").json()["docs"] == 0
    assert ctx.kb.root.endswith(os.path.join("kb", made["id"]))
    assert ctx.products.path.endswith(os.path.join("programs", f"{made['id']}.json"))

    # 切回带货：原来的节目和资料还在
    assert c.post("/api/personas/commerce/activate").status_code == 200
    assert [x["name"] for x in c.get("/api/products").json()["items"]] == ["保温杯"]
    assert c.get("/api/kb/status").json()["docs"] == 1

    # 触发器：persona 作用域读取 / 新增 / 删除
    pid = made["id"]
    trs = c.get(f"/api/personas/{pid}/triggers")
    assert trs.status_code == 200 and trs.json()
    assert c.post(f"/api/personas/{pid}/triggers",
                  json={"name": "custom", "when": "has_new_follow", "priority": 40, "lines": ["谢啦"]}).status_code == 200
    assert any(t["name"] == "custom" for t in c.get(f"/api/personas/{pid}/triggers").json())
    assert c.delete(f"/api/personas/{pid}/triggers/custom").status_code == 200
    # 当前人设不能删；校验错误返回 400 + errors
    assert c.post(f"/api/personas/{pid}/activate").status_code == 200
    assert c.delete(f"/api/personas/{pid}").status_code == 409
    bad = c.put(f"/api/personas/{pid}", json={"intents": [{"name": "bad name", "keywords": ["x"], "qa": "off"}]})
    assert bad.status_code == 400 and bad.json()["errors"]
    assert c.post("/api/personas/commerce/activate").status_code == 200


def test_switch_blocked_while_live(make_client):
    c, ctx = make_client()
    ctx.sessions.list = lambda: [{"session_id": "default", "state": "running"}]
    r = c.post("/api/personas/explainer/activate")
    assert r.status_code == 409 and "正在直播" in r.json()["detail"]
    ctx.sessions.list = lambda: []
    assert c.post("/api/personas/explainer/activate").status_code == 200


def test_switch_clears_residue(make_client):
    c, ctx = make_client()
    # 先造一点旧人设的运行时状态：一个排队的 cue、决策日志、LLM 最近台词、导演能量
    c.post("/api/danmaku/simulate", json={"type": "gift", "nickname": "小王", "gift_name": "玫瑰",
                                          "unit_diamonds": 1, "count": 3})
    assert wait_for(lambda: ctx.cue_queue.snapshot() or ctx.engine.recent_log())
    ctx.llm.note_spoken("旧人设的台词")
    ctx.engine.director.energy = 0.9
    # 停播（会话列表为空）+ 弹幕未连接
    ctx.sessions.list = lambda: []
    ctx.danmaku.status = lambda: {"state": "stopped", "enabled": False}
    made = c.post("/api/personas/entertainment/duplicate", json={"name": "娱乐副本"}).json()
    assert c.post(f"/api/personas/{made['id']}/activate").status_code == 200
    assert ctx.cue_queue.snapshot() == []
    assert ctx.engine.recent_log() == []
    assert list(ctx.llm.history) == []
    assert ctx.engine.director.energy == pytest.approx(0.25)
    assert ctx.engine.director.trigger("product_question") is None        # 旧触发器没了
    assert ctx.engine.director.trigger("joke_request") is not None        # 新触发器在
    assert ctx.cfg["persona"]["id"] == made["id"]


# ---------------------------------------------------------------- 迁移
def test_migrate_legacy_settings_products_and_kb(tmp_path):
    data = tmp_path / "live_data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({
        "version": 7, "saved_at": 1,
        "overrides": {
            "persona.anchor_name": "老美", "persona.prompt": "旧人设提示词",
            "interaction.triggers": {"follow": {"priority": 66, "lines": ["多谢{follow_user}！"]}},
            "interaction.intent.product_keywords": ["多少钱", "香不香"],
            "knowledge.no_answer_text": "{asker}，旧兜底。",
            "host.use_llm": False,
        },
    }, ensure_ascii=False), encoding="utf-8")
    (data / "products.json").write_text(json.dumps({
        "version": 1, "active_id": "p1",
        "products": [{"id": "p1", "name": "旧商品", "segments": [], "enabled": True, "order": 0}],
    }, ensure_ascii=False), encoding="utf-8")
    kb = data / "kb"
    (kb / "files").mkdir(parents=True)
    (kb / "kb.sqlite").write_bytes(b"not-a-real-db")
    (kb / "files" / "doc.md").write_text("旧资料", encoding="utf-8")

    cfg = load_config(os.path.join(ROOT, "live", "configs", "mock.yaml"), {"server": {"data_dir": str(data)}})
    svc = PersonaService(cfg, EventBus())
    pid = svc.active_id
    assert pid.startswith("u") and cfg["persona"]["name"] == "带货主播（迁移）"
    assert cfg["persona"]["anchor_name"] == "老美" and cfg["persona"]["prompt"] == "旧人设提示词"
    assert cfg["persona"]["triggers"]["follow"]["priority"] == 66
    assert "香不香" in next(i for i in cfg["persona"]["intents"] if i["name"] == "product_question")["keywords"]
    assert cfg["persona"]["knowledge"]["no_answer_text"] == "{asker}，旧兜底。"

    raw = json.loads((data / "settings.json").read_text(encoding="utf-8"))
    assert "persona.anchor_name" not in raw["overrides"] and "interaction.triggers" not in raw["overrides"]
    assert raw["overrides"]["host.use_llm"] is False                      # 其他覆盖原样保留
    assert (data / "settings.json.pre-persona.bak").exists()
    assert json.loads((data / "programs" / f"{pid}.json").read_text(encoding="utf-8"))["products"][0]["name"] == "旧商品"
    assert (data / "kb" / pid / "kb.sqlite").exists() and (data / "kb" / pid / "files" / "doc.md").exists()
    assert not (data / "kb" / "kb.sqlite").exists()
    assert json.loads((data / "personas" / "active.json").read_text(encoding="utf-8"))["active"] == pid

    # 幂等：再次启动不会重复迁移
    svc2 = PersonaService(cfg, EventBus())
    assert svc2.active_id == pid and cfg["persona"]["name"] == "带货主播（迁移）"
    assert not (data / "products.json").exists()
