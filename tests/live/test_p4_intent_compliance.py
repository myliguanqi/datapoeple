"""P4：弹幕意图识别、合规过滤。"""
from live.interaction import intent as I
from live.interaction.compliance import Compliance
from live.llm.mock import MockLLM


def test_intent_rules(mock_cfg):
    c = I.IntentClassifier(mock_cfg)
    cases = {
        "这个多少钱": I.PRODUCT_QUESTION, "保质期多久啊": I.PRODUCT_QUESTION, "我165穿什么尺码": I.PRODUCT_QUESTION,
        "包邮吗": I.PRODUCT_QUESTION, "几号链接": I.PURCHASE_INTENT, "已下单": I.PURCHASE_INTENT, "拍了拍了": I.PURCHASE_INTENT,
        "主播好漂亮": I.CHITCHAT, "晚上好": I.CHITCHAT, "加微信领红包": I.SPAM, "VX私我": I.SPAM,
        "1111": I.NOISE, "？？？": I.NOISE, "": I.NOISE,
    }
    for text, kind in cases.items():
        assert c.classify(text).kind == kind, text
    assert c.classify("这个多少钱").matched == "多少钱"


def test_intent_keywords_hot_edit(mock_cfg):
    c = I.IntentClassifier(mock_cfg)
    assert c.classify("能不能便宜点儿啊主播").kind == I.PRODUCT_QUESTION
    assert c.classify("这个香不香").kind == I.CHITCHAT
    spec = next(i for i in mock_cfg["persona"]["intents"] if i["name"] == I.PRODUCT_QUESTION)
    spec["keywords"].append("香不香")                                         # 改人设关键词 → 立即生效
    assert c.classify("这个香不香").kind == I.PRODUCT_QUESTION


def test_intent_llm_classify_optional(mock_cfg):
    llm = MockLLM(default="product_question")
    c = I.IntentClassifier(mock_cfg, llm)
    assert c.classify("这玩意儿靠谱吗").kind == I.CHITCHAT and not llm.calls          # 默认不调 LLM
    mock_cfg["interaction"]["intent"]["llm_classify"] = True
    it = c.classify("这玩意儿靠谱吗")
    assert it.kind == I.PRODUCT_QUESTION and it.by == "llm" and len(llm.calls) == 1
    assert c.classify("哈哈哈哈").kind == I.CHITCHAT and len(llm.calls) == 1           # 不像问句不调


def test_compliance_replace_delete_and_allow(mock_cfg):
    cp = Compliance(mock_cfg)
    r = cp.filter("这是全网最低价，第一好用，百分百正品，加微信再便宜")
    assert r.text == "这是很划算价，很受欢迎好用，正品，再便宜"
    assert set(r.hits) >= {"全网最低", "第一", "百分百", "加微信"}
    ok = cp.filter("有好物第一时间告诉你，最近上新了，第一步先看尺码")
    assert ok.text == "有好物第一时间告诉你，最近上新了，第一步先看尺码" and ok.hits == []
    assert cp.filter("销量第一名").hits == ["第一"]                                # 排名用法照样拦
    assert cp.filter("加V私聊").hits == ["加v"]                                   # 不区分大小写


def test_compliance_block_on_hit(mock_cfg):
    mock_cfg["compliance"]["block_on_hit"] = True
    cp = Compliance(mock_cfg)
    r = cp.filter("这款是顶级好货")
    assert r.blocked and r.text == ""
    fb = cp.filter("这款是顶级好货", allow_block=False)                            # 兜底话术：只替换不拦截
    assert not fb.blocked and fb.text == "这款是高品质好货"


def test_compliance_disabled_and_editable(mock_cfg):
    cp = Compliance(mock_cfg)
    mock_cfg["compliance"]["banned_words"].append("秒杀")
    mock_cfg["compliance"]["replacements"]["秒杀"] = "限时优惠"
    assert cp.filter("今晚秒杀").text == "今晚限时优惠"
    mock_cfg["compliance"]["enabled"] = False
    assert cp.filter("全网最低").text == "全网最低"


def test_policy_lines_are_compliant():
    """所有出厂人设的出厂话术本身不能命中违禁词（否则过滤后会变得不通顺）。"""
    from live.config import load_config
    from live.personas.store import builtin_ids, load_builtin
    cfg = load_config(None)
    cp = Compliance(cfg)
    for pid in builtin_ids():
        for name, spec in load_builtin(pid)["triggers"].items():
            for line in list(spec.get("lines") or []) + [spec.get("merge_clause") or ""]:
                assert cp.filter(str(line)).hits == [], (pid, name, line)
