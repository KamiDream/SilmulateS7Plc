"""Bool / SInt 变量的「周期输出」引擎。

TIA 里让一个输出按周期亮灭，通常要用一两个定时器（TON/TOF）拼一个方波；
模拟器把这件事直接做成变量的属性 —— 变量表的「周期」列填

    500        500 ms 一个周期，1 / 0 各占一半（等价于一个定时器）
    200/800    输出 1 持续 200 ms、输出 0 持续 800 ms（等价于两个定时器）

之后由这里的线程按 ``TICK_MS`` 节拍把这些变量写成 1 / 0：Bool 写一个位，
SInt 整字节写 1 / 0。规则：

* 只在「服务运行 + CPU RUN」时输出（和真实 PLC 一样，STOP 时不刷新）；
* 所有方波的相位都从「进入 RUN 的那一刻」起算，所以同时配置的变量会同步起振；
* 值没变化就不写内存，避免每个节拍都把内存版本号刷一遍。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .dbtypes import layout_variables, parse_pulse
from .events import EventLog
from .store import AreaError, AreaStore
from .tags import layout_tags

logger = logging.getLogger(__name__)

#: 输出节拍：分辨率就是这个值，所以周期最小也按 10 ms 限制
TICK_MS = 10
#: 每 N 个节拍重新扫一遍配置（改变量表后最多 100 ms 生效）
REBUILD_TICKS = 10


class PulseEngine:
    def __init__(self, store: AreaStore, events: EventLog,
                 server_getter: Callable[[], Any],
                 config_getter: Callable[[], dict[str, Any]]) -> None:
        self.store = store
        self.events = events
        self._server_getter = server_getter
        self._config_getter = config_getter
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._entries: list[dict[str, Any]] = []
        self._written: dict[tuple, bool] = {}
        self._ticks = 0
        self._start: float | None = None
        self._logged: tuple | None = None
        self.last_error = ""

    # ------------------------------------------------------------------
    # 线程
    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="plc-pulse",
                                            daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        interval = TICK_MS / 1000.0
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # 周期引擎不能把线程弄死
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("周期输出异常")
                self.events.add("logic", "error",
                                f"周期输出异常：{self.last_error}", level="err")
                self._stop.wait(0.5)
                continue
            self._stop.wait(interval)

    def _tick(self) -> None:
        server = self._server_getter()
        running = bool(server and getattr(server, "running", False)
                       and server.cpu_name == "RUN")
        if not running:
            # 停止后重新起振时重新计时，避免相位“跳变”
            with self._lock:
                was_active = self._logged is not None
                self._logged = None
                self._start = None
                self._ticks = 0
                self._written.clear()
            if was_active:
                self._event("周期输出已暂停（服务停止或 CPU 不是 RUN）", level="warn")
            return

        now = time.monotonic()
        with self._lock:
            if self._start is None:
                self._start = now
                self._ticks = 0
                self._entries = self._build_entries()
            self._ticks += 1
            if self._ticks % REBUILD_TICKS == 0:
                self._entries = self._build_entries()
            elapsed = now - self._start
            entries = list(self._entries)
        self._log_entries(entries)
        self.apply(elapsed, entries)

    def _signature(self, entries: list[dict[str, Any]]) -> tuple:
        return tuple((item["kind"], item["db"], item["area"], item["byte"],
                      item["bit"], item["on"], item["off"]) for item in entries)

    def _log_entries(self, entries: list[dict[str, Any]]) -> None:
        """配置变化时往通信日志里说一声，方便确认“到底有没有生效”。"""
        signature = self._signature(entries)
        with self._lock:
            previous = self._logged
            self._logged = signature
        if signature == previous:
            return
        if not entries:
            if previous:
                self._event("周期输出已停止：没有变量配置周期", level="warn")
            return
        lines = []
        for item in entries[:8]:
            where = f"DB{item['db']}" if item["kind"] == "DB" else f"{item['area']} 区"
            spot = (str(item["byte"]) if item["bit"] is None
                    else f"{item['byte']}.{item['bit']}")
            lines.append(f"{where} {spot} {item['on']}/{item['off']} ms")
        self._event(f"周期输出已启用：{len(entries)} 个变量（CPU RUN 时按周期写 1 / 0）",
                    detail="\n".join(lines))

    def _event(self, message: str, level: str = "ok",
               detail: str = "") -> None:
        if self.events is None:
            return
        try:
            self.events.add("logic", "pulse", message, detail=detail, level=level)
        except Exception as exc:  # 日志失败不能影响输出
            self.last_error = f"写事件失败: {type(exc).__name__}: {exc}"
            logger.debug("写事件失败", exc_info=True)

    # ------------------------------------------------------------------
    # 周期计算（apply 与测试都走这里）
    # ------------------------------------------------------------------
    def apply(self, elapsed: float, entries: list[dict[str, Any]] | None = None
              ) -> None:
        """按已经运行的秒数 ``elapsed`` 写一遍所有周期位。"""
        with self._lock:
            items = list(self._entries if entries is None else entries)
            written = self._written
        for entry in items:
            phase = (elapsed * 1000.0) % entry["period"]
            value = phase < entry["on"]
            key = (entry["area"], entry["db"], entry["byte"], entry["bit"])
            if written.get(key) == value:
                continue
            try:
                if entry["mode"] == "bit":
                    self.store.write_bit(entry["area"], entry["db"],
                                         entry["byte"], entry["bit"], value)
                else:
                    # SInt: 整字节写 1 / 0
                    self.store.write(entry["area"], entry["db"], entry["byte"],
                                     b"\x01" if value else b"\x00")
            except AreaError as exc:
                with self._lock:
                    written.pop(key, None)
                self.last_error = str(exc)
                continue
            with self._lock:
                written[key] = value

    # ------------------------------------------------------------------
    # 从配置里挑出所有带周期的 Bool
    # ------------------------------------------------------------------
    def _build_entries(self) -> list[dict[str, Any]]:
        config = self._config_getter() or {}
        entries: list[dict[str, Any]] = []

        for db in config.get("dbs") or ():
            number = int(db.get("number") or 0)
            variables = db.get("variables") or []
            if not number or not variables:
                continue
            try:
                rows = layout_variables(variables)["rows"]
            except ValueError:
                continue
            by_index = {int(row["index"]): row for row in rows}
            for index, variable in enumerate(variables):
                entry = self._entry("DB", number, variable,
                                    by_index.get(index))
                if entry:
                    entries.append(entry)

        for table in config.get("tag_tables") or ():
            variables = table.get("variables") or []
            if not variables:
                continue
            try:
                rows, _required = layout_tags(variables)
            except ValueError:
                continue
            by_index = {int(row["index"]): row for row in rows}
            for index, variable in enumerate(variables):
                entry = self._entry("TAG", int(table.get("id") or 0), variable,
                                    by_index.get(index))
                if entry:
                    entries.append(entry)

        return entries

    @staticmethod
    def _entry(kind: str, owner: int, variable: dict[str, Any],
               row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        bit = row.get("bit")
        if row.get("kind") == "bool" and bit is not None:
            mode = "bit"
            out_bit: int | None = int(bit)
        elif row.get("kind") == "int" and int(row.get("size") or 0) == 1 \
                and str(row.get("type")) == "SInt":
            mode = "byte"          # SInt 整字节写 1 / 0
            out_bit = None
        else:
            return None
        timings = parse_pulse(variable.get("pulse"))
        if timings is None:
            return None
        on_ms, off_ms = timings
        return {
            "kind": kind,
            "mode": mode,
            "area": row["area"] if kind == "TAG" else "DB",
            "db": owner if kind == "DB" else 0,
            "byte": int(row["offset"]),
            "bit": out_bit,
            "on": on_ms,
            "off": off_ms,
            "period": on_ms + off_ms,
        }
