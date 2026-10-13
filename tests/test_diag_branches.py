# -*- coding: utf-8 -*-
"""诊断与协议解析的**分支回归**（批次 2）：NMEA / Modbus / 帧 / 时序 / decode_fault / 位域。

与 test_frame_parse.py 的分工：那份文件用**公开标准例句**锁住算法正确性（正例）；
本文件补的是**错误分支与陷阱路径**——这些分支此前零覆盖，而它们恰恰是
「看似合理的假答案」的高发区（校验不过装没事 / CRC 错照常解 / 位域 mask=0 恒输出 0）。

decode_fault 的位号不抄实现：直接与 knowledge/fault_bits/cortex_m.json 对账（表驱动），
知识库被改错位号，测试当场红。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcu_debug_mcp import core  # noqa: E402


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _nmea_ck(body):
    """独立实现的 NMEA XOR 校验（被测算法已由 test_frame_parse 的公开例句锚定）。"""
    x = 0
    for ch in body:
        x ^= ord(ch)
    return "%02X" % x


def _nmea(body):
    return "$%s*%s" % (body, _nmea_ck(body))


def _modbus_crc(data):
    """独立实现的 CRC16/Modbus（被测算法已由公开帧 9B F6 锚定）。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _modbus_frame(*byte_vals):
    data = bytes(byte_vals)
    c = _modbus_crc(data)
    return " ".join("%02X" % b for b in data) + " %02X %02X" % (c & 0xFF, (c >> 8) & 0xFF)


# =====================================================================
# NMEA：GSA / GSV / VTG / GLL / 无校验段 / 校验非 hex
# =====================================================================
class TestNmeaBranches(unittest.TestCase):

    def test_gsa_gives_fix_type_and_pdop(self):
        out = core.nmea_parse(_nmea("GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1"))
        self.assertIn("定位类型 3D 定位", out)
        self.assertIn("PDOP 2.5 / HDOP 1.3 / VDOP 2.1", out)
        self.assertIn("参与解算卫星 04/05/09/12/24（5 颗）", out)

    def test_gsv_lists_satellites(self):
        out = core.nmea_parse(_nmea("GPGSV,3,1,11,03,03,111,00,04,15,270,00,06,01,010,00,13,06,292,00"))
        self.assertIn("第 1/3 句，共见 11 颗星", out)
        self.assertIn("卫星 03：仰角 03° 方位 111° 信噪比 00 dBHz", out)

    def test_vtg_gives_course_and_speed(self):
        out = core.nmea_parse(_nmea("GPVTG,054.7,T,034.4,M,005.5,N,010.2,K"))
        self.assertIn("对地航向 054.7°（真北）", out)
        self.assertIn("对地速度 005.5 节 / 010.2 km/h", out)

    def test_gll_gives_position_and_time(self):
        out = core.nmea_parse(_nmea("GPGLL,4916.45,N,12311.12,W,225444,A"))
        self.assertIn("纬度 4916.45 N / 经度 12311.12 W", out)
        self.assertIn("十进制度 49.274167, -123.185333", out)
        self.assertIn("时间 225444 UTC / 状态 有效", out)

    def test_checksumless_sentence_is_declared(self):
        """没有 *hh 校验段 → 明说「无法校验」，不许当校验通过。"""
        out = core.nmea_parse("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
        self.assertIn("无法校验", out)
        self.assertNotIn("校验通过", out)

    def test_nonhex_checksum_field_fails_verification(self):
        """*XX 里 XX 不是 hex → 不能崩，按校验失败处理。"""
        out = core.nmea_parse("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*ZZ")
        self.assertIn("校验不通过", out)

    def test_rmc_void_position_declared(self):
        out = core.nmea_parse(_nmea("GPRMC,123519,V,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"))
        self.assertIn("状态 无效定位", out)


# =====================================================================
# Modbus：异常响应 + 各功能码分支 + 非标准帧
# =====================================================================
class TestModbusBranches(unittest.TestCase):

    def test_exception_response_is_decoded(self):
        """从站 01 对 fc3 回异常 02（非法地址）——必须点出是异常响应而不是照常解寄存器。"""
        out = core.modbus_parse(_modbus_frame(0x01, 0x83, 0x02))
        self.assertIn("异常响应", out)
        self.assertIn("读保持寄存器", out)
        self.assertIn("非法数据地址", out)

    def test_exception_frame_wrong_length_is_declared(self):
        """异常帧 6 字节（多了 1 字节）→ CRC 或抓帧有问题，必须明说。"""
        out = core.modbus_parse(_modbus_frame(0x01, 0x83, 0x02, 0x00))
        self.assertIn("异常帧长度应为 5 字节", out)

    def test_unknown_exception_code_does_not_invent_meaning(self):
        out = core.modbus_parse(_modbus_frame(0x01, 0x83, 0x63))
        self.assertIn("未知异常码", out)

    def test_nonstandard_fc_with_bad_crc_is_declared_not_modbus(self):
        """功能码非标准 + CRC 也错 → 这帧多半根本不是 Modbus。"""
        out = core.modbus_parse("01 41 00 01 00 02 00 03")
        self.assertIn("不是 Modbus RTU 帧", out)

    def test_fc3_short_frame_refused(self):
        """4 字节 "01 03 00 00" 是请求帧/截断帧——不许当成响应解析（真实事故帧）。"""
        out = core.modbus_parse("01 03 00 00")
        self.assertIn("未解读", out)
        self.assertIn("至少 6 字节", out)

    def test_fc3_bytecount_mismatch_is_flagged(self):
        """字节数字段说 4，实际只有 2 字节数据——截断/抄错要点出来。"""
        out = core.modbus_parse(_modbus_frame(0x01, 0x03, 0x04, 0x00, 0x0A))
        self.assertIn("字节数 4 与实际 2 不一致", out)

    def test_fc3_two_registers_decoded_big_endian(self):
        out = core.modbus_parse(_modbus_frame(0x01, 0x03, 0x04, 0x00, 0x0A, 0x01, 0x02))
        self.assertIn("寄存器 0x0000 = 10 (0x000A)", out)
        self.assertIn("寄存器 0x0001 = 258 (0x0102)", out)

    def test_fc4_short_frame_refused(self):
        out = core.modbus_parse("01 04 00 00")
        self.assertIn("未解读", out)

    def test_fc1_coil_bits_decoded(self):
        out = core.modbus_parse(_modbus_frame(0x01, 0x01, 0x01, 0x05))
        self.assertIn("线圈状态字节 0：0=1 1=0 2=1", out)

    def test_fc1_bytecount_mismatch_flagged(self):
        out = core.modbus_parse(_modbus_frame(0x01, 0x01, 0x02, 0x05))
        self.assertIn("字节数 2 与实际 1 不一致", out)

    def test_fc2_short_frame_refused(self):
        out = core.modbus_parse("01 02 00 00")
        self.assertIn("未解读", out)

    def test_fc5_echo_confirmed(self):
        out = core.modbus_parse(_modbus_frame(0x01, 0x05, 0x00, 0x10, 0xFF, 0x00))
        self.assertIn("回显确认：地址 0x0010，值/数量 65280", out)

    def test_fc5_wrong_length_refused(self):
        out = core.modbus_parse(_modbus_frame(0x01, 0x05, 0x00, 0x10))
        self.assertIn("未解读", out)

    def test_fc16_echo_confirmed(self):
        out = core.modbus_parse(_modbus_frame(0x01, 0x10, 0x00, 0x10, 0x00, 0x02))
        self.assertIn("回显确认：地址 0x0010，值/数量 2", out)

    def test_fc15_wrong_length_refused(self):
        out = core.modbus_parse(_modbus_frame(0x01, 0x0F, 0x00, 0x10))
        self.assertIn("未解读", out)

    def test_unknown_fc_says_not_fully_decoded(self):
        """fc 0x2B（Encapsulated Interface）——只给帧头与 CRC 判定，明确说没拆完。"""
        out = core.modbus_parse(_modbus_frame(0x01, 0x2B, 0x0E, 0x01, 0x00))
        self.assertIn("暂不拆解", out)


# =====================================================================
# 通用帧解析：切帧依据缺失 / 校验名错误 / 垃圾数据 / 半包
# =====================================================================
class TestFrameParseBranches(unittest.TestCase):

    def test_no_basis_refuses(self):
        out = core.frame_parse("01 02 03")
        self.assertIn("需要至少一个切帧依据", out)
        self.assertIn("01 02 03", out)

    def test_bad_hex_input_is_error_not_crash(self):
        out = core.frame_parse("AA BB ZZ")
        self.assertIn("ZZ", out)

    def test_bad_header_hex_is_error(self):
        out = core.frame_parse("AA 55 00", header="GG")
        self.assertIn("帧头", out)

    def test_unknown_checksum_name_refused(self):
        """校验名写错不能静默当 none——用户会以为校验过了。"""
        out = core.frame_parse("AA 55 00", header="AA55", checksum="sum16")
        self.assertIn("checksum 只支持", out)
        self.assertIn("sum16", out)

    def test_non_integer_length_offset_refused(self):
        out = core.frame_parse("AA 55 00", header="AA55", length_offset="abc")
        self.assertIn("必须是整数", out)

    def test_garbage_before_header_is_listed(self):
        """帧头前的垃圾字节要逐条列出，不许静默吞。"""
        out = core.frame_parse("DE AD AA 55 02 01 02", header="AA55", length_offset=2, length_size=1)
        self.assertIn("跳过 2 字节垃圾数据", out)

    def test_tail_without_header_is_listed(self):
        out = core.frame_parse("AA 55 02 01 02 FF FF", header="AA55", length_offset=2, length_size=1)
        self.assertIn("找不到帧头", out)

    def test_length_field_overrun_stops(self):
        """长度字段位置超出数据 → 停止并说明，不许越界读。"""
        out = core.frame_parse("AA 55", header="AA55", length_offset=5)
        self.assertIn("越界", out)

    def test_sum8_checksum_matches(self):
        # ★ sum8 是整帧前缀和（含帧头）：AA+55+02+01+02=0x104 → 校验字节 04
        out = core.frame_parse("AA 55 02 01 02 04", header="AA55",
                               length_offset=2, checksum="sum8")
        self.assertIn("sum8 ✓", out)

    def test_sum8_checksum_mismatch_named(self):
        out = core.frame_parse("AA 55 02 01 02 03", header="AA55",
                               length_offset=2, checksum="sum8")
        self.assertIn("✗ 不匹配", out)

    def test_crc16_modbus_checked(self):
        f = _modbus_frame(0x01, 0x03, 0x02, 0x00, 0x0A)
        out = core.frame_parse(f, header="0103", checksum="crc16_modbus")
        self.assertIn("CRC16 ✓", out)

    def test_crc16_modbus_mismatch_named(self):
        out = core.frame_parse("01 03 02 00 0A 00 00", header="0103", checksum="crc16_modbus")
        self.assertIn("CRC16 ✗ 不匹配", out)

    def test_frame_too_short_for_checksum_is_said(self):
        # header 命中但 body 只剩 1 字节，放不下 1 字节校验 → 明说而不是当校验通过
        out = core.frame_parse("AA", header="AA", checksum="sum8")
        self.assertIn("帧太短，放不下校验字节", out)

    def test_more_than_40_frames_are_summarized(self):
        """45 帧 → 显示前 40 帧 + 「还有 5 帧未显示」。"""
        one = "AA 55 01 01"
        out = core.frame_parse(" ".join([one] * 45), header="AA55", length_offset=2)
        self.assertIn("还有 5 帧未显示", out)

    def test_max_frames_guard(self):
        one = "AA 55 01 01"
        out = core.frame_parse(" ".join([one] * 30), header="AA55",
                               length_offset=2, max_frames=5)
        self.assertIn("切出 5 帧", out)


# =====================================================================
# 时序采样：时间戳格式 / 跨零点 / 序号
# =====================================================================
class TestTimingSamples(unittest.TestCase):

    def test_custom_pattern_takes_precedence(self):
        text = "T=1.0s APP seq=7\nT=1.5s APP seq=8\n"
        out = core._timing_samples(text, pattern=r"T=(\d+(?:\.\d+)?)s",
                                   seq_pattern=r"seq=(\d+)")
        self.assertEqual([(t, s) for t, s, _ in out], [(1.0, 7), (1.5, 8)])

    def test_pattern_group_not_number_falls_back(self):
        """自定义 pattern 捕获组不是数字 → 回落到内置格式，不许崩。"""
        text = "[+2.0] TICK abc\n"
        out = core._timing_samples(text, pattern=r"T=(\w+)s")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], 2.0)

    def test_hms_timestamp_parsed_with_fraction(self):
        text = "12:34:56.789 hello\n"
        out = core._timing_samples(text)
        ts, _seq, _ln = out[0]
        self.assertAlmostEqual(ts, 12 * 3600 + 34 * 60 + 56 + 0.789, places=3)

    def test_hms_crossing_midnight_adds_a_day(self):
        """23:59:59 → 00:00:01 必须 +86400：跨零点的采集是常见形态。"""
        text = "23:59:59.900 tick\n00:00:01.100 tick\n"
        out = core._timing_samples(text)
        self.assertAlmostEqual(out[1][0] - out[0][0], 1.2, places=3)

    def test_lead_number_used_as_seq(self):
        text = "[+1.0] 42 tick\n[+1.1] 43 tick\n"
        out = core._timing_samples(text)
        self.assertEqual([s for _t, s, _ in out], [42, 43])

    def test_seq_pattern_non_number_gives_none(self):
        text = "[+1.0] seq=abc\n"
        out = core._timing_samples(text, seq_pattern=r"seq=(\d+)")
        self.assertIsNone(out[0][1])

    def test_lines_without_timestamp_skipped(self):
        text = "# comment\nno stamps here\n[+3.0] tick\n"
        out = core._timing_samples(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], 3.0)


# =====================================================================
# decode_fault：陷阱路径（与知识库位号对账）
# =====================================================================
class TestFaultBitsLedger(unittest.TestCase):
    """位号对账：实现里用到的每一位都必须与 knowledge/fault_bits/cortex_m.json 一致。
    知识库位号被改动（或实现写死错了位号），这里当场红。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROOT, "src", "mcu_debug_mcp", "knowledge",
                            "fault_bits", "cortex_m.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cls.bits = data["registers"]["CFSR"]["bits"]
        cls.hfsr_bits = data["registers"]["HFSR"]["bits"]

    def test_bfarvalid_is_cfsr_bit15(self):
        self.assertEqual(self.bits["BFARVALID"]["bit"], 15)

    def test_mmarvalid_is_cfsr_bit7(self):
        self.assertEqual(self.bits["MMARVALID"]["bit"], 7)

    def test_preciserr_is_cfsr_bit9(self):
        self.assertEqual(self.bits["PRECISERR"]["bit"], 9)

    def test_impreciserr_is_cfsr_bit10(self):
        self.assertEqual(self.bits["IMPRECISERR"]["bit"], 10)

    def test_forced_is_hfsr_bit30(self):
        self.assertEqual(self.hfsr_bits["FORCED"]["bit"], 30)


class TestDecodeFault(unittest.TestCase):
    """decode_fault 三类陷阱：地址可信性 / FORCED 引导 / 架构差异。"""

    def test_m0_explains_no_cfsr_and_points_to_pc_path(self):
        out = core.decode_fault(cfsr=0x82, arch="cortex-m0")
        self.assertIn("没有 CFSR", out)
        self.assertIn("symbol_lookup", out)

    def test_unknown_arch_lists_known_ones(self):
        out = core.decode_fault(cfsr=1, arch="riscv")
        self.assertIn("没有收录架构", out)
        self.assertIn("已收录", out)

    def test_filename_form_also_accepted(self):
        """传 'cortex_m'（文件名形态）也能宽容匹配到 cortex-m 位域表。"""
        out = core.decode_fault(cfsr=0)
        self.assertIn("故障寄存器解码", out)

    def test_unparsable_value_rejected(self):
        out = core.decode_fault(cfsr="hello")
        self.assertIn("无法解析为整数", out)

    def test_out_of_range_value_rejected(self):
        out = core.decode_fault(cfsr=1 << 32)
        self.assertIn("超出 32 位范围", out)

    def test_no_register_at_all_gives_usage(self):
        out = core.decode_fault()
        self.assertIn("至少要给一个寄存器值", out)

    def test_cfsr_zero_says_no_bits_set(self):
        out = core.decode_fault(cfsr=0)
        self.assertIn("无任何故障位置位", out)

    def test_hfsr_forced_without_cfsr_demands_cfsr(self):
        """★ FORCED=1 必须引导去看 CFSR——只看 HFSR 会漏根因。"""
        out = core.decode_fault(hfsr=1 << 30)
        self.assertIn("FORCED=1", out)
        self.assertIn("你还没传 CFSR", out)

    def test_hfsr_forced_with_zero_cfsr_names_the_traps(self):
        out = core.decode_fault(cfsr=0, hfsr=1 << 30)
        self.assertIn("但你传的 CFSR=0", out)

    def test_hfsr_forced_with_cfsr_bits_gives_both(self):
        out = core.decode_fault(cfsr=1 << 15, hfsr=1 << 30)
        self.assertIn("FORCED=1", out)
        self.assertIn("BFARVALID", out)

    # ---- BFAR 可信性 ----
    def test_bfar_without_cfsr_is_unverified(self):
        out = core.decode_fault(bfar=0x40023800)
        self.assertIn("未传 CFSR", out)
        self.assertIn("可信性未验证", out)

    def test_bfarvalid_zero_means_leftover_address(self):
        """★ 核心陷阱：BFARVALID=0 时 BFAR 是残留值，拿去反查会得假结论。"""
        out = core.decode_fault(cfsr=0, bfar=0x40023800)
        self.assertIn("BFARVALID=0", out)
        self.assertIn("不是本次故障地址", out)

    def test_bfarvalid_without_bfar_value_asks_for_it(self):
        out = core.decode_fault(cfsr=1 << 15)
        self.assertIn("补上地址才能反查", out)

    def test_precise_busfault_address_is_trusted(self):
        cfsr = (1 << 15) | (1 << 9)          # BFARVALID + PRECISERR
        out = core.decode_fault(cfsr=cfsr, bfar=0x40023800)
        self.assertIn("此地址可信", out)
        self.assertIn("0x40023800", out)

    def test_imprecise_busfault_address_is_untrusted(self):
        """★ IMPRECISERR=1 时地址不可信——必须给二分法排查指引而不是反查。"""
        cfsr = (1 << 15) | (1 << 10)         # BFARVALID + IMPRECISERR
        out = core.decode_fault(cfsr=cfsr, bfar=0x20001234)
        self.assertIn("不可信", out)
        self.assertIn("二分法", out)

    def test_valid_but_no_precise_bit_says_unknown(self):
        out = core.decode_fault(cfsr=1 << 15, bfar=0x40023800)
        self.assertIn("无法判定精确性", out)

    def test_sentinel_address_is_flagged_even_when_valid(self):
        """0xFFFFFFFF 这类哨兵值即使 VALID=1 也要提醒——valid 不代表值合理。"""
        cfsr = (1 << 15) | (1 << 9)
        out = core.decode_fault(cfsr=cfsr, bfar=0xFFFFFFFF)
        self.assertIn("0xFFFFFFFF", out)
        self.assertIn("擦除态", out)

    # ---- MMFAR ----
    def test_mmfar_without_cfsr_is_unverified(self):
        out = core.decode_fault(mmfar=0x20000010)
        self.assertIn("未传 CFSR", out)

    def test_mmarvalid_zero_means_leftover(self):
        out = core.decode_fault(cfsr=0, mmfar=0x20000010)
        self.assertIn("MMARVALID=0", out)

    def test_mmarvalid_trusted_address(self):
        out = core.decode_fault(cfsr=1 << 7, mmfar=0x20000010)
        self.assertIn("此地址可信", out)

    def test_mmarvalid_sentinel_flagged(self):
        out = core.decode_fault(cfsr=1 << 7, mmfar=0)
        self.assertIn("NULL 解引用", out)

    def test_ufsr_hits_are_grouped_under_ufsr(self):
        """UFSR 命中要按段分组输出（UNDEFINSTR = CFSR bit16）。"""
        out = core.decode_fault(cfsr=1 << 16)
        self.assertIn("[UFSR]", out)


# =====================================================================
# decode_register / decode_register_svd
# =====================================================================
class TestDecodeRegister(unittest.TestCase):
    """位域手工解码：三种合法写法 + 三类坏定义必须跳过并点数。"""

    def test_single_bit_and_range_fields(self):
        out = core.decode_register(0x12345678, [
            {"name": "A", "bit": 0},
            {"name": "B", "lsb": 16, "width": 4},
            {"name": "C", "bits": [4, 7]},
        ])
        self.assertIn("A            [0]       = 0（0b0）", out)
        self.assertIn("B            [19:16]   = 4（0b0100）", out)
        self.assertIn("C            [7:4]     = 7（0b0111）", out)

    def test_string_bits_definition_is_skipped_with_guidance(self):
        """{"bits":"0-7"} 字符串写法（实测踩过的形态）必须跳过并给正确写法。"""
        out = core.decode_register(0x12345678, [{"name": "A", "bits": "0-7"}])
        self.assertIn("1/1 个字段未能解析", out)
        self.assertIn("bits 必须是【两个整数】的列表", out)

    def test_all_fields_skipped_declares_empty_table_meaningless(self):
        out = core.decode_register(0x12345678, [
            {"name": "A", "bits": "0-7"}, {"name": "B", "bits": "8-15"}])
        self.assertIn("2/2 个字段未能解析", out)
        self.assertIn("【没有任何有效信息】", out)

    def test_missing_bit_definition_skipped(self):
        out = core.decode_register(0x12345678, [{"name": "A"}])
        self.assertIn("缺 bit 或 lsb+width", out)

    def test_out_of_range_field_skipped(self):
        out = core.decode_register(0x12345678, [{"name": "A", "lsb": 60, "width": 8}])
        self.assertIn("超出 0~63", out)

    def test_interval_bits_form(self):
        """bits=[16,17] 区间写法：lsb=16, w=2。"""
        out = core.decode_register(0xFFFFFFFF, [{"name": "W", "bits": [16, 17]}])
        self.assertIn("W            [17:16]   = 3（0b11）", out)


SVD = """<?xml version="1.0" encoding="utf-8"?>
<device>
  <peripherals>
    <peripheral><name>RCC</name>
      <register><name>CR</name><size>32</size>
        <fields>
          <field><name>HSION</name><bitRange>[0:0]</bitRange>
            <enumeratedValues><enumeratedValue><name>ON</name><value>1</value></enumeratedValue></enumeratedValues>
          </field>
          <field><name>SW</name><lsb>2</lsb><bitWidth>2</bitWidth>
            <description>System clock switch</description>
          </field>
          <field><name>BAD</name><bitRange>[5:3]</bitRange></field>
          <field><name>REVERSED</name><bitRange>[5:9]</bitRange></field>
        </fields>
      </register>
      <register><name>BSCFG</name><size>32</size></register>
    </peripheral>
    <peripheral><name>USART1</name>
      <register><name>SR[0]</name><size>32</size>
        <fields><field><name>PE</name><bitRange>[0:0]</bitRange></field></fields>
      </register>
    </peripheral>
  </peripherals>
</device>
"""


class TestDecodeRegisterSvd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fd, cls.svd = tempfile.mkstemp(suffix=".svd")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(SVD)
        # 文件要活到所有测试跑完 → 不用 addCleanup，tearDownClass 里删
        import atexit
        atexit.register(_rm, cls.svd)

    def test_missing_file_is_friendly(self):
        out = core.decode_register_svd("no_such.svd", "RCC", "CR", 1)
        self.assertIn("SVD 文件不存在", out)

    def test_broken_xml_is_error_not_crash(self):
        fd, p = tempfile.mkstemp(suffix=".svd")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("<device><peripherals>")
        self.addCleanup(_rm, p)
        out = core.decode_register_svd(p, "RCC", "CR", 1)
        self.assertIn("SVD 解析失败", out)

    def test_unknown_peripheral_named(self):
        out = core.decode_register_svd(self.svd, "TIM9", "CR", 1)
        self.assertIn("没找到外设 TIM9", out)

    def test_unknown_register_named(self):
        out = core.decode_register_svd(self.svd, "RCC", "XYZ", 1)
        self.assertIn("没找到寄存器 XYZ", out)

    def test_register_name_with_index_suffix(self):
        """SVD 里 SR[0] 这类带数组下标的寄存器名，按 [ 前段匹配。"""
        out = core.decode_register_svd(self.svd, "USART1", "SR", 1)
        self.assertIn("USART1->SR", out)
        self.assertIn("PE", out)

    def test_bitrange_and_lsb_width_both_supported(self):
        out = core.decode_register_svd(self.svd, "RCC", "CR", 0b10101)
        self.assertIn("HSION", out)
        self.assertIn("SW", out)

    def test_enum_value_hinted(self):
        out = core.decode_register_svd(self.svd, "RCC", "CR", 0x1)
        self.assertIn("→ ON", out)

    def test_value_over_width_is_declared_and_truncated(self):
        """★ 32 位寄存器喂 0x1_0000_0000 → 必须点出来，不许静默照常解。"""
        out = core.decode_register_svd(self.svd, "RCC", "CR", 0x100000000)
        self.assertIn("超出该寄存器位宽", out)

    def test_negative_value_truncated_unsigned(self):
        out = core.decode_register_svd(self.svd, "RCC", "CR", -1)
        self.assertIn("超出该寄存器位宽", out)

    def test_non_integer_value_rejected(self):
        out = core.decode_register_svd(self.svd, "RCC", "CR", "abc")
        self.assertIn("value 必须是整数", out)

    def test_register_without_fields_says_so(self):
        out = core.decode_register_svd(self.svd, "RCC", "BSCFG", 1)
        self.assertIn("未定义位域", out)

    def test_reversed_bitrange_is_skipped_not_zero(self):
        """★ msb<lsb 的坏定义旧实现会算出 mask=0 恒输出 0（看似合理的假答案）。"""
        out = core.decode_register_svd(self.svd, "RCC", "CR", 0xFFFFFFFF)
        self.assertIn("非法", out)
        self.assertIn("REVERSED", out)


# =====================================================================
# peripheral_api_get：知识层取值的错误分支
# =====================================================================
class TestPeripheralApiGet(unittest.TestCase):

    def test_unknown_driver_lists_available(self):
        out = core.peripheral_api_get("no_such_driver")
        self.assertIn("没有名为 no_such_driver 的记录", out)
        self.assertIn("可用：", out)

    def test_empty_name_lists_available(self):
        out = core.peripheral_api_get("")
        self.assertIn("没给驱动名", out)

    def test_known_driver_gives_full_sections(self):
        recs = core.peripheral_records()
        name = recs[0]["name"]
        out = core.peripheral_api_get(name)
        self.assertIn("【%s】外设：" % name, out)
        self.assertIn("来源：", out)
        self.assertIn("坑：", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
