# -*- coding: utf-8 -*-
"""支持 ``python -m xcpc_replay`` 方式启动（可从任意目录运行）。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
