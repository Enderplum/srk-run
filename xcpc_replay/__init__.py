# -*- coding: utf-8 -*-
"""XCPC 实时榜单回放程序核心包。

模块划分：
- parser : 数据解析模块，负责发现并解析 ``.srk.json`` 文件
- replay : 回放引擎模块，负责重建提交时间线、ICPC 计分与排名、快照校验
- server : HTTP 服务模块，提供静态页面与数据接口
"""

__version__ = "1.0.0"
