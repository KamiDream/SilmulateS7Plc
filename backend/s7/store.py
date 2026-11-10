"""S7 内存区模型。

I / Q / M / DB 都由这里的 ``AreaStore`` 统一持有。python-snap7 的纯 Python
服务端本身也维护一份 ``memory_areas``，但本项目的读写钩子会绕开它、直接访问
``AreaStore``，这样界面编辑、客户端写入和 PLC 逻辑使用的是同一把锁与同一个
版本号，不会出现“界面看到的还是旧数据”的情况。
"""
from __future__ import annotations

import logging
import struct
import threading
from dataclasses import dataclass
from typing import Any

from snap7.datatypes import S7Area
from snap7.type import SrvArea

from .config import area_definitions

logger = logging.getLogger(__name__)


class AreaError(ValueError):
    """内存区不存在、地址越界或数据长度不合法。"""


@dataclass
class Area:
    key: str
    area: str
    db: int
    label: str
    size: int
    data: bytearray

    def info(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "area": self.area,
            "db": self.db,
            "label": self.label,
            "size": self.size,
        }


#: S7 线协议里的区码 -> snap7 服务端注册用的 SrvArea
_SRV_AREA = {
    "I": SrvArea.PE,
    "Q": SrvArea.PA,
    "M": SrvArea.MK,
    "DB": SrvArea.DB,
}

#: S7 线协议区码 -> 本项目的短名
_WIRE_AREA = {
    S7Area.PE: "I",
    S7Area.PA: "Q",
    S7Area.MK: "M",
    S7Area.DB: "DB",
}


def area_key(area: str, db: int = 0) -> str:
    name = (area or "").strip().upper()
    if name == "DB":
        return f"DB{int(db)}"
    if name not in ("I", "Q", "M"):
        raise AreaError(f"未知内存区: {area!r}（可用 I / Q / M / DB）")
    return name


class AreaStore:
    def __init__(self) -> None:
        self._areas: dict[str, Area] = {}
        self._lock = threading.RLock()
        self._version = 0
        self._config_signature: tuple = ()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def rebuild(self, config: dict[str, Any]) -> None:
        """按配置重建内存区；同 key 的旧数据尽量保留（长度取较小值）。"""
        definitions = area_definitions(config)
        with self._lock:
            old = self._areas
            new: dict[str, Area] = {}
            for definition in definitions:
                key = definition["key"]
                size = int(definition["size"])
                data = bytearray(size)
                previous = old.get(key)
                if previous is not None:
                    keep = min(len(previous.data), size)
                    data[:keep] = previous.data[:keep]
                new[key] = Area(
                    key=key,
                    area=definition["area"],
                    db=int(definition["db"]),
                    label=key,
                    size=size,
                    data=data,
                )
            self._areas = new
            self._version += 1
            self._config_signature = tuple(
                (d["key"], d["size"]) for d in definitions
            )

    def register_into(self, server: Any) -> None:
        """把内存区注册给 python-snap7 服务端。

        读写实际走本类，但注册后 ``Server`` 的状态/兼容接口里仍能看到这些区。
        """
        with self._lock:
            areas = list(self._areas.values())
        for item in areas:
            server.register_area(_SRV_AREA[item.area], item.db, item.data)

    def sync_server(self, server: Any | None) -> None:
        """数据库结构在运行中变化后，同步服务端注册的兼容内存区。

        本项目的读写钩子直接用 ``AreaStore``，但 ``Server.memory_areas`` 仍保留
        一份引用；DB 增删/扩展后同步一下，避免兼容接口看到旧引用。
        """
        if server is None:
            return
        with self._lock:
            current = {(item.area, int(item.db)) for item in self._areas.values()}
        for key in list(getattr(server, "memory_areas", {}).keys()):
            s7_area, db = key
            name = _WIRE_AREA.get(s7_area)
            if name is None or (name, int(db)) in current:
                continue
            try:
                server.unregister_area(_SRV_AREA[name], int(db))
            except Exception:  # 旧区域注销失败不影响后续注册
                logger.debug("注销旧内存区失败: %s", key, exc_info=True)
        self.register_into(server)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def signature(self) -> tuple:
        with self._lock:
            return self._config_signature

    def areas_info(self) -> list[dict[str, Any]]:
        with self._lock:
            return [item.info() for item in self._areas.values()]

    def get(self, area: str, db: int = 0) -> Area:
        key = area_key(area, db)
        with self._lock:
            item = self._areas.get(key)
        if item is None:
            raise AreaError(f"内存区 {key} 不存在（请先在服务配置里添加）")
        return item

    def label(self, area: str, db: int = 0) -> str:
        try:
            return area_key(area, db)
        except AreaError:
            return f"{area}{db or ''}"

    def resolve_wire(self, s7_area: S7Area, db: int) -> Area | None:
        name = _WIRE_AREA.get(s7_area)
        if name is None:
            return None
        key = f"DB{int(db)}" if name == "DB" else name
        with self._lock:
            return self._areas.get(key)

    # ------------------------------------------------------------------
    # 界面 / 逻辑使用的读写
    # ------------------------------------------------------------------
    def read(self, area: str, db: int, start: int, count: int) -> bytes:
        item = self.get(area, db)
        start = int(start)
        count = int(count)
        if start < 0 or count < 0:
            raise AreaError("起始地址与长度不能为负数")
        with self._lock:
            if start + count > item.size:
                raise AreaError(
                    f"越界：{item.key} 大小 {item.size}，请求 {start}..{start + count}"
                )
            return bytes(item.data[start:start + count])

    def write(self, area: str, db: int, start: int, data: bytes) -> None:
        item = self.get(area, db)
        start = int(start)
        payload = bytes(data)
        if start < 0:
            raise AreaError("起始地址不能为负数")
        with self._lock:
            if start + len(payload) > item.size:
                raise AreaError(
                    f"越界：{item.key} 大小 {item.size}，写入 {start}..{start + len(payload)}"
                )
            item.data[start:start + len(payload)] = payload
            self._version += 1

    def read_bit(self, area: str, db: int, byte: int, bit: int) -> bool:
        if not 0 <= int(bit) <= 7:
            raise AreaError("位号必须在 0 ~ 7 之间")
        value = self.read(area, db, int(byte), 1)[0]
        return bool(value & (1 << int(bit)))

    def write_bit(self, area: str, db: int, byte: int, bit: int, value: bool) -> None:
        if not 0 <= int(bit) <= 7:
            raise AreaError("位号必须在 0 ~ 7 之间")
        item = self.get(area, db)
        byte = int(byte)
        with self._lock:
            if byte < 0 or byte >= item.size:
                raise AreaError(f"越界：{item.key} 大小 {item.size}，位地址 {byte}.{bit}")
            mask = 1 << int(bit)
            if value:
                item.data[byte] |= mask
            else:
                item.data[byte] &= ~mask & 0xFF
            self._version += 1

    # 逻辑用的标量便捷方法 ------------------------------------------------
    def read_u16(self, area: str, db: int, start: int) -> int:
        return struct.unpack(">H", self.read(area, db, start, 2))[0]

    def write_u16(self, area: str, db: int, start: int, value: int) -> None:
        self.write(area, db, start, struct.pack(">H", int(value) & 0xFFFF))

    def read_i16(self, area: str, db: int, start: int) -> int:
        return struct.unpack(">h", self.read(area, db, start, 2))[0]

    def write_i16(self, area: str, db: int, start: int, value: int) -> None:
        self.write(area, db, start, struct.pack(">h", int(value)))

    def read_u32(self, area: str, db: int, start: int) -> int:
        return struct.unpack(">I", self.read(area, db, start, 4))[0]

    def write_u32(self, area: str, db: int, start: int, value: int) -> None:
        self.write(area, db, start, struct.pack(">I", int(value) & 0xFFFFFFFF))

    def read_i32(self, area: str, db: int, start: int) -> int:
        return struct.unpack(">i", self.read(area, db, start, 4))[0]

    def write_i32(self, area: str, db: int, start: int, value: int) -> None:
        self.write(area, db, start, struct.pack(">i", int(value)))

    def read_f32(self, area: str, db: int, start: int) -> float:
        return struct.unpack(">f", self.read(area, db, start, 4))[0]

    def write_f32(self, area: str, db: int, start: int, value: float) -> None:
        self.write(area, db, start, struct.pack(">f", float(value)))

    # ------------------------------------------------------------------
    # S7 服务端读写钩子（返回 S7 项目返回码）
    # ------------------------------------------------------------------
    def server_read(self, s7_area: S7Area, db: int, start: int,
                    count: int) -> tuple[int, bytearray]:
        item = self.resolve_wire(s7_area, db)
        if item is None:
            return 0x0A, bytearray()          # 对象不存在
        start = int(start)
        count = int(count)
        if start < 0 or count < 0 or start + count > item.size:
            return 0x05, bytearray()          # 地址越界
        with self._lock:
            return 0xFF, bytearray(item.data[start:start + count])

    def server_write(self, s7_area: S7Area, db: int, start: int,
                     payload: bytes | bytearray) -> int:
        item = self.resolve_wire(s7_area, db)
        if item is None:
            return 0x0A
        start = int(start)
        data = bytes(payload)
        if not data:
            return 0x07
        if start < 0 or start + len(data) > item.size:
            return 0x05
        with self._lock:
            item.data[start:start + len(data)] = data
            self._version += 1
        return 0xFF

    # ------------------------------------------------------------------
    # 导出 / 导入（界面用的 JSON 快照）
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {item.key: bytes(item.data).hex() for item in self._areas.values()}

    def restore(self, data: dict[str, Any]) -> int:
        restored = 0
        with self._lock:
            for key, hex_text in (data or {}).items():
                item = self._areas.get(str(key))
                if item is None:
                    continue
                try:
                    raw = bytes.fromhex(str(hex_text))
                except ValueError:
                    continue
                length = min(len(raw), item.size)
                item.data[:length] = raw[:length]
                if length < item.size:
                    item.data[length:] = b"\x00" * (item.size - length)
                restored += 1
            if restored:
                self._version += 1
        return restored
