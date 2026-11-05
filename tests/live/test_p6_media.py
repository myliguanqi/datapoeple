"""P6：素材库（ffmpeg 现场生成几秒的测试视频，不入库）、场景状态机（假时钟）、主持人在带原声视频期间不讲品、讲品视频节点、接口。"""
import os
import shutil
import subprocess

import pytest

from live.core.bus import EventBus
from live.core.clock import FakeClock
from live.host.composer import Composer
from live.host.host import Host
from live.interaction.compliance import Compliance
from live.interaction.cue import Cue
from live.interaction.queue import CueQueue
from live.media.scene import SceneManager
from live.media.store import MediaRejected, MediaStore
from live.products.script import ProductScript
from live.products.store import ProductStore

from fakes import FakeSession, Sessions, wait_for

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="需要 ffmpeg/ffprobe")


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True, timeout=60)


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    out = {"sound": str(d / "sound.mp4"), "mute": str(d / "mute.mp4"), "image": str(d / "bg.png")}
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
            "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out["sound"])
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=240x320:rate=25", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", out["mute"])
    _ffmpeg("-f", "lavfi", "-i", "color=c=0x223355:size=360x640", "-frames:v", "1", out["image"])
    return out


def read(p):
    with open(p, "rb") as f:
        return f.read()


# ---------------------------------------------------------------- 素材库
def test_media_store_probe_cover_and_rejects(mock_cfg, clips):
    st = MediaStore(mock_cfg)
    v = st.add_bytes("产品介绍.mp4", read(clips["sound"]))
    assert v["kind"] == "video" and v["has_audio"] and v["width"] == 320 and 2.5 <= v["duration_s"] <= 3.5
    assert st.cover_path(v) and os.path.basename(st.file_path(v)) == v["id"] + ".mp4"       # 文件名是随机 ID
    m = st.add_bytes("mute.mp4", read(clips["mute"]))
    assert m["kind"] == "video" and not m["has_audio"]
    img = st.add_bytes("背景.png", read(clips["image"]))
    assert img["kind"] == "image" and img["duration_s"] is None and img["height"] == 640
    with pytest.raises(MediaRejected, match="不支持"):
        st.add_bytes("a.txt", b"hello")
    with pytest.raises(MediaRejected, match="无法识别"):
        st.add_bytes("fake.mp4", b"not a video at all" * 100)
    mock_cfg["scene"]["media_max_mb"] = 1
    with pytest.raises(MediaRejected, match="太大"):
        st.add_bytes("big.mp4", b"\0" * (1024 * 1024 + 1))
    assert not [n for n in os.listdir(st.dir) if n.startswith(".upload-")]                  # 被拒的临时文件已删
    st2 = MediaStore(mock_cfg)                                                               # 重启后索引还在
    assert {x["name"] for x in st2.list()} == {"产品介绍.mp4", "mute.mp4", "背景.png"} and len(st2.list("video")) == 2
    st2.update(v["id"], {"name": "改名.mp4", "tags": ["投流", " "]})
    assert st2.get(v["id"])["name"] == "改名.mp4" and st2.get(v["id"])["tags"] == ["投流"]
    st2.delete(v["id"])
    assert st2.get(v["id"]) is None and not os.path.exists(st2.file_path(v)) and not st2.cover_path(v)


# ---------------------------------------------------------------- 场景状态机
@pytest.fixture
def scene_rig(mock_cfg, clips):
    clock = FakeClock()
    media = MediaStore(mock_cfg)
    sound = media.add_bytes("sound.mp4", read(clips["sound"]))
    mute = media.add_bytes("mute.mp4", read(clips["mute"]))
    img = media.add_bytes("bg.png", read(clips["image"]))
    pushed = []
    bus = EventBus()
    prod = {"p": None}
    sm = SceneManager(mock_cfg, bus, media, product_info=lambda: prod["p"], push=lambda sid, ev: pushed.append((sid, ev)),
                      clock=clock, start_thread=False)
    yield sm, clock, pushed, prod, {"sound": sound, "mute": mute, "img": img}, media
    bus.close()


def test_scene_layouts_and_video_lifecycle(scene_rig, mock_cfg):
    sm, clock, pushed, prod, m, media = scene_rig
    s = sm.snapshot("default")
    assert s["layout"] == "dh_full" and s["canvas"]["width"] == 720 and s["canvas"]["height"] == 1280 and s["video"] is None
    assert s["disclosure"] == "本直播间为 AI 数字人直播" and s["subtitle"]["enabled"]
    prod["p"] = {"id": "p1", "name": "暖暖保温杯", "price_text": "到手价 99 元", "selling_points": ["双层真空", "不漏水", "轻", "第四条"]}
    sm.on_products_changed()
    assert pushed[-1][1]["type"] == "scene" and pushed[-1][1]["scene"]["layout"] == "dh_with_card"
    assert pushed[-1][1]["scene"]["card"]["product"]["selling_points"] == ["双层真空", "不漏水", "轻"]
    s = sm.play_video("default", m["sound"]["id"], "pip", "video")
    v = s["video"]
    assert s["layout"] == "video_pip" and s["blocks_script"] and v["url"] == f"/api/media/{m['sound']['id']}/file"
    assert pushed[-1][1]["reason"] == "video_started" and sm.blocks_script("default")
    assert not sm.page_ended("default", "wrong-play-id")                       # play_id 对不上：忽略
    assert sm.page_ended("default", v["play_id"])
    assert sm.snapshot("default")["layout"] == "dh_with_card" and pushed[-1][1]["reason"] == "video_ended"
    assert not sm.page_ended("default", v["play_id"])                         # 第二个场景页的回传：已经结束了
    s = sm.play_video("default", m["sound"]["id"], "full", "mute")
    assert s["layout"] == "video_full" and not s["blocks_script"]              # 静音视频不挡讲品
    clock.advance(2.0)
    sm.tick()
    assert sm.snapshot("default")["video"] is not None
    clock.advance(3.0)                                                         # 超过时长 + 1.5s 兜底
    sm.tick()
    assert sm.snapshot("default")["video"] is None and pushed[-1][1]["reason"] == "video_ended"
    sm.play_video("default", m["mute"]["id"], "split", "video", loop=True)
    assert sm.snapshot("default")["layout"] == "split" and not sm.blocks_script("default")   # 选了原声但文件没有音轨
    clock.advance(60)
    sm.tick()
    assert sm.snapshot("default")["video"] is not None                         # 循环视频不自动结束
    assert sm.stop_video("default")["video"] is None and sm.stop_video("default") is None
    with pytest.raises(ValueError):
        sm.play_video("default", m["img"]["id"])
    with pytest.raises(ValueError):
        sm.play_video("default", m["sound"]["id"], "corner")
    with pytest.raises(KeyError):
        sm.play_video("default", "nope")


def test_scene_settings_card_background_and_delete(scene_rig, mock_cfg):
    sm, clock, pushed, prod, m, media = scene_rig
    prod["p"] = {"id": "p1", "name": "杯子"}
    assert sm.patch("default", card_visible=False)["layout"] == "dh_full"
    assert sm.patch("default", card_visible=True, subtitle=False)["subtitle"]["enabled"] is False
    mock_cfg["scene"]["background"] = f"media:{m['img']['id']}"
    mock_cfg["scene"]["width"], mock_cfg["scene"]["height"] = 1080, 1920
    mock_cfg["compliance"]["ai_disclosure"] = "  "
    n = len(pushed)
    sm.apply_settings(None, {"scene.width"})
    s = pushed[-1][1]["scene"]
    assert len(pushed) == n + 1 and s["canvas"]["background"] == {"image": f"/api/media/{m['img']['id']}/file"}
    assert s["canvas"]["width"] == 1080 and s["disclosure"] == "本直播间为 AI 数字人直播"   # AI 标识不能被清空
    mock_cfg["scene"]["background"] = "#102030"
    assert sm.snapshot("default")["canvas"]["background"] == {"color": "#102030"}
    sm.play_video("default", m["sound"]["id"])
    assert sm.snapshot("default")["layout"] == "video_pip"                    # 默认 scene.video.default_mode = pip
    sm.on_media_deleted(m["sound"]["id"])
    assert sm.snapshot("default")["video"] is None and pushed[-1][1]["reason"] == "video_deleted"


# ---------------------------------------------------------------- 主持人 + 视频
def segs(*texts):
    return [{"text": t, "confirmed": True} for t in texts]


def test_host_pauses_script_during_video_with_sound(mock_cfg, clips, data_dir):
    bus = EventBus()
    sess = FakeSession(bus)
    media = MediaStore(mock_cfg)
    sound = media.add_bytes("sound.mp4", read(clips["sound"]))
    mute = media.add_bytes("mute.mp4", read(clips["mute"]))
    store = ProductStore(os.path.join(data_dir, "products.json"))
    store.create({"name": "杯子", "segments": segs("第一段讲品。", "第二段讲品。")})
    mock_cfg["products"]["segment_gap_s"] = 0
    script = ProductScript(mock_cfg, store, video_lookup=media.get)
    sm = SceneManager(mock_cfg, bus, media, product_info=script.current_info, start_thread=False)
    q = CueQueue(mock_cfg)
    host = Host(mock_cfg, bus, Sessions(sess), q, Composer(mock_cfg, None, Compliance(mock_cfg)), script=script, scenes=sm)
    try:
        assert wait_for(lambda: len(sess.order) == 1 and sess.closed("r1")) and sess.text("r1") == "第一段讲品。"
        sm.play_video("default", sound["id"], "pip", "video")
        assert wait_for(lambda: sess.reqs["r1"]["interrupted"])                 # 带原声的视频一开播，讲品段让路
        assert host.status()["video_blocking"] and not host.script_active()
        q.push(Cue(event="gift_m", priority=60, fallback_text="感谢小王哥的玫瑰！"))
        assert wait_for(lambda: len(sess.order) == 2 and sess.closed("r2")) and sess.text("r2") == "感谢小王哥的玫瑰！"  # 互动照常
        sess.finish("r2")
        assert not wait_for(lambda: len(sess.order) > 2, 0.6)                 # 视频期间不讲品
        sm.stop_video("default", "video_ended")
        assert wait_for(lambda: len(sess.order) == 3 and sess.closed("r3")) and sess.text("r3") == "第一段讲品。"   # 续讲
        sess.finish("r3")
        sm.play_video("default", mute["id"], "full", "mute")                  # 静音视频：讲品继续（数字人配解说）
        assert wait_for(lambda: len(sess.order) == 4 and sess.closed("r4")) and sess.text("r4") == "第二段讲品。"
    finally:
        host.stop()
        bus.close()


def test_script_video_slot_end_and_start(mock_cfg, clips, data_dir):
    clock = FakeClock()
    media = MediaStore(mock_cfg)
    v1 = media.add_bytes("v1.mp4", read(clips["sound"]))
    v2 = media.add_bytes("v2.mp4", read(clips["mute"]))
    store = ProductStore(os.path.join(data_dir, "products.json"))
    p = store.create({"name": "杯子", "segments": segs("一段。", "二段。"), "video_ids": [v1["id"], "missing", v2["id"]]})
    mock_cfg["products"]["segment_gap_s"] = 0
    sc = ProductScript(mock_cfg, store, clock, video_lookup=media.get)
    order = []
    for _ in range(7):
        c = sc.next_segment()
        order.append(c.situation["media_id"] if c.kind == "video" else c.fallback_text)
        sc.on_done(c)
    assert order == ["一段。", "二段。", v1["id"], "一段。", "二段。", v2["id"], "一段。"]   # 每讲完一轮插播一个，轮流；丢失的素材跳过
    mock_cfg["products"]["video_slot"] = "start"
    sc.activate(p.id)
    c = sc.next_segment()
    assert c.kind == "video" and c.situation["mode"] == "pip" and c.situation["audio"] == "video" and c.reason.startswith("讲品「杯子」插播视频")
    sc.on_interrupted(c)                                                     # 视频没播成也不反复重试
    assert sc.next_segment().fallback_text == "一段。"
    mock_cfg["products"]["video_slot"] = "off"
    sc.activate(p.id)
    assert sc.next_segment().kind == "product"


def test_host_plays_video_slot_through_scene(mock_cfg, clips, data_dir):
    bus = EventBus()
    sess = FakeSession(bus)
    media = MediaStore(mock_cfg)
    v = media.add_bytes("v.mp4", read(clips["sound"]))
    store = ProductStore(os.path.join(data_dir, "products.json"))
    store.create({"name": "杯子", "segments": segs("只有这一段。"), "video_ids": [v["id"]]})
    mock_cfg["products"]["segment_gap_s"] = 0
    script = ProductScript(mock_cfg, store, video_lookup=media.get)
    sm = SceneManager(mock_cfg, bus, media, product_info=script.current_info, start_thread=False)
    host = Host(mock_cfg, bus, Sessions(sess), CueQueue(mock_cfg), Composer(mock_cfg, None, Compliance(mock_cfg)), script=script,
                scenes=sm)
    try:
        assert wait_for(lambda: len(sess.order) == 1 and sess.closed("r1"))
        sess.finish("r1")
        assert wait_for(lambda: sm.snapshot("default")["video"] is not None)
        vid = sm.snapshot("default")["video"]
        assert vid["media_id"] == v["id"] and vid["source"] == "script" and len(sess.order) == 1    # 视频节点不开口
        sm.page_ended("default", vid["play_id"])
        assert wait_for(lambda: len(sess.order) == 2 and sess.closed("r2")) and sess.text("r2") == "只有这一段。"
    finally:
        host.stop()
        bus.close()


# ---------------------------------------------------------------- 接口
def test_media_and_scene_api(make_client, clips):
    c, ctx = make_client()
    p = c.post("/api/products", json={"name": "暖暖保温杯"}).json()
    r = c.post("/api/media", files=[("files", ("介绍.mp4", read(clips["sound"]))), ("files", ("bg.png", read(clips["image"]))),
                                    ("files", ("x.txt", b"x"))], data={"product_id": p["id"]})
    assert r.status_code == 200, r.text
    vid, img, bad = r.json()
    assert vid["kind"] == "video" and vid["product_ids"] == [p["id"]] and img["kind"] == "image" and bad["status"] == "rejected"
    assert c.get(f"/api/products/{p['id']}").json()["video_ids"] == [vid["id"]]           # 上传时带 product_id 直接关联
    f = c.get(vid["url"], headers={"Range": "bytes=0-99"})
    assert f.status_code == 206 and len(f.content) == 100 and f.headers["content-type"] == "video/mp4"   # 支持拖进度
    assert c.get(vid["cover_url"]).headers["content-type"] == "image/jpeg"
    assert [x["id"] for x in c.get("/api/media?kind=image").json()] == [img["id"]]
    q = c.post("/api/products", json={"name": "T恤"}).json()
    assert c.put(f"/api/media/{vid['id']}", json={"product_ids": [q["id"]], "name": "新名字.mp4"}).json()["product_ids"] == [q["id"]]
    assert c.get(f"/api/products/{p['id']}").json()["video_ids"] == []

    assert c.get("/scene").status_code == 200 and "scene.js" in c.get("/scene").text
    s = c.get("/api/sessions/default/scene").json()
    assert s["layout"] == "dh_with_card" and s["card"]["product"]["name"] == "暖暖保温杯"
    s = c.post("/api/sessions/default/scene/video", json={"media_id": vid["id"], "mode": "split", "audio": "video"}).json()
    assert s["layout"] == "split" and s["video"]["name"] == "新名字.mp4"
    assert ctx.host.status()["video_blocking"]
    msgs = [m["data"]["reason"] for m in ctx.feed.recent("scene.changed")]
    assert "video_started" in msgs                                                         # 推到 /ws/console
    assert c.post("/api/sessions/default/scene/video", json={"media_id": img["id"]}).status_code == 400
    assert c.post("/api/sessions/default/scene/video", json={"media_id": "nope"}).status_code == 404
    assert c.post("/api/sessions/default/scene/video/stop").json()["video"] is None
    assert c.put("/api/sessions/default/scene", json={"card_visible": False}).json()["layout"] == "dh_full"

    c.put("/api/settings", json={"changes": {"scene.background": f"media:{img['id']}"}})
    assert c.get(f"/api/media/{img['id']}").json()["is_background"]
    r = c.delete(f"/api/media/{vid['id']}")
    assert r.status_code == 409 and "T恤" in r.json()["detail"]                             # 被产品引用：先提示
    assert c.delete(f"/api/media/{vid['id']}?force=true").json()["unlinked_products"] == [q["id"]]
    assert c.get(f"/api/products/{q['id']}").json()["video_ids"] == []
    assert c.delete(f"/api/media/{img['id']}").status_code == 409
    assert c.delete(f"/api/media/{img['id']}?force=true").json()["background_cleared"]
    assert ctx.cfg["scene"]["background"] == "" and c.get("/api/media").json() == []


def test_scene_settings_validation(make_client):
    c, ctx = make_client()
    r = c.put("/api/settings", json={"changes": {"compliance.ai_disclosure": "  "}})
    assert r.status_code == 400 and "AI 标识" in r.json()["errors"]["compliance.ai_disclosure"]
    r = c.put("/api/settings", json={"changes": {"scene.pip_rect": {"x": 0, "y": 0, "w": 0}}})
    assert r.status_code == 400 and "scene.pip_rect" in r.json()["errors"]
    r = c.put("/api/settings", json={"changes": {"scene.ducking": 0.5, "scene.video.default_mode": "full"}})
    assert r.status_code == 200 and set(r.json()["applied"]) == {"scene.ducking", "scene.video.default_mode"}
    assert ctx.scenes.snapshot("default")["ducking"] == 0.5
