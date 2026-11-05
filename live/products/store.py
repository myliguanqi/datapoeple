"""产品库持久化：live_data/products.json（原子替换写入），线程安全 CRUD + 排序 + 当前讲解中的产品（active_id）。"""
from __future__ import annotations

import copy
import json
import os
import threading
import time

from loguru import logger

from live.products.models import Product, ScriptSegment, validate_patch, validate_segments


class ProductStore:
    def __init__(self, path: str, bus=None):
        self.path = path
        self.bus = bus
        self._lock = threading.RLock()
        self.products: dict = {}
        self.active_id: str | None = None
        self._load()

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("products") or []:
                p = Product.from_dict(d)
                if p.name:
                    self.products[p.id] = p
            self.active_id = data.get("active_id") if data.get("active_id") in self.products else None
        except Exception as e:                      # noqa: BLE001
            logger.warning(f"产品库读取失败（{self.path}），按空库启动: {e}")

    def _save(self, reason: str = "update", product_id: str | None = None) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "saved_at": time.time(), "active_id": self.active_id,
                       "products": [p.to_dict() for p in self.ordered()]}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        if self.bus is not None:
            self.bus.publish("products.changed", {"reason": reason, "product_id": product_id, "active_id": self.active_id})

    # ------------------------------------------------------------------ 读
    def ordered(self) -> list:
        with self._lock:
            return sorted(self.products.values(), key=lambda p: (p.order, p.created_at))

    def list(self) -> list:
        return [copy.deepcopy(p) for p in self.ordered()]

    def get(self, pid: str | None) -> Product | None:
        with self._lock:
            p = self.products.get(pid) if pid else None
            return copy.deepcopy(p) if p else None

    def __len__(self) -> int:
        return len(self.products)

    # ------------------------------------------------------------------ 写
    def create(self, data: dict) -> Product:
        patch = validate_patch({"name": data.get("name"), **{k: v for k, v in data.items() if k != "name"}})
        segs = validate_segments(data.get("segments") or []) if data.get("segments") else []
        with self._lock:
            order = max((p.order for p in self.products.values()), default=-1) + 1
            p = Product(name=patch.pop("name"), segments=segs, order=patch.pop("order", order), **patch)
            self.products[p.id] = p
            self._save("create", p.id)
            return copy.deepcopy(p)

    def update(self, pid: str, patch: dict) -> Product | None:
        patch = validate_patch(patch)
        with self._lock:
            p = self.products.get(pid)
            if p is None:
                return None
            for k, v in patch.items():
                setattr(p, k, v)
            p.updated_at = time.time()
            self._save("update", pid)
            return copy.deepcopy(p)

    def set_segments(self, pid: str, items: list) -> Product | None:
        segs = validate_segments(items)
        with self._lock:
            p = self.products.get(pid)
            if p is None:
                return None
            old = {s.id: s for s in p.segments}
            for s in segs:                          # 文本没变就保留原来的生成来源与合规命中记录
                o = old.get(s.id)
                if o is not None and o.text == s.text:
                    s.source, s.hits = o.source, o.hits
                elif o is not None:
                    s.source = "manual"
            p.segments = segs
            p.updated_at = time.time()
            self._save("script", pid)
            return copy.deepcopy(p)

    def add_segments(self, pid: str, segs: list, replace_unconfirmed: bool = True) -> Product | None:
        """生成的讲解稿追加进来（待确认）；默认先去掉旧的待确认段，已确认的保留。"""
        with self._lock:
            p = self.products.get(pid)
            if p is None:
                return None
            keep = [s for s in p.segments if s.confirmed] if replace_unconfirmed else list(p.segments)
            p.segments = (keep + [s if isinstance(s, ScriptSegment) else ScriptSegment.from_dict(s) for s in segs])[:50]
            p.updated_at = time.time()
            self._save("script", pid)
            return copy.deepcopy(p)

    def delete(self, pid: str) -> bool:
        with self._lock:
            if self.products.pop(pid, None) is None:
                return False
            if self.active_id == pid:
                self.active_id = None
            self._save("delete", pid)
            return True

    def reorder(self, ids: list) -> list:
        with self._lock:
            rest = [p.id for p in self.ordered() if p.id not in ids]
            for i, pid in enumerate([x for x in ids if x in self.products] + rest):
                self.products[pid].order = i
            self._save("order")
            return self.list()

    def set_active(self, pid: str | None) -> None:
        with self._lock:
            if pid is not None and pid not in self.products:
                raise KeyError(pid)
            if self.active_id != pid:
                self.active_id = pid
                self._save("active", pid)
