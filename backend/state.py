"""应用运行时状态：配置、内存、S7 服务、PLC 逻辑。"""
from __future__ import annotations

import atexit
import logging
import socket
import threading
import time
from typing import Any

from .s7.config import (
    CONFIG_PATH,
    LOGIC_PATH,
    load_config,
    normalize_config,
    save_config,
    structure_signature,
)
from .s7.dbtypes import (
    decode_scalar,
    encode_scalar,
    layout_variables,
    normalize_variables,
    parse_bool_text,
    parse_type,
)
from .s7.events import EventLog
from .s7.logic import LogicEngine
from .s7.pulse import PulseEngine
from .s7.scl import symbol_catalog
from .s7.server import ClientTracker, SimServer
from .s7.store import AreaError, AreaStore
from .s7.tags import (
    combined_required,
    find_tag_conflicts,
    layout_tags,
    normalize_tags,
    tag_areas,
)

logger = logging.getLogger(__name__)


def _lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ips.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(str(info[4][0]))
    except OSError:
        pass
    return sorted(ip for ip in ips if ip and not ip.startswith("127."))


class Simulator:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.config = load_config()
        save_config(self.config)

        self.events = EventLog()
        self.store = AreaStore()
        self.store.rebuild(self.config)
        self.tracker = ClientTracker()
        self.server: SimServer | None = None
        self._restart_required = False
        self._lock = threading.RLock()

        self.logic = LogicEngine(self.store, self.events, LOGIC_PATH,
                                 lambda: self.server, lambda: self.config)
        self.logic.interval_ms = int(self.config["logic"]["interval_ms"])
        try:
            self.logic.load_file()
        except Exception as exc:  # 坏脚本不应阻止界面启动
            logger.warning("用户逻辑加载失败", exc_info=True)
            self.events.add("system", "logic_load_error",
                            f"用户逻辑加载失败：{exc}", level="err")
        self.logic.enabled = bool(self.config["logic"]["enabled"]) and self.logic.loaded
        self.logic.start()

        # 「周期」列配置的 Bool 变量由它按 10 ms 节拍写 1 / 0
        self.pulse = PulseEngine(self.store, self.events,
                                 lambda: self.server, lambda: self.config)
        self.pulse.start()

        self.events.add(
            "system", "started",
            "模拟器已就绪",
            detail=f"配置文件: {CONFIG_PATH}\n用户逻辑: {LOGIC_PATH}",
            level="ok",
        )

    # ------------------------------------------------------------------
    # 服务
    # ------------------------------------------------------------------
    def _blank_server_status(self) -> dict[str, Any]:
        server = self.config["server"]
        return {
            "running": False,
            "host": server["host"],
            "port": int(server["port"]),
            "cpu": "STOP",
            "cpu_code": 4,
            "client_count": 0,
            "max_clients": int(server["max_clients"]),
            "pdu_size": int(server["pdu_size"]),
            "started_at": None,
            "uptime_s": 0.0,
            "stats": {
                "requests": 0, "reads": 0, "writes": 0,
                "bytes_in": 0, "bytes_out": 0,
                "connections": 0, "disconnects": 0, "errors": 0,
            },
            "error": "",
        }

    def server_status(self) -> dict[str, Any]:
        if self.server is not None:
            return self.server.status()
        return self._blank_server_status()

    def start_server(self) -> dict[str, Any]:
        with self._lock:
            if self.server is not None and self.server.running:
                self.server.stop()

            # 启动前按当前配置重建内存区（同 key 数据尽量保留）
            self.store.rebuild(self.config)
            server = SimServer(self.store, self.config, self.events, self.tracker)
            self.store.register_into(server)
            self.server = server
            host = self.config["server"]["host"]
            port = int(self.config["server"]["port"])
            # 启动失败时也保留 server 实例，让状态页能看到具体错误
            server.start_to(host, port)
            self._restart_required = False
            return server.status()

    def stop_server(self) -> dict[str, Any]:
        with self._lock:
            if self.server is not None:
                self.server.stop()
            self._restart_required = False
            return self.server_status()

    def set_cpu(self, state: str) -> dict[str, Any]:
        with self._lock:
            if self.server is None or not self.server.running:
                raise RuntimeError("S7 服务未运行，无法切换 CPU 状态")
            self.server.set_cpu(state)
            return self.server.status()

    def update_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            old_signature = structure_signature(self.config)
            new_config = normalize_config(raw, self.config)
            new_signature = structure_signature(new_config)
            save_config(new_config)
            self.config = new_config

            if self.server is not None:
                self.server.update_config(new_config)

            if self.server is None or not self.server.running:
                # 未运行时立即按新结构重建内存区（数据按 key/长度保留）
                self.store.rebuild(new_config)
                self._restart_required = False
            else:
                self._restart_required = old_signature != new_signature

            self.logic.interval_ms = int(new_config["logic"]["interval_ms"])
            self.events.add(
                "system", "config_saved",
                "配置已保存" + ("（部分项目需重启服务生效）"
                                if self._restart_required else ""),
                level="warn" if self._restart_required else "ok",
            )
            return {
                "config": self.config,
                "restart_required": self._restart_required,
            }

    def overview(self) -> dict[str, Any]:
        server = self.server_status()
        return {
            "ok": True,
            "uptime_s": round(time.time() - self.started_at, 1),
            "server": server,
            "clients": self.tracker.snapshot(),
            "memory": {
                "version": self.store.version,
                "areas": self.store.areas_info(),
            },
            "logic": self.logic.status(include_source=False),
            "config": self.config,
            "restart_required": bool(self._restart_required
                                      and server.get("running")),
            "lan_ips": _lan_ips(),
            "config_path": str(CONFIG_PATH),
            "logic_path": str(LOGIC_PATH),
            "latest_log_seq": self.events.latest_seq(),
        }

    # ------------------------------------------------------------------
    # 内存区
    # ------------------------------------------------------------------
    def memory_read(self, area: str, db: int, start: int, count: int) -> dict[str, Any]:
        item = self.store.get(area, db)
        start = max(0, int(start))
        count = int(count)
        if start >= item.size:
            raise AreaError(f"起始地址 {start} 超出 {item.key} 大小 {item.size}")
        if count < 0:
            raise AreaError("长度不能为负数")
        count = min(count, item.size - start)
        data = self.store.read(area, db, start, count)
        return {
            "area": item.area,
            "db": item.db,
            "key": item.key,
            "start": start,
            "count": count,
            "size": item.size,
            "version": self.store.version,
            "data": data.hex(),
        }

    def memory_write(self, area: str, db: int, start: int,
                     data: bytes) -> dict[str, Any]:
        self.store.write(area, db, start, data)
        item = self.store.get(area, db)
        preview = bytes(data[:48]).hex(" ").upper()
        self.events.add(
            "ui", "write",
            f"界面写入 {item.key} {start}..{start + len(data)}  [{preview}]",
            detail=f"HEX: {bytes(data[:256]).hex(' ').upper()}",
            level="ok",
        )
        return {"version": self.store.version, "count": len(data)}

    def memory_fill(self, area: str, db: int, start: int, count: int,
                    value: int) -> dict[str, Any]:
        if count < 0:
            raise AreaError("长度不能为负数")
        byte = int(value) & 0xFF
        self.store.write(area, db, start, bytes([byte]) * count)
        item = self.store.get(area, db)
        self.events.add("ui", "fill",
                        f"界面填充 {item.key} {start}..{start + count} = 0x{byte:02X}")
        return {"version": self.store.version, "count": count}

    def memory_bit(self, area: str, db: int, byte: int, bit: int,
                   value: bool) -> dict[str, Any]:
        self.store.write_bit(area, db, byte, bit, bool(value))
        item = self.store.get(area, db)
        self.events.add(
            "ui", "bit",
            f"界面置位 {item.key}.{byte}.{bit} = {1 if value else 0}",
        )
        return {"version": self.store.version}

    # ------------------------------------------------------------------
    # PLC 变量表（TIA 风格 I / Q / M，支持多张变量表）
    # ------------------------------------------------------------------
    _TAG_AREA_KEYS = ("I", "Q", "M")

    def _tag_monitor(self, row: dict[str, Any]) -> str:
        try:
            scalar = parse_type(row["type"]).scalar
            if scalar is None:
                return ""
            if scalar.kind == "bool":
                value = self.store.read_bit(str(row["area"]), 0,
                                            int(row["offset"]), int(row["bit"]))
                return "true" if value else "false"
            data = self.store.read(str(row["area"]), 0,
                                   int(row["offset"]), int(row["size"]))
            return decode_scalar(scalar, data)
        except Exception:  # 监视值只是展示，读失败不影响界面
            logger.debug("读取变量监视值失败", exc_info=True)
            return "—"

    def _tag_tables(self, area: str | None = None) -> list[dict[str, Any]]:
        tables = self.config.get("tag_tables") or []
        if area is None:
            return tables
        area = str(area).strip().upper()
        return [table for table in tables
                if str(table.get("area") or "").upper() == area]

    def _tag_table_entry(self, table_id: int) -> dict[str, Any]:
        table_id = int(table_id)
        for table in self._tag_tables():
            if int(table["id"]) == table_id:
                return table
        raise ValueError(f"变量表 #{table_id} 不存在")

    def _tag_areas(self) -> dict[str, int]:
        return {key: int(self.config["areas"][key]) for key in self._TAG_AREA_KEYS}

    def _tag_table_summaries(self, area: str | None = None) -> list[dict[str, Any]]:
        return [{
            "id": int(table["id"]),
            "area": str(table.get("area") or ""),
            "name": str(table.get("name") or ""),
            "comment": str(table.get("comment") or ""),
            "variable_count": len(table.get("variables") or []),
        } for table in self._tag_tables(area)]

    def _tag_groups(self, table_id: int | None = None,
                    variables: list[dict[str, Any]] | None = None,
                    name: str | None = None
                    ) -> list[tuple[Any, str, list[dict[str, Any]]]]:
        """拼出全部变量表；如果传了 ``variables``，用它替换指定表的内容。"""
        groups: list[tuple[Any, str, list[dict[str, Any]]]] = []
        for table in self._tag_tables():
            current_id = int(table["id"])
            if table_id is not None and current_id == int(table_id):
                tags = (normalize_tags(variables) if variables is not None
                        else normalize_tags(table.get("variables")))
                label = name or str(table.get("name") or f"变量表 {current_id}")
            else:
                tags = normalize_tags(table.get("variables"))
                label = str(table.get("name") or f"变量表 {current_id}")
            groups.append((current_id, label, tags))
        return groups

    def _tags_payload(self, table: dict[str, Any],
                      variables: list[dict[str, Any]] | None = None,
                      name: str | None = None) -> dict[str, Any]:
        table_id = int(table["id"])
        tags = normalize_tags(variables if variables is not None
                              else table.get("variables"))
        groups = self._tag_groups(table_id, tags, name)
        conflicts = find_tag_conflicts(groups)
        required = combined_required(groups)
        rows, _ = layout_tags(tags)
        for row in rows:
            row["monitor"] = self._tag_monitor(row)
        areas = self._tag_areas()
        grow = {key: int(required.get(key, 0)) for key in self._TAG_AREA_KEYS
                if int(required.get(key, 0)) > areas[key]}
        table_conflicts = [item for item in conflicts if table_id in item["keys"]]
        conflict_indexes = sorted({
            index
            for item in table_conflicts
            for key, index in zip(item["keys"], item["indexes"])
            if key == table_id
        })
        return {
            "area": str(table.get("area") or ""),
            "table": {
                "id": table_id,
                "area": str(table.get("area") or ""),
                "name": name or str(table.get("name") or ""),
                "comment": str(table.get("comment") or ""),
                "variable_count": len(tags),
            },
            "tables": self._tag_table_summaries(str(table.get("area") or "")),
            "variables": tags,
            "rows": rows,
            "required": required,
            "areas": areas,
            "grow": grow,
            "conflicts": conflicts,
            "conflict_indexes": conflict_indexes,
            "version": self.store.version,
        }

    def _empty_tags_payload(self, area: str) -> dict[str, Any]:
        areas = self._tag_areas()
        return {
            "area": area,
            "table": None,
            "tables": [],
            "variables": [],
            "rows": [],
            "required": {key: 0 for key in self._TAG_AREA_KEYS},
            "areas": areas,
            "grow": {},
            "conflicts": [],
            "conflict_indexes": [],
            "version": self.store.version,
        }

    def _check_table_area(self, table: dict[str, Any],
                          variables: list[dict[str, Any]]) -> None:
        area = str(table.get("area") or "")
        foreign = tag_areas(variables) - {area}
        if foreign:
            raise ValueError(
                f"变量表「{table.get('name') or table.get('id')}」是 {area} 区，"
                f"不能使用 {', '.join(sorted(foreign))} 区地址")

    def tags_list(self, area: str, table_id: int | None = None) -> dict[str, Any]:
        area = str(area or "").strip().upper()
        if area not in self._TAG_AREA_KEYS:
            raise ValueError("变量表区域只能是 I / Q / M")
        tables = self._tag_tables(area)
        if not tables:
            return self._empty_tags_payload(area)
        table = tables[0] if table_id is None else self._tag_table_entry(table_id)
        if str(table.get("area") or "") != area:
            raise ValueError(f"变量表 #{table_id} 不属于 {area} 区")
        return self._tags_payload(table)

    def tags_monitor(self, area: str, table_id: int | None = None) -> dict[str, Any]:
        """只取「监视值」的轻量接口（页面快刷新用）。"""
        area = str(area or "").strip().upper()
        if area not in self._TAG_AREA_KEYS:
            raise ValueError("变量表区域只能是 I / Q / M")
        tables = self._tag_tables(area)
        if not tables:
            return {"area": area, "table": None, "variables": [], "rows": [],
                    "required": {}, "areas": {},
                    "version": self.store.version}
        table = tables[0] if table_id is None else self._tag_table_entry(table_id)
        if str(table.get("area") or "") != area:
            raise ValueError(f"变量表 #{table_id} 不属于 {area} 区")
        rows, _required = layout_tags(normalize_tags(table.get("variables")))
        for row in rows:
            row["monitor"] = self._tag_monitor(row)
        return {"area": area, "version": self.store.version, "rows": [
            {"index": int(row["index"]), "monitor": row.get("monitor", "")}
            for row in rows]}

    def tags_preview(self, area: str, table_id: int, raw_variables: Any,
                     name: str | None = None) -> dict[str, Any]:
        # 预览只做地址解析/布局和冲突检查，不落盘，便于边编辑边看规范化后的地址。
        area = str(area or "").strip().upper()
        table = self._tag_table_entry(table_id)
        if str(table.get("area") or "") != area:
            raise ValueError(f"变量表 #{table_id} 不属于 {area} 区")
        variables = normalize_tags(raw_variables)
        self._check_table_area(table, variables)
        return self._tags_payload(table, variables, name)

    def tags_save(self, area: str, table_id: int, raw_variables: Any,
                  name: str | None = None,
                  comment: str | None = None) -> dict[str, Any]:
        with self._lock:
            area = str(area or "").strip().upper()
            table = self._tag_table_entry(table_id)
            if str(table.get("area") or "") != area:
                raise ValueError(f"变量表 #{table_id} 不属于 {area} 区")
            variables = normalize_tags(raw_variables)
            self._check_table_area(table, variables)
            new_name = (str(name).strip()[:40] if name is not None
                        else str(table.get("name") or ""))
            if not new_name:
                raise ValueError("变量表名称不能为空")
            for other in self._tag_tables():
                if int(other["id"]) != int(table_id) and \
                        str(other.get("name") or "").lower() == new_name.lower():
                    raise ValueError(f"变量表名称重复: {new_name}")
            groups = self._tag_groups(table_id, variables, new_name)
            conflicts = find_tag_conflicts(groups)
            if conflicts:
                raise ValueError("；".join(
                    item["message"] for item in conflicts[:3]))
            required = combined_required(groups)
            grown: dict[str, int] = {}
            for key in self._TAG_AREA_KEYS:
                need = int(required.get(key, 0))
                if need > 65536:
                    raise ValueError(f"{key} 区变量需要 {need} 字节，超过上限 65536")
                size = int(self.config["areas"][key])
                if need > size:
                    self.config["areas"][key] = need
                    grown[key] = need
            table["name"] = new_name
            table["comment"] = (str(comment).strip()[:80] if comment is not None
                                else str(table.get("comment") or ""))
            table["variables"] = variables
            save_config(self.config)
            self.store.rebuild(self.config)
            self.store.sync_server(self.server)
            if self.server is not None:
                self.server.update_config(self.config)
            self._restart_required = False
            detail = f"{len(variables)} 个变量"
            if grown:
                detail += "；扩展 " + "、".join(f"{k} 区 -> {v} 字节"
                                                for k, v in grown.items())
            self.events.add("ui", "tags_schema",
                            f"变量表「{new_name}」已保存：{len(variables)} 个变量",
                            detail=detail, level="ok")
            payload = self._tags_payload(table)
            payload["grown"] = grown
            return payload

    def tags_write(self, table_id: int, index: int, raw_variables: Any,
                   value: Any) -> dict[str, Any]:
        with self._lock:
            table = self._tag_table_entry(table_id)
            variables = (normalize_tags(raw_variables) if raw_variables is not None
                         else normalize_tags(table.get("variables")))
            rows, _ = layout_tags(variables)
            row = next((item for item in rows
                        if int(item["index"]) == int(index)), None)
            if row is None:
                raise ValueError(f"PLC 变量 #{index} 不存在")
            scalar = parse_type(row["type"]).scalar
            if scalar is None:
                raise ValueError("该变量不支持直接写入")
            if scalar.kind == "bool":
                parsed = parse_bool_text(value)
                self.store.write_bit(str(row["area"]), 0, int(row["offset"]),
                                     int(row["bit"]), parsed)
                monitor = "true" if parsed else "false"
            else:
                data = encode_scalar(scalar, value)
                self.store.write(str(row["area"]), 0, int(row["offset"]), data)
                monitor = decode_scalar(scalar, data)
            self.events.add(
                "ui", "tag_write",
                f"界面写入 {row['address']} ({row['name']}) = {monitor}",
                level="ok",
            )
            return {
                "monitor": monitor,
                "name": row["name"],
                "address": row["address"],
                "version": self.store.version,
            }

    def tag_table_create(self, area: str, name: str = "",
                         comment: str = "") -> dict[str, Any]:
        with self._lock:
            area = str(area or "").strip().upper()
            if area not in self._TAG_AREA_KEYS:
                raise ValueError("变量表区域只能是 I / Q / M")
            tables = self._tag_tables()
            if len(tables) >= 64:
                raise ValueError("变量表过多（最多 64 张）")
            used_ids = {int(table["id"]) for table in tables}
            table_id = 1
            while table_id in used_ids:
                table_id += 1
            used_names = {str(table.get("name") or "").lower()
                          for table in tables}
            if name.strip():
                new_name = str(name).strip()[:40]
            else:
                new_name = f"{area} 区变量表{table_id}"
                suffix = table_id
                while new_name.lower() in used_names:
                    suffix += 1
                    new_name = f"{area} 区变量表{suffix}"
            if new_name.lower() in used_names:
                raise ValueError(f"变量表名称重复: {new_name}")
            table = {
                "id": table_id,
                "area": area,
                "name": new_name,
                "comment": str(comment or "")[:80].strip(),
                "variables": [],
            }
            self.config.setdefault("tag_tables", []).append(table)
            self.config["tag_tables"].sort(key=lambda item: int(item["id"]))
            save_config(self.config)
            self.events.add("ui", "tag_table_create",
                            f"新建 {area} 区变量表「{new_name}」", level="ok")
            return self._tags_payload(table)

    def tag_table_delete(self, table_id: int) -> list[dict[str, Any]]:
        with self._lock:
            table = self._tag_table_entry(table_id)
            self.config["tag_tables"] = [
                item for item in self._tag_tables()
                if int(item["id"]) != int(table_id)]
            save_config(self.config)
            self.events.add("ui", "tag_table_delete",
                            f"删除 PLC 变量表「{table.get('name') or table_id}」",
                            level="warn")
            return self._tag_table_summaries()

    # ------------------------------------------------------------------
    # 数据块（TIA 风格变量表）
    # ------------------------------------------------------------------
    def _db_entry(self, number: int) -> dict[str, Any]:
        number = int(number)
        for item in self.config.get("dbs", []):
            if int(item["number"]) == number:
                return item
        raise ValueError(f"DB{number} 不存在")

    @staticmethod
    def _db_variables(db: dict[str, Any]) -> list[dict[str, Any]]:
        return normalize_variables(db.get("variables"))

    def _row_monitor(self, number: int, row: dict[str, Any]) -> str:
        try:
            scalar = parse_type(row["type"]).scalar
            if scalar is None:
                return ""
            if scalar.kind == "bool":
                value = self.store.read_bit("DB", number, int(row["offset"]),
                                            int(row["bit"]))
                return "true" if value else "false"
            data = self.store.read("DB", number, int(row["offset"]),
                                   int(row["size"]))
            return decode_scalar(scalar, data)
        except Exception:  # 监视值只是展示，读失败不影响界面
            logger.debug("读取 DB 监视值失败", exc_info=True)
            return "—"

    def _decorate_rows(self, number: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for row in rows:
            if row.get("is_array"):
                children = row.get("children") or []
                for child in children:
                    child["monitor"] = self._row_monitor(number, child)
                row["monitor"] = f"{len(children)} 项"
            else:
                row["monitor"] = self._row_monitor(number, row)
        return rows

    def _db_payload(self, db: dict[str, Any],
                    variables: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        number = int(db["number"])
        vars_ = self._db_variables(db) if variables is None else variables
        layout = layout_variables(vars_)
        rows = self._decorate_rows(number, layout["rows"])
        return {
            "db": {
                "number": number,
                "name": str(db.get("name") or f"DB{number}"),
                "size": int(db["size"]),
                "comment": str(db.get("comment") or ""),
            },
            "variables": vars_,
            "rows": rows,
            "required_size": int(layout["required_size"]),
            "version": self.store.version,
        }

    def db_list(self) -> list[dict[str, Any]]:
        result = []
        for db in self.config.get("dbs", []):
            variables = self._db_variables(db)
            required = int(layout_variables(variables)["required_size"])
            result.append({
                "number": int(db["number"]),
                "name": str(db.get("name") or f"DB{int(db['number'])}"),
                "size": int(db["size"]),
                "comment": str(db.get("comment") or ""),
                "variable_count": len(variables),
                "required_size": required,
            })
        return result

    def db_info(self, number: int) -> dict[str, Any]:
        return self._db_payload(self._db_entry(number))

    def db_monitor(self, number: int) -> dict[str, Any]:
        """只取「监视值」的轻量接口（页面快刷新用，不带变量声明等字段）。"""
        db = self._db_entry(number)
        layout = layout_variables(self._db_variables(db))
        rows = self._decorate_rows(int(number), layout["rows"])
        return {"version": self.store.version, "rows": [
            {"index": int(row["index"]),
             "monitor": row.get("monitor", ""),
             "children": [{"element_index": int(child["element_index"]),
                           "monitor": child.get("monitor", "")}
                          for child in row.get("children") or []]}
            for row in rows]}

    def db_preview(self, number: int, raw_variables: Any) -> dict[str, Any]:
        db = self._db_entry(number)
        variables = normalize_variables(raw_variables)
        # 预览只负责布局；起始值合法性在保存时检查，避免改类型时
        # 因为旧的起始值暂时不匹配而连表格都无法刷新。
        payload = self._db_payload(db, variables)
        payload["resize_required"] = payload["required_size"] > int(db["size"])
        return payload

    def db_schema_save(self, number: int, raw_variables: Any,
                       name: str | None = None,
                       comment: str | None = None) -> dict[str, Any]:
        with self._lock:
            db = self._db_entry(number)
            variables = normalize_variables(raw_variables)
            layout = layout_variables(variables)
            self._validate_start_rows(layout["rows"])
            if name is not None:
                new_name = str(name).strip()[:64] or f"DB{int(number)}"
                for other in self.config.get("dbs", []):
                    if (int(other["number"]) != int(number)
                            and str(other.get("name") or "").lower() == new_name.lower()):
                        raise ValueError(f"DB 名称重复: {new_name}")
                db["name"] = new_name
            if comment is not None:
                db["comment"] = str(comment).strip()[:80]
            required = int(layout["required_size"])
            if required > 65536:
                raise ValueError(f"变量表需要 {required} 字节，超过 DB 上限 65536")
            old_size = int(db["size"])
            db["variables"] = variables
            db["size"] = max(old_size, required, 1)
            save_config(self.config)
            self.store.rebuild(self.config)
            self.store.sync_server(self.server)
            if self.server is not None:
                self.server.update_config(self.config)
            self._restart_required = False
            resized = int(db["size"]) != old_size
            self.events.add(
                "ui", "db_schema",
                f"DB{int(number)} 变量表已保存：{len(variables)} 个变量，"
                f"需要 {required} 字节" + (f"（DB 扩展到 {db['size']} 字节）" if resized else ""),
                level="ok",
            )
            payload = self._db_payload(db)
            payload["resized"] = resized
            return payload

    def db_create(self, number: int, size: int | None = None,
                  comment: str = "", name: str = "") -> dict[str, Any]:
        number = int(number)
        if not 1 <= number <= 65535:
            raise ValueError("DB 编号必须在 1 ~ 65535 之间")
        if any(int(item["number"]) == number for item in self.config.get("dbs", [])):
            raise ValueError(f"DB{number} 已存在")
        new_name = str(name or "").strip()[:64] or f"DB{number}"
        if any(str(item.get("name") or "").lower() == new_name.lower()
               for item in self.config.get("dbs", [])):
            raise ValueError(f"DB 名称重复: {new_name}")
        # 新建 DB 不再要求用户填大小：先建最小区，保存变量表时按布局自动扩展。
        if size is None or str(size).strip() == "":
            size = 1
        else:
            size = int(size)
        if not 1 <= size <= 65536:
            raise ValueError("DB 大小必须在 1 ~ 65536 字节之间")
        comment = str(comment or "")[:80]
        with self._lock:
            self.config.setdefault("dbs", []).append({
                "number": number, "name": new_name, "size": size,
                "comment": comment, "variables": [],
            })
            self.config["dbs"].sort(key=lambda item: int(item["number"]))
            save_config(self.config)
            self.store.rebuild(self.config)
            self.store.sync_server(self.server)
            if self.server is not None:
                self.server.update_config(self.config)
            self._restart_required = False
            self.events.add("ui", "db_create",
                            f"新建 DB{number}（变量表保存时自动扩展大小）",
                            level="ok")
            return self.db_info(number)

    def db_delete(self, number: int) -> list[dict[str, Any]]:
        number = int(number)
        with self._lock:
            db = self._db_entry(number)
            self.config["dbs"] = [item for item in self.config.get("dbs", [])
                                  if int(item["number"]) != number]
            save_config(self.config)
            self.store.rebuild(self.config)
            self.store.sync_server(self.server)
            if self.server is not None:
                self.server.update_config(self.config)
            self._restart_required = False
            self.events.add("ui", "db_delete",
                            f"删除 DB{number}（{db.get('comment') or '无备注'}）",
                            level="warn")
            return self.db_list()

    @staticmethod
    def _validate_start_rows(rows: list[dict[str, Any]]) -> None:
        """保存/预览前检查所有起始值，避免把坏值写进配置。"""
        for row in rows:
            targets = row.get("children") if row.get("is_array") else [row]
            for target in targets or []:
                scalar = parse_type(target["type"]).scalar
                if scalar is None:
                    continue
                start = target.get("start")
                if scalar.kind == "bool":
                    parse_bool_text(start)
                else:
                    encode_scalar(scalar, start)

    @staticmethod
    def _find_db_row(layout: dict[str, Any], index: int,
                     element: int | None) -> dict[str, Any]:
        rows = layout.get("rows") or []
        row = next((item for item in rows if int(item["index"]) == int(index)), None)
        if row is None:
            raise ValueError(f"变量 #{index} 不存在")
        if row.get("is_array"):
            if element is None:
                raise ValueError("数组变量请指定元素下标")
            children = row.get("children") or []
            if not 0 <= int(element) < len(children):
                raise ValueError("数组元素下标越界")
            return children[int(element)]
        return row

    def _write_db_value(self, number: int, row: dict[str, Any], value: Any) -> str:
        scalar = parse_type(row["type"]).scalar
        if scalar is None:
            raise ValueError("数组行不能直接写入，请指定元素")
        if scalar.kind == "bool":
            parsed = parse_bool_text(value)
            self.store.write_bit("DB", number, int(row["offset"]),
                                 int(row["bit"]), parsed)
            return "true" if parsed else "false"
        data = encode_scalar(scalar, value)
        self.store.write("DB", number, int(row["offset"]), data)
        return decode_scalar(scalar, data)

    def db_write_value(self, number: int, raw_variables: Any, index: int,
                       element: int | None, value: Any) -> dict[str, Any]:
        number = int(number)
        with self._lock:
            db = self._db_entry(number)
            variables = (self._db_variables(db) if raw_variables is None
                         else normalize_variables(raw_variables))
            layout = layout_variables(variables)
            row = self._find_db_row(layout, index, element)
            monitor = self._write_db_value(number, row, value)
            self.events.add(
                "ui", "db_write",
                f"界面写入 DB{number}.{row['name']} = {monitor}",
                level="ok",
            )
            return {
                "monitor": monitor,
                "offset_text": row["offset_text"],
                "version": self.store.version,
            }

    def db_apply_start_values(self, number: int) -> dict[str, Any]:
        number = int(number)
        with self._lock:
            db = self._db_entry(number)
            variables = self._db_variables(db)
            layout = layout_variables(variables)
            writes: list[tuple[dict[str, Any], str]] = []
            for row in layout["rows"]:
                if row.get("is_array"):
                    for child in row.get("children") or []:
                        writes.append((child, child.get("start")))
                else:
                    writes.append((row, row.get("start")))
            # 先全部编码，避免写一半才发现某个起始值不合法
            for row, start in writes:
                scalar = parse_type(row["type"]).scalar
                if scalar is None:
                    raise ValueError("数组行不能直接写入")
                if scalar.kind != "bool":
                    encode_scalar(scalar, start)
                else:
                    parse_bool_text(start)
            # 与 TIA 的“下载”一致：先清空 DB，再写入起始值，
            # 避免旧的实际值/填充字节残留。
            area = self.store.get("DB", number)
            self.store.write("DB", number, 0, bytes(area.size))
            count = 0
            for row, start in writes:
                self._write_db_value(number, row, start)
                count += 1
            self.events.add(
                "ui", "db_download",
                f"DB{number} 下载起始值：清空后写入 {count} 个值",
                level="ok",
            )
            payload = self._db_payload(db)
            payload["count"] = count
            return payload

    def db_snapshot_start_values(self, number: int) -> dict[str, Any]:
        number = int(number)
        with self._lock:
            db = self._db_entry(number)
            variables = self._db_variables(db)
            layout = layout_variables(variables)
            self._decorate_rows(number, layout["rows"])
            for row in layout["rows"]:
                if row.get("is_array"):
                    row["start"] = [child.get("monitor", "")
                                    for child in row.get("children") or []]
                else:
                    row["start"] = row.get("monitor", "")
            # 把带 monitor 的行映射回声明列表
            for index, row in enumerate(layout["rows"]):
                variables[index]["start"] = row["start"]
            db["variables"] = variables
            save_config(self.config)
            self.events.add(
                "ui", "db_snapshot",
                f"DB{number} 实际值 → 起始值：{len(variables)} 个变量",
                level="ok",
            )
            return self._db_payload(db)

    # ------------------------------------------------------------------
    # 客户端
    # ------------------------------------------------------------------
    def disconnect_peer(self, peer: str) -> bool:
        ok = self.tracker.disconnect(peer)
        if ok:
            self.events.add("ui", "disconnect", f"界面断开客户端 {peer}", level="warn")
        return ok

    # ------------------------------------------------------------------
    # 用户逻辑
    # ------------------------------------------------------------------
    def logic_status(self, include_source: bool = False) -> dict[str, Any]:
        return self.logic.status(include_source=include_source)

    def logic_symbols(self) -> dict[str, Any]:
        return {"groups": symbol_catalog(self.config)}

    def logic_save_source(self, source: str) -> dict[str, Any]:
        self.logic.reload(source, persist=True)
        return self.logic.status(include_source=False)

    def logic_enable(self, enabled: bool) -> dict[str, Any]:
        result = self.logic.configure(enabled=bool(enabled), interval_ms=None)
        self.config["logic"]["enabled"] = bool(result["enabled"])
        save_config(self.config)
        return result

    def logic_interval(self, interval_ms: int) -> dict[str, Any]:
        result = self.logic.configure(enabled=None, interval_ms=interval_ms)
        self.config["logic"]["interval_ms"] = int(result["interval_ms"])
        save_config(self.config)
        return result

    def logic_reload_file(self) -> dict[str, Any]:
        self.logic.load_file()
        return self.logic.status(include_source=False)

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        try:
            self.pulse.shutdown()
        except Exception:  # 退出阶段尽力而为
            logger.debug("停止周期输出线程失败", exc_info=True)
        try:
            self.logic.shutdown()
        except Exception:  # 退出阶段尽力而为
            logger.debug("停止 PLC 逻辑线程失败", exc_info=True)
        try:
            if self.server is not None:
                self.server.stop()
        except Exception:  # 退出阶段尽力而为
            logger.debug("停止 S7 服务失败", exc_info=True)


_SIM: Simulator | None = None
_SIM_LOCK = threading.Lock()
_atexit_registered = False


def get_sim() -> Simulator:
    global _SIM, _atexit_registered
    with _SIM_LOCK:
        if _SIM is None:
            if not _atexit_registered:
                atexit.register(lambda: _SIM and _SIM.shutdown())
                _atexit_registered = True
            _SIM = Simulator()
        return _SIM
