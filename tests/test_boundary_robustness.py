"""入参边界健壮性回归。

工具层绝不能把「用户给错了参数」抛成异常——到了 MCP 客户端就变成一句
Python 内部错误（ToolError），用户看不到任何排查提示。
也绝不能静默把空值替换成一个看似合法的默认值——那是「看似合理的假答案」。

每条都双向：该拒绝的必须拒绝，该正常工作的必须不受影响。
来源：2026-09-21 真实数据回放抓出的 4 类缺陷（不是单测happy path）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402


class TestDecodeRegisterWidth(unittest.TestCase):
    """decode_register 的 width 非法时必须友好拒绝。

    原实现：`1 << width` 在 width=-1 时抛 ValueError("negative shift count")，
    直接冒出 MCP 层；width=0 时又算出恒 0 的假位域。
    """

    def test_negative_width_refused(self):
        out = core.decode_register(1, [{"name": "A", "bit": 0}], -1)
        self.assertIn("不合法", out)
        self.assertIn("width", out)

    def test_zero_width_refused(self):
        self.assertIn("不合法", core.decode_register(1, [{"name": "A", "bit": 0}], 0))

    def test_huge_width_refused(self):
        self.assertIn("不合法", core.decode_register(1, [], 999))

    def test_non_int_width_refused(self):
        self.assertIn("必须", core.decode_register(1, [], "wide"))

    def test_normal_widths_still_work(self):
        """合法位宽（1~64）一个都不能误伤。"""
        out = core.decode_register(0x40002, [{"name": "PLLSRC", "lsb": 16, "width": 2}], 32)
        self.assertIn("PLLSRC", out)
        self.assertIn("= 0", out)
        for w in (1, 8, 16, 32, 64):
            self.assertIn("寄存器值", core.decode_register(1, [{"name": "X", "bit": 0}], w))


class TestHandleRefusal(unittest.TestCase):
    """无效串口句柄必须友好拒绝，绝不能抛异常。"""

    def test_get_session_returns_none(self):
        """get_session 只如实回答「有没有」，不抛。"""
        self.assertIsNone(core.get_session(99999))

    def test_tools_refuse_friendly(self):
        for out in (core.serial_read(99999, seconds=0.1),
                    core.serial_send(99999, "AT"),
                    core.serial_session_log(99999)):
            self.assertIn("无效的串口句柄", out)

    def test_close_same_wording(self):
        """同一件事（句柄无效）四个工具口径必须一致，都得提句柄号。"""
        self.assertIn("99999", core.serial_close(99999))


class TestPinCheckEmptyFunc(unittest.TestCase):
    """pin_check 功能名为空必须报错——静默当 GPIO 会给出「无硬冲突」假通过。"""

    MCU = "stm32f103c8t6"

    def test_empty_string_flagged(self):
        out = core.pin_check(self.MCU, {"PA9": ""})
        self.assertIn("(空)", out)
        self.assertIn("必须改", out)
        self.assertNotIn("✓ PA9", out)          # 不能出现「通过」图标

    def test_none_flagged(self):
        self.assertIn("(空)", core.pin_check(self.MCU, {"PA9": None}))

    def test_blank_only_flagged(self):
        self.assertIn("(空)", core.pin_check(self.MCU, {"PA9": "   "}))

    def test_mixed_still_checks_others(self):
        """一项空不能拖累其他项照常检查。"""
        out = core.pin_check(self.MCU, {"PA9": "", "PA10": "USART1_RX"})
        self.assertIn("(空)", out)
        self.assertIn("USART1_RX", out)

    def test_normal_unchanged(self):
        out = core.pin_check(self.MCU, {"PA9": "USART1_TX", "PA10": "USART1_RX"})
        self.assertIn("全部通过", out)

    def test_real_conflict_still_caught(self):
        out = core.pin_check(self.MCU, {"PA9": "USART1_TX", "PB6": "USART1_TX"})
        self.assertIn("remap", out)


class TestDriverGetEmptyName(unittest.TestCase):
    """driver_get('') 不能走进「目录存在但没内容」分支。

    原实现 os.path.join(驱动库, "") 拼出的是驱动库根目录（确实存在），
    于是输出"驱动  目录存在，但里面没有任何可读内容"——用户完全看不懂。
    """

    def test_empty_name(self):
        out = core.driver_get("")
        self.assertIn("没给驱动名", out)
        self.assertNotIn("目录存在", out)

    def test_blank_name(self):
        self.assertIn("没给驱动名", core.driver_get("   "))

    def test_real_driver_unchanged(self):
        self.assertIn("dht11", core.driver_get("dht11"))

    def test_unknown_driver_still_friendly(self):
        self.assertIn("没有名为", core.driver_get("nope"))


class TestLargeLogNote(unittest.TestCase):
    """大日志提示：规则是全量扫描（~1.2 MB/s），贴十几 MB 得让人知道要等。

    不写死条数——写死的数字会随规则增减腐烂，而这条断言与条数无关。

    提示本身不是正确性防线，但「工具卡住了 / 没跑完」的错觉会让人误判结果。
    这里只测提示的开关逻辑，不真造 MB 级输入（那会把测试拖慢）。
    """

    def test_small_no_note(self):
        self.assertEqual(core._large_log_note(0), "")
        self.assertEqual(core._large_log_note(1024), "")
        self.assertEqual(core._large_log_note(core._LARGE_LOG_BYTES), "")   # 边界：等于不算超

    def test_large_has_note(self):
        note = core._large_log_note(core._LARGE_LOG_BYTES + 1)
        self.assertIn("MB", note)
        self.assertIn("tail", note)

    def test_note_reports_size(self):
        note = core._large_log_note(8 * 1024 * 1024)
        self.assertIn("8.0 MB", note)

    def test_normal_log_output_has_no_note(self):
        """正常规模日志的输出里不该出现这句提示（不能干扰正常结论）。"""
        out = core.analyze_log("AT+CWMODE=1\r\nERROR\r\n")
        self.assertNotIn("全量扫描", out)


if __name__ == "__main__":
    unittest.main()
