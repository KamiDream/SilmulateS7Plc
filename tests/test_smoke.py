"""端到端冒烟测试：Flask API + python-snap7 客户端 + PLC 逻辑。

运行:
    /home/meng_xi/miniconda3/envs/simuulateS7/bin/python -m unittest tests.test_smoke -v

测试使用临时数据目录（S7SIM_DATA_DIR），不会改动项目里的 data/。
"""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
import time
import unittest

# 必须在导入 backend 之前设置，config.py 在导入时读取该变量。
_TMP = tempfile.mkdtemp(prefix="s7sim-test-")
os.environ["S7SIM_DATA_DIR"] = _TMP

import snap7                                                    # noqa: E402
from snap7.type import Area                                      # noqa: E402

from backend.app import create_app                               # noqa: E402
from backend.state import get_sim                                # noqa: E402


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port = _free_port()
        cls.app = create_app()
        cls.api = cls.app.test_client()
        result = cls.post("/api/config", {
            "server": {"host": "127.0.0.1", "port": cls.port,
                       "pdu_size": 480, "max_clients": 8},
            "areas": {"I": 32, "Q": 32, "M": 64},
            "dbs": [{"number": 1, "size": 128, "comment": "test"}],
            "logic": {"enabled": False, "interval_ms": 100},
        })
        assert result["ok"], result
        cls.post("/api/server/start")
        cls.s7 = snap7.Client()
        cls.s7.connect("127.0.0.1", 0, 2, cls.port)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.s7.disconnect()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            cls.api.post("/api/server/stop")
        except Exception:                                        # noqa: BLE001
            pass
        try:
            get_sim().shutdown()
        except Exception:                                        # noqa: BLE001
            pass
        shutil.rmtree(_TMP, ignore_errors=True)

    @classmethod
    def post(cls, url: str, data=None) -> dict:
        response = cls.api.post(url, json=data or {})
        payload = response.get_json() or {}
        assert response.status_code == 200, (url, response.status_code, payload)
        assert payload.get("ok") is not False, (url, payload)
        return payload

    @classmethod
    def get(cls, url: str, **params) -> dict:
        response = cls.api.get(url, query_string=params)
        payload = response.get_json() or {}
        assert response.status_code == 200, (url, response.status_code, payload)
        assert payload.get("ok") is not False, (url, payload)
        return payload

    # ------------------------------------------------------------------
    def test_01_api_and_snap7_read_write(self) -> None:
        # API 写 -> snap7 读
        self.post("/api/memory/write",
                  {"area": "DB", "db": 1, "start": 0, "hex": "11 22 33 44"})
        self.assertEqual(bytes(self.s7.read_area(Area.DB, 1, 0, 4)).hex(), "11223344")

        # snap7 写 -> API 读
        self.s7.write_area(Area.PA, 0, 0, bytearray([0xAB, 0xCD]))
        q = self.get("/api/memory", area="Q", db=0, start=0, count=4)
        self.assertEqual(q["data"], "abcd0000")

        # 位操作
        self.post("/api/memory/bit",
                  {"area": "I", "db": 0, "byte": 0, "bit": 0, "value": True})
        self.assertEqual(bytes(self.s7.read_area(Area.PE, 0, 0, 1)), b"\x01")

        # 多变量读取（python-snap7 3.3.1 的优化路径）
        code, items = self.s7.read_multi_vars([
            {"area": Area.DB, "db_number": 1, "start": 0, "size": 4},
            {"area": Area.MK, "db_number": 0, "start": 0, "size": 2},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(bytes(items[0]).hex(), "11223344")

    def test_02_szl_and_clients(self) -> None:
        info = self.s7.get_cpu_info()
        self.assertIn(b"CPU 315", info.ModuleTypeName)
        self.assertEqual(info.SerialNumber, b"S C-C2UR28922012")
        order = self.s7.get_order_code()
        self.assertIn(b"6ES7", order.OrderCode)
        self.assertEqual(int(self.s7.get_pdu_length()), 480)

        clients = self.get("/api/clients")["clients"]
        self.assertGreaterEqual(len(clients), 1)
        self.assertGreater(clients[0]["requests"], 0)

        # CPU RUN/STOP 控制
        self.assertEqual(self.post("/api/server/cpu", {"state": "STOP"})["server"]["cpu"], "STOP")
        self.assertEqual(self.post("/api/server/cpu", {"state": "RUN"})["server"]["cpu"], "RUN")

    def test_03_logs(self) -> None:
        logs = self.get("/api/logs", since=0, limit=1000)["items"]
        kinds = {(item["source"], item["kind"]) for item in logs}
        self.assertIn(("s7", "read"), kinds)
        self.assertIn(("s7", "write"), kinds)
        self.assertIn(("ui", "write"), kinds)
        self.assertIn(("server", "client_connect"), kinds)
        self.assertTrue(any("DB1" in item["summary"] for item in logs
                            if item["kind"] == "write"))

    def test_04_logic(self) -> None:
        # 全新环境：逻辑文件为空，示例只作为编辑器占位文字
        initial = self.get("/api/logic", source=1)
        self.assertEqual(initial["source"], "")
        self.assertTrue(initial["loaded"])

        # 先清掉前面测试留下的输入/输出，避免“Q 本来就是 1”造成假通过
        for area in ("I", "Q"):
            self.post("/api/memory/bit",
                      {"area": area, "db": 0, "byte": 0, "bit": 0, "value": False})

        source = "%Q0.0 := %I0.0;\n"
        self.post("/api/logic/source", {"source": source})
        self.assertTrue(self.post("/api/logic/enable", {"enabled": True})["enabled"])
        self.post("/api/memory/bit",
                  {"area": "I", "db": 0, "byte": 0, "bit": 0, "value": True})

        deadline = time.time() + 3.0
        q_value = b"\x00"
        status = {}
        while time.time() < deadline:
            q_value = bytes(self.s7.read_area(Area.PA, 0, 0, 1))
            status = self.get("/api/logic")
            if q_value and q_value[0] & 0x01 and status["scans"] > 0:
                break
            time.sleep(0.05)
        self.assertEqual(q_value[0] & 0x01, 1)
        self.assertGreater(status["scans"], 0)
        self.assertEqual(status["errors"], 0)
        self.post("/api/logic/enable", {"enabled": False})

    def test_05_logic_error_disables(self) -> None:
        self.post("/api/logic/source", {"source": "%Q0.0 := 1 / 0;\n"})
        self.post("/api/logic/enable", {"enabled": True})
        time.sleep(0.5)
        status = self.get("/api/logic")
        self.assertFalse(status["enabled"])
        self.assertGreaterEqual(status["errors"], 1)
        self.assertIn("除数为 0", status["last_error"])

        # 语法错误应该是 400，而不是 500
        response = self.api.post("/api/logic/source",
                                 json={"source": "IF TRUE THEN\n"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse((response.get_json() or {}).get("ok"))

        # 未知变量在编译期就会被拦下
        response = self.api.post("/api/logic/source",
                                 json={"source": '"NoSuchTag" := TRUE;'})
        self.assertEqual(response.status_code, 400)
        self.assertIn("未知变量", response.get_json()["error"])

    def test_06_config_restart_and_persistence(self) -> None:
        marker = {"area": "DB", "db": 1, "start": 16, "hex": "DE AD BE EF"}
        self.post("/api/memory/write", marker)

        result = self.post("/api/config", {"areas": {"M": 128}})
        self.assertTrue(result["restart_required"])
        self.assertTrue(self.get("/api/overview")["restart_required"])

        self.s7.disconnect()
        self.post("/api/server/stop")
        self.post("/api/server/start")
        self.assertFalse(self.get("/api/overview")["restart_required"])

        # 重启后数据仍在（同 key 按较小长度保留）
        type(self).s7 = snap7.Client()
        type(self).s7.connect("127.0.0.1", 0, 2, self.port)
        self.assertEqual(
            bytes(self.s7.read_area(Area.DB, 1, 16, 4)).hex(), "deadbeef")

        # PDU 配置生效
        self.s7.disconnect()
        self.post("/api/config", {"server": {"pdu_size": 240}})
        self.post("/api/server/stop")
        self.post("/api/server/start")
        type(self).s7 = snap7.Client()
        type(self).s7.connect("127.0.0.1", 0, 2, self.port)
        self.assertEqual(int(self.s7.get_pdu_length()), 240)
        self.assertEqual(len(bytes(self.s7.read_area(Area.DB, 1, 0, 100))), 100)

    def test_07_db_editor_layout_and_values(self) -> None:
        variables = [
            {"name": "A", "type": "Bool", "start": "true", "comment": ""},
            {"name": "N", "type": "Int", "start": "16#1234", "comment": ""},
            {"name": "R", "type": "Real", "start": "1.5", "comment": ""},
            {"name": "S", "type": "String[5]", "start": "AB", "comment": ""},
            {"name": "Arr", "type": "Array[0..3] of Bool",
             "start": ["true", "false", "true", "false"], "comment": ""},
        ]
        preview = self.post("/api/db/preview",
                            {"number": 1, "variables": variables})
        offsets = [(row["name"], row["offset_text"], row["size"])
                   for row in preview["rows"]]
        self.assertEqual(offsets, [
            ("A", "0.0", 1), ("N", "2.0", 2), ("R", "4.0", 4),
            ("S", "8.0", 7), ("Arr", "15.0", 1),
        ])
        self.assertEqual(preview["required_size"], 16)

        saved = self.post("/api/db/schema",
                          {"number": 1, "variables": variables})
        self.assertEqual(saved["required_size"], 16)
        self.assertEqual(saved["db"]["name"], "DB1")

        # 下载起始值后，按布局写入的字节应与 TIA 标准 DB 一致
        self.post("/api/db/apply-start", {"number": 1})
        raw = bytes(self.s7.read_area(Area.DB, 1, 0, 16)).hex()
        self.assertEqual(raw, "010012343fc000000502414200000005")

        # 按变量写入监视值
        written = self.post("/api/db/write", {
            "number": 1, "variables": variables, "index": 1,
            "value": "16#ABCD",
        })
        self.assertEqual(written["monitor"], "-21555")
        self.assertEqual(bytes(self.s7.read_area(Area.DB, 1, 2, 2)).hex(), "abcd")

        # 实际值 → 起始值
        snapshot = self.post("/api/db/snapshot", {"number": 1})
        starts = {item["name"]: item["start"] for item in snapshot["variables"]}
        self.assertEqual(starts["N"], "-21555")

        # 创建/删除 DB：新建不需要大小，先建最小区，变量保存时再扩展
        created = self.post("/api/db/create",
                            {"number": 2, "name": "Second", "comment": "new"})
        self.assertEqual(created["db"]["size"], 1)
        self.assertEqual(created["db"]["name"], "Second")
        names = [item["number"] for item in self.get("/api/db/list")["dbs"]]
        self.assertIn(2, names)

        # DB 名称全局唯一
        response = self.api.post("/api/db/create",
                                 json={"number": 3, "name": "Second"})
        self.assertEqual(response.status_code, 400)
        response = self.api.post("/api/db/schema",
                                 json={"number": 2, "variables": [],
                                       "name": "DB1"})
        self.assertEqual(response.status_code, 400)

        # 重命名 DB1，供 SCL 使用 "Motor".Field
        renamed = self.post("/api/db/schema", {
            "number": 1, "variables": variables, "name": "Motor"})
        self.assertEqual(renamed["db"]["name"], "Motor")

        self.post("/api/db/delete", {"number": 2})
        names = [item["number"] for item in self.get("/api/db/list")["dbs"]]
        self.assertNotIn(2, names)

    def test_08_plc_tag_table(self) -> None:
        i_tags = [{"name": "StartButton", "type": "Bool",
                   "address": "%I0.0", "comment": "启动"}]
        q_tags = [{"name": "Lamp", "type": "Bool",
                   "address": "Q0.1", "comment": ""}]
        m_tags = [
            {"name": "Speed", "type": "Int", "address": "M10.3", "comment": ""},
            {"name": "Temp", "type": "Real", "address": "MD20", "comment": ""},
        ]
        # 预览/保存时地址按类型规范化：%I0.0 -> I0.0、M10.3(Bool->Int) -> MW10
        preview = self.post("/api/tags/preview",
                            {"area": "M", "table": 3, "variables": m_tags})
        self.assertEqual([row["address"] for row in preview["rows"]],
                         ["MW10", "MD20"])
        self.assertEqual(preview["required"], {"I": 0, "Q": 0, "M": 24})
        self.assertEqual(preview["grow"], {})
        self.assertEqual(preview["area"], "M")

        self.post("/api/tags", {"area": "I", "table": 1, "variables": i_tags})
        self.post("/api/tags", {"area": "Q", "table": 2, "variables": q_tags})
        saved = self.post("/api/tags", {"area": "M", "table": 3,
                                         "variables": m_tags})
        self.assertEqual(saved["variables"][0]["address"], "MW10")
        self.assertEqual(saved["grow"], {})

        # 区域页只能声明本区域地址
        response = self.api.post("/api/tags", json={
            "area": "M", "table": 3, "variables": i_tags})
        self.assertEqual(response.status_code, 400)
        self.assertIn("I 区", response.get_json()["error"])

        # 界面写监视值 -> snap7 读到相同字节
        self.post("/api/tags/write", {"table": 3, "variables": m_tags,
                                      "index": 0, "value": "16#1234"})
        self.assertEqual(bytes(self.s7.read_area(Area.MK, 0, 10, 2)).hex(), "1234")
        self.post("/api/tags/write", {"table": 3, "variables": m_tags,
                                      "index": 1, "value": "2.5"})
        self.assertEqual(bytes(self.s7.read_area(Area.MK, 0, 20, 4)).hex(), "40200000")
        self.post("/api/tags/write", {"table": 1, "variables": i_tags,
                                      "index": 0, "value": True})
        self.assertEqual(bytes(self.s7.read_area(Area.PE, 0, 0, 1))[0] & 0x01, 1)
        self.post("/api/tags/write", {"table": 2, "variables": q_tags,
                                      "index": 0, "value": "true"})
        self.assertEqual(bytes(self.s7.read_area(Area.PA, 0, 0, 1))[0] & 0x02, 0x02)

        monitors = {row["name"]: row["monitor"]
                    for row in self.get("/api/tags", area="M", table=3)["rows"]}
        self.assertEqual(monitors["Speed"], "4660")
        self.assertEqual(monitors["Temp"], "2.5")
        self.assertEqual(self.get("/api/tags", area="I")["rows"][0]["monitor"], "true")
        self.assertEqual(self.get("/api/tags", area="Q")["rows"][0]["monitor"], "true")

        # 客户端写入后，变量表的监视值跟着变
        self.s7.write_area(Area.MK, 0, 10, bytearray([0xFF, 0xFE]))
        monitors = {row["name"]: row["monitor"]
                    for row in self.get("/api/tags", area="M", table=3)["rows"]}
        self.assertEqual(monitors["Speed"], "-2")

        # 地址越界/非法、重名、值越界都是 400
        bad = [
            {"area": "I", "table": 1,
             "variables": [{"name": "X", "type": "Bool", "address": "Z0.0"}]},
            {"area": "I", "table": 1,
             "variables": [{"name": "X", "type": "Bool", "address": "I1.0"},
                           {"name": "x", "type": "Bool", "address": "I2.0"}]},
            {"area": "I", "table": 1,
             "variables": [{"name": "X", "type": "Bool", "address": "Q1.0"}]},
        ]
        for payload in bad:
            response = self.api.post("/api/tags", json=payload)
            self.assertEqual(response.status_code, 400, payload)
        response = self.api.post("/api/tags/write",
                                 json={"table": 3, "variables": m_tags,
                                       "index": 0, "value": "40000"})
        self.assertEqual(response.status_code, 400)

        # 变量地址超过区域大小时自动扩展，保存后可直接读写
        grown = self.post("/api/tags", {"area": "M", "table": 3, "variables": m_tags + [
            {"name": "Big", "type": "Byte", "address": "M300", "comment": ""}]})
        self.assertEqual(grown["grown"], {"M": 301})
        self.assertEqual(grown["areas"]["M"], 301)
        self.post("/api/tags/write", {"table": 3, "variables": grown["variables"],
                                      "index": 2, "value": "16#7F"})
        self.assertEqual(bytes(self.s7.read_area(Area.MK, 0, 300, 1)).hex(), "7f")

        # 反过来把区域缩小到变量放不下时，配置保存应被拒绝
        response = self.api.post("/api/config", json={"areas": {"M": 100}})
        self.assertEqual(response.status_code, 400)

    def test_09_tag_tables_and_conflicts(self) -> None:
        # 在同一区域（M）新建第二张变量表
        created = self.post("/api/tags/create",
                            {"area": "M", "name": "M 辅助表", "comment": "test"})
        table_id = created["table"]["id"]
        self.assertEqual(created["area"], "M")
        self.assertEqual(len(created["tables"]), 2)
        self.assertEqual({item["id"] for item in created["tables"]},
                         {3, table_id})

        # 跨表冲突：MB10/MB11 与主表的 MW10 重叠
        conflict = [{"name": "Byte10", "type": "Byte", "address": "MB10"}]
        response = self.api.post("/api/tags", json={
            "area": "M", "table": table_id, "variables": conflict})
        self.assertEqual(response.status_code, 400)
        self.assertIn("冲突", response.get_json()["error"])
        self.assertIn("MW10", response.get_json()["error"])
        preview = self.post("/api/tags/preview", {
            "area": "M", "table": table_id, "variables": conflict})
        self.assertTrue(preview["conflicts"])
        self.assertEqual(preview["conflict_indexes"], [0])

        # 不冲突的地址可以保存；同一字节不同位不冲突
        self.post("/api/tags", {"area": "M", "table": table_id, "variables": [
            {"name": "Spare1", "type": "Bool", "address": "M30.0"},
            {"name": "Spare2", "type": "Bool", "address": "M30.1"}]})

        # 同一张表内的重叠也要拦
        response = self.api.post("/api/tags", json={
            "area": "M", "table": table_id, "variables": [
                {"name": "Bit0", "type": "Bool", "address": "M31.0"},
                {"name": "Bit0b", "type": "Bool", "address": "M31.0"}]})
        self.assertEqual(response.status_code, 400)

        # I 区页面/接口只看得到 I 区变量表
        input_payload = self.get("/api/tags", area="I")
        self.assertEqual([item["area"] for item in input_payload["tables"]], ["I"])
        response = self.api.get("/api/tags", query_string={"area": "I", "table": table_id})
        self.assertEqual(response.status_code, 400)

        # 变量表重命名/备注随保存一起生效；跨区域重名也被拒绝
        renamed = self.post("/api/tags", {"area": "M", "table": table_id,
                                          "variables": [
                                              {"name": "Spare1", "type": "Bool",
                                               "address": "M30.0"}],
                                          "name": "M 备用表", "comment": "renamed"})
        self.assertEqual(renamed["table"]["name"], "M 备用表")
        self.assertEqual(renamed["table"]["comment"], "renamed")
        response = self.api.post("/api/tags", json={
            "area": "M", "table": table_id, "variables": [],
            "name": "输入变量表"})
        self.assertEqual(response.status_code, 400)

        # 删除第二张表后回到一张；不存在的表返回 400
        deleted = self.post("/api/tags/delete", {"table": table_id})
        self.assertEqual([item["id"] for item in deleted["tables"]], [1, 2, 3])
        response = self.api.post("/api/tags/delete", json={"table": table_id})
        self.assertEqual(response.status_code, 400)

    def test_10_legacy_tags_config_migrates(self) -> None:
        from backend.s7.config import normalize_config
        # 旧版扁平 tags：按区域拆成 I/Q/M 三张表
        config = normalize_config({"tags": [
            {"name": "OldI", "type": "Bool", "address": "I0.0"},
            {"name": "OldQ", "type": "Bool", "address": "Q0.0"},
            {"name": "OldM", "type": "Int", "address": "MW10"},
        ]})
        self.assertNotIn("tags", config)
        tables = config["tag_tables"]
        self.assertEqual([table["area"] for table in tables], ["I", "Q", "M"])
        self.assertEqual([table["id"] for table in tables], [1, 2, 3])
        self.assertEqual([table["name"] for table in tables],
                         ["默认变量表", "默认变量表 · Q", "默认变量表 · M"])
        self.assertEqual([table["variables"][0]["name"] for table in tables],
                         ["OldI", "OldQ", "OldM"])

        # 旧版多表但没有 area 字段：混合表按区域拆分
        config = normalize_config({"tag_tables": [{
            "name": "混合",
            "variables": [
                {"name": "A", "type": "Bool", "address": "I0.0"},
                {"name": "B", "type": "Byte", "address": "MB5"},
            ],
        }]})
        tables = config["tag_tables"]
        self.assertEqual([table["area"] for table in tables], ["I", "M"])
        self.assertEqual([table["name"] for table in tables], ["混合", "混合 · M"])

    def test_11_scl_symbolic_tags_and_db(self) -> None:
        # 当前变量表：StartButton(I0.0)、Lamp(Q0.1)、Speed(MW10)、Temp、Big
        # DB1 已命名为 Motor（test_07），N 是偏移 2 的 Int
        self.post("/api/memory/bit", {"area": "I", "db": 0, "byte": 0,
                                      "bit": 0, "value": False})
        self.post("/api/memory/bit", {"area": "Q", "db": 0, "byte": 0,
                                      "bit": 1, "value": False})
        self.post("/api/tags/write", {"table": 3, "index": 0, "value": "41"})

        # 变量目录：DB 变量展示为 "DB名".变量，标签展示为 "Tag"
        catalog = self.get("/api/logic/symbols")
        symbols = {item["symbol"] for group in catalog["groups"]
                   for item in group["items"]}
        self.assertIn('"StartButton"', symbols)
        self.assertIn('"Lamp"', symbols)
        self.assertIn('"Motor".N', symbols)
        self.assertIn('"Motor".R', symbols)

        source = (
            '"Lamp" := "StartButton";\n'
            'IF "Speed" > 40 THEN\n'
            '    "Motor".N := "Speed" + 1;\n'
            'ELSE\n'
            '    "Motor".N := 0;\n'
            'END_IF;\n'
        )
        status = self.post("/api/logic/source", {"source": source})
        self.assertEqual(status["language"], "SCL")
        self.assertTrue(status["loaded"])
        self.assertTrue(self.post("/api/logic/enable", {"enabled": True})["enabled"])

        self.post("/api/memory/bit", {"area": "I", "db": 0, "byte": 0,
                                      "bit": 0, "value": True})
        deadline = time.time() + 3.0
        q_raw = b"\x00"
        db_value = b"\x00\x00"
        while time.time() < deadline:
            q_raw = bytes(self.s7.read_area(Area.PA, 0, 0, 1))
            db_value = bytes(self.s7.read_area(Area.DB, 1, 2, 2))
            if q_raw[0] & 0x02 and db_value == b"\x00\x2a":
                break
            time.sleep(0.05)
        self.assertEqual(q_raw[0] & 0x02, 0x02)
        self.assertEqual(db_value, b"\x00\x2a")      # 41 + 1

        # 旧的 DB 编号写法仍可作为别名
        self.post("/api/logic/source", {"source": '"DB1".N := 1;\n'})

        status = self.get("/api/logic")
        self.assertGreater(status["scans"], 0)
        self.assertEqual(status["errors"], 0)
        self.post("/api/logic/enable", {"enabled": False})

        # String 变量在编译期给出明确提示
        response = self.api.post("/api/logic/source",
                                 json={"source": '"Motor".S := \'x\';'})
        self.assertEqual(response.status_code, 400)
        self.assertIn("暂不支持", response.get_json()["error"])

        # 恢复默认的绝对地址示例，避免影响后续调试
        self.post("/api/logic/source", {"source": "%Q0.0 := %I0.0;\n"})

    def test_12_periodic_output(self) -> None:
        """变量表的「周期」列：Bool 写位、SInt 写字节，在 CPU RUN 时自己输出 1 / 0。"""
        # 归一化：100 保持；250/250 对称折算成 500；不填就是不起振
        payload = self.post("/api/db/schema", {
            "number": 1,
            "variables": [
                {"name": "Blink", "type": "Bool", "start": "false", "pulse": "100"},
                {"name": "Half", "type": "Bool", "start": "false", "pulse": "250/250"},
                {"name": "Always", "type": "Bool", "start": "false"},
                {"name": "Counter", "type": "SInt", "start": "0", "pulse": "80"},
            ],
        })
        pulses = {item["name"]: item["pulse"] for item in payload["variables"]}
        self.assertEqual(pulses["Blink"], "100")
        self.assertEqual(pulses["Half"], "500")
        self.assertEqual(pulses["Always"], "")
        self.assertEqual(pulses["Counter"], "80")

        self.post("/api/server/cpu", {"state": "RUN"})

        # DB 里的 Bool 确实在翻转（100 ms 周期，1 s 内必定既看到 1 也看到 0）
        seen = set()
        deadline = time.time() + 3.0
        while time.time() < deadline and len(seen) < 2:
            seen.add(bytes(self.s7.read_area(Area.DB, 1, 0, 1))[0] & 0x01)
            time.sleep(0.02)
        self.assertEqual(seen, {0, 1})

        # SInt 变量：整字节写 1 / 0（3 个 Bool 后按字节对齐，Counter 在 byte 1）
        seen_int = set()
        deadline = time.time() + 3.0
        while time.time() < deadline and len(seen_int) < 2:
            seen_int.add(bytes(self.s7.read_area(Area.DB, 1, 1, 1))[0])
            time.sleep(0.02)
        self.assertEqual(seen_int, {0, 1})

        # I/Q/M 变量表同样生效
        table_id = self.post("/api/tags/create", {
            "area": "Q", "name": "周期输出"})["table"]["id"]
        self.post("/api/tags", {
            "area": "Q", "table": table_id,
            "variables": [
                {"name": "Lamp", "type": "Bool",
                 "address": "Q0.3", "pulse": "60"},
                {"name": "Duty", "type": "SInt",
                 "address": "QB4", "pulse": "60"},
            ],
        })
        seen_q = set()
        deadline = time.time() + 3.0
        while time.time() < deadline and len(seen_q) < 2:
            seen_q.add((bytes(self.s7.read_area(Area.PA, 0, 0, 1))[0] >> 3) & 0x01)
            time.sleep(0.02)
        self.assertEqual(seen_q, {0, 1})

        seen_qb = set()
        deadline = time.time() + 3.0
        while time.time() < deadline and len(seen_qb) < 2:
            seen_qb.add(bytes(self.s7.read_area(Area.PA, 0, 4, 1))[0])
            time.sleep(0.02)
        self.assertEqual(seen_qb, {0, 1})

        # 非 Bool 变量配周期要明确报错
        response = self.api.post("/api/db/preview", json={
            "number": 1,
            "variables": [{"name": "Count", "type": "Int", "pulse": "500"}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Bool", response.get_json()["error"])

        # 轻量监视接口（页面快刷新用，只回监视值）
        monitor = self.get("/api/db/monitor", number=1)["rows"]
        self.assertEqual(monitor[0]["index"], 0)
        self.assertIn(monitor[0]["monitor"], ("true", "false", "0", "1"))
        self.assertIn("variables", self.get("/api/db", number=1))
        self.assertNotIn("variables", self.get("/api/db/monitor", number=1))
        tag_monitor = self.get("/api/tags/monitor", area="Q", table=table_id)["rows"]
        self.assertEqual(len(tag_monitor), 2)
        self.assertIn(tag_monitor[0]["monitor"], ("true", "false"))

        # 关掉周期，避免影响后续手工调试
        self.post("/api/db/schema", {"number": 1, "variables": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
