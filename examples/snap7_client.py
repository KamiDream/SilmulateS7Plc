#!/usr/bin/env python3
"""独立示例：用 python-snap7 连接本模拟器并读写。

先启动 run.py 并在网页里启动 S7 服务，然后:

    python examples/snap7_client.py --port 1102
"""
from __future__ import annotations

import argparse
import sys

import snap7
from snap7.type import Area


def main() -> int:
    parser = argparse.ArgumentParser(description="python-snap7 连接 S7 模拟器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1102,
                        help="服务配置里的端口 (标准端口 102 需要特权)")
    parser.add_argument("--rack", type=int, default=0)
    parser.add_argument("--slot", type=int, default=1)
    parser.add_argument("--db", type=int, default=1, help="要读写的 DB 号")
    args = parser.parse_args()

    client = snap7.Client()
    try:
        client.connect(args.host, args.rack, args.slot, args.port)
        print(f"connected: {args.host}:{args.port} (rack={args.rack}, slot={args.slot})")

        info = client.get_cpu_info()
        order = client.get_order_code()
        print("CPU       :", info.ModuleTypeName.decode(errors="replace"))
        print("Serial    :", info.SerialNumber.decode(errors="replace"))
        print("Order     :", order.OrderCode.decode(errors="replace"))
        print("PDU       :", client.get_pdu_length(), " CPU state:", client.get_cpu_state())

        # I / Q / M 读写（默认配置就有这三个区域）
        payload = bytearray([0x11, 0x22, 0x33, 0x44])
        client.write_area(Area.MK, 0, 0, payload)
        print("M0 write :", payload.hex())
        print("M0 read  :", bytes(client.read_area(Area.MK, 0, 0, 8)).hex())

        # DB 写 -> 读（默认没有 DB，需先在“数据块”页新建）
        try:
            client.write_area(Area.DB, args.db, 0, payload)
            print(f"DB{args.db} write :", payload.hex())
            print(f"DB{args.db} read  :", bytes(client.read_area(Area.DB, args.db, 0, 8)).hex())
        except Exception as exc:                               # noqa: BLE001
            print(f"DB{args.db} 跳过（请先在“数据块”页新建 DB{args.db}）: {exc}")

        print("I0 read  :", bytes(client.read_area(Area.PE, 0, 0, 4)).hex())
        print("Q0 read  :", bytes(client.read_area(Area.PA, 0, 0, 4)).hex())
        return 0
    except Exception as exc:                                   # noqa: BLE001
        print(f"连接或读写失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("请确认：run.py 正在运行、网页里已点“启动服务”、端口一致。",
              file=sys.stderr)
        return 1
    finally:
        try:
            client.disconnect()
        except Exception:                                      # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
