"""素材库：视频（mp4 / webm / mov / m4v）与图片（背景图：png / jpg / webp）。

上传：校验扩展名与大小（scene.media_max_mb）→ 存 live_data/media/<随机ID>.<ext>（防路径穿越）→ ffprobe 取时长/分辨率/有没有音轨
→ ffmpeg 截一张封面（视频）→ 写索引 live_data/media/media.json（原子替换）。ffprobe 认不出来的文件直接拒绝。
和产品的关联只存在产品上（Product.video_ids），这里不重复存。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
import time
import uuid

from loguru import logger

VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class MediaRejected(ValueError):
    pass


def ffprobe_path(ffmpeg: str) -> str:
    """ffmpeg 路径 → 同目录的 ffprobe（server.ffmpeg 写的是完整路径时也能找到）。"""
    d, base = os.path.split(ffmpeg or "ffmpeg")
    name = base.replace("ffmpeg", "ffprobe") if "ffmpeg" in base else "ffprobe"
    return os.path.join(d, name) if d else (shutil.which(name) or name)


def probe(path: str, ffmpeg: str = "ffmpeg", timeout: float = 20.0) -> dict:
    try:
        out = subprocess.run([ffprobe_path(ffmpeg), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                             capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MediaRejected(f"ffprobe 运行失败（{type(e).__name__}），检查 ffmpeg 是否在 PATH 或 server.ffmpeg") from None
    if out.returncode != 0:
        raise MediaRejected("不是有效的视频/图片文件（ffprobe 无法识别）")
    data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise MediaRejected("文件里没有画面")
    dur = (data.get("format") or {}).get("duration") or v.get("duration")
    return {"width": int(v.get("width") or 0), "height": int(v.get("height") or 0), "codec": v.get("codec_name"),
            "duration_s": round(float(dur), 2) if dur not in (None, "N/A") else None, "has_audio": a is not None,
            "audio_codec": a.get("codec_name") if a else None}


class MediaStore:
    def __init__(self, cfg: dict, bus=None):
        self.cfg = cfg
        self.bus = bus
        self.dir = os.path.join(cfg["server"]["data_dir"], "media")
        os.makedirs(self.dir, exist_ok=True)
        self.index_path = os.path.join(self.dir, "media.json")
        self._lock = threading.RLock()
        self.items: dict = {}
        self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        if not os.path.isfile(self.index_path):
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                for it in json.load(f).get("items") or []:
                    if os.path.isfile(self.file_path(it)):
                        self.items[it["id"]] = it
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"素材库索引读取失败，按空库启动: {e}")

    def _save(self, reason: str, media_id: str | None = None) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": sorted(self.items.values(), key=lambda x: x["created_at"])}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)
        if self.bus is not None:
            self.bus.publish("media.changed", {"reason": reason, "media_id": media_id})

    # ------------------------------------------------------------------ 读
    def file_path(self, it: dict) -> str:
        return os.path.join(self.dir, f"{it['id']}{it['ext']}")

    def cover_path(self, it: dict) -> str | None:
        p = os.path.join(self.dir, f"{it['id']}.cover.jpg")
        return p if os.path.isfile(p) else None

    def get(self, media_id: str) -> dict | None:
        with self._lock:
            it = self.items.get(media_id)
            return copy.deepcopy(it) if it else None

    def list(self, kind: str | None = None) -> list:
        with self._lock:
            items = sorted(self.items.values(), key=lambda x: x["created_at"], reverse=True)
            return [copy.deepcopy(x) for x in items if kind is None or x["kind"] == kind]

    @staticmethod
    def url(it: dict) -> str:
        return f"/api/media/{it['id']}/file"

    # ------------------------------------------------------------------ 写
    def check_name(self, filename: str) -> tuple[str, str]:
        name = os.path.basename(filename or "").strip() or "未命名"
        ext = os.path.splitext(name)[1].lower()
        if ext not in VIDEO_EXTS + IMAGE_EXTS:
            raise MediaRejected(f"不支持的文件类型 {ext or '（无扩展名）'}，视频只支持 {' / '.join(VIDEO_EXTS)}，图片 {' / '.join(IMAGE_EXTS)}")
        return name, ext

    def new_temp(self, ext: str) -> str:
        return os.path.join(self.dir, f".upload-{uuid.uuid4().hex[:12]}{ext}")

    def add_file(self, filename: str, temp_path: str, tags: list | None = None) -> dict:
        """temp_path 是已经写到素材目录里的临时文件（上传接口边收边写，不整个读进内存）；成功后改名为正式文件。"""
        name, ext = self.check_name(filename)
        size = os.path.getsize(temp_path)
        limit = int(self.cfg["scene"]["media_max_mb"]) * 1024 * 1024
        try:
            if size == 0:
                raise MediaRejected("文件是空的")
            if size > limit:
                raise MediaRejected(f"文件太大（{size / 1048576:.0f} MB），上限 {self.cfg['scene']['media_max_mb']} MB")
            meta = probe(temp_path, self.cfg["server"]["ffmpeg"])
        except MediaRejected:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        kind = "video" if ext in VIDEO_EXTS else "image"
        it = {"id": uuid.uuid4().hex[:12], "name": name[:200], "ext": ext, "kind": kind, "size": size,
              "mime": MIME.get(ext, "application/octet-stream"), "tags": [str(t)[:30] for t in tags or []][:20],
              "created_at": time.time(), **meta}
        if kind == "image":
            it.update(duration_s=None, has_audio=False)
        os.replace(temp_path, self.file_path(it))
        if kind == "video":
            self._make_cover(it)
        with self._lock:
            self.items[it["id"]] = it
            self._save("add", it["id"])
        logger.info(f"素材入库：{name}（{kind} {it['width']}x{it['height']} {it.get('duration_s') or ''}s）")
        return copy.deepcopy(it)

    def add_bytes(self, filename: str, data: bytes, tags: list | None = None) -> dict:
        _, ext = self.check_name(filename)
        tmp = self.new_temp(ext)
        with open(tmp, "wb") as f:
            f.write(data)
        return self.add_file(filename, tmp, tags)

    def _make_cover(self, it: dict) -> None:
        at = min(1.0, (it.get("duration_s") or 0) / 2)
        out = os.path.join(self.dir, f"{it['id']}.cover.jpg")
        try:
            subprocess.run([self.cfg["server"]["ffmpeg"], "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", self.file_path(it),
                            "-frames:v", "1", "-vf", "scale=360:-2", out], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not os.path.isfile(out):
            logger.warning(f"封面生成失败：{it['name']}")

    def update(self, media_id: str, patch: dict) -> dict | None:
        with self._lock:
            it = self.items.get(media_id)
            if it is None:
                return None
            if "name" in patch and str(patch["name"] or "").strip():
                it["name"] = str(patch["name"]).strip()[:200]
            if "tags" in patch:
                it["tags"] = [str(t).strip()[:30] for t in patch["tags"] or [] if str(t).strip()][:20]
            self._save("update", media_id)
            return copy.deepcopy(it)

    def delete(self, media_id: str) -> dict | None:
        with self._lock:
            it = self.items.pop(media_id, None)
            if it is None:
                return None
            self._save("delete", media_id)
        for p in (self.file_path(it), os.path.join(self.dir, f"{it['id']}.cover.jpg")):
            try:
                os.remove(p)
            except OSError:
                pass
        return it
