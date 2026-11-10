"""进程内事件日志（环形缓冲）。

服务端、界面写入和 PLC 逻辑都往这里写；前端按 seq 游标增量拉取。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque


class EventLog:
    def __init__(self, capacity: int = 5000) -> None:
        self._items: Deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, source: str, kind: str, summary: str, *,
            detail: str = "", peer: str = "", level: str = "info") -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            item = {
                "seq": self._seq,
                "ts": time.time(),
                "source": source,
                "kind": kind,
                "level": level,
                "peer": peer,
                "summary": summary,
                "detail": detail,
            }
            self._items.append(item)
            return item

    def since(self, seq: int = 0, limit: int = 300,
              source: str = "all") -> dict[str, Any]:
        with self._lock:
            items = [i for i in self._items if i["seq"] > seq]
        if source and source != "all":
            items = [i for i in items if i["source"] == source]
        total = len(items)
        if limit and total > limit:
            items = items[-limit:]
        return {
            "items": items,
            "total": total,
            "latest_seq": self.latest_seq(),
        }

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        return items[-limit:] if limit else []

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def clear(self) -> int:
        with self._lock:
            self._items.clear()
            return self._seq

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
