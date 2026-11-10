"""SCL（Structured Control Language）常用子集的编译器与解释器。

支持的范围（面向开关量 / 数值逻辑调试，不追求完整 TIA 语法）：

* 赋值：``"Tag" := 表达式;``、``"DB1".Field := 表达式;``、``%Q0.0 := %I0.0;``
* 条件：``IF cond THEN ... ELSIF cond THEN ... ELSE ... END_IF;``
* 运算：``+ - * / MOD = <> < > <= >= AND OR XOR NOT``
* 常量：整数、实数、``TRUE`` / ``FALSE``、``16#FF``
* 函数：``ABS SQRT MIN MAX LIMIT ROUND TRUNC LOG`` 以及常用类型转换
* 注释：``//``、``(* ... *)``、``/* ... */``

所有变量名都必须来自当前的 I/Q/M 变量表或数据块；也支持绝对地址。
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from typing import Any

from .dbtypes import ScalarType, encode_scalar, layout_variables, parse_type
from .tags import parse_tag_address


class SclError(ValueError):
    """SCL 编译/运行错误（带行列信息）。"""

    def __init__(self, message: str, line: int = 0, column: int = 0) -> None:
        if line:
            super().__init__(f"第 {line} 行第 {column} 列: {message}")
        else:
            super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------
@dataclass
class Token:
    kind: str
    value: Any
    line: int
    column: int


_KEYWORDS = {
    "IF", "THEN", "ELSIF", "ELSE", "END_IF",
    "AND", "OR", "XOR", "NOT", "MOD", "TRUE", "FALSE",
}

_TOKEN_RE = re.compile(r"""
 (?P<WS>\s+)
|(?P<COMMENT>//[^\n]*|\(\*.*?\*\)|/\*.*?\*/)
|(?P<NUMBER>(?:16|2|8)\#[0-9A-Fa-f_]+|\d+\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)
|(?P<STRING>'(?:[^']|'')*')
|(?P<QUOTED>"(?:[^"]|"")*")
|(?P<ADDRESS>%?(?:[IQM][BWD]?\d+(?:\.\d+)?|DB\d+\.(?:DB[BWDX]\d+(?:\.\d+)?|[A-Za-z_]\w*)))
|(?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
|(?P<OP>:=|<=|>=|<>|[-+*/()<>=.,;:])
""", re.VERBOSE | re.DOTALL)


def _number(text: str) -> Any:
    raw = text.replace("_", "")
    if "#" in raw:
        base, digits = raw.split("#", 1)
        return int(digits, int(base))
    if "." in raw or "e" in raw.lower():
        return float(raw)
    return int(raw)


def tokenize(source: str) -> list[Token]:
    line_starts = [0] + [index + 1 for index, char in enumerate(source)
                         if char == "\n"]

    def position(index: int) -> tuple[int, int]:
        line = bisect.bisect_right(line_starts, index)
        return line, index - line_starts[line - 1] + 1

    tokens: list[Token] = []
    pos = 0
    while pos < len(source):
        match = _TOKEN_RE.match(source, pos)
        if match is None:
            line, column = position(pos)
            raise SclError(f"无法识别的字符 {source[pos]!r}", line, column)
        kind = match.lastgroup or "UNKNOWN"
        text = match.group()
        if kind not in ("WS", "COMMENT"):
            line, column = position(pos)
            if kind == "NUMBER":
                value: Any = _number(text)
            elif kind == "STRING":
                value = text[1:-1].replace("''", "'")
            elif kind == "QUOTED":
                value = text[1:-1].replace('""', '"')
            elif kind == "IDENT" and text.upper() in _KEYWORDS:
                kind, value = "KEYWORD", text.upper()
            elif kind == "ADDRESS":
                value = text.removeprefix("%")
            else:
                value = text
            tokens.append(Token(kind, value, line, column))
        pos = match.end()
    line, column = position(len(source))
    tokens.append(Token("EOF", "", line, column))
    return tokens


# ---------------------------------------------------------------------------
# 语法树
# ---------------------------------------------------------------------------
@dataclass
class Literal:
    line: int
    column: int
    value: Any


@dataclass
class VarRef:
    line: int
    column: int
    path: list[str]


@dataclass
class Call:
    line: int
    column: int
    name: str
    args: list[Any]


@dataclass
class Unary:
    line: int
    column: int
    op: str
    operand: Any


@dataclass
class Binary:
    line: int
    column: int
    op: str
    left: Any
    right: Any


@dataclass
class Assign:
    line: int
    column: int
    target: VarRef
    value: Any


@dataclass
class IfStmt:
    line: int
    column: int
    branches: list[tuple[Any, list[Any]]]
    otherwise: list[Any]


@dataclass
class CallStmt:
    line: int
    column: int
    call: Call


# ---------------------------------------------------------------------------
# 语法分析
# ---------------------------------------------------------------------------
_WRAPPER_HEAD = re.compile(
    r"^\s*(?:PROGRAM|ORGANIZATION_BLOCK|FUNCTION_BLOCK|FUNCTION)\b.*$",
    re.IGNORECASE)
_WRAPPER_TAIL = re.compile(
    r"^\s*END_(?:PROGRAM|ORGANIZATION_BLOCK|FUNCTION_BLOCK|FUNCTION)\s*;?\s*$",
    re.IGNORECASE)
_BEGIN_LINE = re.compile(r"^\s*BEGIN\s*;?\s*$", re.IGNORECASE)


def _strip_wrapper(source: str) -> str:
    """容忍从 TIA 粘贴的 PROGRAM ... BEGIN ... END_PROGRAM 外壳（保留行号）。"""
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if (_WRAPPER_HEAD.match(line) or _WRAPPER_TAIL.match(line)
                or _BEGIN_LINE.match(line)):
            lines[index] = ""
    return "\n".join(lines)


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    # -- token helpers --------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        index = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def advance(self) -> Token:
        token = self.peek()
        if token.kind != "EOF":
            self.index += 1
        return token

    def at(self, kind: str, value: str | None = None) -> bool:
        token = self.peek()
        if token.kind != kind:
            return False
        if value is None:
            return True
        return str(token.value).upper() == value.upper()

    def at_keyword(self, value: str) -> bool:
        return self.at("KEYWORD", value)

    def eat(self, kind: str, value: str | None = None) -> Token | None:
        if self.at(kind, value):
            return self.advance()
        return None

    def expect(self, kind: str, value: str | None = None) -> Token:
        if self.at(kind, value):
            return self.advance()
        token = self.peek()
        expected = value or kind
        got = token.value if token.kind != "EOF" else "文件结尾"
        raise SclError(f"期望 {expected}，实际是 {got}", token.line, token.column)

    # -- statements -----------------------------------------------------
    def parse(self) -> list[Any]:
        body: list[Any] = []
        while not self.at("EOF"):
            if self.eat("OP", ";"):
                continue
            body.append(self.statement())
        return body

    def statement(self) -> Any:
        token = self.peek()
        if token.kind == "KEYWORD":
            if token.value == "IF":
                return self.if_statement()
            raise SclError(
                f"暂不支持关键字 {token.value}（支持 IF/THEN/ELSIF/ELSE/END_IF）",
                token.line, token.column)
        if (token.kind == "IDENT" and self.peek(1).kind == "OP"
                and self.peek(1).value == "("):
            call = self.call()
            self.expect("OP", ";")
            return CallStmt(call.line, call.column, call)
        return self.assignment()

    def assignment(self) -> Assign:
        target = self.reference()
        self.expect("OP", ":=")
        value = self.expression()
        self.expect("OP", ";")
        return Assign(target.line, target.column, target, value)

    def if_statement(self) -> IfStmt:
        token = self.expect("KEYWORD", "IF")
        condition = self.expression()
        self.expect("KEYWORD", "THEN")
        stops = {"ELSIF", "ELSE", "END_IF"}
        body = self.block(stops)
        branches: list[tuple[Any, list[Any]]] = [(condition, body)]
        while self.at_keyword("ELSIF"):
            self.advance()
            cond = self.expression()
            self.expect("KEYWORD", "THEN")
            branches.append((cond, self.block(stops)))
        otherwise: list[Any] = []
        if self.eat("KEYWORD", "ELSE"):
            otherwise = self.block(stops)
        self.expect("KEYWORD", "END_IF")
        self.eat("OP", ";")
        return IfStmt(token.line, token.column, branches, otherwise)

    def block(self, stops: set[str]) -> list[Any]:
        body: list[Any] = []
        while not self.at("EOF"):
            token = self.peek()
            if token.kind == "KEYWORD" and token.value in stops:
                break
            if self.eat("OP", ";"):
                continue
            body.append(self.statement())
        if self.at("EOF"):
            token = self.peek()
            raise SclError(f"缺少 {'/'.join(sorted(stops))}", token.line, token.column)
        return body

    # -- expressions ----------------------------------------------------
    def expression(self) -> Any:
        return self.or_expr()

    def or_expr(self) -> Any:
        node = self.xor_expr()
        while self.at_keyword("OR"):
            token = self.advance()
            node = Binary(token.line, token.column, "OR", node, self.xor_expr())
        return node

    def xor_expr(self) -> Any:
        node = self.and_expr()
        while self.at_keyword("XOR"):
            token = self.advance()
            node = Binary(token.line, token.column, "XOR", node, self.and_expr())
        return node

    def and_expr(self) -> Any:
        node = self.comparison()
        while self.at_keyword("AND"):
            token = self.advance()
            node = Binary(token.line, token.column, "AND", node, self.comparison())
        return node

    def comparison(self) -> Any:
        node = self.additive()
        while True:
            token = self.peek()
            if token.kind == "OP" and token.value in ("=", "<>", "<", ">", "<=", ">="):
                self.advance()
                node = Binary(token.line, token.column, token.value, node,
                              self.additive())
            else:
                return node

    def additive(self) -> Any:
        node = self.multiplicative()
        while True:
            token = self.peek()
            if token.kind == "OP" and token.value in ("+", "-"):
                self.advance()
                node = Binary(token.line, token.column, token.value, node,
                              self.multiplicative())
            else:
                return node

    def multiplicative(self) -> Any:
        node = self.unary()
        while True:
            token = self.peek()
            if token.kind == "OP" and token.value in ("*", "/"):
                self.advance()
                node = Binary(token.line, token.column, token.value, node,
                              self.unary())
            elif token.kind == "KEYWORD" and token.value == "MOD":
                self.advance()
                node = Binary(token.line, token.column, "MOD", node, self.unary())
            else:
                return node

    def unary(self) -> Any:
        token = self.peek()
        if token.kind == "KEYWORD" and token.value == "NOT":
            self.advance()
            return Unary(token.line, token.column, "NOT", self.unary())
        if token.kind == "OP" and token.value in ("-", "+"):
            self.advance()
            return Unary(token.line, token.column, token.value, self.unary())
        return self.primary()

    def primary(self) -> Any:
        token = self.peek()
        if token.kind == "NUMBER":
            self.advance()
            return Literal(token.line, token.column, token.value)
        if token.kind == "STRING":
            self.advance()
            return Literal(token.line, token.column, token.value)
        if token.kind == "KEYWORD":
            if token.value in ("TRUE", "FALSE"):
                self.advance()
                return Literal(token.line, token.column, token.value == "TRUE")
            raise SclError(f"表达式里不能使用关键字 {token.value}",
                           token.line, token.column)
        if token.kind == "OP" and token.value == "(":
            self.advance()
            node = self.expression()
            self.expect("OP", ")")
            return node
        if (token.kind == "IDENT" and self.peek(1).kind == "OP"
                and self.peek(1).value == "("):
            return self.call()
        if token.kind in ("IDENT", "QUOTED", "ADDRESS"):
            return self.reference()
        got = token.value if token.kind != "EOF" else "文件结尾"
        raise SclError(f"无法解析表达式 {got}", token.line, token.column)

    def call(self) -> Call:
        token = self.advance()
        self.expect("OP", "(")
        args: list[Any] = []
        if not self.at("OP", ")"):
            args.append(self.expression())
            while self.eat("OP", ","):
                args.append(self.expression())
        self.expect("OP", ")")
        return Call(token.line, token.column, str(token.value), args)

    def reference(self) -> VarRef:
        token = self.advance()
        if token.kind in ("ADDRESS", "IDENT", "QUOTED"):
            path = [str(token.value)]
        else:
            got = token.value if token.kind != "EOF" else "文件结尾"
            raise SclError(f"这里需要变量，实际是 {got}", token.line, token.column)
        while self.eat("OP", "."):
            part = self.advance()
            if part.kind in ("IDENT", "QUOTED", "NUMBER", "ADDRESS"):
                path.append(str(part.value))
            else:
                raise SclError("变量名不合法", part.line, part.column)
        return VarRef(token.line, token.column, path)


# ---------------------------------------------------------------------------
# 符号表
# ---------------------------------------------------------------------------
@dataclass
class Symbol:
    label: str
    area: str
    db: int
    offset: int
    bit: int | None
    scalar: ScalarType


def _scalar_of(name: str) -> ScalarType:
    scalar = parse_type(name).scalar
    assert scalar is not None
    return scalar


_BOOL_TYPE = _scalar_of("Bool")
_BYTE_TYPE = _scalar_of("Byte")
_INT_TYPE = _scalar_of("Int")
_DINT_TYPE = _scalar_of("DInt")

_DB_ABS_RE = re.compile(r"^DB(\d+)\.DB([XBWD])(\d+)(?:\.(\d+))?$", re.IGNORECASE)
_AREA_ABS_RE = re.compile(r"^([IQM])([BWD])?(\d+)(?:\.(\d+))?$", re.IGNORECASE)

_NUMERIC_KINDS = ("int", "uint", "real")


class SymbolTable:
    def __init__(self, config: dict[str, Any]) -> None:
        self.tags: dict[str, list[Symbol]] = {}
        self.db_fields: dict[str, Symbol] = {}
        self._build(config)

    # -- 构建 -----------------------------------------------------------
    def _build(self, config: dict[str, Any]) -> None:
        for table in config.get("tag_tables") or []:
            for variable in table.get("variables") or []:
                try:
                    spec = parse_type(variable.get("type"))
                    if spec.is_array or spec.scalar is None:
                        continue
                    parsed = parse_tag_address(variable.get("address"), spec)
                except ValueError:
                    continue
                name = str(variable.get("name") or "").lower()
                symbol = Symbol(
                    label=f"{variable.get('name')} ({parsed['address']})",
                    area=str(parsed["area"]),
                    db=0,
                    offset=int(parsed["offset"]),
                    bit=parsed["bit"],
                    scalar=spec.scalar,
                )
                self.tags.setdefault(name, []).append(symbol)

        for db in config.get("dbs") or []:
            number = int(db["number"])
            db_name = str(db.get("name") or f"DB{number}")
            layout = layout_variables(db.get("variables") or [])
            for row in layout["rows"]:
                if row.get("is_array"):
                    continue
                scalar = parse_type(row["type"]).scalar
                if scalar is None:
                    continue
                symbol = Symbol(
                    label=f"{db_name}.{row['name']}",
                    area="DB",
                    db=number,
                    offset=int(row["offset"]),
                    bit=row.get("bit"),
                    scalar=scalar,
                )
                field = str(row["name"]).lower()
                self.db_fields[f"db{number}.{field}"] = symbol
                self.db_fields[f"{db_name.lower()}.{field}"] = symbol

    # -- 解析 -----------------------------------------------------------
    def resolve(self, path: list[str], node: Any) -> Symbol:
        if len(path) == 1:
            name = str(path[0]).strip()
            key = name.removeprefix("%")
            db_key = key.lower()
            if db_key in self.db_fields:
                return self.db_fields[db_key]
            candidates = self.tags.get(name.lower())
            if candidates:
                unique = {
                    (item.area, item.db, item.offset, item.bit, item.scalar.name): item
                    for item in candidates
                }
                if len(unique) > 1:
                    labels = "、".join(sorted({item.label for item in candidates}))
                    raise SclError(
                        f"变量名 {name} 在多张变量表中重复（{labels}），"
                        f"请改用绝对地址", node.line, node.column)
                return next(iter(unique.values()))
            absolute = self._absolute(key, node)
            if absolute is not None:
                return absolute
            raise SclError(f"未知变量 {name}", node.line, node.column)

        if len(path) == 2:
            base, field = str(path[0]).strip(), str(path[1]).strip()
            base = base.removeprefix("%")
            match = re.fullmatch(r"DB(\d+)", base, re.IGNORECASE)
            if match is not None:
                key = f"db{int(match.group(1))}.{field.lower()}"
            else:
                key = f"{base.lower()}.{field.lower()}"
            if key in self.db_fields:
                return self.db_fields[key]
            raise SclError(f"未知数据块变量 {base}.{field}",
                           node.line, node.column)

        raise SclError(f"不支持的变量写法 {'.'.join(path)}",
                       node.line, node.column)

    def _absolute(self, text: str, node: Any) -> Symbol | None:
        match = _DB_ABS_RE.match(text)
        if match:
            number = int(match.group(1))
            code = match.group(2).upper()
            offset = int(match.group(3))
            bit_text = match.group(4)
            if code == "X":
                bit = int(bit_text) if bit_text is not None else 0
                if not 0 <= bit <= 7:
                    raise SclError("位号必须在 0 ~ 7 之间", node.line, node.column)
                return Symbol(f"DB{number}.DBX{offset}.{bit}", "DB", number,
                              offset, bit, _BOOL_TYPE)
            scalar = {"B": _BYTE_TYPE, "W": _INT_TYPE, "D": _DINT_TYPE}[code]
            return Symbol(f"DB{number}.DB{code}{offset}", "DB", number,
                          offset, None, scalar)

        match = _AREA_ABS_RE.match(text)
        if match:
            area = match.group(1).upper()
            code = (match.group(2) or "").upper()
            offset = int(match.group(3))
            bit_text = match.group(4)
            if bit_text is not None:
                bit = int(bit_text)
                if not 0 <= bit <= 7:
                    raise SclError("位号必须在 0 ~ 7 之间", node.line, node.column)
                return Symbol(f"{area}{offset}.{bit}", area, 0, offset, bit,
                              _BOOL_TYPE)
            if code == "B":
                scalar = _BYTE_TYPE
            elif code == "W":
                scalar = _INT_TYPE
            elif code == "D":
                scalar = _DINT_TYPE
            else:
                raise SclError(
                    f"绝对地址 {text} 需要位号（如 {area}{offset}.0）"
                    f"或 B/W/D 前缀（如 {area}W{offset}）",
                    node.line, node.column)
            return Symbol(f"{area}{code}{offset}", area, 0, offset, None, scalar)
        return None


def _ensure_supported(symbol: Symbol, node: Any) -> None:
    if symbol.scalar.kind not in ("bool",) + _NUMERIC_KINDS:
        raise SclError(
            f"SCL 暂不支持 {symbol.scalar.name} 类型变量 {symbol.label}",
            node.line, node.column)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
_FUNCTIONS: dict[str, int] = {
    "ABS": 1, "SQRT": 1, "MIN": 2, "MAX": 2, "LIMIT": 3,
    "ROUND": 1, "TRUNC": 1, "LOG": 1, "TRACE": 1,
    "INT_TO_REAL": 1, "DINT_TO_REAL": 1,
    "REAL_TO_INT": 1, "REAL_TO_DINT": 1, "BOOL_TO_INT": 1,
}


def _validate(node: Any, symbols: SymbolTable) -> None:
    if isinstance(node, Literal):
        return
    if isinstance(node, VarRef):
        _ensure_supported(symbols.resolve(node.path, node), node)
        return
    if isinstance(node, Unary):
        _validate(node.operand, symbols)
        return
    if isinstance(node, Binary):
        _validate(node.left, symbols)
        _validate(node.right, symbols)
        return
    if isinstance(node, Call):
        name = node.name.upper()
        if name not in _FUNCTIONS:
            raise SclError(f"未知函数 {node.name}", node.line, node.column)
        expected = _FUNCTIONS[name]
        if len(node.args) != expected:
            raise SclError(f"{name} 需要 {expected} 个参数，收到 {len(node.args)} 个",
                           node.line, node.column)
        for arg in node.args:
            _validate(arg, symbols)
        return
    if isinstance(node, Assign):
        _ensure_supported(symbols.resolve(node.target.path, node.target), node.target)
        _validate(node.value, symbols)
        return
    if isinstance(node, IfStmt):
        for condition, body in node.branches:
            _validate(condition, symbols)
            for stmt in body:
                _validate(stmt, symbols)
        for stmt in node.otherwise:
            _validate(stmt, symbols)
        return
    if isinstance(node, CallStmt):
        _validate(node.call, symbols)
        return
    raise SclError(f"内部错误：未知语法节点 {type(node).__name__}")


# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------
def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "整数"
    if isinstance(value, float):
        return "实数"
    if isinstance(value, str):
        return "字符串"
    return type(value).__name__


def _trunc_div(left: int, right: int) -> int:
    quotient = abs(left) // abs(right)
    return -quotient if (left < 0) != (right < 0) else quotient


class Runtime:
    def __init__(self, store: Any, events: Any, config: dict[str, Any]) -> None:
        self.store = store
        self.events = events
        self.symbols = SymbolTable(config)

    # -- 语句 -----------------------------------------------------------
    def execute(self, node: Any) -> None:
        if isinstance(node, Assign):
            self.write(node.target, self.evaluate(node.value))
            return
        if isinstance(node, IfStmt):
            for condition, body in node.branches:
                if self.truth(self.evaluate(condition), condition):
                    for stmt in body:
                        self.execute(stmt)
                    return
            for stmt in node.otherwise:
                self.execute(stmt)
            return
        if isinstance(node, CallStmt):
            self.evaluate(node.call)
            return
        raise SclError(f"内部错误：未知语句 {type(node).__name__}",
                       getattr(node, "line", 0), getattr(node, "column", 0))

    @staticmethod
    def truth(value: Any, node: Any) -> bool:
        if isinstance(value, bool):
            return value
        raise SclError(f"条件必须是 Bool 表达式，实际是 {_type_name(value)}",
                       node.line, node.column)

    # -- 表达式 ---------------------------------------------------------
    def evaluate(self, node: Any) -> Any:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, VarRef):
            return self.read(node)
        if isinstance(node, Unary):
            return self._unary(node)
        if isinstance(node, Binary):
            return self._binary(node)
        if isinstance(node, Call):
            return self._call(node)
        raise SclError(f"内部错误：未知表达式 {type(node).__name__}",
                       getattr(node, "line", 0), getattr(node, "column", 0))

    def _unary(self, node: Unary) -> Any:
        value = self.evaluate(node.operand)
        if node.op == "NOT":
            if isinstance(value, bool):
                return not value
            if isinstance(value, int):
                return ~value
            raise SclError("NOT 只支持 Bool 或整数", node.line, node.column)
        number = self._numeric(value, node)
        return -number if node.op == "-" else +number

    def _binary(self, node: Binary) -> Any:
        op = node.op
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)

        if op in ("AND", "OR", "XOR"):
            if isinstance(left, bool) and isinstance(right, bool):
                if op == "AND":
                    return left and right
                if op == "OR":
                    return left or right
                return left != right
            if (isinstance(left, int) and not isinstance(left, bool)
                    and isinstance(right, int) and not isinstance(right, bool)):
                if op == "AND":
                    return left & right
                if op == "OR":
                    return left | right
                return left ^ right
            raise SclError(f"{op} 只支持两个 Bool 或两个整数",
                           node.line, node.column)

        if op in ("=", "<>"):
            if isinstance(left, bool) or isinstance(right, bool):
                if not isinstance(left, bool) or not isinstance(right, bool):
                    raise SclError("Bool 只能和 Bool 比较", node.line, node.column)
                equal = left == right
            elif isinstance(left, str) or isinstance(right, str):
                if not isinstance(left, str) or not isinstance(right, str):
                    raise SclError("字符串只能和字符串比较", node.line, node.column)
                equal = left == right
            else:
                equal = self._numeric(left, node) == self._numeric(right, node)
            return equal if op == "=" else not equal

        if op in ("<", ">", "<=", ">="):
            a = self._numeric(left, node)
            b = self._numeric(right, node)
            if op == "<":
                return a < b
            if op == ">":
                return a > b
            if op == "<=":
                return a <= b
            return a >= b

        if op in ("+", "-", "*"):
            a = self._numeric(left, node)
            b = self._numeric(right, node)
            if isinstance(a, int) and isinstance(b, int):
                if op == "+":
                    return a + b
                if op == "-":
                    return a - b
                return a * b
            if op == "+":
                return float(a) + float(b)
            if op == "-":
                return float(a) - float(b)
            return float(a) * float(b)

        if op == "/":
            a = self._numeric(left, node)
            b = self._numeric(right, node)
            if b == 0:
                raise SclError("除数为 0", node.line, node.column)
            if isinstance(a, int) and isinstance(b, int):
                return _trunc_div(a, b)
            return float(a) / float(b)

        if op == "MOD":
            a = self._numeric(left, node)
            b = self._numeric(right, node)
            if not isinstance(a, int) or not isinstance(b, int):
                raise SclError("MOD 只支持整数", node.line, node.column)
            if b == 0:
                raise SclError("MOD 的除数为 0", node.line, node.column)
            return a - _trunc_div(a, b) * b

        raise SclError(f"不支持的运算符 {op}", node.line, node.column)

    @staticmethod
    def _numeric(value: Any, node: Any) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SclError(f"需要数值，实际是 {_type_name(value)}",
                           node.line, node.column)
        return value

    def _call(self, node: Call) -> Any:
        name = node.name.upper()
        args = [self.evaluate(arg) for arg in node.args]
        if name in ("LOG", "TRACE"):
            message = args[0]
            text = f"{message:g}" if isinstance(message, float) else str(message)
            self.events.add("logic", "message", f"SCL: {text[:300]}")
            return None
        if name == "ABS":
            return abs(self._numeric(args[0], node))
        if name == "SQRT":
            number = self._numeric(args[0], node)
            if number < 0:
                raise SclError("SQRT 参数不能为负数", node.line, node.column)
            return math.sqrt(number)
        if name in ("MIN", "MAX"):
            a = self._numeric(args[0], node)
            b = self._numeric(args[1], node)
            return (min if name == "MIN" else max)(a, b)
        if name == "LIMIT":
            low = self._numeric(args[0], node)
            value = self._numeric(args[1], node)
            high = self._numeric(args[2], node)
            return min(max(value, low), high)
        if name == "ROUND":
            number = float(self._numeric(args[0], node))
            return (math.floor(number + 0.5) if number >= 0
                    else math.ceil(number - 0.5))
        if name == "TRUNC":
            return int(self._numeric(args[0], node))
        if name in ("INT_TO_REAL", "DINT_TO_REAL"):
            return float(self._numeric(args[0], node))
        if name in ("REAL_TO_INT", "REAL_TO_DINT"):
            return int(self._numeric(args[0], node))
        if name == "BOOL_TO_INT":
            value = args[0]
            if not isinstance(value, bool):
                raise SclError("BOOL_TO_INT 需要 Bool 参数", node.line, node.column)
            return 1 if value else 0
        raise SclError(f"未知函数 {node.name}", node.line, node.column)

    # -- 变量读写 -------------------------------------------------------
    def read(self, node: VarRef) -> Any:
        symbol = self.symbols.resolve(node.path, node)
        _ensure_supported(symbol, node)
        if symbol.scalar.kind == "bool":
            return self.store.read_bit(symbol.area, symbol.db, symbol.offset,
                                       int(symbol.bit or 0))
        data = self.store.read(symbol.area, symbol.db, symbol.offset,
                               symbol.scalar.size)
        return self._decode(symbol.scalar, data)

    @staticmethod
    def _decode(scalar: ScalarType, data: bytes) -> Any:
        raw = bytes(data)
        if scalar.kind in ("int", "uint"):
            value = int.from_bytes(raw[:scalar.size], "big")
            if scalar.kind == "int" and value >= (1 << (scalar.bits - 1)):
                value -= 1 << scalar.bits
            return value
        if scalar.kind == "real":
            import struct
            return struct.unpack(">f" if scalar.size == 4 else ">d",
                                 raw[:scalar.size])[0]
        raise SclError(f"不支持的变量类型 {scalar.name}")

    def write(self, node: VarRef, value: Any) -> None:
        symbol = self.symbols.resolve(node.path, node)
        _ensure_supported(symbol, node)
        kind = symbol.scalar.kind
        if kind == "bool":
            if not isinstance(value, bool):
                raise SclError(
                    f"{symbol.label} 是 Bool，不能赋 {_type_name(value)}",
                    node.line, node.column)
            self.store.write_bit(symbol.area, symbol.db, symbol.offset,
                                 int(symbol.bit or 0), value)
            return
        if kind in ("int", "uint"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise SclError(
                    f"{symbol.label} 是 {symbol.scalar.name}，不能赋 {_type_name(value)}",
                    node.line, node.column)
        elif kind == "real" and (isinstance(value, bool)
                                 or not isinstance(value, (int, float))):
            raise SclError(
                f"{symbol.label} 是 {symbol.scalar.name}，不能赋 {_type_name(value)}",
                node.line, node.column)
        try:
            data = encode_scalar(symbol.scalar, value)
        except ValueError as exc:
            raise SclError(str(exc), node.line, node.column) from exc
        self.store.write(symbol.area, symbol.db, symbol.offset, data)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
@dataclass
class SclProgram:
    body: list[Any]
    source: str
    line_count: int

    def validate(self, config: dict[str, Any]) -> None:
        symbols = SymbolTable(config)
        for statement in self.body:
            _validate(statement, symbols)

    def run(self, store: Any, events: Any, config: dict[str, Any]) -> None:
        runtime = Runtime(store, events, config)
        for statement in self.body:
            runtime.execute(statement)


def compile_scl(source: str, config: dict[str, Any]) -> SclProgram:
    stripped = _strip_wrapper(source)
    tokens = tokenize(stripped)
    body = Parser(tokens).parse()
    program = SclProgram(body=body, source=source,
                         line_count=len(source.splitlines()))
    program.validate(config)
    return program


# ---------------------------------------------------------------------------
# 界面用：可拖拽的变量目录（TIA 风格的 "DB名".变量）
# ---------------------------------------------------------------------------
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote(text: str) -> str:
    return '"' + str(text).replace('"', '""') + '"'


def _field(text: str) -> str:
    text = str(text)
    return text if _IDENT_RE.match(text) else _quote(text)


def _supported(scalar: ScalarType | None) -> bool:
    return scalar is not None and scalar.kind in ("bool",) + _NUMERIC_KINDS


def symbol_catalog(config: dict[str, Any]) -> list[dict[str, Any]]:
    """返回按区域/数据块分组的可拖拽变量目录。"""
    groups: list[dict[str, Any]] = []
    area_labels = (("I", "输入 I"), ("Q", "输出 Q"), ("M", "位存储 M"))
    for area, label in area_labels:
        items: list[dict[str, Any]] = []
        addresses: dict[str, set[str]] = {}
        for table in config.get("tag_tables") or []:
            for variable in table.get("variables") or []:
                try:
                    spec = parse_type(variable.get("type"))
                    if spec.is_array or not _supported(spec.scalar):
                        continue
                    parsed = parse_tag_address(variable.get("address"), spec)
                except ValueError:
                    continue
                if parsed["area"] != area:
                    continue
                name = str(variable.get("name") or "")
                addresses.setdefault(name.lower(), set()).add(parsed["address"])
                items.append({
                    "symbol": _quote(name),
                    "name": name,
                    "type": spec.name,
                    "comment": str(variable.get("comment") or ""),
                    "address": parsed["address"],
                    "table": str(table.get("name") or ""),
                    "ambiguous": False,
                })
        for item in items:
            if len(addresses.get(item["name"].lower(), set())) > 1:
                item["ambiguous"] = True
        if items:
            groups.append({"label": label, "area": area, "items": items})

    for db in config.get("dbs") or []:
        number = int(db["number"])
        db_name = str(db.get("name") or f"DB{number}")
        items = []
        layout = layout_variables(db.get("variables") or [])
        for row in layout["rows"]:
            if row.get("is_array"):
                continue
            scalar = parse_type(row["type"]).scalar
            if scalar is None or not _supported(scalar):
                continue
            items.append({
                "symbol": f"{_quote(db_name)}.{_field(row['name'])}",
                "name": str(row["name"]),
                "type": scalar.name,
                "comment": str(row.get("comment") or ""),
                "address": f"DB{number}.{row['offset_text']}",
                "table": f"DB{number} · {db_name}",
                "ambiguous": False,
            })
        if items:
            groups.append({
                "label": f"DB{number} · {db_name}",
                "area": "DB",
                "db": number,
                "items": items,
            })
    return groups
