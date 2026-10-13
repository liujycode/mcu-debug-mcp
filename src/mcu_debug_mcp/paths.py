# -*- coding: utf-8 -*-
"""mcu-debug-mcp · 数据根定位（paths.py）

为什么单独一个模块：
    原来 core.py 用 ``sys_dir = dirname(__file__)`` **同时**当代码目录与数据目录，
    于是 pip 安装后 ``knowledge/`` 与 ``drivers/`` 不在包里 —— 知识类工具全部返回空，
    而 ``logs/`` 又试图写进 site-packages。根因就是「代码与数据不分家」。
    本模块把这件事收成唯一口径。

定位优先级（高 → 低）：

    1. 环境变量 ``MCU_DEBUG_MCP_HOME``  —— 显式指定：多环境切换、测试隔离、CI
    2. ``~/.mcu-debug-mcp/``            —— 用户级：本地私有知识、社区补充包
    3. 模块所在目录                      —— 随包分发的内置数据（源码运行或 pip 安装）

层叠（overlay）语义 —— 这是关键，不是简单的"选一个根"：

    * 读**单个文件**：取「优先级最高且存在」的那份 —— 同名文件可被上层整份覆盖
    * 列**目录**：把各层的同名子目录**合并**，同名以高优先级为准
      —— 于是上层只放自己新增的那一份即可，内置条目不会凭空消失

只依赖标准库，不 import core / analyzer，避免循环依赖。
"""

import fnmatch
import os

#: 本模块所在目录（源码运行时 = src/mcu_debug_mcp；pip 安装后 = 包在 site-packages 的目录）
PKG_DIR = os.path.dirname(os.path.abspath(__file__))

#: 显式指定数据根的环境变量名
ENV_HOME = "MCU_DEBUG_MCP_HOME"

#: 用户级数据根目录名（位于 home 下）
USER_DIR_NAME = ".mcu-debug-mcp"

_write_root_cache = []


def pkg_dir():
    """内置数据所在目录（模块目录）。"""
    return PKG_DIR


def user_dir():
    """用户级数据根 ``~/.mcu-debug-mcp``；取不到 home 时返回 None。"""
    try:
        home = os.path.expanduser("~")
    except Exception:
        return None
    if not home or home == "~":
        return None
    return os.path.join(home, USER_DIR_NAME)


def data_roots():
    """数据根列表，优先级从高到低，只返回**真实存在**的目录。

    环境变量指向不存在的目录时会被忽略（不静默当成有效根，也不报错中断）——
    是否提示由 ``describe()`` 负责。
    """
    cands = []
    env = os.environ.get(ENV_HOME)
    if env:
        cands.append(os.path.abspath(env))
    ud = user_dir()
    if ud:
        cands.append(ud)
    cands.append(PKG_DIR)

    seen, out = set(), []
    for c in cands:
        key = os.path.normcase(os.path.abspath(c))
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(c):
            out.append(c)
    return out


def find(*parts):
    """按优先级查找单个文件/目录，返回绝对路径；全部不存在返回 None。"""
    if not parts:
        return None
    rel = os.path.join(*parts)
    for root in data_roots():
        p = os.path.join(root, rel)
        if os.path.exists(p):
            return p
    return None


def layers(*parts):
    """``parts`` 拼出的子目录在各数据层中的绝对路径（高优先级在前，只保留存在的）。

    例：``layers("knowledge", "pinmap")``
    """
    if not parts:
        return []
    rel = os.path.join(*parts)
    out = []
    for root in data_roots():
        p = os.path.join(root, rel)
        if os.path.isdir(p):
            out.append(p)
    return out


def layer_dirs(*parts):
    """层叠列举某子目录下的**子目录**：``[(name, abspath), ...]``。

    同名以高优先级为准（先扫到的胜出），结果按名称排序。
    """
    merged = {}
    for base in layers(*parts):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            p = os.path.join(base, name)
            if not os.path.isdir(p):
                continue
            merged.setdefault(name.lower(), (name, p))
    return sorted(merged.values(), key=lambda t: t[1].lower())


def layer_files(*parts, **kw):
    """层叠列举某子目录下的**文件**：``[(name, abspath), ...]``。

    ``pattern`` 默认 ``*.json``；同名以高优先级为准。
    """
    pattern = kw.pop("pattern", "*.json")
    if kw:
        raise TypeError("layer_files() 收到未知参数：%s" % ", ".join(sorted(kw)))
    merged = {}
    for base in layers(*parts):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            p = os.path.join(base, name)
            if os.path.isfile(p) and fnmatch.fnmatch(name, pattern):
                merged.setdefault(name.lower(), (name, p))
    return sorted(merged.values(), key=lambda t: t[1].lower())


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".mcu-debug-mcp.write-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        os.remove(probe)
        return True
    except OSError:
        return False


def is_source_tree():
    """``PKG_DIR`` 是不是源码仓库（而非 pip 装出来的 site-packages）。

    2026-10-13 C 档重构后包在 ``<root>/src/mcu_debug_mcp/``，判据改为
    往上两级找 ``pyproject.toml``（wheel 里不含它，源码仓库里一定有）。
    """
    return os.path.isfile(
        os.path.join(PKG_DIR, os.pardir, os.pardir, "pyproject.toml"))


def writable_root():
    """可写的**数据根**（会话日志、用户外置知识等落盘用）。

    顺序：``$MCU_DEBUG_MCP_HOME`` → ``~/.mcu-debug-mcp``
    →（仅源码树）包目录兜底。

    ★ 包目录【不是】数据目录，即使它可写也不放前面：
      实测确认 pip 装到 venv / --user 时 site-packages 是**可写**的，
      靠「能不能写」来选根，会话日志就会写进安装目录 —— 卸载残留、
      多个 venv 互相串、只读镜像里静默失败，全从这里来。
    ★ 源码运行时也不落仓库根：那会让项目根持续产生文件（logs/），
      而「代码目录与数据目录分家」正是这个模块存在的理由。
    找不到任何可写位置时返回包目录（让上层自己处理写失败，不假装成功）。
    """
    if _write_root_cache:
        return _write_root_cache[0]
    cands = []
    env = os.environ.get(ENV_HOME)
    if env:
        cands.append(os.path.abspath(env))
    ud = user_dir()
    if ud:
        cands.append(ud)
    # 包目录只作最后兜底（极端环境：home 不可写），且只认源码树
    if is_source_tree():
        cands.append(PKG_DIR)
    for c in cands:
        if _writable(c):
            _write_root_cache.append(c)
            return c
    _write_root_cache.append(PKG_DIR)
    return PKG_DIR


def logs_dir():
    """会话日志目录（不保证存在，由调用方按需创建）。"""
    return os.path.join(writable_root(), "logs")


def reset_cache():
    """清掉可写根缓存（测试用：切换 ``MCU_DEBUG_MCP_HOME`` 后重新探测）。"""
    _write_root_cache.clear()


def describe():
    """给 ``env_status`` 用的一段现状：数据根分层、来源、可写落点。"""
    env = os.environ.get(ENV_HOME)
    lines = []
    if env:
        state = "有效" if os.path.isdir(env) else "【已设置但目录不存在，已忽略】"
        lines.append("  - %s = %s  —— %s" % (ENV_HOME, env, state))
    else:
        lines.append("  - %s 未设置" % ENV_HOME)
    roots = data_roots()
    for i, r in enumerate(roots, 1):
        tag = "包内置" if os.path.normcase(r) == os.path.normcase(PKG_DIR) else "外置补充"
        lines.append("  - 第 %d 层（%s）：%s" % (i, tag, r))
    if not roots:
        lines.append("  - 【无任何数据根可用，知识类工具会全部返回空】")
    # ★ 标注的是**数据根的来源**，不是「本仓是不是源码树」。
    #   旧写法把 is_source_tree() 的结果贴在这行后面，于是源码运行时会出现
    #   「可写数据根：C:\Users\x\.mcu-debug-mcp（源码树）」——数据根在用户目录，
    #   却标成源码树，读的人会被带偏（这是上一轮 D2 修完留下的表述缺陷）。
    wr = writable_root()
    env_abs = os.path.abspath(env) if env else None
    ud = user_dir()
    if env_abs and os.path.normcase(wr) == os.path.normcase(env_abs):
        origin = "由 $%s 指定" % ENV_HOME
    elif ud and os.path.normcase(wr) == os.path.normcase(ud):
        origin = "用户目录 ~/.mcu-debug-mcp"
    else:
        origin = "包目录兜底（home 不可写，异常环境）"
    lines.append("  - 可写数据根（日志/外置知识落点）：%s（%s）" % (wr, origin))
    lines.append("  - 会话日志落点：%s" % logs_dir())
    return "\n".join(lines)
