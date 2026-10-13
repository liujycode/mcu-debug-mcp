# -*- coding: utf-8 -*-
"""协议报文解析回归（G08）：NMEA 0183（GPS/北斗）+ 通用二进制帧。

这两件事的共同风险都是**静默产出错数据**：

    · NMEA：校验不过却照常解出经纬度 → 用户拿着错坐标去定位；
    · 二进制帧：切不动就悄悄丢字节 → 用户以为收全了，其实丢了一半。

所以每条都按三档钉住：**对帧给人话 / 错帧明说不可信 / 切不动逐条报出来**。

校验算法用公开可查的标准例句（含公开校验和）验证，而不是拿自己算的值自证——
自己算的校验和自己写的验签永远一致，验不出算法写错。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core  # noqa: E402


#: 公开标准例句（GGA/RMC 的经典样例，校验和 47 / 6A 为公开值）
WIKI_GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
WIKI_RMC = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"

#: 真实 Modbus RTU 响应帧：从站01 读保持寄存器，字节数=4，CRC=9B F6
MODBUS_RESP = "01 03 04 00 0A 00 0B 9B F6"


class TestNmeaParse(unittest.TestCase):
    """G08 前半：NMEA 0183 解析。"""

    def test_canonical_gga_is_checksum_verified(self):
        """公开例句必须验签通过——校验算法写错这里立刻红。"""
        out = core.nmea_parse(WIKI_GGA)
        self.assertIn("✓ 校验通过", out)
        self.assertNotIn("不可信", out)

    def test_gga_gives_human_readable_position(self):
        """不能只回显原文——要给人话解读（时间/经纬度/质量/星数/海拔）。"""
        out = core.nmea_parse(WIKI_GGA)
        self.assertIn("时间 123519 UTC", out)
        self.assertIn("十进制度 48.117300, 11.516667", out)
        self.assertIn("单点定位(GPS)", out)
        self.assertIn("卫星数 08", out)
        self.assertIn("海拔 545.4 m", out)

    def test_rmc_gives_speed_in_kmh(self):
        """RMC 给的是节，要顺手换算成 km/h（用户看的是 km/h）。"""
        out = core.nmea_parse(WIKI_RMC)
        self.assertIn("状态 有效定位", out)
        self.assertIn("地面速度 22.40 节 = 41.48 km/h", out)

    def test_bad_checksum_is_declared_untrustworthy(self):
        """★ 核心判据：校验不过不能装作没事，要明说这句不可信。"""
        bad = WIKI_GGA[:-2] + "00"
        out = core.nmea_parse(bad)
        self.assertIn("✗ 校验不通过", out)
        self.assertIn("不可信", out)
        self.assertIn("其中 1 条校验不通过", out)

    def test_missing_checksum_is_not_treated_as_pass(self):
        """没有 *hh 段要说「无法校验」，不能默认通过（默认通过 = 假安全感）。"""
        out = core.nmea_parse(WIKI_GGA[:-3])
        self.assertIn("无 *hh 校验段，无法校验", out)
        self.assertNotIn("✓ 校验通过", out)

    def test_non_nmea_lines_are_skipped_not_parsed(self):
        """混进来的普通日志行要单独指出并跳过，不能当数据吃进去。"""
        out = core.nmea_parse("boot ok\n" + WIKI_GGA)
        self.assertIn("跳过（不是 NMEA 语句", out)
        self.assertIn("boot ok", out)
        self.assertIn("✓ 校验通过", out)

    def test_unknown_sentence_type_is_not_guessed(self):
        """没内置解读的类型要明说，并原样保留字段——不猜。"""
        body = "GPZDA,201530.00,04,07,2002,00,00"
        out = core.nmea_parse("$%s*%02X" % (body, core._nmea_xor(body)))
        self.assertIn("未内置解读的语句类型", out)

    def test_empty_input_is_friendly(self):
        self.assertIn("没有输入", core.nmea_parse(""))

    def test_none_input_does_not_crash(self):
        """L4 边界：None 不能抛异常。"""
        self.assertIn("没有输入", core.nmea_parse(None))


class TestFrameParse(unittest.TestCase):
    """G08 后半：通用二进制帧切帧。"""

    def test_header_only_slicing(self):
        """只给帧头：按「下一个帧头」切帧。"""
        out = core.frame_parse("AA 55 02 05 06 AA 55 04 01 02 03 04", header="AA55")
        self.assertIn("切出 2 帧", out)
        self.assertIn("#1 @0  帧长 5  载荷 3", out)
        self.assertIn("#2 @5  帧长 7  载荷 5", out)

    def test_length_field_slicing(self):
        """给长度字段：帧长由长度字段算，不靠猜。"""
        out = core.frame_parse("AA 55 04 01 02 03 04 AA 55 02 05 06",
                               header="AA55", length_offset=2, length_size=1)
        self.assertIn("切出 2 帧", out)
        self.assertIn("#1 @0  帧长 7  载荷 4", out)
        self.assertIn("#2 @7  帧长 5  载荷 2", out)

    def test_little_endian_length_field(self):
        out = core.frame_parse("AA 55 03 00 01 02 03", header="AA55",
                               length_offset=2, length_size=2, endian="little")
        self.assertIn("帧长 7  载荷 3", out)

    def test_sum8_verified(self):
        out = core.frame_parse("AA 55 01 02 03 05", header="AA55", checksum="sum8")
        self.assertIn("sum8 ✓", out)

    def test_sum8_mismatch_is_declared(self):
        """★ 校验不过要明说不匹配，并把帧内值/计算值都给出来。"""
        out = core.frame_parse("AA 55 01 02 03 06", header="AA55", checksum="sum8")
        self.assertIn("sum8 ✗ 不匹配", out)
        self.assertIn("帧内 06 / 计算 05", out)

    def test_modbus_crc_frame_verified(self):
        """★ 真实 Modbus RTU 帧：长度字段（字节数）**不含**末尾 CRC。

        切帧时必须把校验字节补进帧长，否则 CRC 会被切掉、永远验不过——
        这是本工具最容易犯的「看起来能用其实全错」的错。
        """
        out = core.frame_parse(MODBUS_RESP, length_offset=2, length_size=1,
                               checksum="crc16_modbus")
        self.assertIn("CRC16 ✓", out)
        self.assertIn("帧长 9  载荷 4", out)

    def test_truncated_frame_is_reported_not_dropped(self):
        """★ 核心判据：帧被截断要说出来，不能悄悄少给一帧。"""
        out = core.frame_parse("AA 55 04 01", header="AA55",
                               length_offset=2, length_size=1)
        self.assertIn("截断", out)
        self.assertIn("不静默丢弃", out)

    def test_garbage_before_header_is_reported(self):
        """帧头前的垃圾字节要被点名，不能默认忽略。"""
        out = core.frame_parse("00 11 22 AA 55 02 05 06", header="AA55")
        self.assertIn("跳过 3 字节垃圾数据", out)

    def test_trailing_bytes_without_header_are_reported(self):
        out = core.frame_parse("AA 55 04 01 02 03 04", header="AA55",
                               length_offset=2, length_size=1,
                               length_includes_header=True)
        self.assertIn("找不到帧头", out)
        self.assertIn("这 3 字节未解析", out)

    def test_no_framing_hint_refuses_to_guess(self):
        """★ 既没帧头也没长度偏移 → 拒绝瞎切，并说清需要什么。"""
        out = core.frame_parse(MODBUS_RESP)
        self.assertIn("需要至少一个切帧依据", out)
        self.assertIn("length_offset", out)

    def test_unknown_checksum_is_refused(self):
        """写错校验方式名不能当没写——否则用户以为校验过了。"""
        out = core.frame_parse("AA 55 01 02", header="AA55", checksum="sum16")
        self.assertIn("只支持", out)
        self.assertIn("sum16", out)

    def test_empty_and_none_input_are_friendly(self):
        """L4 边界：空串 / None 都不能抛异常。"""
        self.assertIn("没有数据（空）", core.frame_parse(""))
        self.assertIn("没有数据（空）", core.frame_parse(None))

    def test_odd_hex_and_bad_chars_are_explained(self):
        self.assertIn("奇数", core.frame_parse("AA 5"))
        self.assertIn("非十六进制字符", core.frame_parse("ZZ 11"))

    def test_negative_length_offset_does_not_crash(self):
        """L4 边界：非法偏移要报错而不是抛异常或死循环。"""
        out = core.frame_parse("01 02 03", length_offset=-1, length_size=0)
        self.assertIn("截断", out)

    def test_max_frames_is_respected(self):
        out = core.frame_parse("AA 55 02 05 06 " * 5, header="AA55", max_frames=2)
        self.assertIn("切出 2 帧", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
