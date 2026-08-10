# -*- coding: utf-8 -*-
"""XCPC 实时榜单回放程序 — 程序入口。

用法示例：
    python main.py                      # 启动服务并自动打开浏览器
    python main.py --port 9000          # 指定端口
    python main.py --srk-dir ./data     # 指定数据根目录（默认当前目录）
    python main.py --no-browser         # 不自动打开浏览器
    python main.py --validate           # 只校验数据并退出（不启动服务）
"""

import sys

from xcpc_replay.cli import main

if __name__ == "__main__":
    sys.exit(main())
