"""对 python-snap7 纯 Python S7 服务端的薄封装。

只做四件事：
  1. 把 ``AreaStore`` 的内存区注册进去，并把读写重定向到 ``AreaStore``；
  2. 跟踪客户端连接、请求数、收发字节；
  3. 把读写/连接事件写进统一日志；
  4. 用配置里的 CPU 信息回答 SZL（get_cpu_info / get_order_code）。

协议编解码仍然完全交给 python-snap7，避免自己重写 S7 协议带来的兼容性坑。
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Any

from snap7.datatypes import S7Area
from snap7.server import CPUState, Server
from snap7.s7protocol import S7Function, S7PDUType

from .events import EventLog
from .store import AreaStore

_HEX_INLINE_LIMIT = 48
_HEX_DETAIL_LIMIT = 256


def _hex_bytes(data: bytes | bytearray, limit: int) -> str:
    raw = bytes(data[:limit])
    text = raw.hex(" ").upper()
    return text + (" …" if len(data) > limit else "")


class ServerStats:
    """服务端累计计数器（每次启动清零）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, int] = {
            "requests": 0,
            "reads": 0,
            "writes": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "connections": 0,
            "disconnects": 0,
            "errors": 0,
        }

    def reset(self) -> None:
        with self._lock:
            for key in self._data:
                self._data[key] = 0

    def inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._data[key] = self._data.get(key, 0) + int(amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._data)


class ClientTracker:
    """按 (ip, port) 跟踪当前连接。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    @staticmethod
    def peer_of(address: tuple[str, int]) -> str:
        return f"{address[0]}:{address[1]}"

    def add(self, address: tuple[str, int], sock: socket.socket) -> str:
        peer = self.peer_of(address)
        now = time.time()
        with self._lock:
            self._sessions[peer] = {
                "peer": peer,
                "ip": address[0],
                "port": address[1],
                "connected_at": now,
                "last_seen": now,
                "requests": 0,
                "reads": 0,
                "writes": 0,
                "read_bytes": 0,
                "write_bytes": 0,
                "bytes_in": 0,
                "bytes_out": 0,
                "socket": sock,
            }
        return peer

    def remove(self, address: tuple[str, int]) -> dict[str, Any] | None:
        peer = self.peer_of(address)
        with self._lock:
            return self._sessions.pop(peer, None)

    def touch(self, peer: str) -> None:
        with self._lock:
            session = self._sessions.get(peer)
            if session is not None:
                session["requests"] += 1
                session["last_seen"] = time.time()

    def add_bytes(self, peer: str, incoming: int, outgoing: int) -> None:
        with self._lock:
            session = self._sessions.get(peer)
            if session is not None:
                session["bytes_in"] += incoming
                session["bytes_out"] += outgoing

    def record_read(self, peer: str, size: int) -> None:
        with self._lock:
            session = self._sessions.get(peer)
            if session is not None:
                session["reads"] += 1
                session["read_bytes"] += size

    def record_write(self, peer: str, size: int) -> None:
        with self._lock:
            session = self._sessions.get(peer)
            if session is not None:
                session["writes"] += 1
                session["write_bytes"] += size

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            items = []
            for session in self._sessions.values():
                item = {k: v for k, v in session.items() if k != "socket"}
                item["connected_s"] = round(now - session["connected_at"], 1)
                item["idle_s"] = round(now - session["last_seen"], 1)
                items.append(item)
        return sorted(items, key=lambda s: s["connected_at"])

    def disconnect(self, peer: str) -> bool:
        with self._lock:
            session = self._sessions.get(peer)
            sock = session.get("socket") if session else None
        if sock is None:
            return False
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        return True

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def _friendly_error(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if "permission denied" in lowered or "errno 13" in lowered:
        return ("启动失败：端口 102 是特权端口，普通用户不能监听。"
                "请改用 1102 等高位端口，或给 Python 授予 CAP_NET_BIND_SERVICE。")
    if "address already in use" in lowered or "errno 98" in lowered:
        return "启动失败：端口已被占用，请换一个端口或先停止占用它的程序。"
    if "cannot assign requested address" in lowered or "errno 99" in lowered:
        return "启动失败：监听地址不属于本机，请改成 0.0.0.0 或本机网卡地址。"
    return f"启动失败：{text}"


class SimServer(Server):
    def __init__(self, store: AreaStore, config: dict[str, Any],
                 events: EventLog, tracker: ClientTracker) -> None:
        super().__init__(log=False,
                         max_clients=int(config["server"]["max_clients"]))
        self.store = store
        self.cfg = config
        self.events = events
        self.tracker = tracker
        self.stats = ServerStats()
        self._local = threading.local()
        self._started_at: float | None = None
        self._last_error = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def update_config(self, config: dict[str, Any]) -> None:
        """更新运行时使用的配置（CPU 信息 / PDU 等，下一次协商生效）。"""
        self.cfg = config

    def start(self, tcp_port: int = 102) -> int:
        self._last_error = ""
        try:
            result = super().start(tcp_port)
        except Exception as exc:  # noqa: BLE001 - 统一转成可展示的中文错误
            self._last_error = _friendly_error(exc)
            self.events.add("server", "start_failed", self._last_error, level="err")
            raise RuntimeError(self._last_error) from exc
        self._started_at = time.time()
        self.stats.reset()
        self.events.add(
            "server", "started",
            f"S7 服务已启动，监听 {self.host}:{self.port}",
            detail=f"CPU={self.cfg['cpu']['model']}  PDU={self.cfg['server']['pdu_size']}",
            level="ok",
        )
        return result

    def stop(self) -> int:
        was_running = self.running
        result = super().stop()
        self.tracker.clear()
        self._started_at = None
        if was_running:
            self.events.add("server", "stopped", "S7 服务已停止")
        return result

    def set_cpu(self, state: str) -> str:
        value = (state or "").strip().upper()
        if value == "RUN":
            self.set_cpu_status(int(CPUState.RUN))
        elif value == "STOP":
            self.set_cpu_status(int(CPUState.STOP))
        elif value == "UNKNOWN":
            self.set_cpu_status(0)
        else:
            raise ValueError("CPU 状态只能是 RUN / STOP")
        if self.running:
            self.events.add("server", "cpu", f"CPU 状态切换为 {value}",
                            level="ok" if value == "RUN" else "warn")
        return self.cpu_name

    @property
    def cpu_name(self) -> str:
        if self.cpu_state == CPUState.RUN:
            return "RUN"
        if self.cpu_state == CPUState.STOP:
            return "STOP"
        return "UNKNOWN"

    def status(self) -> dict[str, Any]:
        uptime = round(time.time() - self._started_at, 1) if self._started_at else 0.0
        return {
            "running": bool(self.running),
            "host": self.host,
            "port": self.port,
            "cpu": self.cpu_name,
            "cpu_code": int(self.cpu_state),
            "client_count": self.client_count,
            "max_clients": self.max_clients,
            "pdu_size": int(self.cfg["server"]["pdu_size"]),
            "started_at": self._started_at,
            "uptime_s": uptime,
            "stats": self.stats.snapshot(),
            "error": self._last_error,
        }

    # ------------------------------------------------------------------
    # 客户端连接跟踪
    # ------------------------------------------------------------------
    def _handle_client(self, client_socket: socket.socket,
                       address: tuple[str, int]) -> None:
        peer = self.tracker.add(address, client_socket)
        self.stats.inc("connections")
        self.events.add("server", "client_connect", f"客户端已连接 {peer}",
                        peer=peer, level="ok")
        try:
            super()._handle_client(client_socket, address)
        finally:
            session = self.tracker.remove(address)
            self.stats.inc("disconnects")
            extra = ""
            if session:
                extra = (f"请求 {session['requests']} 次，"
                         f"读 {session['reads']} / 写 {session['writes']}")
            self.events.add("server", "client_disconnect",
                            f"客户端已断开 {peer}", detail=extra, peer=peer)

    # ------------------------------------------------------------------
    # 请求统计 + 控制类请求日志
    # ------------------------------------------------------------------
    def _process_request(self, request_data: bytes,
                         client_address: tuple[str, int]) -> bytes | None:
        peer = ClientTracker.peer_of(client_address)
        self._local.peer = peer
        self.tracker.touch(peer)
        self.stats.inc("requests")
        self.stats.inc("bytes_in", len(request_data))

        parsed: dict[str, Any] = {}
        try:
            parsed = self._parse_request(request_data)
        except Exception:  # noqa: BLE001 - 日志解析失败不影响协议处理
            parsed = {}

        try:
            response = super()._process_request(request_data, client_address)
        except Exception:
            self.stats.inc("errors")
            raise
        finally:
            self._local.peer = None

        if response:
            self.stats.inc("bytes_out", len(response))
            self.tracker.add_bytes(peer, len(request_data), len(response))

        self._log_control_request(peer, parsed)
        return response

    def _log_control_request(self, peer: str, parsed: dict[str, Any]) -> None:
        params = parsed.get("parameters") or {}
        pdu_type = parsed.get("pdu_type")
        function = params.get("function_code")

        if function == S7Function.SETUP_COMMUNICATION:
            requested = params.get("pdu_length")
            configured = int(self.cfg["server"]["pdu_size"])
            negotiated = max(240, min(int(requested or 480), configured))
            self.events.add(
                "s7", "negotiate",
                f"PDU 协商：请求 {requested} / 配置 {configured} → {negotiated}",
                peer=peer,
            )
        elif function == S7Function.PLC_CONTROL:
            restart = params.get("restart_type")
            service = params.get("pi_service")
            if service:
                text = service.decode("latin-1", "replace")
                self.events.add("s7", "plc_control",
                                f"PLC 控制请求：{text}", peer=peer)
            else:
                kind = "冷启动" if restart == 2 else "热启动"
                self.events.add("s7", "plc_control", f"客户端请求 CPU {kind}",
                                peer=peer, level="ok")
        elif function == S7Function.PLC_STOP:
            self.events.add("s7", "plc_control", "客户端请求 CPU STOP",
                            peer=peer, level="warn")
        elif pdu_type == S7PDUType.USERDATA:
            # SZL / 时钟 / 块信息请求很频繁，默认不逐条记录；失败由协议层处理。
            return
        elif function not in (None, S7Function.READ_AREA, S7Function.WRITE_AREA):
            self.events.add("s7", "request",
                            f"未特殊处理的功能码 0x{int(function):02X}",
                            peer=peer, level="warn")

    # ------------------------------------------------------------------
    # 读写钩子：走 AreaStore，并记录日志
    # ------------------------------------------------------------------
    def _read_from_memory_area(self, area: S7Area, db_number: int, start: int,
                               count: int) -> tuple[int, bytearray]:
        return_code, data = self.store.server_read(area, db_number, start, count)
        peer = getattr(self._local, "peer", None)
        if peer:
            label = self._area_label(area, db_number)
            if return_code == 0xFF:
                self.stats.inc("reads")
                self.tracker.record_read(peer, len(data))
                preview = _hex_bytes(data, _HEX_INLINE_LIMIT)
                self.events.add(
                    "s7", "read",
                    f"读 {label} {start}..{start + len(data)}"
                    + (f"  [{preview}]" if preview else ""),
                    detail=self._detail(label, start, data),
                    peer=peer,
                )
            else:
                self.stats.inc("errors")
                self.events.add(
                    "s7", "read_error",
                    f"读取失败 {label} {start}..{start + max(count, 0)}"
                    f"（返回码 0x{return_code:02X}）",
                    peer=peer, level="warn",
                )
        return return_code, data

    def _write_to_memory_area(self, area: S7Area, db_number: int,
                              start: int, write_data: bytearray) -> bool:
        return_code = self.store.server_write(area, db_number, start, write_data)
        self._log_write(area, db_number, start, write_data, return_code)
        return return_code == 0xFF

    def _write_item_to_memory_area(self, area: S7Area, db_number: int,
                                   start: int, write_data: bytearray) -> int:
        return_code = self.store.server_write(area, db_number, start, write_data)
        self._log_write(area, db_number, start, write_data, return_code)
        return return_code

    def _log_write(self, area: S7Area, db_number: int, start: int,
                   data: bytes | bytearray, return_code: int) -> None:
        peer = getattr(self._local, "peer", None)
        if not peer:
            return
        label = self._area_label(area, db_number)
        if return_code == 0xFF:
            self.stats.inc("writes")
            self.tracker.record_write(peer, len(data))
            preview = _hex_bytes(data, _HEX_INLINE_LIMIT)
            self.events.add(
                "s7", "write",
                f"写 {label} {start}..{start + len(data)}  [{preview}]",
                detail=self._detail(label, start, data),
                peer=peer,
            )
        else:
            self.stats.inc("errors")
            self.events.add(
                "s7", "write_error",
                f"写入失败 {label} {start}（返回码 0x{return_code:02X}）",
                peer=peer, level="warn",
            )

    @staticmethod
    def _detail(label: str, start: int, data: bytes | bytearray) -> str:
        return (f"{label} 偏移 {start}，长度 {len(data)} 字节\n"
                f"HEX: {_hex_bytes(data, _HEX_DETAIL_LIMIT)}")

    @staticmethod
    def _area_label(area: S7Area, db_number: int) -> str:
        return {
            S7Area.PE: "I",
            S7Area.PA: "Q",
            S7Area.MK: "M",
            S7Area.DB: f"DB{db_number}",
            S7Area.CT: "CT",
            S7Area.TM: "TM",
        }.get(area, str(area))

    # ------------------------------------------------------------------
    # PDU 协商与 SZL
    # ------------------------------------------------------------------
    def _handle_setup_communication(self, request: dict[str, Any]) -> bytes:
        params = request["parameters"]
        requested = int(params.get("pdu_length", 480) or 480)
        configured = int(self.cfg["server"]["pdu_size"])
        negotiated = max(240, min(requested, configured))

        header = struct.pack(
            ">BBHHHHBB",
            0x32,
            S7PDUType.ACK_DATA,
            0x0000,
            request["sequence"],
            0x0008,
            0x0000,
            0x00,
            0x00,
        )
        parameters = struct.pack(
            ">BBHHH",
            S7Function.SETUP_COMMUNICATION,
            0x00,
            1,
            1,
            negotiated,
        )
        return header + parameters

    @staticmethod
    def _put_text(buf: bytearray, offset: int, length: int, text: str) -> None:
        # 字段是定长且按 C 字符串读取，必须保留结尾的 NUL；同时避免截断
        # 多字节字符（否则客户端会看到半个汉字）。
        raw = str(text).encode("utf-8", "replace")[: max(0, length - 1)]
        while raw:
            try:
                raw.decode("utf-8")
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
        buf[offset:offset + len(raw)] = raw

    def _get_szl_data(self, szl_id: int, szl_index: int) -> bytes | None:
        cpu = self.cfg["cpu"]
        if szl_id == 0x001C:                       # 组件识别 / get_cpu_info
            data = bytearray(210)
            self._put_text(data, 6, 24, cpu["as_name"])
            self._put_text(data, 40, 24, cpu["model"])
            self._put_text(data, 108, 26, cpu["copyright"])
            self._put_text(data, 142, 24, cpu["serial"])
            self._put_text(data, 176, 32, cpu["model"])
            return bytes(data)

        if szl_id == 0x0011:                       # 订货号 / get_order_code
            mlfb = str(cpu["order_code"]).encode("utf-8", "replace")[:20]
            mlfb = mlfb.ljust(20, b"\x00")
            record_len = 26
            record = (struct.pack(">H", 0x0001) + mlfb
                      + struct.pack("BBBB", 0x00, 3, 3, 0))
            return struct.pack(">HH", record_len, 1) + record

        if szl_id == 0x0131:                       # 通信参数 / get_cp_info
            return struct.pack(
                ">HHHH",
                int(self.cfg["server"]["pdu_size"]),
                int(self.cfg["server"]["max_clients"]),
                12,
                12,
            )

        if szl_id == 0x0232:                       # 保护等级 / get_protection
            return struct.pack(">HHHHH", 1, 0, 0, 0, 0)

        if szl_id == 0x0000:                       # 支持的 SZL 列表
            return b"".join(struct.pack(">H", value)
                            for value in (0x0000, 0x0011, 0x001C, 0x0131, 0x0232))

        return super()._get_szl_data(szl_id, szl_index)
