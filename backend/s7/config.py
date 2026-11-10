"""配置模型与持久化。

配置保存在项目根目录的 ``data/config.json``。所有字段在写入前都会做一次
归一化与范围校验，避免界面传上来的字符串/越界值把服务端带进奇怪状态。
"""
from __future__ import annotations

import json
import logging
import os
import socket
from copy import deepcopy
from pathlib import Path
from typing import Any

from .dbtypes import layout_variables, normalize_variables
from .tags import (
    combined_required,
    find_tag_conflicts,
    normalize_tag_tables,
    normalize_tags,
)

logger = logging.getLogger(__name__)

#: 项目根目录 (backend/s7/config.py -> backend/s7 -> backend -> 根)
ROOT = Path(__file__).resolve().parents[2]
#: 允许测试/多实例把运行数据放到别处，不污染仓库里的 data/。
DATA_DIR = Path(os.environ.get("S7SIM_DATA_DIR") or (ROOT / "data"))
CONFIG_PATH = DATA_DIR / "config.json"
LOGIC_PATH = DATA_DIR / "user_logic.scl"

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 1102,
        "pdu_size": 480,
        "max_clients": 16,
    },
    "cpu": {
        "model": "CPU 315-2 PN/DP",
        "order_code": "6ES7 315-2EH14-0AB0",
        "serial": "S C-C2UR28922012",
        "as_name": "SIM-S7-PLC",
        "copyright": "SilmulateS7Plc",
        "rack": 0,
        "slot": 1,
    },
    "areas": {
        "I": 128,
        "Q": 128,
        "M": 256,
    },
    "dbs": [],
    "tag_tables": [
        {
            "id": 1,
            "area": "I",
            "name": "输入变量表",
            "comment": "",
            "variables": [],
        },
        {
            "id": 2,
            "area": "Q",
            "name": "输出变量表",
            "comment": "",
            "variables": [],
        },
        {
            "id": 3,
            "area": "M",
            "name": "位存储变量表",
            "comment": "",
            "variables": [],
        },
    ],
    "logic": {
        "enabled": False,
        "interval_ms": 100,
    },
}

_AREA_KEYS = ("I", "Q", "M")
_MAX_AREA_BYTES = 65536
_MAX_DBS = 64


def _as_int(value: Any, default: int, minimum: int, maximum: int,
            field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number < minimum or number > maximum:
        raise ValueError(f"{field} 必须在 {minimum} ~ {maximum} 之间，收到 {value!r}")
    return number


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_text(value: Any, default: str, maximum: int = 64) -> str:
    text = str(value).strip() if value is not None else ""
    return (text or default)[:maximum]


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def normalize_config(raw: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """把任意来源的配置归一化成完整、合法的配置字典。

    只接受已知字段；缺省值来自 ``DEFAULT_CONFIG``（或传入的 base）。
    非法值抛 ``ValueError``，由 API 层转成 400。
    """
    merged = deepcopy(base or DEFAULT_CONFIG)
    raw_dict: dict[str, Any] = raw if isinstance(raw, dict) else {}

    src_server = _section(raw_dict, "server")
    server = merged["server"]
    if "host" in src_server:
        host = _as_text(src_server.get("host"), server["host"], 64)
        try:
            socket.inet_aton(host)
        except OSError as exc:
            raise ValueError(f"监听地址不是合法的 IPv4 地址: {host}") from exc
        server["host"] = host
    if "port" in src_server:
        server["port"] = _as_int(src_server.get("port"), server["port"], 1, 65535, "端口")
    if "pdu_size" in src_server:
        pdu = _as_int(src_server.get("pdu_size"), server["pdu_size"],
                      240, 960, "PDU 长度")
        if pdu not in (240, 480, 960):
            raise ValueError("PDU 长度只能是 240 / 480 / 960")
        server["pdu_size"] = pdu
    if "max_clients" in src_server:
        server["max_clients"] = _as_int(src_server.get("max_clients"),
                                        server["max_clients"], 1, 128, "最大客户端数")

    src_cpu = _section(raw_dict, "cpu")
    cpu = merged["cpu"]
    if "model" in src_cpu:
        cpu["model"] = _as_text(src_cpu.get("model"), cpu["model"], 32)
    if "order_code" in src_cpu:
        cpu["order_code"] = _as_text(src_cpu.get("order_code"), cpu["order_code"], 20)
    if "serial" in src_cpu:
        cpu["serial"] = _as_text(src_cpu.get("serial"), cpu["serial"], 24)
    if "as_name" in src_cpu:
        cpu["as_name"] = _as_text(src_cpu.get("as_name"), cpu["as_name"], 24)
    if "copyright" in src_cpu:
        cpu["copyright"] = _as_text(src_cpu.get("copyright"), cpu["copyright"], 26)
    if "rack" in src_cpu:
        cpu["rack"] = _as_int(src_cpu.get("rack"), cpu["rack"], 0, 7, "机架号")
    if "slot" in src_cpu:
        cpu["slot"] = _as_int(src_cpu.get("slot"), cpu["slot"], 0, 31, "槽位号")

    src_areas = _section(raw_dict, "areas")
    for key in _AREA_KEYS:
        if key in src_areas:
            merged["areas"][key] = _as_int(src_areas.get(key), merged["areas"][key],
                                           1, _MAX_AREA_BYTES, f"{key} 区大小")

    raw_dbs = raw_dict.get("dbs")
    if isinstance(raw_dbs, list):
        dbs: list[dict[str, Any]] = []
        seen: set[int] = set()
        for index, item in enumerate(raw_dbs[: _MAX_DBS]):
            if not isinstance(item, dict):
                continue
            number = _as_int(item.get("number"), index + 1, 1, 65535,
                             f"DB{index + 1} 编号")
            if number in seen:
                raise ValueError(f"DB 编号重复: {number}")
            seen.add(number)
            size = _as_int(item.get("size"), 256, 1, _MAX_AREA_BYTES,
                           f"DB{number} 大小")
            name = _as_text(item.get("name"), f"DB{number}", 64)
            comment = _as_text(item.get("comment"), "", 80)
            variables = normalize_variables(item.get("variables"))
            required = int(layout_variables(variables)["required_size"])
            if required > _MAX_AREA_BYTES:
                raise ValueError(
                    f"DB{number} 变量表需要 {required} 字节，超过上限 {_MAX_AREA_BYTES}")
            # 变量表需要更大空间时自动扩展 DB，避免出现“变量存在但内存区不够”
            size = max(size, required)
            dbs.append({"number": number, "name": name, "size": size,
                        "comment": comment, "variables": variables})
        names: set[str] = set()
        for db in dbs:
            key = str(db["name"]).lower()
            if key in names:
                raise ValueError(f"DB 名称重复: {db['name']}")
            names.add(key)
        merged["dbs"] = sorted(dbs, key=lambda d: d["number"])

    # 新版多变量表；兼容旧版扁平 "tags" 列表（自动迁移为默认变量表）
    if "tag_tables" in raw_dict:
        merged["tag_tables"] = normalize_tag_tables(raw_dict.get("tag_tables"))
    elif "tags" in raw_dict:
        legacy = normalize_tags(raw_dict.get("tags"))
        merged["tag_tables"] = ([{
            "id": 1, "name": "默认变量表", "comment": "",
            "variables": legacy,
        }] if legacy else [])
    merged.pop("tags", None)
    tag_tables = normalize_tag_tables(merged.get("tag_tables"))
    merged["tag_tables"] = tag_tables

    # PLC 变量使用绝对地址，跨变量表检查重叠，并确保所在区足够大；
    # 缩小区时给出明确提示，而不是悄悄把变量截断。
    groups = [(table["id"], table["name"], table["variables"])
              for table in tag_tables]
    conflicts = find_tag_conflicts(groups)
    if conflicts:
        raise ValueError("；".join(item["message"] for item in conflicts[:3]))
    required = combined_required(groups)
    for key in _AREA_KEYS:
        need = int(required.get(key, 0))
        size = int(merged["areas"][key])
        if need > size:
            raise ValueError(
                f"PLC 变量占用 {key} 区 {need} 字节，但 {key} 区只有 {size} 字节；"
                f"请把 {key} 区调到至少 {need} 字节")

    src_logic = _section(raw_dict, "logic")
    logic = merged["logic"]
    if "enabled" in src_logic:
        logic["enabled"] = _as_bool(src_logic.get("enabled"), logic["enabled"])
    if "interval_ms" in src_logic:
        logic["interval_ms"] = _as_int(src_logic.get("interval_ms"),
                                       logic["interval_ms"], 10, 5000, "扫描周期")

    return merged


def load_config() -> dict[str, Any]:
    """读取 data/config.json；不存在或损坏时回退到默认值。"""
    if not CONFIG_PATH.exists():
        return deepcopy(DEFAULT_CONFIG)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return normalize_config(raw)
    except Exception as exc:  # 配置损坏不应阻止程序启动
        logger.warning("读取配置失败，使用默认配置: %s", exc, exc_info=True)
        return deepcopy(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    """原子写入配置，避免写一半断电留下坏 JSON。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CONFIG_PATH)


def area_definitions(config: dict[str, Any]) -> list[dict[str, Any]]:
    """把配置展开成内存区列表（顺序稳定，供服务端注册和界面展示）。"""
    areas: list[dict[str, Any]] = []
    for key, label in (("I", "输入 I"), ("Q", "输出 Q"), ("M", "位存储 M")):
        areas.append({
            "key": key,
            "area": key,
            "db": 0,
            "label": label,
            "size": int(config["areas"][key]),
            "writable": True,
        })
    for item in config["dbs"]:
        number = int(item["number"])
        areas.append({
            "key": f"DB{number}",
            "area": "DB",
            "db": number,
            "label": f"数据块 DB{number}",
            "size": int(item["size"]),
            "writable": True,
            "comment": item.get("comment", ""),
        })
    return areas


def structure_signature(config: dict[str, Any]) -> tuple:
    """判断配置里哪些变化需要重启 S7 服务（监听地址、端口、内存区结构等）。"""
    server = config["server"]
    return (
        server["host"],
        server["port"],
        server["max_clients"],
        tuple((k, int(config["areas"][k])) for k in _AREA_KEYS),
        tuple((int(d["number"]), int(d["size"])) for d in config["dbs"]),
    )
