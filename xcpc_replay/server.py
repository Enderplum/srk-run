# -*- coding: utf-8 -*-
"""HTTP 服务模块。

基于 Python 标准库 ``http.server`` 实现零依赖 Web 服务：
- ``GET /``            返回回放主页
- ``GET /static/*``    返回前端静态资源（style.css / app.js）
- ``GET /api/files``   返回 srk 文件夹中可用的 ``.srk.json`` 文件列表
- ``GET /api/contest`` 返回指定比赛的完整回放数据载荷
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import parser, replay
from .parser import ContestData

#: 前端静态资源目录（随包分发）
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class ReplayHTTPServer(ThreadingHTTPServer):
    """带业务上下文（srk 根目录、载荷缓存）的线程化 HTTP 服务器。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, srk_root: str, quiet: bool = False):
        super().__init__(server_address, ReplayRequestHandler)
        self.srk_root = srk_root
        self.quiet = quiet
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()


class ReplayRequestHandler(BaseHTTPRequestHandler):
    """请求处理器：路由分发 + 静态资源 + JSON API。"""

    server: ReplayHTTPServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    def do_GET(self):  # noqa: N802 - http.server 约定命名
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._serve_file(os.path.join(WEB_DIR, "index.html"), "html")
            elif path == "/favicon.ico":
                self._serve_file(os.path.join(WEB_DIR, "favicon.svg"), "svg")
            elif path.startswith("/static/"):
                name = os.path.basename(path[len("/static/"):])
                self._serve_file(os.path.join(WEB_DIR, name), None)
            elif path == "/api/files":
                self._api_files()
            elif path == "/api/contest":
                self._api_contest(parse_qs(parsed.query))
            else:
                self._send_text("404 Not Found", status=404)
        except BrokenPipeError:
            pass  # 客户端提前断开，忽略
        except Exception as exc:  # 兜底：避免单个请求拖垮服务
            self._send_text("500 Internal Server Error: %s" % exc, status=500)

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #
    def _api_files(self) -> None:
        files = parser.find_srk_files(self.server.srk_root)
        names = [os.path.basename(f) for f in files]
        default = names[0] if names else None
        self._send_json({"files": names, "default": default})

    def _api_contest(self, query: Dict[str, List[str]]) -> None:
        file_name = (query.get("file") or [""])[0]
        if not file_name:
            self._send_json({"error": "缺少 file 参数"}, status=400)
            return
        available = {os.path.basename(f) for f in parser.find_srk_files(self.server.srk_root)}
        if file_name not in available:
            self._send_json(
                {"error": "文件不可用（仅支持 srk/ 子文件夹中的 .srk.json）：%s" % file_name},
                status=400)
            return
        payload = self._load_payload(file_name)
        if payload is None:
            self._send_json({"error": "文件解析失败，请检查数据文件"}, status=500)
            return
        self._send_json(payload)

    def _load_payload(self, file_name: str) -> Optional[Dict[str, Any]]:
        """带缓存的载荷构建：同名文件只解析一次。"""
        with self.server._cache_lock:
            if file_name in self.server._cache:
                return self.server._cache[file_name]
        data = self._parse_contest(file_name)
        if data is None:
            return None
        payload = replay.build_payload(data)
        with self.server._cache_lock:
            self.server._cache[file_name] = payload
        return payload

    def _parse_contest(self, file_name: str) -> Optional[ContestData]:
        for f in parser.find_srk_files(self.server.srk_root):
            if os.path.basename(f) == file_name:
                try:
                    return parser.parse_srk(f)
                except ValueError as exc:
                    if not self.server.quiet:
                        print("[xcpc-replay] %s" % exc)
                    return None
        return None

    # ------------------------------------------------------------------ #
    # 响应工具
    # ------------------------------------------------------------------ #
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: str, forced_kind: Optional[str]) -> None:
        if not os.path.isfile(path):
            self._send_text("404 Not Found", status=404)
            return
        kind = forced_kind or (mimetypes.guess_type(path)[0] or "application/octet-stream")
        if kind == "html":
            kind = "text/html; charset=utf-8"
        elif kind == "js":
            kind = "text/javascript; charset=utf-8"
        elif kind == "css":
            kind = "text/css; charset=utf-8"
        elif kind == "svg":
            kind = "image/svg+xml"
        with open(path, "rb") as fp:
            body = fp.read()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.server.quiet:
            super().log_message(fmt, *args)


def create_server(srk_root: str, host: str = "127.0.0.1", port: int = 8000,
                  quiet: bool = False) -> ReplayHTTPServer:
    """创建服务器实例；若端口被占用则自动向后顺延探测可用端口。

    Returns:
        绑定成功的服务器（可通过 ``server.server_address[1]`` 获取实际端口）。
    """
    for candidate in range(port, port + 100):
        try:
            return ReplayHTTPServer((host, candidate), srk_root, quiet=quiet)
        except OSError:
            continue
    raise OSError("在端口 %d-%d 范围内均无法绑定，请尝试 --port 指定其他端口" % (port, port + 99))
