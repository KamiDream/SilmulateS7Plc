"""Flask 应用组装：页面、静态资源、API 蓝图、错误兜底。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider

from .api import bp as api_bp
from .s7.config import CONFIG_PATH, LOGIC_PATH
from .state import get_sim

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


class AppJSONProvider(DefaultJSONProvider):
    """中文直接输出，方便 curl / 日志阅读。"""

    ensure_ascii = False
    sort_keys = False


def _boot_json() -> str:
    sim = get_sim()
    payload = {
        "overview": sim.overview(),
        "logs": sim.events.tail(200),
    }
    # </script> 会提前结束内联 JSON 的 script 标签
    return json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATE_DIR),
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )
    app.json = AppJSONProvider(app)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    app.register_blueprint(api_bp)

    @app.get("/")
    def index():
        return render_template("index.html", boot_json=_boot_json())

    @app.get("/favicon.ico")
    def favicon():
        return app.send_static_file("logo.svg")

    @app.get("/<path:path>")
    def spa(path: str):
        if path.startswith("api/"):
            return jsonify({"ok": False, "error": f"接口不存在: /{path}"}), 404
        return render_template("index.html", boot_json=_boot_json())

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": f"接口不存在: {request.path}"}), 404
        return render_template("index.html", boot_json=_boot_json())

    @app.errorhandler(500)
    def server_error(exc):                                  # pragma: no cover
        return jsonify({"ok": False, "error": f"服务器内部错误: {exc}"}), 500

    @app.after_request
    def no_cache_api(response):
        # 本地工具，改完前端就希望立刻生效：API 和静态资源都不缓存。
        if request.path.startswith(("/api/", "/static/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()


def paths() -> dict[str, str]:
    """给启动脚本展示用的路径。"""
    return {"config": str(CONFIG_PATH), "logic": str(LOGIC_PATH),
            "web": str(WEB_DIR), "cwd": os.getcwd()}
