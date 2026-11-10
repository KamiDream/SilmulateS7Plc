"""REST API。前端所有操作都走这里。"""
from __future__ import annotations

import logging
import os
import sys
from functools import wraps
from typing import Any, Callable

import snap7
from flask import Blueprint, jsonify, request

from ..s7.config import CONFIG_PATH, DEFAULT_CONFIG, LOGIC_PATH
from ..s7.dbtypes import type_suggestions
from ..s7.logic import DEFAULT_LOGIC_SOURCE
from ..s7.store import AreaError
from ..s7.tags import tag_type_suggestions
from ..state import get_sim

logger = logging.getLogger(__name__)
bp = Blueprint("api", __name__, url_prefix="/api")

APP_VERSION = "0.1.0"
MAX_READ = 8192


def fail(message: Any, status: int = 400):
    return jsonify({"ok": False, "error": str(message)}), status


def api_route(fn: Callable):
    """把领域异常统一转成 {ok:false,error}，避免前端收到 Flask 的 HTML 错误页。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (AreaError, ValueError, RuntimeError, SyntaxError) as exc:
            return fail(exc, 400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("API 未处理异常")
            return fail(f"内部错误: {type(exc).__name__}: {exc}", 500)
    return wrapper


def body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _int(data: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc


def _bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def parse_hex(text: Any) -> bytes:
    """接受 ``11 22 33`` / ``112233`` / ``0x11 0x22`` / ``11:22`` 等写法。"""
    cleaned = str(text or "")
    for token in ("0x", "0X", " ", "\t", "\r", "\n", ":", "-", ",", "_"):
        cleaned = cleaned.replace(token, "")
    if not cleaned:
        raise ValueError("HEX 数据不能为空")
    if len(cleaned) % 2:
        raise ValueError("HEX 数据长度必须是偶数（每个字节两位）")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"HEX 数据不合法: {text!r}") from exc


# ---------------------------------------------------------------------------
# 元信息 / 总览
# ---------------------------------------------------------------------------
@bp.get("/meta")
def meta():
    sim = get_sim()
    privileged = False
    try:
        privileged = os.geteuid() != 0
    except AttributeError:
        privileged = False
    return jsonify({
        "ok": True,
        "app": "SilmulateS7Plc",
        "version": APP_VERSION,
        "python": sys.version.split()[0],
        "snap7": getattr(snap7, "__version__", "?"),
        "pid": os.getpid(),
        "config_path": str(CONFIG_PATH),
        "logic_path": str(LOGIC_PATH),
        "default_config": DEFAULT_CONFIG,
        "logic_template": DEFAULT_LOGIC_SOURCE,
        "logic_language": "SCL",
        "db_types": type_suggestions(),
        "tag_types": tag_type_suggestions(),
        "limits": {
            "area_max": 65536,
            "db_max": 64,
            "db_number_max": 65535,
            "pdu_options": [240, 480, 960],
            "interval_min": 10,
            "interval_max": 5000,
            "max_clients": 128,
        },
        "needs_privilege_for_102": privileged,
        "overview": sim.overview(),
    })


@bp.get("/overview")
def overview():
    return jsonify(get_sim().overview())


@bp.get("/config")
def get_config():
    sim = get_sim()
    return jsonify({
        "ok": True,
        "config": sim.config,
        "restart_required": bool(sim._restart_required and sim.server
                                  and sim.server.running),
    })


@bp.post("/config")
@api_route
def save_config_api():
    result = get_sim().update_config(body())
    return jsonify({"ok": True, **result})


# ---------------------------------------------------------------------------
# 服务控制
# ---------------------------------------------------------------------------
@bp.post("/server/start")
@api_route
def server_start():
    sim = get_sim()
    status = sim.start_server()
    return jsonify({"ok": True, "server": status, "overview": sim.overview()})


@bp.post("/server/stop")
@api_route
def server_stop():
    sim = get_sim()
    return jsonify({"ok": True, "server": sim.stop_server(),
                    "overview": sim.overview()})


@bp.post("/server/cpu")
@api_route
def server_cpu():
    data = body()
    state = str(data.get("state", "")).strip().upper()
    sim = get_sim()
    status = sim.set_cpu(state)
    return jsonify({"ok": True, "server": status})


# ---------------------------------------------------------------------------
# 内存区
# ---------------------------------------------------------------------------
@bp.get("/memory/areas")
def memory_areas():
    sim = get_sim()
    return jsonify({
        "ok": True,
        "version": sim.store.version,
        "areas": sim.store.areas_info(),
    })


@bp.get("/memory")
@api_route
def memory_read():
    args = request.args
    area = str(args.get("area", "M"))
    db = int(args.get("db", 0) or 0)
    start = int(args.get("start", 0) or 0)
    count = min(int(args.get("count", 128) or 128), MAX_READ)
    result = get_sim().memory_read(area, db, start, count)
    return jsonify({"ok": True, **result})


@bp.post("/memory/write")
@api_route
def memory_write():
    data = body()
    raw = parse_hex(data.get("hex", ""))
    result = get_sim().memory_write(
        str(data.get("area", "")), _int(data, "db", 0),
        _int(data, "start", 0), raw,
    )
    return jsonify({"ok": True, **result})


@bp.post("/memory/fill")
@api_route
def memory_fill():
    data = body()
    result = get_sim().memory_fill(
        str(data.get("area", "")), _int(data, "db", 0),
        _int(data, "start", 0), _int(data, "count", 0),
        _int(data, "value", 0),
    )
    return jsonify({"ok": True, **result})


# ---------------------------------------------------------------------------
# PLC 变量表（TIA 风格 I / Q / M，支持多张变量表）
# ---------------------------------------------------------------------------
@bp.get("/tags")
@api_route
def tags_list():
    raw = request.args.get("table")
    table_id = int(raw) if raw not in (None, "") else None
    return jsonify({"ok": True, **get_sim().tags_list(
        str(request.args.get("area", "M")), table_id)})


@bp.get("/tags/monitor")
@api_route
def tags_monitor():
    raw = request.args.get("table")
    table_id = int(raw) if raw not in (None, "") else None
    return jsonify({"ok": True, **get_sim().tags_monitor(
        str(request.args.get("area", "M")), table_id)})


@bp.post("/tags/preview")
@api_route
def tags_preview():
    data = body()
    return jsonify({"ok": True, **get_sim().tags_preview(
        str(data.get("area") or "M"), _int(data, "table", 1),
        data.get("variables", []), data.get("name"))})


@bp.post("/tags")
@api_route
def tags_save():
    data = body()
    return jsonify({"ok": True, **get_sim().tags_save(
        str(data.get("area") or "M"), _int(data, "table", 1),
        data.get("variables", []), data.get("name"), data.get("comment"))})


@bp.post("/tags/write")
@api_route
def tags_write():
    data = body()
    return jsonify({"ok": True, **get_sim().tags_write(
        _int(data, "table", 1), _int(data, "index", 0),
        data.get("variables"), data.get("value", ""))})


@bp.post("/tags/create")
@api_route
def tags_create():
    data = body()
    return jsonify({"ok": True, **get_sim().tag_table_create(
        str(data.get("area") or "M"), str(data.get("name") or ""),
        str(data.get("comment") or ""))})


@bp.post("/tags/delete")
@api_route
def tags_delete():
    return jsonify({"ok": True, "tables": get_sim().tag_table_delete(
        _int(body(), "table", 1))})


# ---------------------------------------------------------------------------
# 数据块（TIA 风格变量表）
# ---------------------------------------------------------------------------
@bp.get("/db/list")
def db_list():
    return jsonify({"ok": True, "dbs": get_sim().db_list()})


@bp.get("/db")
@api_route
def db_info():
    number = int(request.args.get("number", 1) or 1)
    return jsonify({"ok": True, **get_sim().db_info(number)})


@bp.get("/db/monitor")
@api_route
def db_monitor():
    number = int(request.args.get("number", 1) or 1)
    return jsonify({"ok": True, **get_sim().db_monitor(number)})


@bp.post("/db/preview")
@api_route
def db_preview():
    data = body()
    return jsonify({"ok": True, **get_sim().db_preview(
        _int(data, "number", 1), data.get("variables", []))})


@bp.post("/db/schema")
@api_route
def db_schema():
    data = body()
    return jsonify({"ok": True, **get_sim().db_schema_save(
        _int(data, "number", 1), data.get("variables", []),
        data.get("name"), data.get("comment"))})


@bp.post("/db/create")
@api_route
def db_create():
    data = body()
    raw_size = data.get("size")
    size = None if raw_size in (None, "") else int(raw_size)
    return jsonify({"ok": True, **get_sim().db_create(
        _int(data, "number", 1), size, str(data.get("comment", "")),
        str(data.get("name") or ""))})


@bp.post("/db/delete")
@api_route
def db_delete():
    data = body()
    return jsonify({"ok": True, "dbs": get_sim().db_delete(_int(data, "number", 1))})


@bp.post("/db/write")
@api_route
def db_write():
    data = body()
    element = data.get("element")
    if element is not None and element != "":
        element = int(element)
    else:
        element = None
    return jsonify({"ok": True, **get_sim().db_write_value(
        _int(data, "number", 1), data.get("variables"),
        _int(data, "index", 0), element, data.get("value", ""))})


@bp.post("/db/apply-start")
@api_route
def db_apply_start():
    data = body()
    return jsonify({"ok": True, **get_sim().db_apply_start_values(
        _int(data, "number", 1))})


@bp.post("/db/snapshot")
@api_route
def db_snapshot():
    data = body()
    return jsonify({"ok": True, **get_sim().db_snapshot_start_values(
        _int(data, "number", 1))})


@bp.post("/memory/bit")
@api_route
def memory_bit():
    data = body()
    value = data.get("value")
    if isinstance(value, str):
        value = value.strip().lower() in ("1", "true", "on", "yes")
    result = get_sim().memory_bit(
        str(data.get("area", "")), _int(data, "db", 0),
        _int(data, "byte", 0), _int(data, "bit", 0), bool(value),
    )
    return jsonify({"ok": True, **result})


# ---------------------------------------------------------------------------
# 客户端列表 / 日志
# ---------------------------------------------------------------------------
@bp.get("/clients")
def clients():
    sim = get_sim()
    status = sim.server_status()
    return jsonify({
        "ok": True,
        "clients": sim.tracker.snapshot(),
        "client_count": status["client_count"],
        "max_clients": status["max_clients"],
        "stats": status["stats"],
    })


@bp.post("/clients/disconnect")
@api_route
def clients_disconnect():
    peer = str(body().get("peer", ""))
    if not peer:
        raise ValueError("缺少 peer")
    ok = get_sim().disconnect_peer(peer)
    return jsonify({"ok": True, "disconnected": ok})


@bp.get("/logs")
@api_route
def logs():
    args = request.args
    since = int(args.get("since", 0) or 0)
    limit = min(int(args.get("limit", 300) or 300), 2000)
    source = str(args.get("source", "all") or "all")
    return jsonify({"ok": True, **get_sim().events.since(since, limit, source)})


@bp.post("/logs/clear")
def logs_clear():
    latest = get_sim().events.clear()
    return jsonify({"ok": True, "latest_seq": latest})


# ---------------------------------------------------------------------------
# PLC 逻辑
# ---------------------------------------------------------------------------
@bp.get("/logic")
def logic_status():
    include_source = request.args.get("source", "0") in ("1", "true", "yes")
    sim = get_sim()
    return jsonify({"ok": True, **sim.logic_status(include_source=include_source)})


@bp.get("/logic/symbols")
def logic_symbols():
    return jsonify({"ok": True, **get_sim().logic_symbols()})


@bp.post("/logic/source")
@api_route
def logic_source():
    source = body().get("source")
    if not isinstance(source, str):
        raise ValueError("缺少 source")
    status = get_sim().logic_save_source(source)
    return jsonify({"ok": True, **status})


@bp.post("/logic/enable")
@api_route
def logic_enable():
    data = body()
    status = get_sim().logic_enable(_bool(data, "enabled"))
    return jsonify({"ok": True, **status})


@bp.post("/logic/interval")
@api_route
def logic_interval():
    status = get_sim().logic_interval(_int(body(), "interval_ms", 100))
    return jsonify({"ok": True, **status})


@bp.post("/logic/reload")
@api_route
def logic_reload():
    status = get_sim().logic_reload_file()
    return jsonify({"ok": True, **status})
