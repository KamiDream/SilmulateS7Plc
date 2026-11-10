"""PLC 扫描逻辑引擎（SCL 子集）。

用户逻辑放在 ``data/user_logic.scl``。首次运行生成空文件，界面上以占位文字显示示例；
点击“填入示例”才会把模板写进编辑器。编译器只接受 SCL 的常用子集（赋值、
IF/ELSIF/ELSE、算术/比较/逻辑运算、少量函数），变量名来自 I/Q/M 变量表和
数据块，也支持绝对地址。

扫描线程只在「服务运行 + CPU RUN + 逻辑已启用」时执行程序；运行异常会
自动停用逻辑并把错误写进日志，避免每个周期刷屏。
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .events import EventLog
from .scl import SclProgram, compile_scl
from .store import AreaStore

logger = logging.getLogger(__name__)

DEFAULT_LOGIC_SOURCE = '''\
// SilmulateS7Plc · SCL 扫描逻辑
//
// 这里只写循环扫描要执行的语句，不需要 PROGRAM / END_PROGRAM。
// 变量名来自「输入 I / 输出 Q / 位存储 M」变量表和「数据块」变量表；
// 也可以使用绝对地址（I0.0 / MW10 / DB1.DBW0）。
//
// 支持的语法：
//   "Tag" := 表达式;              "DB1".Field := 表达式;
//   %Q0.0 := %I0.0;
//   IF cond THEN ... ELSIF cond THEN ... ELSE ... END_IF;
//   运算符：+ - * / MOD = <> < > <= >= AND OR XOR NOT
//   函数：ABS SQRT MIN MAX LIMIT ROUND TRUNC LOG
//   常量：TRUE / FALSE / 123 / 1.5 / 16#FF
//
// 示例：把 I0.0 直接映射到 Q0.0
%Q0.0 := %I0.0;

// 声明了变量后可以改用符号名，例如：
//   "RunLamp" := "StartButton";
//
//   IF "Temperature" > 80.0 THEN
//       "RunLamp" := FALSE;
//   ELSE
//       "RunLamp" := TRUE;
//   END_IF;
'''


class LogicEngine:
    def __init__(self, store: AreaStore, events: EventLog, path: Path,
                 server_getter: Callable[[], Any],
                 config_getter: Callable[[], dict[str, Any]]) -> None:
        self.store = store
        self.events = events
        self.path = Path(path)
        self._server_getter = server_getter
        self._config_getter = config_getter
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.enabled = False
        self.interval_ms = 100
        self.scans = 0
        self.errors = 0
        self.last_ms = 0.0
        self.last_scan_ts = 0.0
        self.last_error = ""
        self.loaded = False
        self._source = ""
        self._program: SclProgram | None = None

    # ------------------------------------------------------------------
    # 线程生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="plc-logic",
                                            daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                enabled = self.enabled
                program = self._program
                interval = max(10, int(self.interval_ms)) / 1000.0
                loaded = self.loaded
            wait = interval
            if not enabled or not loaded or program is None:
                wait = 0.2
            else:
                server = self._server_getter()
                should_scan = bool(server and server.running
                                   and server.cpu_name == "RUN")
                if should_scan:
                    started = time.perf_counter()
                    with self._lock:
                        scan_index = self.scans + 1
                    try:
                        program.run(self.store, self.events,
                                    self._config_getter())
                        elapsed = (time.perf_counter() - started) * 1000.0
                        with self._lock:
                            self.scans = scan_index
                            self.last_ms = round(elapsed, 3)
                            self.last_scan_ts = time.time()
                            self.last_error = ""
                    except Exception as exc:
                        detail = traceback.format_exc(limit=8)
                        logger.exception("SCL 逻辑执行异常，已自动停用")
                        with self._lock:
                            self.errors += 1
                            self.last_error = f"{type(exc).__name__}: {exc}"
                            self.enabled = False
                        self.events.add(
                            "logic", "error",
                            f"SCL 逻辑执行异常，已自动停用：{self.last_error}",
                            detail=detail[-4000:], level="err",
                        )
                        wait = 0.2
                else:
                    wait = 0.2
            self._stop.wait(wait)

    # ------------------------------------------------------------------
    # 加载 / 重载
    # ------------------------------------------------------------------
    def load_file(self) -> None:
        self._ensure_file()
        try:
            source = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            self._set_error(f"读取逻辑文件失败: {exc}")
            raise
        # 旧版本会把示例模板直接写入文件；首次升级时清空一次，
        # 之后用户主动保存的模板不会再被清掉。
        marker = self.path.with_name(self.path.name + ".migrated")
        if not marker.exists():
            if source.strip() == DEFAULT_LOGIC_SOURCE.strip():
                source = ""
                self._write_source("")
            marker.write_text("1", encoding="utf-8")
        self.reload(source, persist=False)

    def reload(self, source: str | None = None, persist: bool = True) -> None:
        if source is None:
            source = self.path.read_text(encoding="utf-8")
        self._ensure_file()
        program = compile_scl(source, self._config_getter())
        if persist:
            self._write_source(source)

        with self._lock:
            self._source = source
            self._program = program
            self.loaded = True
            self.last_error = ""
        self.events.add(
            "logic", "reloaded",
            f"SCL 逻辑已编译（{len(source.splitlines())} 行）",
            level="ok",
        )

    def _write_source(self, source: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(source, encoding="utf-8")
        tmp.replace(self.path)

    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def _set_error(self, message: str) -> None:
        with self._lock:
            self.last_error = message
            self.loaded = False
        self.events.add("logic", "error", message, level="err")

    # ------------------------------------------------------------------
    # 运行时配置
    # ------------------------------------------------------------------
    def configure(self, enabled: bool | None = None,
                  interval_ms: int | None = None) -> dict[str, Any]:
        with self._lock:
            if interval_ms is not None:
                value = int(interval_ms)
                if not 10 <= value <= 5000:
                    raise ValueError("扫描周期必须在 10 ~ 5000 ms 之间")
                self.interval_ms = value
            if enabled is not None:
                desired = bool(enabled)
                if desired and not self.loaded:
                    raise RuntimeError("SCL 逻辑未加载，无法启用（请先保存并重载）")
                if desired and self._program is None:
                    raise RuntimeError("SCL 程序不存在，无法启用")
                if desired != self.enabled:
                    self.enabled = desired
                    if desired:
                        self.errors = 0
                        self.last_error = ""
                        self.events.add("logic", "enabled",
                                        "SCL 逻辑已启用", level="ok")
                    else:
                        self.events.add("logic", "disabled", "SCL 逻辑已停用")
            return self.status(include_source=False)

    def status(self, include_source: bool = True) -> dict[str, Any]:
        with self._lock:
            source = self._source
            result = {
                "language": "SCL",
                "enabled": bool(self.enabled),
                "loaded": bool(self.loaded),
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "interval_ms": int(self.interval_ms),
                "scans": int(self.scans),
                "errors": int(self.errors),
                "last_ms": self.last_ms,
                "last_scan_ts": self.last_scan_ts,
                "last_error": self.last_error,
                "path": str(self.path),
                "source_lines": len(source.splitlines()),
            }
            if include_source:
                result["source"] = source
            return result
