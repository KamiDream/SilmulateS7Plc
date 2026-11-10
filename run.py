#!/usr/bin/env python3
"""SilmulateS7Plc 启动入口。

用法:
    python run.py                     # Web UI: http://127.0.0.1:5000
    python run.py --no-browser
    python run.py --web-port 5050
    python run.py --web-host 0.0.0.0  # 允许局域网访问 (会执行用户 Python 逻辑, 注意风险)
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import threading
import time
import webbrowser

from backend.app import create_app
from backend.state import get_sim


def main() -> int:
    parser = argparse.ArgumentParser(description="S7 PLC 模拟器 (Web 配置界面)")
    parser.add_argument("--web-host", default="127.0.0.1",
                        help="Web UI 监听地址 (默认 127.0.0.1)")
    parser.add_argument("--web-port", type=int, default=5005,
                        help="Web UI 端口 (默认 5000)")
    parser.add_argument("--no-browser", action="store_true",
                        help="不要自动打开浏览器")
    parser.add_argument("--debug", action="store_true", help="Flask 调试模式")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("snap7").setLevel(logging.ERROR)

    sim = get_sim()
    server = sim.config["server"]
    print()
    print("  SilmulateS7Plc · S7 PLC 模拟器")
    print(f"  Web UI : http://{args.web_host}:{args.web_port}")
    print(f"  S7 配置: {server['host']}:{server['port']} "
          f"(在网页里启动服务)")
    print(f"  配置   : {sim.overview()['config_path']}")
    print(f"  用户逻辑: {sim.overview()['logic_path']}")
    print()

    if args.web_host not in ("127.0.0.1", "localhost", "::1"):
        print("  ⚠ Web UI 监听在非回环地址；任何能访问它的人都可以修改 PLC 变量和 SCL 逻辑。")
        print()

    if not args.no_browser:
        def open_later() -> None:
            time.sleep(0.8)
            with contextlib.suppress(Exception):
                webbrowser.open(f"http://127.0.0.1:{args.web_port}")
        threading.Thread(target=open_later, daemon=True).start()

    app = create_app()
    try:
        app.run(host=args.web_host, port=args.web_port,
                debug=args.debug, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        sim.shutdown()
        print("已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
