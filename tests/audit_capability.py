# -*- coding: utf-8 -*-
"""能力面审计（N-05）：coverage.json × 实现 × 文档，三方对账。

为什么要有这个脚本
------------------
本项目此前「一次次补」的结构性根因是：待办散落在文档各处，缺口靠人偶然发现 ⟹ 必然遗漏。
本脚本把三件事机器化：

    1. **广度**：coverage.json 声明的每条能力轴，实测现状与 target 的差距
    2. **深度**：每个已注册工具在 L0–L6 阶梯上的位置（L0 有工具 ≠ 做完了）
    3. **脱节**：文档写的数字、清单记的目标、代码实现的事实，三者对不上就报错

两类结果分开对待（这是关键，否则脚本会变成"永远红的墙"）：

    * **缺口（gap）** —— 还没做到。属计划内，列出来，**不影响退出码**。
    * **脱节（mismatch）** —— 说的和做的不一致（文档写 33 个工具、实现是 34）、
      或出现死代码。属错误，**退出码 1**。

用法：
    python tests/audit_capability.py           # 人读报告
    python tests/audit_capability.py --json    # 机器可读（CI 可解析）
    python tests/audit_capability.py --quiet   # 只报脱节与死代码（CI 用）

深度阶梯的判定口径（**启发式**，目的在「自动浮出缺口」，不追求百分之百精确）：
    L0 已注册       server.py 里有 @mcp.tool() 且被注册
    L1 离线可复现   tests/ 里有该工具的调用（不是 happy-path 与否另论）
    L2 真机可验     coverage.json 的 liveVerified 里显式声明过（板子在手才有）
    L3 判别力双向   其测试函数体内出现「拒绝类」断言（没有/无效/需要/不能/为空…）
    L4 边界不崩     其测试函数体内出现非法入参（""/None/0/-1/[]/{}）
    L6 链路连通     其测试函数体内同时出现 ≥2 个不同工具（跨工具联测）
    （L5 知识溯源是知识层属性，按条目级 source 统计，见 validate_knowledge.py）
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core       # noqa: E402
import analyzer   # noqa: E402
import paths      # noqa: E402

COVERAGE_PATH = os.path.join(ROOT, "coverage.json")

REFUSAL_PAT = re.compile(
    r'assertIn\(\s*["\'](?:没有|无效|需要|不能|请先|未知|未收录|为空|查不到|只支持|不支持)'
    r'|assertNotIn\(|assertRaises|"没有收录"|"错误码知识库为空"'
)
BOUNDARY_PAT = re.compile(r'\(\s*(?:""|None|0\s*[,)]|-1|\[\]|\{\}|"")')
TEST_FUNC_PAT = re.compile(r"^\s*def (test_\w+)\(", re.M)
CLASS_PAT = re.compile(r"^class (\w+)", re.M)


# ---------------------------------------------------------------
# 采集
# ---------------------------------------------------------------

def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _test_sources():
    out = {}
    tdir = os.path.join(ROOT, "tests")
    for fn in sorted(os.listdir(tdir)):
        if fn.endswith(".py"):
            out["tests/" + fn] = _read(os.path.join("tests", fn))
    return out


def _enclosing_class(src, pos):
    """pos 之前最近的一个 ``class X``（取不到返回空串）。"""
    cls = ""
    for m in CLASS_PAT.finditer(src):
        if m.start() < pos:
            cls = m.group(1)
        else:
            break
    return cls


def _split_test_funcs(src):
    """把测试文件切成 {唯一名: 函数体}（够用的近似切分，不追求 AST 精确）。

    ★ 键必须**唯一**，不能只用函数名 —— 原来的实现就是只用函数名，
      同名测试会互相覆盖。真机验收那批新工具正好撞上：
      ``FlashVerifyTest.test_refuses_bad_inputs``（含 L3 要的拒绝断言
      ``assertIn("只支持", ...)``）与 ``TimingAnalysisTest.test_refuses_bad_inputs``
      （不含）同名，后者把前者覆盖掉 ⟹ **flash_verify 的 L3 被算成 False**。

      这是质量门自己的**假阴性**，比假阳性危险得多：明明有判别力断言，审计却说没有，
      于是"缺口还没补"的错觉会一直挂着，人还会去补一遍已经有的东西。
      ⟹ 键改成 ``"类名::函数名"``，不同类的同名测试各归各家。
    """
    funcs = {}
    marks = [(m.start(), m.group(1)) for m in TEST_FUNC_PAT.finditer(src)]
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
        cls = _enclosing_class(src, pos)
        funcs[("%s::%s" % (cls, name)) if cls else name] = src[pos:end]
    return funcs


def server_decorators():
    src = _read("server.py")
    return {
        "tool": len(re.findall(r"@mcp\.tool\(\)", src)),
        "resource": len(re.findall(r"@mcp\.resource\(", src)),
        "prompt": len(re.findall(r"@mcp\.prompt", src)),
    }


def registered_tools():
    """server.py 里 @mcp.tool() 注册的工具名（顺序即源码顺序）。"""
    src = _read("server.py")
    return re.findall(r"@mcp\.tool\(\)\s*\ndef (\w+)\(", src)


# ---------------------------------------------------------------
# probe 求值
# ---------------------------------------------------------------

def _layer_count(subdir, kind):
    if kind == "layer_dirs":
        return len(paths.layer_dirs(*subdir))
    return len(paths.layer_files(*subdir))


def _errcode_entries():
    total = 0
    for _name, p in paths.layer_files("knowledge", "error_codes"):
        try:
            with open(p, encoding="utf-8") as fh:
                total += len(json.load(fh).get("entries") or {})
        except Exception:
            pass
    return total


def _rule_count():
    """生效规则数：优先用 analyzer 自己的口径（内置 RULES + 外置 _EXTRA_RULES）。

    ★ 不自己数一遍：一旦这里另写一套，就成了「同一事实两处定义」——
      正是 audit_constants.py 在防的那类脱节。
    """
    fn = getattr(analyzer, "rule_count", None)
    if callable(fn):
        return fn()
    return (len(getattr(analyzer, "RULES", []) or [])
            + len(getattr(analyzer, "_EXTRA_RULES", []) or []))


def _guard_op_count():
    """登记在隔离层 guard.OPS 里、且风险说明字段齐全的操作数。

    ★数的是「闸门化了的操作」，不是代码行数：N-11 的判据是
      「每一个会动硬件的操作都有统一防护」——登记了 + 全字段齐全才算数。
      新写的写操作若忘了登记，这个数不涨，N-11 就不会被误标成已完成。
    """
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import guard
    need = ("title", "needs", "irreversible", "why", "how")
    return sum(1 for spec in guard.OPS.values() if all(k in spec for k in need))


_PYTEST_COLLECTED = None


def _pytest_collected():
    """真跑 `pytest --collect-only` 数收集项（含 parametrize 展开后的每一项）。

    ★ 为什么不用 AST 静态数：parametrize 一个函数展开成多项，静态数必然对不上；
      为什么不做全量执行：审计跑全量 pytest 会递归触发「测审计器的测试」，
      且 CI 里 pytest 已有独立门禁，这里只对「文档声明的总数」负责。
    ★ 2026-10-13 新增的教训：README 手写「699 passed」而实测 704——手写数字
      必然过期，从此文档里的测试总数由本探针机器锁定。
    """
    global _PYTEST_COLLECTED
    if _PYTEST_COLLECTED is None:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q",
                 "--no-header", "-p", "no:cacheprovider"],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600)
            m = re.search(r"(\d+)\s+tests?\s+collected", r.stdout or "")
            _PYTEST_COLLECTED = int(m.group(1)) if m else -1
        except Exception:
            _PYTEST_COLLECTED = -1
    return _PYTEST_COLLECTED


def eval_probe(probe, ctx):
    kind = probe.get("kind")
    if kind == "core_len":
        fn = getattr(core, probe["call"], None)
        return len(fn()) if callable(fn) else 0
    if kind == "core_has":
        return sum(1 for n in probe["names"] if hasattr(core, n))
    if kind in ("layer_dirs", "layer_files"):
        return _layer_count(probe["subdir"], kind)
    if kind == "errcode_entries":
        return _errcode_entries()
    if kind == "rules":
        return _rule_count()
    if kind == "attr_len":
        mod = core if probe.get("module") == "core" else analyzer
        v = getattr(mod, probe["attr"], None)
        return len(v) if v is not None else 0
    if kind == "decorators":
        return server_decorators()[probe["what"]]
    if kind == "file_exists":
        return sum(1 for p in probe["paths"] if os.path.exists(os.path.join(ROOT, p)))
    if kind == "axes_met":
        ids = probe["ids"]
        met = 0
        for ax in ctx["coverage"]["axes"]:
            if ax["id"] in ids and ctx["axis_now"][ax["id"]] >= ax["target"]:
                met += 1
        return met
    if kind == "guard_ops":
        return _guard_op_count()
    if kind == "tools_with_assertions":
        return ctx["depth"]["with_assertions"]
    if kind == "pytest_collected":
        return _pytest_collected()
    if kind == "manual":
        return probe.get("now", 0)
    return 0


def probe_target(probe, default=None, ctx=None):
    """probe 的达标线。file_exists 用声明路径数，axes_met 用轴数，
    tools_with_assertions 用工具总数（动态——工具数会变，写死就会过期）。"""
    if probe.get("kind") == "tools_with_assertions":
        return ctx["depth"]["total"] if ctx else 0
    if default is not None:
        return default
    if probe.get("kind") == "file_exists":
        return len(probe["paths"])
    if probe.get("kind") == "axes_met":
        return len(probe["ids"])
    return 1


# ---------------------------------------------------------------
# 深度矩阵
# ---------------------------------------------------------------

def depth_matrix(tools, tests, live_verified):
    rows = []
    for t in tools:
        hit_funcs = []
        for fname, src in tests.items():
            for fname2, body in _split_test_funcs(src).items():
                if re.search(r"\b%s\s*\(" % re.escape(t), body):
                    hit_funcs.append((fname, fname2, body))
        others = set(re.findall(r"\bcore\.(\w+)\s*\(", "\n".join(b for _f, _n, b in hit_funcs)))
        rows.append({
            "tool": t,
            "L0": True,
            "L1": bool(hit_funcs),
            "L2": t in live_verified,
            "L3": any(REFUSAL_PAT.search(b) for _f, _n, b in hit_funcs),
            "L4": any(BOUNDARY_PAT.search(b) for _f, _n, b in hit_funcs),
            "L6": len(others - {t}) >= 1,
        })
    return rows


# ---------------------------------------------------------------
# 死代码
# ---------------------------------------------------------------

def _top_level_names(src):
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = node.lineno
    return out


def dead_code():
    """顶层函数/常量：全仓（源码 + 测试 + 文档）零引用即视为死代码候选。"""
    search_blobs = []
    for rel in ("core.py", "analyzer.py", "paths.py", "server.py"):
        search_blobs.append(_read(rel))
    tdir = os.path.join(ROOT, "tests")
    for fn in sorted(os.listdir(tdir)):
        if fn.endswith(".py"):
            search_blobs.append(_read(os.path.join("tests", fn)))
    blob = "\n".join(search_blobs)

    suspects = []
    for rel in ("core.py", "analyzer.py", "paths.py"):
        src = _read(rel)
        for name, lineno in _top_level_names(src).items():
            if name.startswith("__"):
                continue
            if name in ("main",):
                continue
            if len(re.findall(r"\b%s\b" % re.escape(name), blob)) <= 1:
                suspects.append({"file": rel, "line": lineno, "name": name})
    return sorted(suspects, key=lambda d: (d["file"], d["line"]))


# ---------------------------------------------------------------
# 隔离与安全：静态对账
#
# 为什么不只靠行为测试：行为测试只能证明「已登记的那些操作拦得住」。
# 隔离层最真实的失效方式是**新增的写操作忘了接闸门** —— 那一刻所有测试都还是绿的，
# 而工具会在用户机器上直接动手。所以这里对着源码做对账。
# ---------------------------------------------------------------

#: 会真正操作硬件的工具 → 它必须接上的闸门调用
GUARD_WIRING = {
    "serial_open":          'guard.check("serial_open"',
    "serial_send":          'guard.check("serial_write"',
    "at_sequence":          'guard.check("at_sequence"',
    "baudrate_autodetect":  'guard.check("baud_probe"',
    "serial_pair_record":   'guard.check("pair_record"',
    "flash_firmware":       'guard.check("flash_firmware"',
}

#: 真正构造串口对象的地方，只允许出现在这个函数里（唯一入口）
SERIAL_CTOR_OWNER = "_open_serial"


def _func_of_line(lines, lineno):
    """向上找最近的顶层 def，返回函数名（找不到返回空串）。"""
    for i in range(lineno - 1, -1, -1):
        m = re.match(r"^def\s+(\w+)", lines[i])
        if m:
            return m.group(1)
    return ""


def isolation_audit():
    """隔离层静态对账：返回 (checks, failures)。

    对账五件事：
      ① 真实串口构造只有一个入口（否则连 DTR 抑制都会被绕过）
      ② guard.OPS ↔ core 调用点 双向一一对应（登记了没接 / 接了没登记，都报）
      ③ 不可逆操作必须真的把 confirm 传下去
      ④ 测试侧隔离基础设施在位（conftest 锁档位 + 禁端口 + 替身；isolation.py 唯一放行入口）
      ⑤ 默认档位是最保守的 readonly
    """
    checks, failures = [], []

    def rec(ok, name, why_failed=""):
        checks.append({"ok": ok, "name": name, "detail": why_failed if not ok else ""})
        if not ok:
            failures.append({"label": "隔离失效", "detail": "%s —— %s" % (name, why_failed)})

    core_src = _read("core.py")
    lines = core_src.splitlines()

    # ① 串口构造唯一入口
    ctors = [(i + 1, l) for i, l in enumerate(lines) if "serial.Serial(" in l]
    stray = [(n, l.strip()) for n, l in ctors if _func_of_line(lines, n) != SERIAL_CTOR_OWNER]
    rec(not stray, "串口构造只有 %s 一个入口" % SERIAL_CTOR_OWNER,
        "以下位置绕过了统一入口（DTR 抑制与隔离都会失效）：%s"
        % "; ".join("第 %d 行 %s" % (n, t) for n, t in stray))

    # ② guard.OPS ↔ core 调用点（双向）
    ops, default_mode = [], ""
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import guard as _guard
        ops = sorted(_guard.OPS)
        default_mode = _guard.DEFAULT_MODE
    except Exception as e:
        rec(False, "导入 guard 模块", "无法导入 guard.py：%s" % e)

    if ops:
        used = sorted({m.group(1) for m in re.finditer(r'guard\.check\(\s*"([a-z_]+)"', core_src)})
        miss = [o for o in ops if o not in used]
        rec(not miss, "guard.OPS 登记的每个操作都在 core 里接了闸门",
            "登记了但 core 里找不到调用点：%s（登记了不埋点 = 以为有隔离，其实没有）"
            % ", ".join(miss))
        extra = [o for o in used if o not in ops]
        rec(not extra, "core 里每个闸门调用都在 guard.OPS 里有登记",
            "core 里调用了未登记的操作：%s（未登记的操作 guard 会保守拒绝，功能是坏的）"
            % ", ".join(extra))

    # ③ 不可逆操作必须传 confirm
    # ★ 口径要覆盖 guard.OPS 里**所有** irreversible 操作，不能只认 flash_ 前缀 ——
    #   早期这里写死 `"(flash_[a-z_]+)"`，于是新加的不可逆操作（logs_prune /
    #   reset_flags_clear）全都不在对账范围内，「二次确认形同虚设」这类漏接
    #   就永远不会被报出来。宁可让新操作因为这条检查而必须显式接上。
    irrev = []
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import guard as _g2
        irrev = sorted(k for k, v in _g2.OPS.items() if v.get("irreversible"))
    except Exception as e:                                # noqa: BLE001
        rec(False, "读取 guard.OPS 里的不可逆操作", "导入 guard.py 失败：%s" % e)
    if irrev:
        for op in irrev:
            m = re.search(r'guard\.check\(\s*"%s"[^)]*\)' % re.escape(op), core_src)
            call = " ".join(m.group(0).split()) if m else ""
            rec(bool(m) and "confirm=" in call,
                "不可逆操作 %s 把 confirm 传给了闸门" % op,
                ("core 里找不到 guard.check(\"%s\") 的调用点" % op) if not m else
                ("调用是 `%s` —— 没传 confirm，二次确认形同虚设" % call))

    # ④ 测试侧隔离基础设施
    conf_p = os.path.join(ROOT, "tests", "conftest.py")
    if os.path.exists(conf_p):
        conf = open(conf_p, encoding="utf-8").read()
        for token, name in (("MCU_DEBUG_MCP_PORT_DENY", "conftest 禁掉全部端口"),
                            ("serial.Serial =", "conftest 替换真实串口构造"),
                            ("MCU_DEBUG_MCP_MODE", "conftest 锁死隔离档位")):
            rec(token in conf, name, "tests/conftest.py 里找不到 %s" % token)
    else:
        rec(False, "tests/conftest.py 存在", "缺少测试隔离基础设施：测试可能接通真实设备")
    iso_p = os.path.join(ROOT, "tests", "isolation.py")
    rec(os.path.exists(iso_p), "tests/isolation.py 存在（临时放行的唯一入口）",
        "缺少该文件：各测试会自己写一套临时放行逻辑，必然出现泄漏")

    # ⑤ 默认档位
    if default_mode:
        rec(default_mode == "readonly", "默认档位是最保守的 readonly",
            "默认档位是 %r —— 默认就能写硬件，等于没有隔离" % default_mode)

    return checks, failures


# ---------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------

def run_audit():
    with open(COVERAGE_PATH, encoding="utf-8") as fh:
        coverage = json.load(fh)
    tests = _test_sources()
    dec = server_decorators()
    tools = registered_tools()

    ctx = {"coverage": coverage, "axis_now": {}, "depth": {}}

    axes = []
    for ax in coverage["axes"]:
        now = eval_probe(ax["probe"], ctx)
        target = probe_target(ax["probe"], ax.get("target"), ctx)
        ctx["axis_now"][ax["id"]] = now
        axes.append({**ax, "now": now, "target": target, "met": now >= target})

    rows = depth_matrix(tools, tests, coverage.get("liveVerified") or [])
    ctx["depth"] = {
        "rows": rows,
        "total": len(rows),
        "with_assertions": sum(1 for r in rows if r["L1"]),
    }

    gaps = []
    for g in coverage["gaps"]:
        now = eval_probe(g["probe"], ctx)
        target = probe_target(g["probe"], g.get("target"), ctx)
        gaps.append({**g, "now": now, "target": target, "done": now >= target})

    # 文档对账用的 metric 表
    objs = {a["id"]: a["now"] for a in axes}
    metrics = dict(objs)
    # 测试总数不占能力轴（它不是"能力"，是回归面规模），但要对账：
    # 直接注入 metric，docNumbers 里用 pytest_collected 引用。
    metrics["pytest_collected"] = _pytest_collected()

    mismatches = []
    for dn in coverage.get("docNumbers", []):
        p = os.path.join(ROOT, dn["file"])
        if not os.path.exists(p):
            mismatches.append({"label": dn["label"], "detail": "文件不存在：%s" % dn["file"]})
            continue
        src = open(p, encoding="utf-8").read()
        group = dn.get("group", 1)
        # ★ 2026-10-13：re.search 只取**首个**匹配 —— README 前半已更新、
        #   后半还留着旧数字时，首匹配命中新值，旧值全部落在盲区
        #   （实测：README:50 写 55 而 :202/:292 还是 35，审计报 OK）。
        #   改 finditer 全量：每一处声明都必须对上。
        hits = list(re.finditer(dn["regex"], src))
        if not hits:
            mismatches.append({"label": dn["label"],
                               "detail": "文档里没找到该声明（正则未命中）：%s" % dn["regex"]})
            continue
        want = metrics.get(dn["metric"])
        if want is None:
            mismatches.append({"label": dn["label"], "detail": "清单里没有 metric %s" % dn["metric"]})
            continue
        for m in hits:
            got = int(m.group(group))
            if got != want:
                line = src[:m.start()].count("\n") + 1
                mismatches.append({
                    "label": dn["label"],
                    "detail": "%s:%d 写 %d，实测 %d" % (dn["file"], line, got, want),
                })

    dead = dead_code()
    for d in dead:
        mismatches.append({
            "label": "死代码",
            "detail": "%s:%d 顶层 `%s` 全仓零引用（实例化但从不调用那类）"
                      % (d["file"], d["line"], d["name"]),
        })

    iso_checks, iso_failures = isolation_audit()
    mismatches.extend(iso_failures)

    return {
        "axes": axes,
        "gaps": gaps,
        "depth": ctx["depth"],
        "decorators": dec,
        "tools": tools,
        "isolation": {"checks": iso_checks, "failures": iso_failures},
        "mismatches": mismatches,
        "metrics": metrics,
        "summary": {
            "axes_total": len(axes),
            "axes_met": sum(1 for a in axes if a["met"]),
            "gaps_total": len(gaps),
            "gaps_done": sum(1 for g in gaps if g["done"]),
            "tools": len(tools),
            "tools_with_assertions": ctx["depth"]["with_assertions"],
            "isolation_checks": len(iso_checks),
            "isolation_failures": len(iso_failures),
            "mismatches": len(mismatches),
        },
    }


# ---------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------

LADDER = ["L0", "L1", "L2", "L3", "L4", "L6"]


def render(rep, verbose=True):
    L = []
    s = rep["summary"]
    L.append("mcu-debug-mcp · 能力面审计（coverage.json × 实现 × 文档）")
    L.append("=" * 78)

    L.append("")
    L.append("【一】广度轴：%d/%d 达标" % (s["axes_met"], s["axes_total"]))
    L.append("  %-30s %6s %6s %6s  %s" % ("能力轴", "现状", "目标", "差距", "计划"))
    for a in rep["axes"]:
        if a["met"] and not verbose:
            continue
        mark = "✓" if a["met"] else "·"
        L.append("  %s %-28s %6d %6d %6d  %s"
                 % (mark, a["name"], a["now"], a["target"], a["now"] - a["target"], a.get("plan", "")))
    not_met = [a for a in rep["axes"] if not a["met"]]
    if not_met:
        L.append("  —— 未达标 %d 项（计划内缺口，不阻塞）" % len(not_met))
        for a in not_met:
            note = a.get("note") or (a["probe"].get("reason") if a["probe"].get("kind") == "manual" else "")
            if note:
                L.append("     · %s：%s" % (a["name"], note))

    L.append("")
    L.append("【二】MCP 三原语：tools %d · resources %d · prompts %d"
             % (rep["decorators"]["tool"], rep["decorators"]["resource"], rep["decorators"]["prompt"]))

    L.append("")
    L.append("【三】深度阶梯（L0 有工具 / L1 离线可复现 / L2 真机 / L3 判别力双向 / L4 边界不崩 / L6 链路）")
    L.append("  %-24s %s" % ("工具", "  ".join(LADDER)))
    missing = []
    for r in rep["depth"]["rows"]:
        L.append("  %-24s %s" % (r["tool"],
                                 "   ".join("✓" if r[k] else "·" for k in LADDER)))
        if not r["L1"]:
            missing.append(r["tool"])
    L.append("  —— 有专项断言：%d/%d" % (rep["depth"]["with_assertions"], rep["depth"]["total"]))
    if missing:
        L.append("  —— 缺 L1 专项断言（对应 N-14）：%s" % ", ".join(missing))

    L.append("")
    L.append("【四】缺口清单：%d/%d 已完成" % (s["gaps_done"], s["gaps_total"]))
    for b in sorted({g["batch"] for g in rep["gaps"]}, key=lambda x: str(x)):
        items = [g for g in rep["gaps"] if g["batch"] == b]
        done = sum(1 for g in items if g["done"])
        L.append("  批次 %s：%d/%d" % (b, done, len(items)))
        for g in items:
            L.append("    [%s] %-6s %s" % ("✓" if g["done"] else " ", g["id"], g["title"]))

    L.append("")
    L.append("【五】脱节检查（文档 × 清单 × 实现）")
    if not rep["mismatches"]:
        L.append("  ✓ 无脱节：文档数字与实测一致，无死代码")
    else:
        for m in rep["mismatches"]:
            L.append("  ✗ %s：%s" % (m["label"], m["detail"]))

    L.append("")
    iso = rep.get("isolation") or {"checks": [], "failures": []}
    L.append("【六】隔离与安全：%d 项检查，%d 项失败"
             % (len(iso["checks"]), len(iso["failures"])))
    for c in iso["checks"]:
        L.append("  %s %s%s" % ("✓" if c["ok"] else "✗", c["name"],
                                ("  —— " + c["detail"]) if not c["ok"] else ""))

    L.append("")
    L.append("=" * 78)
    L.append("结论：缺口 %d 项待做（计划内，不影响退出码）；脱节/隔离失效 %d 项（错误，退出码 1）"
             % (s["gaps_total"] - s["gaps_done"], s["mismatches"]))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="能力面审计：coverage.json × 实现 × 文档")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出脱节与死代码（CI 用）")
    args = ap.parse_args(argv)

    rep = run_audit()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif args.quiet:
        if rep["mismatches"]:
            print(render(rep))
        else:
            print("✓ 无脱节（缺口 %d 项待做，见 coverage.json）"
                  % (rep["summary"]["gaps_total"] - rep["summary"]["gaps_done"]))
    else:
        print(render(rep))
    return 1 if rep["mismatches"] else 0


if __name__ == "__main__":
    sys.exit(main())
