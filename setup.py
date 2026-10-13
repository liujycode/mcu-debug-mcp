# -*- coding: utf-8 -*-
"""构建配置（极简版）——真正的打包声明全在 pyproject.toml。

历史沿革（为什么这个文件还在，而不是删掉）：

    2026-10-13 之前：本文件用 build_py 钩子把 knowledge/ drivers/ tools/
    整目录复制进 build_lib —— 因为平铺布局（py-modules）下 package-data
    不生效。2026-10-13 审查实测：该钩子只盖 wheel 直构路径，sdist 路径
    不经过它 → sdist 空壳传染 wheel（P0-1）。C 档重构改为 src 正规包 +
    include-package-data，钩子整体退役（数据目录物理在包内，单一事实来源）。

    ★ 但本文件保留两件事——**构建临时目录重定向**（实测踩出来的硬约束）：

    1. build_base 指向一次性临时目录：只要仓库里留着上次的 build/，下一次
       构建就要「删除 + 新建」覆盖文件。在带批量删除保护的环境里，一次
       覆盖 60+ 个文件会撞上批量删除闸门（阈值 50），pip 只报
       ``metadata-generation-failed``，完全不提真正原因（实测信息：
       ``SAFE_DELETE_BULK_CONFIRM_REQUIRED {"count":50,"threshold":50}``）。
    2. egg_base 同理改道：默认在源码根写 ``mcu_debug_mcp.egg-info/``，
       跑一次打包冒烟测试就冒出来一个残留目录（2026-09-22 实测）。
       ★ egg_base 目录必须先建出来，否则
         ``error in 'egg_base' option: ... does not exist or is not a directory``
       （pip 只回一句笼统的 metadata-generation-failed）。
"""

import os
import tempfile

from setuptools import setup

#: 构建产物落在一次性的临时目录里；仓库内不再产生 build/ 残留
BUILD_BASE = tempfile.mkdtemp(prefix="mcu-debug-mcp-build-")

#: egg_info 的落点也必须改道（理由见模块 docstring）
EGG_BASE = os.path.join(BUILD_BASE, "egg")
os.makedirs(EGG_BASE, exist_ok=True)

setup(options={"build": {"build_base": BUILD_BASE},
               "egg_info": {"egg_base": EGG_BASE}})
