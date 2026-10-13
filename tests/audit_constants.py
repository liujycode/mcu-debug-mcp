#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""常量表一致性审计（B④）。

背景：`serial_list` 曾经出过一个典型 bug——同一个 USB VID:PID，
在 `core._USB_UART_BRIDGE_IDS` 里被当成"已知 UART 桥"，却在
`analyzer.USB_SERIAL_VIDPID` 里查不到，于是同一个口一处报"FT2232H"、
另一处报"未收录"。根因是**同一事实写了两处、且没有同步机制**。

本项目里还有若干"同一事实两处定义"的结构：

  | 事实             | 定义处 A（内置 fallback）        | 定义处 B（外置知识库）                    |
  |------------------|----------------------------------|-------------------------------------------|
  | USB VID:PID 归类 | core._USB_CDC_IDS / _UART_BRIDGE | analyzer.USB_SERIAL_VIDPID（芯片名+驱动） |
  | Modbus 异常码    | core._MODBUS_EXC_FALLBACK        | knowledge/error_codes/modbus_exception.json |
  | ESP8266 CWJAP 码 | analyzer._CWJAP_FALLBACK         | knowledge/error_codes/cwjap.json          |
  | +CME ERROR 码    | analyzer._CME_CODES 的 fallback  | knowledge/error_codes/cme.json            |
  | HardFault 位     | analyzer._HARDFAULT_BITS         | knowledge/fault_bits/cortex_m.json        |

其中 fallback 的设计意图是"知识库文件缺失时保底"。但**保底表一旦和
知识库脱节，就会造成同一个码两处给不同解释**——而且是静默的：
文件在时用 B，文件丢了用 A，用户永远看不到差异。

本脚本把这些配对做成可机器校验的断言。跑法：
    python tests/audit_constants.py          # 打印报告，有 ISSUE 时 rc=1
    python -m pytest tests/test_constants_audit.py -q   # 纳入回归
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import analyzer  # noqa: E402
import core  # noqa: E402

KNOWLEDGE = os.path.join(ROOT, "knowledge")


def _load_error_codes(fname):
    with open(os.path.join(KNOWLEDGE, "error_codes", fname), "r", encoding="utf-8") as f:
        return (json.load(f).get("entries") or {})


def _load_fault_bits():
    with open(os.path.join(KNOWLEDGE, "fault_bits", "cortex_m.json"), "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 1) USB VID:PID 三处定义（core CDC / core 桥 / analyzer 芯片名表）
# --------------------------------------------------------------------------
def check_usb_vidpid():
    issues = []
    known = set(analyzer.USB_SERIAL_VIDPID.keys())
    for name, tbl in (("_USB_CDC_IDS", core._USB_CDC_IDS),
                      ("_USB_UART_BRIDGE_IDS", core._USB_UART_BRIDGE_IDS)):
        for (vid, pid) in tbl:
            key = "%04X:%04X" % (vid, pid)
            if key not in known:
                issues.append(
                    "%s 里的 %s 在 analyzer.USB_SERIAL_VIDPID 中缺失 —— "
                    "同一个口会被一处识别、另一处说'不认识'" % (name, key))
    # 反向：analyzer 收录但 core 两边都不归类（只算提示，不算错误——
    # analyzer 表可以比 core 归类更宽）
    cdck = {"%04X:%04X" % (v, p) for (v, p) in core._USB_CDC_IDS}
    brk = {"%04X:%04X" % (v, p) for (v, p) in core._USB_UART_BRIDGE_IDS}
    for key in known:
        if key in cdck and key in brk:
            issues.append("%s 同时被 CDC 表和 UART 桥表收录 —— 归类有歧义" % key)
    return issues


# --------------------------------------------------------------------------
# 2) 错误码表：内置 fallback vs knowledge/*.json
# --------------------------------------------------------------------------
def _cmp_code_tables(label, fallback, json_entries):
    """fallback: {code: meaning 或 (meaning, hint)}；json_entries: {code: {meaning, hint?}}"""
    issues = []
    fb_keys = set(map(str, fallback.keys()))
    js_keys = set(map(str, json_entries.keys()))
    only_fb = sorted(fb_keys - js_keys)
    only_js = sorted(js_keys - fb_keys)
    if only_fb:
        issues.append("%s：内置 fallback 有、知识库没有的码 %s —— "
                      "知识库文件在时用户看不到这些码" % (label, only_fb))
    if only_js:
        issues.append("%s：知识库有、内置 fallback 没有的码 %s —— "
                      "知识库一旦缺失，这些码就查不到了" % (label, only_js))
    for k in sorted(fb_keys & js_keys):
        fb = fallback[k]
        fb_meaning = fb[0] if isinstance(fb, (tuple, list)) else fb
        js_meaning = (json_entries[k] or {}).get("meaning", "")
        if str(fb_meaning).strip() != str(js_meaning).strip():
            issues.append("%s 码 %s：fallback 释义 %r ≠ 知识库释义 %r"
                          % (label, k, fb_meaning, js_meaning))
    return issues


def check_error_codes():
    issues = []
    issues += _cmp_code_tables("Modbus 异常码", core._MODBUS_EXC_FALLBACK,
                               _load_error_codes("modbus_exception.json"))
    issues += _cmp_code_tables("+CWJAP", analyzer._CWJAP_FALLBACK,
                               _load_error_codes("cwjap.json"))
    issues += _cmp_code_tables("+CME ERROR", analyzer._CME_FALLBACK,
                               _load_error_codes("cme.json"))
    return issues


# --------------------------------------------------------------------------
# 3) HardFault 位：analyzer._HARDFAULT_BITS vs fault_bits/cortex_m.json
# --------------------------------------------------------------------------
def check_fault_bits():
    issues = []
    data = _load_fault_bits()
    regs = data.get("registers") or {}

    def collect_bits(node, acc):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "bits" and isinstance(v, dict):
                    acc.update(v.keys())
                else:
                    collect_bits(v, acc)
        elif isinstance(node, list):
            for it in node:
                collect_bits(it, acc)

    json_bits = set()
    collect_bits(regs, json_bits)

    # 直接读运行时对象，不解析源码文本（源码格式一变就失效，太脆）
    an_bits = set()
    for pat, _desc in analyzer._HARDFAULT_BITS:
        m = re.search(r"\(\?i\)\s*([A-Z0-9_]+)", pat)
        if m:
            an_bits.add(m.group(1))

    if not an_bits:
        issues.append("analyzer._HARDFAULT_BITS 里解析不出位名（数据结构改了？）——审计脚本需同步")
        return issues
    if not json_bits:
        issues.append("fault_bits/cortex_m.json 里没解析到 bits（结构改了？）——审计脚本需同步")
        return issues

    only_an = sorted(an_bits - json_bits)
    if only_an:
        issues.append("analyzer._HARDFAULT_BITS 有、cortex_m.json 没有的位 %s —— "
                      "日志规则与 SVD 知识库脱节" % only_an)
    return issues


# --------------------------------------------------------------------------
# 4) analyze_log 的规则总数 vs fault_patterns.json 外置规则数
# --------------------------------------------------------------------------
def check_rule_counts():
    issues = []
    with open(os.path.join(KNOWLEDGE, "fault_patterns.json"), "r", encoding="utf-8") as f:
        ext = len(json.load(f).get("rules") or [])
    n_extra = len(getattr(analyzer, "_EXTRA_RULES", []))
    if ext != n_extra:
        issues.append("fault_patterns.json 有 %d 条外置规则，但 analyzer 只加载了 %d 条 —— "
                      "有规则没被读进来（静默失效）" % (ext, n_extra))
    return issues


# --------------------------------------------------------------------------
# 5) 写死在文案里的「规则条数」 vs 实测条数
#
# 与 1-4 同型，但脱节方式更阴：文案里的数字不属于任何数据结构，
# 加了一条规则，代码全对、测试全绿，只有用户看到的说明还写着旧数字
# （2026-09-22 实测命中：规则已 45 条，server.py 的 instructions 与
#  core.py 的 docstring 仍写 44，pyproject 描述也还是 44）。
# 一致性审计不覆盖它，就永远只能靠人偶然看见。
#
# 只扫「现状声明」类文件；CHANGELOG / 测试里的历史叙述（「当时 44 条」）不算脱节。
# --------------------------------------------------------------------------
_RULE_COUNT_SOURCES = (
    "core.py", "analyzer.py", "server.py", "paths.py",
    "README.md", "pyproject.toml",
    "docs/MEMORY_DEBUG.md", "docs/KNOWLEDGE_SCHEMA.md",
    "examples/offline_demo.py", "examples/README.md",
    "tools/live_acceptance.py",
)

#: 这些写法都是在描述「现在一共多少条」，必须与实测一致。
#: 注意 `(?!族)`：「N 条规则族」是子分类计数（烧录失败诊断有几种族），不是总数——
#: 一刀切会把它误报成脱节（2026-09-22 实测踩到，README 的「8 条规则族」）。
_RULE_COUNT_PATTERNS = (
    r"模式库共\s*(\d+)\s*条",
    r"(\d+)\s*条(?:异常模式库|模式库|规则)(?!族)",
    r"覆盖的模式（\s*(\d+)\s*条",
    r"(\d+)\s*条故障模式",
)


def check_hardcoded_rule_counts():
    issues = []
    want = analyzer.rule_count()
    for rel in _RULE_COUNT_SOURCES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for pat in _RULE_COUNT_PATTERNS:
            for m in re.finditer(pat, src):
                got = int(m.group(1))
                if got != want:
                    line = src[:m.start()].count("\n") + 1
                    issues.append("%s:%d 写「%s」，实测规则 %d 条 —— "
                                  "加规则时漏改这处文案" % (rel, line, m.group(0), want))
    return issues


CHECKS = [
    ("USB VID:PID 三处定义", check_usb_vidpid),
    ("错误码表 fallback↔知识库", check_error_codes),
    ("HardFault 位 规则↔知识库", check_fault_bits),
    ("外置规则条数", check_rule_counts),
    ("文案里的规则条数", check_hardcoded_rule_counts),
]


def run():
    all_issues = []
    for name, fn in CHECKS:
        try:
            iss = fn()
        except Exception as e:  # 审计器自身出错也要暴露，不能静默算通过
            iss = ["审计器异常（%s）：%s" % (name, e)]
        tag = "OK  " if not iss else "ISSUE"
        print("[%s] %s" % (tag, name))
        for s in iss:
            print("        - " + s)
        all_issues += iss
    print()
    print("常量表一致性审计：%d 项错误" % len(all_issues))
    return 1 if all_issues else 0


if __name__ == "__main__":
    sys.exit(run())
