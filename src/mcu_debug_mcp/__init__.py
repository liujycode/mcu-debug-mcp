# -*- coding: utf-8 -*-
"""mcu-debug-mcp · 包入口。

★ 本文件刻意保持最小：只放版本号，不做任何 import ——
  包内模块互为依赖（core → analyzer/guard/paths），在 ``__init__`` 里
  提前 import 任何子模块都会制造循环导入或把 MCP 框架拖进「import 包
  就必然加载」的路径（MCP server 只应被 server.py 拉起）。

版本单一来源：pyproject.toml 用 ``dynamic = ["version"]`` 从这里读
（``[tool.setuptools.dynamic] version = {attr = ...}``），改版本只改这一处。
"""

__version__ = "4.5.5"
