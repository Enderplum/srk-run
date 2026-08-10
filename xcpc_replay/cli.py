# -*- coding: utf-8 -*-
"""命令行入口实现。

被 ``main.py`` 与 ``python -m xcpc_replay`` 两种方式复用，
保证从任意工作目录启动行为一致。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from typing import Optional

from . import __version__, parser, replay, server

#: 项目根目录（本文件所在目录的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_srk_root(srk_dir: Optional[str]) -> str:
    """确定 srk 数据根目录：优先 CLI 指定，其次当前工作目录，最后项目目录。"""
    if srk_dir:
        return os.path.abspath(srk_dir)
    for root in (os.getcwd(), PROJECT_ROOT):
        if os.path.isdir(os.path.join(root, "srk")):
            return root
    return os.getcwd()


def run_validate(srk_root: str) -> int:
    """校验模式：解析全部文件并回放，输出一致性检查结果。"""
    files = parser.find_srk_files(srk_root)
    if not files:
        print("未在 %s/srk 文件夹中发现任何 .srk.json 文件" % srk_root)
        return 2
    ok = True
    for path in files:
        name = os.path.basename(path)
        try:
            data = parser.parse_srk(path)
        except ValueError as exc:
            print("[失败] %s：%s" % (name, exc))
            ok = False
            continue
        issues = replay.validate_against_snapshot(data)
        if issues:
            ok = False
            print("[不一致] %s：共 %d 处" % (name, len(issues)))
            for line in issues[:8]:
                print("    - %s" % line)
        else:
            print("[通过] %s：%d 支队伍、%d 道题、%d 条提交，回放与快照完全一致"
                  % (name, len(data.teams), len(data.problems), len(data.events)))
    print()
    print("校验%s" % ("通过" if ok else "存在不一致"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="xcpc-replay",
        description="XCPC 实时榜单回放程序：解析 srk/ 下的 .srk.json 并在网页端回放比赛过程。",
    )
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8000, help="起始端口（默认 8000，被占用时自动顺延）")
    ap.add_argument("--srk-dir", default=None, help="数据根目录（默认当前工作目录）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--quiet", action="store_true", help="关闭访问日志")
    ap.add_argument("--validate", action="store_true", help="校验数据后退出，不启动服务")
    ap.add_argument("--version", action="version", version="xcpc-replay %s" % __version__)
    args = ap.parse_args(argv)

    srk_root = resolve_srk_root(args.srk_dir)
    if args.validate:
        return run_validate(srk_root)

    files = parser.find_srk_files(srk_root)
    if not files:
        print("警告：在 %s/srk 文件夹中未发现 .srk.json 文件，页面将提示可用数据为空" % srk_root)

    try:
        httpd = server.create_server(srk_root, host=args.host, port=args.port, quiet=args.quiet)
    except OSError as exc:
        print("启动失败：%s" % exc)
        return 1

    actual_port = httpd.server_address[1]
    url = "http://%s:%d/" % (args.host, actual_port)

    print("=" * 58)
    print("  XCPC 实时榜单回放程序  v%s" % __version__)
    print("  数据目录    : %s/srk" % srk_root)
    print("  可用数据文件: %d 个" % len(files))
    print("  访问地址    : %s" % url)
    print("  按 Ctrl+C 停止服务")
    print("=" * 58)

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        httpd.server_close()
    return 0
