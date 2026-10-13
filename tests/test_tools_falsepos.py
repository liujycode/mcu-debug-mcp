# -*- coding: utf-8 -*-
"""老工具的真实数据双向回归（B③）：modbus_parse / decode_register_svd /
driver_get / pin_check。

设计原则（本项目的血泪教训）：只测"能跑通"等于没测。本文件专门钉住
【不该给结论时，必须在输出里说清"我判断不了"】——即"看似合理的假答案"防线。
下面每条反向断言，都是拿真实风格数据回放时实际踩出来的坑，不是想象出来的。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isolation import relaxed_env  # noqa: E402  测试辅助：临时解除隔离
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcu_debug_mcp import core  # noqa: E402


def _modbus_frame(body_hex):
    """按 Modbus RTU 拼一个 CRC 正确的完整帧（body 为 addr+fc+…）。"""
    b = bytes.fromhex(body_hex.replace(" ", ""))
    c = core._crc16_modbus(b)
    return (b + bytes([c & 0xFF, (c >> 8) & 0xFF])).hex().upper()


# ============================================================
# 一、modbus_parse：帧结构与功能码对不上时必须拒绝解读
# ============================================================

class TestModbusParseFalsePos(unittest.TestCase):

    def test_normal_read_holding_registers(self):
        out = core.modbus_parse(_modbus_frame("01 03 06 00 0A 00 14 00 1E"))
        self.assertIn("CRC：通过", out)
        self.assertIn("寄存器 0x0000 = 10", out)
        self.assertIn("寄存器 0x0002 = 30", out)

    def test_exception_response_decoded(self):
        out = core.modbus_parse(_modbus_frame("01 83 02"))
        self.assertIn("异常响应", out)
        self.assertIn("非法数据地址", out)

    def test_truncated_frame_refuses_to_interpret(self):
        """★4 字节的 01 03 00 00：旧实现只回一句"功能码 03：读保持寄存器"就当解析完了。"""
        out = core.modbus_parse("01 03 00 00")
        self.assertIn("未解读", out)
        self.assertNotIn("寄存器 0x", out)

    def test_write_single_struct_mismatch_refuses(self):
        """5 字节的 01 06 00 01 00：写单寄存器响应应为 8 字节。"""
        out = core.modbus_parse("01 06 00 01 00")
        self.assertIn("未解读", out)

    def test_write_multiple_struct_mismatch_refuses(self):
        out = core.modbus_parse(_modbus_frame("01 10 00 01"))   # 6 字节，应为 8 字节
        self.assertIn("未解读", out)

    def test_non_modbus_frame_flagged(self):
        """★CRC 不匹配 + 功能码非标准：必须点出"这帧很可能不是 Modbus"。"""
        out = core.modbus_parse("AA 55 01 00 02 00 FF FF")
        self.assertIn("不是 Modbus RTU 帧", out)

    def test_standard_fc7_not_called_nonstandard(self):
        """★FC07 是标准功能码（读异常状态）：说它"非标准"本身就是假答案。"""
        out = core.modbus_parse(_modbus_frame("01 07 00"))
        self.assertIn("读异常状态", out)
        self.assertNotIn("非标准", out)

    def test_unknown_fc_never_silent(self):
        """未知功能码也必须给出"暂不拆解"的话，不能只回帧头就结束。"""
        out = core.modbus_parse(_modbus_frame("01 2D 00"))
        self.assertIn("暂不拆解", out)

    def test_write_coil_echo(self):
        out = core.modbus_parse(_modbus_frame("01 05 00 01 FF 00"))
        self.assertIn("回显确认", out)

    def test_bad_hex_and_too_short(self):
        self.assertIn("HEX", core.modbus_parse(""))
        self.assertIn("HEX", core.modbus_parse("zz yy"))


# ============================================================
# 二、decode_register_svd：值域 / SVD 数据异常时必须点出来
# ============================================================

_SVD_DIR = None
_SVD = None


def setUpModule():
    """SVD 夹具造在【系统临时目录】，绝不落项目目录（否则跑测试就产垃圾）。"""
    global _SVD_DIR, _SVD
    _SVD_DIR = tempfile.mkdtemp(prefix="mcu_dbg_svd_")
    _SVD = os.path.join(_SVD_DIR, "t.svd")
    with open(_SVD, "w", encoding="utf-8") as f:
        f.write("""<?xml version="1.0" encoding="utf-8"?>
<device><name>TEST</name><peripherals><peripheral><name>RCC</name><registers>
<register><name>CR</name><addressOffset>0x0</addressOffset><size>32</size><fields>
<field><name>HSION</name><bitRange>[0:0]</bitRange><description>Internal HS enable</description></field>
<field><name>PLLSRC</name><bitRange>[17:16]</bitRange><enumeratedValues>
<enumeratedValue><name>HSI_DIV2</name><value>0</value></enumeratedValue>
<enumeratedValue><name>HSE</name><value>1</value></enumeratedValue></enumeratedValues></field>
<field><name>BAD_RANGE</name><bitRange>[16:17]</bitRange><description>非法 bitRange</description></field>
</fields></register></registers></peripheral></peripherals></device>""")


def tearDownModule():
    import shutil
    if _SVD_DIR and os.path.isdir(_SVD_DIR):
        shutil.rmtree(_SVD_DIR, ignore_errors=True)


class TestDecodeSvdFalsePos(unittest.TestCase):

    def test_normal_decode(self):
        out = core.decode_register_svd(_SVD, "RCC", "CR", 0x00040002)
        self.assertIn("HSION", out)
        self.assertIn("HSI_DIV2", out)

    def test_value_out_of_width_warns(self):
        """★32 位寄存器喂 0x100000000：旧实现静默照常解码。"""
        out = core.decode_register_svd(_SVD, "RCC", "CR", 0x100000000)
        self.assertIn("超出该寄存器位宽", out)

    def test_negative_value_warns_and_truncates(self):
        out = core.decode_register_svd(_SVD, "RCC", "CR", -1)
        self.assertIn("超出该寄存器位宽", out)
        self.assertIn("HSION", out)         # 仍给出按位宽截断后的解读

    def test_max_valid_value_no_warning(self):
        out = core.decode_register_svd(_SVD, "RCC", "CR", 0xFFFFFFFF)
        self.assertNotIn("超出该寄存器位宽", out)

    def test_illegal_bitrange_not_silent(self):
        """★bitRange=[16:17]（msb<lsb）旧实现算出位宽 0、恒输出 0，看着像个正常位域。"""
        out = core.decode_register_svd(_SVD, "RCC", "CR", 0x00040002)
        self.assertIn("位域定义非法", out)

    def test_missing_svd_and_peripheral(self):
        self.assertIn("SVD 文件不存在", core.decode_register_svd("D:/nope.svd", "RCC", "CR", 0))
        self.assertIn("没找到外设", core.decode_register_svd(_SVD, "NOPE", "CR", 0))


# ============================================================
# 三、driver_get：空内容不能返回空串
# ============================================================

class TestDriverGetFalsePos(unittest.TestCase):

    def test_existing_driver_returns_content(self):
        out = core.driver_get("dht11")
        self.assertTrue(out.strip())
        self.assertIn("=====", out)

    def test_missing_driver_lists_available(self):
        out = core.driver_get("no_such_driver_xyz")
        self.assertIn("没有名为", out)
        self.assertIn("dht11", out)


# ============================================================
# 四、pin_check：幻觉引脚 / 双占 / 硬约束
# ============================================================

class TestPinCheckFalsePos(unittest.TestCase):

    MCU = "stm32f103c8t6"

    def test_normal_assignment_passes(self):
        out = core.pin_check(self.MCU, {"PA9": "USART1_TX", "PA10": "USART1_RX"})
        self.assertIn("引脚检查", out)

    def test_hallucinated_pin_is_error(self):
        out = core.pin_check(self.MCU, {"PZ9": "GPIO"})
        self.assertIn("没有此引脚", out)
        self.assertIn("必须改", out)

    def test_swd_pin_warned(self):
        self.assertIn("SWD", core.pin_check(self.MCU, {"PA13": "GPIO"}))

    def test_remap_duplicate_function_warned(self):
        """★PA9 用默认档 TX，PB6 是 TX 的 remap 档——同一信号不能两脚同用。"""
        out = core.pin_check(self.MCU, {"PA9": "USART1_TX", "PB6": "USART1_TX"})
        self.assertIn("已经分配给", out)

    def test_unknown_mcu_lists_available(self):
        out = core.pin_check("no_such_mcu_xyz", {"PA9": "GPIO"})
        self.assertIn("没有收录 MCU", out)

    def test_empty_pins_rejected(self):
        self.assertIn("pins 需要是", core.pin_check(self.MCU, {}))

    def test_esp32_flash_pin_is_error(self):
        out = core.pin_check("esp32dev", {"GPIO6": "GPIO"})
        self.assertIn("flash 专用", out)

    def test_esp32_input_only_cannot_output(self):
        out = core.pin_check("esp32dev", {"GPIO34": "PWM_OUT"})
        self.assertIn("仅输入脚", out)


# ============================================================
# 五、串口工具的降级路径与核心防线（离线可测部分）
# ============================================================

class TestSerialToolDegrade(unittest.TestCase):

    def test_at_line_match_requires_full_line(self):
        """★核心防线：设备日志里偶然出现的 OK 不能被判成 AT 应答（假通过）。"""
        rx = core.re.compile("OK")
        hit, _ = core._at_line_match("boot OK done\n", rx)
        self.assertFalse(hit)
        hit2, line2 = core._at_line_match("\r\nOK\r\n", rx)
        self.assertTrue(hit2)

    def test_at_line_match_loose_opt_in(self):
        rx = core.re.compile("OK")
        hit, _ = core._at_line_match("boot OK done\n", rx, loose=True)
        self.assertTrue(hit)

    def test_at_sequence_bad_steps(self):
        self.assertIn("steps 需要是列表", core.at_sequence("COM_NOT_EXIST", None))

    def test_at_sequence_bad_port(self):
        # 本条验证的是「端口打不开时的降级文案」——隔离层里面的业务逻辑，
        # 不是隔离层本身，所以临时解除隔离（隔离层另有 test_isolation.py 专测）。
        with relaxed_env():
            out = core.at_sequence("COM_NOT_EXIST", [{"cmd": "AT", "expect": "OK"}])
        self.assertIn("失败", out)

    def test_baudrate_offline_no_discrimination_refuses(self):
        """★各档同分（离线样本）时必须拒绝给推荐，不能编一个"第一名"。"""
        out = core.baudrate_autodetect("COM_NOT_EXIST", log_text="hello\nworld\n")
        self.assertIn("判不出来", out)

    def test_baudrate_all_ports_unopenable_gives_conclusion(self):
        """★全部候选都打不开时必须给结论，不能只列一串"打不开的口"。

        临时解除隔离：本条测的是隔离层里面的降级逻辑，不是隔离层本身。
        """
        with relaxed_env():
            out = core.baudrate_autodetect("COM_NOT_EXIST", candidates=[115200, 9600])
        self.assertIn("打不开", out)
        self.assertIn("不在波特率", out)


if __name__ == "__main__":
    unittest.main()
