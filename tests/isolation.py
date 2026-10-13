# -*- coding: utf-8 -*-
"""测试辅助：临时解除隔离（**仅用于验证隔离层里面的业务逻辑**）。

背景：``conftest.py`` 默认把整个测试进程关进「readonly + 端口全禁」的笼子里。
但有些测试要验证的**不是**隔离层，而是笼子里面的业务逻辑——例如
「端口打不开时必须给出结论，不能只甩一串打不开的口」。

这类测试就显式声明「我在用已授权的环境跑」，用完**立刻恢复**。
为什么用上下文管理器而不是改全局：避免测试之间互相污染，
那类污染会制造最难查的故障（单跑绿、全跑红）。
"""

import contextlib
import os
import unittest.mock as mock


@contextlib.contextmanager
def relaxed_env(mode="off", deny=None):
    """临时解除 guard 隔离。

    mode : "off"（全放行）或 "write"（放行写，仍保留名单检查）
    deny : 传字符串则设为端口禁用名单；默认 None 表示**清空名单**
    """
    env = {
        "MCU_DEBUG_MCP_MODE": mode,
        "MCU_DEBUG_MCP_PORT_DENY": deny if deny is not None else "",
    }
    with mock.patch.dict(os.environ, env):
        yield
