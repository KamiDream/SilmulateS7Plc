"""DB 变量类型、TIA 风格布局与值编解码。

这里只做“声明层”的事情，不碰内存：

* ``parse_type``      解析 Bool / Int / Real / String[n] / Array[a..b] of ...
* ``layout_variables`` 按 S7 标准 DB 的对齐规则计算偏移量/位偏移
* ``encode_scalar`` / ``decode_scalar`` 在“声明类型”和“字节”之间转换

布局规则是经典 S7（非优化）DB 的规则：Bool 按位打包，Byte/Char 按 1 字节、
Word/Int 按 2 字节、DWord/DInt/Real 按 4 字节、LReal 按 8 字节对齐；
String 占 ``2 + n`` 字节，按 2 字节对齐。
"""
from __future__ import annotations

import math
import re
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_MAX_NAME = 64
_MAX_COMMENT = 200
_MAX_VARIABLES = 512
_MAX_ARRAY_ELEMENTS = 4096


@dataclass(frozen=True)
class ScalarType:
    name: str
    kind: str          # bool / uint / int / real / char / string
    size: int          # 占用字节数
    align: int         # 对齐字节数
    bits: int = 0      # 位宽（Bool=1）
    default: str = "0"
    display_hex: bool = False
    max_len: int | None = None

    @property
    def label(self) -> str:
        if self.kind == "bool":
            return f"{self.name} · 1 位"
        if self.kind == "string":
            return f"{self.name} · {self.size} 字节"
        return f"{self.name} · {self.size} 字节"


@dataclass(frozen=True)
class TypeSpec:
    kind: str                    # scalar / array
    name: str                    # 规范化后的类型名
    scalar: ScalarType | None = None
    element: ScalarType | None = None
    low: int = 0
    count: int = 0
    align: int = 1
    size: int = 1

    @property
    def is_array(self) -> bool:
        return self.kind == "array"

    @property
    def label(self) -> str:
        if self.is_array:
            return f"{self.name} · {self.size} 字节"
        return self.scalar.label if self.scalar else self.name


_BOOL = ScalarType("Bool", "bool", 1, 1, 1, "false")
_BYTE = ScalarType("Byte", "uint", 1, 1, 8, "16#00", True)
_SINT = ScalarType("SInt", "int", 1, 1, 8, "0")
_USINT = ScalarType("USInt", "uint", 1, 1, 8, "0")
_WORD = ScalarType("Word", "uint", 2, 2, 16, "16#0000", True)
_INT = ScalarType("Int", "int", 2, 2, 16, "0")
_UINT = ScalarType("UInt", "uint", 2, 2, 16, "0")
_DWORD = ScalarType("DWord", "uint", 4, 4, 32, "16#00000000", True)
_DINT = ScalarType("DInt", "int", 4, 4, 32, "0")
_UDINT = ScalarType("UDInt", "uint", 4, 4, 32, "0")
_REAL = ScalarType("Real", "real", 4, 4, 32, "0.0")
_LREAL = ScalarType("LReal", "real", 8, 8, 64, "0.0")
_CHAR = ScalarType("Char", "char", 1, 1, 8, " ")

_BASE_TYPES: dict[str, ScalarType] = {
    item.name.lower(): item
    for item in (
        _BOOL, _BYTE, _SINT, _USINT, _WORD, _INT, _UINT,
        _DWORD, _DINT, _UDINT, _REAL, _LREAL, _CHAR,
    )
}

_ARRAY_RE = re.compile(
    r"^array\s*\[\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*\]\s*of\s+(.+)$", re.IGNORECASE)
_STRING_RE = re.compile(r"^string\s*(?:\[\s*(\d+)\s*\])?$", re.IGNORECASE)
_INT_LITERAL_RE = re.compile(r"^(16|10|2|8)#([+-]?[0-9a-fA-F]+)$", re.IGNORECASE)


def parse_type(text: Any) -> TypeSpec:
    """解析用户输入的类型；返回规范化类型或抛 ValueError。"""
    raw = str(text or "").strip()
    if not raw:
        raw = "Bool"

    array_match = _ARRAY_RE.match(raw)
    if array_match:
        low = int(array_match.group(1))
        high = int(array_match.group(2))
        if high < low:
            raise ValueError(f"Array 上界小于下界: {raw}")
        count = high - low + 1
        if count > _MAX_ARRAY_ELEMENTS:
            raise ValueError(f"Array 元素过多（最多 {_MAX_ARRAY_ELEMENTS}）: {raw}")
        element = parse_scalar(array_match.group(3).strip())
        size = (count + 7) // 8 if element.kind == "bool" else count * element.size
        return TypeSpec(
            kind="array",
            name=f"Array[{low}..{high}] of {element.name}",
            element=element,
            low=low,
            count=count,
            align=element.align,
            size=size,
        )

    string_match = _STRING_RE.match(raw)
    if string_match:
        length = int(string_match.group(1)) if string_match.group(1) else 254
        if not 1 <= length <= 254:
            raise ValueError("String 长度必须在 1 ~ 254 之间")
        scalar = ScalarType(f"String[{length}]", "string", 2 + length, 2,
                            0, "", False, length)
        return TypeSpec(kind="scalar", name=scalar.name, scalar=scalar,
                        align=scalar.align, size=scalar.size)

    scalar = parse_scalar(raw)
    return TypeSpec(kind="scalar", name=scalar.name, scalar=scalar,
                    align=scalar.align, size=scalar.size)


def parse_scalar(text: Any) -> ScalarType:
    raw = str(text or "").strip()
    string_match = _STRING_RE.match(raw)
    if string_match:
        length = int(string_match.group(1)) if string_match.group(1) else 254
        if not 1 <= length <= 254:
            raise ValueError("String 长度必须在 1 ~ 254 之间")
        return ScalarType(f"String[{length}]", "string", 2 + length, 2,
                          0, "", False, length)
    scalar = _BASE_TYPES.get(raw.lower())
    if scalar is None:
        raise ValueError(f"不支持的数据类型: {raw!r}")
    return scalar


def align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) // alignment * alignment


def _start_list(value: Any, count: int, default: str) -> list[str]:
    if isinstance(value, list):
        items = [str(item) for item in value]
    elif value is None or value == "":
        items = []
    else:
        items = [str(value)]
    return (items + [default] * count)[:count]


# ---------------------------------------------------------------------------
# 「周期输出」—— 让一个 Bool 变量按指定周期自己输出 1 / 0
# ---------------------------------------------------------------------------
#: 周期字段的两种写法： ``500``（对称） / ``200/800``（不对称）
_PULSE_RE = re.compile(r"^\s*(\d+)\s*(?:[/:]\s*(\d+)\s*)?$")
PULSE_MIN_MS = 10
PULSE_MAX_MS = 3600000


def parse_pulse(text: Any) -> tuple[int, int] | None:
    """解析变量表里的「周期」列。

    返回 ``(输出 1 的毫秒数, 输出 0 的毫秒数)``；空值返回 ``None``（不起振）。
    两种写法：

    * ``500`` —— 500 ms 一个周期，1 / 0 各占一半（TIA 里用一个定时器）；
    * ``200/800`` —— 输出 1 持续 200 ms、输出 0 持续 800 ms（TIA 里用两个定时器）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _PULSE_RE.match(raw)
    if not match:
        raise ValueError(f"周期写法不对: {raw!r}（示例: 500 或 200/800）")
    head = int(match.group(1))
    if match.group(2) is None:
        on_ms = off_ms = head / 2.0
        if on_ms != int(on_ms):
            raise ValueError(f"周期 {head} ms 不能均分，请用 200/800 这样的写法")
        on_ms = off_ms = int(on_ms)
    else:
        on_ms, off_ms = head, int(match.group(2))
    for label, value in (("输出 1 的时间", on_ms), ("输出 0 的时间", off_ms)):
        if not PULSE_MIN_MS <= value <= PULSE_MAX_MS:
            raise ValueError(
                f"周期里{label}必须在 {PULSE_MIN_MS} ~ {PULSE_MAX_MS} ms 之间")
    return int(on_ms), int(off_ms)


def format_pulse(on_ms: int, off_ms: int) -> str:
    """把 ``(on, off)`` 还原成变量表里的写法（对称时写成单个周期值）。"""
    if int(on_ms) == int(off_ms):
        return str(int(on_ms) * 2)
    return f"{int(on_ms)}/{int(off_ms)}"


#: 允许配置周期输出的标量类型（Bool = 写位 1/0，SInt = 写字节 1/0）
PULSE_TYPES = ("Bool", "SInt")


def pulse_type_ok(spec: Any) -> bool:
    """该类型能不能配周期输出（只看类型，不看具体写法）。"""
    return (spec is not None and not spec.is_array and spec.scalar is not None
            and spec.scalar.name in PULSE_TYPES)


def normalize_pulse(text: Any, spec: Any, name: str = "") -> str:
    """校验并规范化「周期」字段；Bool / SInt 以外的类型会直接报错。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    label = f"变量 {name}: " if name else ""
    if spec is None or spec.is_array:
        raise ValueError(f"{label}周期输出只支持 Bool / SInt 标量变量，数组请改用单个变量")
    if not pulse_type_ok(spec):
        raise ValueError(
            f"{label}周期输出只支持 Bool / SInt 变量（当前类型 {spec.name}）")
    timings = parse_pulse(raw)
    return "" if timings is None else format_pulse(*timings)


def normalize_variables(raw: Any) -> list[dict[str, Any]]:
    """校验并规范化变量声明（不计算偏移）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("DB 变量表必须是数组")
    if len(raw) > _MAX_VARIABLES:
        raise ValueError(f"变量过多（最多 {_MAX_VARIABLES} 个）")

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 个变量不是对象")
        name = str(item.get("name") or f"Var{index + 1}").strip()[:_MAX_NAME]
        if not name:
            name = f"Var{index + 1}"
        if name.lower() in seen:
            raise ValueError(f"变量名重复: {name}")
        seen.add(name.lower())

        spec = parse_type(item.get("type"))
        if spec.is_array:
            assert spec.element is not None
            starts = _start_list(item.get("start"), spec.count, spec.element.default)
            start: str | list[str] = starts
        else:
            assert spec.scalar is not None
            value = item.get("start")
            start = spec.scalar.default if value is None else str(value)
        comment = str(item.get("comment") or "")[:_MAX_COMMENT].strip()
        pulse = normalize_pulse(item.get("pulse"), spec, name)
        result.append({
            "name": name,
            "type": spec.name,
            "start": start,
            "comment": comment,
            "pulse": pulse,
        })
    return result


def layout_variables(variables: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """按 S7 标准 DB 规则计算偏移量。

    返回 ``{"rows": [...], "required_size": int}``。每行包含：

    * ``index`` / ``name`` / ``type`` / ``kind`` / ``size`` / ``offset`` / ``bit``
    * ``offset_text``：TIA 风格的 ``0.0`` / ``2.0`` / ``3.5``
    * ``start`` / ``comment``
    * ``is_array`` 与 ``children``（Array 元素行）
    """
    offset = 0
    bit = 0
    rows: list[dict[str, Any]] = []

    for index, var in enumerate(variables):
        spec = parse_type(var.get("type"))
        row: dict[str, Any] = {
            "index": index,
            "name": str(var.get("name") or f"Var{index + 1}"),
            "type": spec.name,
            "comment": str(var.get("comment") or ""),
            "is_array": spec.is_array,
            "children": [],
        }

        if not spec.is_array:
            assert spec.scalar is not None
            scalar = spec.scalar
            if scalar.kind == "bool":
                if bit >= 8:
                    offset += 1
                    bit = 0
                row.update({
                    "kind": "bool",
                    "offset": offset,
                    "bit": bit,
                    "offset_text": f"{offset}.{bit}",
                    "size": 1,
                })
                row["start"] = str(var.get("start") or "false")
                bit += 1
            else:
                if bit > 0:
                    offset += 1
                    bit = 0
                offset = align_up(offset, scalar.align)
                row.update({
                    "kind": scalar.kind,
                    "offset": offset,
                    "bit": None,
                    "offset_text": f"{offset}.0",
                    "size": scalar.size,
                })
                row["start"] = str(var.get("start") if var.get("start") is not None
                                  else scalar.default)
                offset += scalar.size
            rows.append(row)
            continue

        # Array ---------------------------------------------------------
        assert spec.element is not None
        element = spec.element
        starts = _start_list(var.get("start"), spec.count, element.default)
        children: list[dict[str, Any]] = []
        row["kind"] = "array"
        row["start"] = starts

        if element.kind == "bool":
            if bit >= 8:
                offset += 1
                bit = 0
            start_offset, start_bit = offset, bit
            for element_index in range(spec.count):
                if bit >= 8:
                    offset += 1
                    bit = 0
                children.append({
                    "element_index": element_index,
                    "name": f"{row['name']}[{spec.low + element_index}]",
                    "type": element.name,
                    "kind": "bool",
                    "offset": offset,
                    "bit": bit,
                    "offset_text": f"{offset}.{bit}",
                    "size": 1,
                    "start": starts[element_index],
                })
                bit += 1
            row["offset"] = start_offset
            row["bit"] = start_bit
            row["offset_text"] = f"{start_offset}.{start_bit}"
            row["size"] = (offset + (1 if bit > 0 else 0)) - start_offset
        else:
            if bit > 0:
                offset += 1
                bit = 0
            offset = align_up(offset, element.align)
            start_offset = offset
            for element_index in range(spec.count):
                offset = align_up(offset, element.align)
                children.append({
                    "element_index": element_index,
                    "name": f"{row['name']}[{spec.low + element_index}]",
                    "type": element.name,
                    "kind": element.kind,
                    "offset": offset,
                    "bit": None,
                    "offset_text": f"{offset}.0",
                    "size": element.size,
                    "start": starts[element_index],
                })
                offset += element.size
            row["offset"] = start_offset
            row["bit"] = None
            row["offset_text"] = f"{start_offset}.0"
            row["size"] = offset - start_offset

        row["children"] = children
        rows.append(row)

    required = offset + (1 if bit > 0 else 0)
    return {"rows": rows, "required_size": required}


# ---------------------------------------------------------------------------
# 值编解码
# ---------------------------------------------------------------------------
def parse_bool_text(value: Any) -> bool:
    text = str(value).strip().lower()
    if text in ("1", "true", "on", "yes"):
        return True
    if text in ("0", "false", "off", "no", ""):
        return False
    raise ValueError(f"Bool 值只能是 true/false 或 1/0，收到 {value!r}")


def _parse_int_literal(value: Any) -> tuple[int, bool]:
    text = str(value).strip().replace("_", "")
    if not text:
        raise ValueError("整数值不能为空")
    match = _INT_LITERAL_RE.match(text)
    if match:
        return int(match.group(2), int(match.group(1))), True
    if text.lower().startswith(("0x", "0b", "0o")):
        return int(text, 0), True
    return int(text, 10), False


def encode_scalar(scalar: ScalarType, value: Any) -> bytes:
    """把文本值编码成该类型对应的字节（Bool 返回 1 字节 0/1）。"""
    if scalar.kind == "bool":
        return bytes([1 if parse_bool_text(value) else 0])

    if scalar.kind in ("uint", "int"):
        number, bit_pattern = _parse_int_literal(value)
        mask = (1 << scalar.bits) - 1
        if scalar.kind == "int":
            if bit_pattern:
                raw = number & mask
            else:
                minimum = -(1 << (scalar.bits - 1))
                maximum = (1 << (scalar.bits - 1)) - 1
                if not minimum <= number <= maximum:
                    raise ValueError(
                        f"{scalar.name} 范围是 {minimum} ~ {maximum}，收到 {value!r}")
                raw = number & mask
        else:
            if not 0 <= number <= mask:
                raise ValueError(f"{scalar.name} 范围是 0 ~ {mask}，收到 {value!r}")
            raw = number
        return raw.to_bytes(scalar.size, "big")

    if scalar.kind == "real":
        number = float(str(value).strip())
        if not math.isfinite(number):
            raise ValueError(f"{scalar.name} 必须是有限浮点数")
        return struct.pack(">f" if scalar.size == 4 else ">d", number)

    if scalar.kind == "char":
        text = str(value)
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
            text = text[1:-1]
        if not text:
            code = 0
        elif re.fullmatch(r"16#[0-9a-fA-F]{1,2}", text, re.IGNORECASE):
            code = int(text.split("#", 1)[1], 16)
        elif len(text) == 1:
            try:
                code = text.encode("latin-1")[0]
            except UnicodeEncodeError as exc:
                raise ValueError("Char 只支持 Latin-1 单字节字符") from exc
        else:
            raise ValueError("Char 只能是单个字符")
        return bytes([code & 0xFF])

    if scalar.kind == "string":
        text = str(value)
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
            text = text[1:-1]
        try:
            raw = text.encode("latin-1", "replace")
        except Exception:                                      # noqa: BLE001
            raw = text.encode("ascii", "replace")
        maximum = scalar.max_len or 254
        if len(raw) > maximum:
            raise ValueError(f"{scalar.name} 最多 {maximum} 个字符")
        payload = bytearray(scalar.size)
        payload[0] = maximum & 0xFF
        payload[1] = len(raw) & 0xFF
        payload[2:2 + len(raw)] = raw
        return bytes(payload)

    raise ValueError(f"无法编码类型 {scalar.name}")


def decode_scalar(scalar: ScalarType, data: bytes | bytearray) -> str:
    raw = bytes(data)
    if scalar.kind == "bool":
        return "true" if raw and raw[0] & 1 else "false"

    if scalar.kind in ("uint", "int"):
        value = int.from_bytes(raw[:scalar.size], "big")
        if scalar.kind == "int" and value >= (1 << (scalar.bits - 1)):
            value -= 1 << scalar.bits
        if scalar.display_hex:
            return "16#" + f"{int.from_bytes(raw[:scalar.size], 'big'):0{scalar.size * 2}X}"
        return str(value)

    if scalar.kind == "real":
        value = struct.unpack(">f" if scalar.size == 4 else ">d",
                              raw[:scalar.size])[0]
        if not math.isfinite(value):
            return "NaN"
        return format(value, ".7g" if scalar.size == 4 else ".15g")

    if scalar.kind == "char":
        code = raw[0] if raw else 0
        if code == 0:
            return ""
        if 32 <= code < 127:
            return chr(code)
        return f"16#{code:02X}"

    if scalar.kind == "string":
        if len(raw) < 2:
            return ""
        actual = min(raw[1], len(raw) - 2)
        return bytes(raw[2:2 + actual]).decode("latin-1", "replace")

    return ""


def type_suggestions() -> list[dict[str, str]]:
    """给界面 datalist 用的常用类型建议（不是限制，类型列可以自由输入）。"""
    suggestions: list[dict[str, str]] = []
    for scalar in (_BOOL, _BYTE, _SINT, _USINT, _WORD, _INT, _UINT,
                   _DWORD, _DINT, _UDINT, _REAL, _LREAL, _CHAR):
        suggestions.append({"value": scalar.name, "label": scalar.label})
    for length in (10, 20, 50, 80):
        scalar = ScalarType(f"String[{length}]", "string", 2 + length, 2,
                            0, "", False, length)
        suggestions.append({"value": scalar.name, "label": scalar.label})
    for type_name in ("Bool", "Byte", "Int", "DInt", "Real", "String[20]"):
        spec = parse_type(f"Array[0..9] of {type_name}")
        suggestions.append({"value": spec.name, "label": spec.label})
    return suggestions
