#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B④ 常量表一致性审计 —— 纳入回归。

把 tests/audit_constants.py 的四项检查变成 pytest 断言，并额外证明
审计器**不是自证式**的（人为制造脱节必须被抓出来）。
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from mcu_debug_mcp import analyzer  # noqa: E402
import audit_constants as ac  # noqa: E402
from mcu_debug_mcp import core  # noqa: E402


class TestConstantsAudit(unittest.TestCase):

    def test_usb_vidpid_three_places_in_sync(self):
        self.assertEqual([], ac.check_usb_vidpid())

    def test_error_code_fallbacks_match_knowledge(self):
        self.assertEqual([], ac.check_error_codes())

    def test_fault_bits_match_knowledge(self):
        self.assertEqual([], ac.check_fault_bits())

    def test_external_rule_count_loaded(self):
        self.assertEqual([], ac.check_rule_counts())

    def test_hardcoded_rule_counts_in_docs_are_current(self):
        """文案里写死的「N 条规则/模式库」必须等于实测条数。

        这类脱节代码全对、测试全绿，只有用户看到的说明是旧数字——
        2026-09-22 实测命中 server.py instructions 与 core.py docstring 仍写 44。
        """
        self.assertEqual([], ac.check_hardcoded_rule_counts())

    # ---- 反自证：制造脱节，审计必须报出来 ----
    def test_injected_usb_gap_is_caught(self):
        """往 core._USB_CDC_IDS 塞一个 analyzer 表里没有的 VID:PID，
        审计必须报 —— 否则这条检查是恒真的摆设。"""
        fake = (0xFFFF, 0xFFFE)
        core._USB_CDC_IDS.add(fake)
        try:
            issues = ac.check_usb_vidpid()
        finally:
            core._USB_CDC_IDS.discard(fake)
        self.assertTrue(issues, "注入的脱节没被抓到 —— 审计器形同虚设")
        self.assertTrue(any("FFFF:FFFE" in s for s in issues),
                        "报出的 issue 里没有注入的那个 VID:PID：%s" % issues)
        # 清理后必须恢复干净
        self.assertEqual([], ac.check_usb_vidpid())

    def test_injected_code_drift_is_caught(self):
        """篡改 fallback 的一条释义，审计必须报出来。"""
        saved = analyzer._CWJAP_FALLBACK["2"]
        analyzer._CWJAP_FALLBACK["2"] = "故意改错的释义"
        try:
            issues = ac.check_error_codes()
        finally:
            analyzer._CWJAP_FALLBACK["2"] = saved
        self.assertTrue(any("+CWJAP" in s and "码 2" in s for s in issues),
                        "篡改 fallback 释义没被抓到：%s" % issues)
        self.assertEqual([], ac.check_error_codes())

    def test_injected_fault_bit_gap_is_caught(self):
        """往 _HARDFAULT_BITS 塞一个 cortex_m.json 没有的位名，审计必须报出来。"""
        analyzer._HARDFAULT_BITS.append((r"(?i)NOTAREALBIT", "测试用"))
        try:
            issues = ac.check_fault_bits()
        finally:
            analyzer._HARDFAULT_BITS.pop()
        self.assertTrue(any("NOTAREALBIT" in s for s in issues),
                        "注入的假位名没被抓到：%s" % issues)
        self.assertEqual([], ac.check_fault_bits())

    def test_injected_stale_rule_count_is_caught(self):
        """反自证：临时改一条 analyzer 规则数声明，审计必须报出来。

        做法是把「实测条数」人为抬高 1——文案里的数字立刻与之不符。
        若审计是恒真的，这里就会静默通过。
        """
        saved = analyzer.RULES
        analyzer.RULES = list(saved) + [("fake", "注入用")]
        try:
            issues = ac.check_hardcoded_rule_counts()
        finally:
            analyzer.RULES = saved
        self.assertTrue(issues, "注入的条数脱节没被抓到 —— 这条检查是恒真的摆设")
        self.assertEqual([], ac.check_hardcoded_rule_counts())

    def test_rule_family_count_is_not_a_false_positive(self):
        """★ 误报防线：README 的「8 条规则族」是子分类计数，不是总数。

        一刀切匹配「N 条规则」会把它当成脱节报出来——审计器一旦误报，
        就会被当成噪音关掉，所以这条必须钉住。
        """
        import re
        for pat in ac._RULE_COUNT_PATTERNS:
            self.assertIsNone(re.search(pat, "烧录失败诊断…共 8 条规则族"),
                              "「N 条规则族」被子分类误伤：%s" % pat)


if __name__ == "__main__":
    unittest.main()
