"""PLC 变量（I / Q / M）的 TIA 风格声明与地址解析。

与 DB 不同，I/Q/M 变量使用绝对地址，不需要自动偏移：

    StartButton   Bool    I0.0
    Speed         Int     MW10
    Temperature   Real    MD20
    Name          String[20]  MB30

支持 ``%I0.0`` / ``I0.0`` / ``IB0`` / ``IW0`` / ``ID0`` 等写法；写回时统一
规范化成标准地址。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .dbtypes import (ScalarType, decode_scalar, encode_scalar, normalize_pulse,
                      parse_type, type_suggestions)

_ADDR_RE = re.compile(
    r"^%?\s*([IQM])\s*(?:([BWD])\s*)?(\d+)(?:\.\s*([0-7]))?$",
    re.IGNORECASE,
)
_MAX_TAGS = 512
_MAX_TAG_TABLES = 64
_MAX_AREA_BYTES = 65536


def _prefix_for(scalar: ScalarType) -> str:
    if scalar.size <= 1:
        return "B"
    if scalar.size == 2:
        return "W"
    return "D"


def parse_tag_address(text: Any, spec: Any) -> dict[str, Any]:
    """解析绝对地址并按类型规范化。

    Bool 需要/默认一个位地址；非 Bool 使用字节偏移，输入里的位号会被忽略
    （例如把 Bool 的 ``M10.3`` 改成 Int 时自动变成 ``MW10``）。
    """
    raw = str(text or "").strip()
    match = _ADDR_RE.match(raw)
    if not match:
        raise ValueError(f"地址不合法: {raw!r}（示例: I0.0 / Q0.1 / MW10 / MD20）")
    area = match.group(1).upper()
    offset = int(match.group(3))
    bit_value = match.group(4)
    bit = int(bit_value) if bit_value is not None else None
    if not 0 <= offset <= 65535:
        raise ValueError("地址偏移必须在 0 ~ 65535 之间")
    if spec.is_array:
        raise ValueError("PLC 变量暂不支持数组类型，请使用数据块页")

    scalar = spec.scalar
    if scalar is None:
        raise ValueError("无效的数据类型")
    if scalar.kind == "bool":
        if bit is None:
            bit = 0
        address = f"{area}{offset}.{bit}"
    else:
        bit = None
        if offset + scalar.size > _MAX_AREA_BYTES:
            raise ValueError(
                f"地址 {raw} + {scalar.name} 超出 {_MAX_AREA_BYTES} 字节")
        address = f"{area}{_prefix_for(scalar)}{offset}"
    return {
        "area": area,
        "offset": offset,
        "bit": bit,
        "address": address,
        "size": scalar.size,
        "kind": scalar.kind,
        "type": spec.name,
    }


def normalize_tags(raw: Any) -> list[dict[str, Any]]:
    """校验并规范化变量声明。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("PLC 变量表必须是数组")
    if len(raw) > _MAX_TAGS:
        raise ValueError(f"变量过多（最多 {_MAX_TAGS} 个）")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 个变量不是对象")
        name = str(item.get("name") or f"Tag{index + 1}").strip()[:64]
        if not name:
            name = f"Tag{index + 1}"
        if name.lower() in seen:
            raise ValueError(f"变量名重复: {name}")
        seen.add(name.lower())
        spec = parse_type(item.get("type"))
        parsed = parse_tag_address(item.get("address"), spec)
        comment = str(item.get("comment") or "")[:200].strip()
        result.append({
            "name": name,
            "type": spec.name,
            "address": parsed["address"],
            "comment": comment,
            "pulse": normalize_pulse(item.get("pulse"), spec, name),
        })
    return result


def layout_tags(tags: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """返回变量行与各区域所需的最小字节数。"""
    rows: list[dict[str, Any]] = []
    required = {"I": 0, "Q": 0, "M": 0}
    for index, tag in enumerate(tags):
        spec = parse_type(tag.get("type"))
        parsed = parse_tag_address(tag.get("address"), spec)
        area = parsed["area"]
        end = int(parsed["offset"]) + (1 if parsed["kind"] == "bool" else int(parsed["size"]))
        required[area] = max(required[area], end)
        rows.append({
            "index": index,
            "name": str(tag.get("name") or f"Tag{index + 1}"),
            "type": parsed["type"],
            "address": parsed["address"],
            "area": area,
            "offset": parsed["offset"],
            "bit": parsed["bit"],
            "size": parsed["size"],
            "kind": parsed["kind"],
            "comment": str(tag.get("comment") or ""),
            "pulse": str(tag.get("pulse") or ""),
        })
    return rows, required


def tag_type_suggestions() -> list[dict[str, str]]:
    """I/Q/M 变量可用的标量类型（数组类型留给 DB 页）。"""
    return [item for item in type_suggestions()
            if not str(item["value"]).lower().startswith("array")]


#: 允许的区域顺序（侧边栏也是这个顺序）
TAG_AREAS = ("I", "Q", "M")


def tag_areas(tags: Iterable[dict[str, Any]]) -> set[str]:
    """返回一组变量声明涉及的区域。"""
    areas: set[str] = set()
    for tag in tags:
        spec = parse_type(tag.get("type"))
        areas.add(parse_tag_address(tag.get("address"), spec)["area"])
    return areas


# ---------------------------------------------------------------------------
# 多张变量表（类似 TIA 的多个 PLC 变量表 / 类似 DB 的多页）
# ---------------------------------------------------------------------------
def normalize_tag_tables(raw: Any) -> list[dict[str, Any]]:
    """校验并规范化变量表列表。

    每张变量表：``{"id", "area", "name", "comment", "variables"}``，其中
    ``area`` 是 I / Q / M，且该表只能声明本区域的地址。

    旧版本（没有 ``area`` 字段）的变量表会按变量地址自动归属；如果一张表
    混合了多个区域，会拆成多张以区域后缀命名的表。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("变量表必须是数组")
    if len(raw) > _MAX_TAG_TABLES:
        raise ValueError(f"变量表过多（最多 {_MAX_TAG_TABLES} 张）")

    explicit: set[int] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 张变量表不是对象")
        value = item.get("id")
        if value in (None, ""):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"变量表编号不合法: {value!r}") from exc
        if not 1 <= number <= 65535:
            raise ValueError("变量表编号必须在 1 ~ 65535 之间")
        if number in explicit:
            raise ValueError(f"变量表编号重复: {number}")
        explicit.add(number)

    used_ids = set(explicit)
    used_names: set[str] = set()

    def alloc_id() -> int:
        number = 1
        while number in used_ids:
            number += 1
        used_ids.add(number)
        return number

    def take_name(base: str) -> str:
        base = (base or "变量表").strip()[:40] or "变量表"
        name = base
        suffix = 2
        while name.lower() in used_names:
            extra = f" ({suffix})"
            name = (base[:40 - len(extra)] + extra).strip()
            suffix += 1
        used_names.add(name.lower())
        return name

    tables: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        variables = normalize_tags(item.get("variables"))
        raw_name = str(item.get("name") or "").strip()[:40]
        comment = str(item.get("comment") or "")[:80].strip()
        value = item.get("id")
        table_id = int(value) if value not in (None, "") else alloc_id()
        area = str(item.get("area") or "").strip().upper()
        if area and area not in TAG_AREAS:
            raise ValueError(f"变量表区域只能是 I / Q / M，收到 {area!r}")
        if area:
            foreign = tag_areas(variables) - {area}
            if foreign:
                bad = next(variable for variable in variables
                           if parse_tag_address(
                               variable["address"], parse_type(variable["type"]))["area"]
                           != area)
                raise ValueError(
                    f"变量表「{raw_name or table_id}」属于 {area} 区，"
                    f"但变量 {bad['name']} 的地址 {bad['address']} 不在 {area} 区")
            parts = [(area, variables)]
        else:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for variable in variables:
                parsed = parse_tag_address(variable["address"],
                                           parse_type(variable["type"]))
                grouped.setdefault(parsed["area"], []).append(variable)
            if not grouped:
                parts = [("M", [])]
            elif len(grouped) == 1:
                parts = [(next(iter(grouped)), variables)]
            else:
                # 旧版混合表：拆成三张（只保留实际出现的区域）
                parts = [(key, grouped[key]) for key in TAG_AREAS if key in grouped]

        for part_index, (part_area, part_vars) in enumerate(parts):
            if part_index == 0:
                part_id = table_id
                part_name = take_name(raw_name or f"变量表{table_id}")
            else:
                part_id = alloc_id()
                part_name = take_name(f"{raw_name or f'变量表{table_id}'} · {part_area}")
            tables.append({
                "id": part_id,
                "area": part_area,
                "name": part_name,
                "comment": comment,
                "variables": part_vars,
            })
    if len(tables) > _MAX_TAG_TABLES:
        raise ValueError(f"变量表过多（最多 {_MAX_TAG_TABLES} 张）")
    return sorted(tables, key=lambda table: table["id"])


def _bit_span(row: dict[str, Any]) -> tuple[int, int]:
    """把一行变量换算成位区间 ``[start, end)``，用于跨类型比较重叠。"""
    if row.get("kind") == "bool":
        start = int(row["offset"]) * 8 + int(row["bit"])
        return start, start + 1
    start = int(row["offset"]) * 8
    return start, (int(row["offset"]) + int(row["size"])) * 8


def find_tag_conflicts(
        groups: Iterable[tuple[Any, str, Iterable[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """检查多张变量表之间的地址冲突。

    ``groups`` 的每一项是 ``(表编号, 表名称, 变量声明)``。返回结构化冲突：
    ``keys`` 是涉及的表编号，``indexes`` 是对应表内的变量下标。
    同一 I/Q/M 地址空间里，Bool 只占 1 位，其它类型按字节区间比较。
    """
    by_area: dict[str, list[dict[str, Any]]] = {"I": [], "Q": [], "M": []}
    for key, label, tags in groups:
        rows, _ = layout_tags(tags)
        for row in rows:
            start, end = _bit_span(row)
            by_area.setdefault(row["area"], []).append({
                "start": start,
                "end": end,
                "row": row,
                "key": key,
                "label": label,
            })

    conflicts: list[dict[str, Any]] = []
    for area, items in by_area.items():
        items.sort(key=lambda item: (item["start"], item["end"]))
        previous: dict[str, Any] | None = None
        for item in items:
            if previous is not None and item["start"] < previous["end"]:
                first = previous
                second = item
                conflicts.append({
                    "area": area,
                    "keys": [first["key"], second["key"]],
                    "indexes": [int(first["row"]["index"]),
                                int(second["row"]["index"])],
                    "message": (
                        f"{area} 区地址冲突：{first['label']} "
                        f"{first['row']['name']} ({first['row']['address']}) 与 "
                        f"{second['label']} {second['row']['name']} "
                        f"({second['row']['address']}) 重叠"),
                })
            if previous is None or item["end"] > previous["end"]:
                previous = item
    return conflicts


def combined_required(
        groups: Iterable[tuple[Any, str, Iterable[dict[str, Any]]]]) -> dict[str, int]:
    """所有变量表合并后，I/Q/M 各区至少需要的字节数。"""
    required = {"I": 0, "Q": 0, "M": 0}
    for _key, _label, tags in groups:
        _, one = layout_tags(tags)
        for area in required:
            required[area] = max(required[area], int(one.get(area, 0)))
    return required


def encode_tag_value(kind: ScalarType, value: Any) -> bytes:
    return encode_scalar(kind, value)


def decode_tag_value(kind: ScalarType, data: bytes | bytearray) -> str:
    return decode_scalar(kind, data)
