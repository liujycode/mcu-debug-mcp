# -*- coding: utf-8 -*-
"""产物解析器回归（批次 1）：Keil / IAR .map + Intel HEX + 固件段规整 + parse_map 派发。

为什么这族是第一批（TEST_PLAN.md 批次 1）：v4.5.4 的「.map 假函数名」就出在这一族——
解析器一旦静默给错，下游 symbol_lookup / stack_unwind / flash_verify 全部跟着错。
而 Keil / IAR 两族在 2026-10-13 前**一行测试都没有**。

夹具纪律：
    · Keil / IAR 的 map 文本按两家工具链的公开输出格式构造（Image Symbol Table /
      ENTRY LIST 是格式关键字，不是知识库条目）——解析器测试的标准做法；
    · GNU map 用 tests/fixtures/arm_mcu.map（真工具链产物）；
    · 每条错误路径至少一条测试，不只测"正常能跑"。
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcu_debug_mcp import core  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def _rm(path):
    """临时文件清理：环境的安全删除钩子偶尔短暂占用刚写完的文件，
    清不掉就留给系统 TEMP 清理——不为这个让测试误报。"""
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------
# Keil / ARMCC .map
# ---------------------------------------------------------------------
KEIL_MAP = """\
Linking...
Program Size: Code=1234 RO-data=567 RW-data=89 ZI-data=1011
FromELF: creating hex file...
"\\out\\fw.axf" - 0 Error(s), 0 Warning(s).
Image Symbol Table

    Local Symbols
    mpu6050_read                           0x08004a2c   Thumb Code    28  mpu6050.o(.text)
    uart_puts                              0x08004a10   Thumb Code    40  main.o(.text)
    .hidden_section_thing                  0x08004a30   Thumb Code    16  x.o(.text)

Memory Map of the image

  Image Entry point : 0x08000100

  Total RO  Size (Code + RO Data)                12345 (  12.06kB)
  Total RW  Size (RW Data + ZI Data)              5678 (   5.55kB)
  Total ROM Size (Code + RO Data + RW Data)      12389 (  12.10kB)
"""


class TestKeilMapParser(unittest.TestCase):
    """_parse_map_keil：Image Symbol Table 正例 + 点号符号跳过 + 段汇总。"""

    @classmethod
    def setUpClass(cls):
        cls.parsed = core._parse_map_keil(KEIL_MAP)

    def test_format_tagged_keil(self):
        self.assertEqual(self.parsed["format"], "keil")

    def test_symbols_parsed_with_size(self):
        names = {s["name"]: s for s in self.parsed["symbols"]}
        self.assertIn("mpu6050_read", names)
        self.assertEqual(names["mpu6050_read"]["addr"], 0x08004A2C)
        self.assertEqual(names["mpu6050_read"]["size"], 28)
        self.assertEqual(names["uart_puts"]["size"], 40)

    def test_dot_symbols_are_skipped(self):
        """点号开头是段符号不是函数——进符号表会让反查给出段名假答案。"""
        names = [s["name"] for s in self.parsed["symbols"]]
        self.assertNotIn(".hidden_section_thing", names)

    def test_total_sizes_from_summary_lines(self):
        segs = self.parsed["segments"]
        self.assertEqual(segs["Total_RO"]["size"], 12345)
        self.assertEqual(segs["Total_RW"]["size"], 5678)
        self.assertEqual(segs["Total_ROM"]["size"], 12389)

    def test_local_symbols_header_does_not_leak_in(self):
        """"Local Symbols" 行本身不许被当符号解析进去。"""
        names = [s["name"] for s in self.parsed["symbols"]]
        self.assertNotIn("Local", names)
        self.assertNotIn("Symbols", names)

    def test_no_total_lines_gives_empty_segments(self):
        parsed = core._parse_map_keil("Image Symbol Table\n    a  0x08000000  Thumb Code  4  a.o(.text)\n")
        self.assertEqual(parsed["segments"], {})
        self.assertEqual(len(parsed["symbols"]), 1)


# ---------------------------------------------------------------------
# IAR .map
# ---------------------------------------------------------------------
IAR_MAP = """\
##############################################################################
# IAR ELF Linker
#
# Sections
#

*******************************************************************************
*** MODULE SUMMARY ***
    1 234 bytes of readonly code memory
      567 bytes of readwrite data memory

*******************************************************************************
*** ENTRY LIST ***
Entry  Address  Size  Type  Object
-----  -------  ----  ----  ------
mpu6050_read            0x08004a2c   0x1c  Code  Gb  mpu6050.o [1]
uart_puts               0x08004a10   0x28  Code  Gb  main.o [1]
.hidden_iar_thing       0x08004a40   0x10  Code  Lc  x.o [2]
CSTACK$$Base            0x20000010   --    Gb  - Linker created -
"""


class TestIarMapParser(unittest.TestCase):
    """_parse_map_iar：ENTRY LIST 正例 + *** 截断 + bytes of 汇总。

    ★ 夹具按 IAR 官方输出格式构造（IAR MyPages #000001409 与公开教程一致）：
      ``*** ENTRY LIST ***`` 是一行，表头后直接是符号行——
      「Linker created」条目大小列是 ``--``，必须被跳过而不是解析成 0。
    """

    @classmethod
    def setUpClass(cls):
        cls.parsed = core._parse_map_iar(IAR_MAP)

    def test_format_tagged_iar(self):
        self.assertEqual(self.parsed["format"], "iar")

    def test_entry_list_symbols_hex_sizes(self):
        """IAR 的大小是十六进制（0x1c=28）——与 Keil 的十进制不同，解析错了全盘错。"""
        names = {s["name"]: s for s in self.parsed["symbols"]}
        self.assertEqual(names["mpu6050_read"]["addr"], 0x08004A2C)
        self.assertEqual(names["mpu6050_read"]["size"], 0x1C)
        self.assertEqual(names["uart_puts"]["size"], 0x28)

    def test_dot_symbols_skipped(self):
        names = [s["name"] for s in self.parsed["symbols"]]
        self.assertNotIn(".hidden_iar_thing", names)

    def test_linker_created_entries_without_size_are_skipped(self):
        """``--`` 大小的 Linker created 条目没有真实大小，不许以 size=0 混进表。"""
        names = [s["name"] for s in self.parsed["symbols"]]
        self.assertNotIn("CSTACK$$Base", names)

    def test_table_header_and_separator_not_symbols(self):
        names = [s["name"] for s in self.parsed["symbols"]]
        for junk in ("Entry", "-----"):
            self.assertNotIn(junk, names)

    def test_module_summary_line_ends_symbol_collection(self):
        """符号收集在 ENTRY LIST 前的 MODULE SUMMARY 处就该停（此夹具中它在前面）。"""
        names = [s["name"] for s in self.parsed["symbols"]]
        self.assertEqual(len(names), 2)

    def test_bytes_of_summary_lines(self):
        segs = self.parsed["segments"]
        self.assertEqual(segs["readonly_code"]["size"], 1234)
        self.assertEqual(segs["readwrite_data"]["size"], 567)


# ---------------------------------------------------------------------
# parse_map 派发层
# ---------------------------------------------------------------------
class TestParseMapDispatch(unittest.TestCase):
    """parse_map：三种格式识别 + 认不出不猜 + ELF + 各输出段。"""

    def _write(self, text, suffix=".map"):
        fd, p = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(_rm, p)
        return p

    def test_missing_file_is_friendly(self):
        out = core.parse_map(r"Z:\no\such\file.map")
        self.assertIn("文件不存在", out)

    def test_empty_file(self):
        out = core.parse_map(self._write(""))
        self.assertIn("文件是空的", out)

    def test_unknown_format_refuses_to_guess(self):
        """认不出就明说——不猜是本函数 docstring 写明的纪律。"""
        out = core.parse_map(self._write("hello world\nnothing useful here\n"))
        self.assertIn("认不出这个 .map 的格式", out)
        self.assertIn("没有猜", out)
        self.assertIn("hello world", out)          # 附前几行供确认

    def test_keil_file_dispatches_to_keil(self):
        out = core.parse_map(self._write(KEIL_MAP))
        self.assertIn("（格式：KEIL）", out)
        self.assertIn("mpu6050_read", out)

    def test_iar_file_dispatches_to_iar(self):
        out = core.parse_map(self._write(IAR_MAP))
        self.assertIn("（格式：IAR）", out)
        self.assertIn("uart_puts", out)

    def test_gnu_real_fixture_dispatches_to_gnu(self):
        """真工具链产物（tests/fixtures/arm_mcu.map）走 GNU 解析。"""
        out = core.parse_map(os.path.join(FIXTURES, "arm_mcu.map"))
        self.assertIn("（格式：GNU）", out)

    def test_elf_that_fails_parse_explains(self):
        """ELF 魔数在但结构是垃圾 → 明说解析失败并指向 readelf，不吐二进制乱码。"""
        fd, p = tempfile.mkstemp(suffix=".elf")
        with os.fdopen(fd, "wb") as f:
            f.write(b"\x7fELF" + b"\x00" * 64)
        self.addCleanup(_rm, p)
        out = core.parse_map(p)
        self.assertIn("ELF", out)
        self.assertIn("解析失败", out)

    def test_keil_with_no_symbols_says_trimmed(self):
        """识别出格式但啥都没解析出来 → 明说可能被裁剪，不硬给空表。"""
        out = core.parse_map(self._write("Image Symbol Table\n\nMemory Map of the image\n"))
        self.assertIn("没解析出任何段或符号", out)

    def test_host_program_warning(self):
        """含 ≥3 个主机运行时符号 → 大字提醒这不是 MCU 固件的 map。"""
        text = ("Image Symbol Table\n"
                "    __imp_GetLastError      0x00401000   Thumb Code    8  a.obj(.text)\n"
                "    __acrt_iob_func         0x00401010   Thumb Code    8  a.obj(.text)\n"
                "    __mingw_dummy           0x00401020   Thumb Code    8  a.obj(.text)\n"
                "\nMemory Map of the image\n"
                "  Total RO  Size (Code + RO Data)   100 ( 0.10kB)\n")
        out = core.parse_map(self._write(text))
        self.assertIn("【重要提醒】", out)
        self.assertIn("不是 MCU 固件", out)

    def test_suspect_warning_single_hit(self):
        """只有 1 个主机特征符号 → 降级为 △ 提示，不上来就吓人。"""
        text = ("Image Symbol Table\n"
                "    __mingw_only_one        0x00401000   Thumb Code    8  a.obj(.text)\n"
                "    normal_app_func         0x08004000   Thumb Code    8  a.obj(.text)\n"
                "\nMemory Map of the image\n"
                "  Total RO  Size (Code + RO Data)   100 ( 0.10kB)\n")
        out = core.parse_map(self._write(text))
        self.assertIn("△ 提示", out)
        self.assertNotIn("【重要提醒】", out)

    def test_duplicate_names_are_flagged(self):
        text = ("Image Symbol Table\n"
                "    func_a                  0x08000100   Thumb Code    8  a.o(.text)\n"
                "    func_a                  0x08000200   Thumb Code    8  b.o(.text)\n"
                "\nMemory Map of the image\n"
                "  Total RO  Size (Code + RO Data)   100 ( 0.10kB)\n")
        out = core.parse_map(self._write(text))
        self.assertIn("同名多地址符号", out)
        self.assertIn("func_a", out)

    def test_stack_symbols_listed(self):
        text = ("Image Symbol Table\n"
                "    _Min_Stack_Size         0x08000000   Data        1024  startup.o(.data)\n"
                "\nMemory Map of the image\n"
                "  Total RO  Size (Code + RO Data)   100 ( 0.10kB)\n")
        out = core.parse_map(self._write(text))
        self.assertIn("栈/堆相关符号", out)
        self.assertIn("_Min_Stack_Size", out)

    def test_top_n_limits_symbol_list(self):
        syms = "".join(
            "    sym_%02d                    0x0800%04x   Thumb Code    %d  a.o(.text)\n"
            % (i, 0x100 + i * 0x10, 8 + i)
            for i in range(30))
        text = ("Image Symbol Table\n" + syms +
                "\nMemory Map of the image\n"
                "  Total RO  Size (Code + RO Data)   100 ( 0.10kB)\n")
        out = core.parse_map(self._write(text), top_n=5)
        self.assertIn("占用最大的 5 个符号", out)
        self.assertNotIn("占用最大的 30 个", out)

    def test_app_symbols_rank_before_runtime(self):
        """大缓冲在应用代码里——排序必须让用户的 buf 压过库符号。"""
        text = ("Image Symbol Table\n"
                "    __libc_giant_thing      0x08001000   Thumb Code  9000  lib.o(.text)\n"
                "    my_big_buffer           0x08003000   Thumb Code  8000  app.o(.text)\n"
                "\nMemory Map of the image\n"
                "  Total RO  Size (Code + RO Data)   100 ( 0.10kB)\n")
        out = core.parse_map(self._write(text))
        head = out.split("占用最大的", 1)[1]
        self.assertLess(head.index("my_big_buffer"), head.index("__libc_giant_thing"),
                        "应用符号应排在运行时库符号前面")
        self.assertIn("运行时/库符号", out)

    def test_address_range_line(self):
        out = core.parse_map(self._write(KEIL_MAP))
        # Python 的 hex() 会去掉前导零：0x08004a2c → '0x8004a2c'
        self.assertIn("地址范围：", out)
        self.assertIn("0x8004a2c", out)

    def test_gnu_map_uses_real_fixture_symbols(self):
        """GNU map（真产物）必须能给出符号与下一步指引。"""
        out = core.parse_map(os.path.join(FIXTURES, "arm_mcu.map"))
        self.assertIn("下一步", out)
        self.assertIn("symbol_lookup", out)


# ---------------------------------------------------------------------
# Intel HEX 严格解析
# ---------------------------------------------------------------------
def _ihex_line(rec_type, payload, addr=0x0000):
    """手工组装一条 Intel HEX 记录（独立于被测代码，含真校验和）。"""
    n = len(payload)
    raw = bytes([n, (addr >> 8) & 0xFF, addr & 0xFF, rec_type]) + payload
    ck = (-sum(raw)) & 0xFF
    return ":%02X%04X%02X%s%02X" % (n, addr, rec_type, payload.hex().upper(), ck)


class TestParseIhex(unittest.TestCase):
    """_parse_ihex：每条错误路径都要一条测试——烧录校验放过一条坏记录等于放过坏扇区。"""

    def test_single_record_roundtrip(self):
        text = _ihex_line(0x00, b"\x01\x02\x03", addr=0x0800) + "\n" + _ihex_line(0x01, b"")
        segs = core._parse_ihex(text)
        self.assertEqual(segs, [(0x0800, b"\x01\x02\x03")])

    def test_adjacent_records_merge(self):
        text = (_ihex_line(0x00, b"\xaa\xbb", addr=0x1000) + "\n"
                + _ihex_line(0x00, b"\xcc\xdd", addr=0x1002) + "\n"
                + _ihex_line(0x01, b""))
        segs = core._parse_ihex(text)
        self.assertEqual(segs, [(0x1000, b"\xaa\xbb\xcc\xdd")])

    def test_gap_between_records_keeps_two_segments(self):
        text = (_ihex_line(0x00, b"\x11", addr=0x1000) + "\n"
                + _ihex_line(0x00, b"\x22", addr=0x2000) + "\n"
                + _ihex_line(0x01, b""))
        self.assertEqual(len(core._parse_ihex(text)), 2)

    def test_linear_address_record_type04(self):
        """类型 04 = 线性基址（<<16）：0x0800 → 0x08000000。"""
        text = (_ihex_line(0x04, b"\x08\x00") + "\n"
                + _ihex_line(0x00, b"\xaa", addr=0x0000) + "\n"
                + _ihex_line(0x01, b""))
        segs = core._parse_ihex(text)
        self.assertEqual(segs[0][0], 0x08000000)

    def test_segment_address_record_type02(self):
        """类型 02 = 段基址（<<4）：0x1234 → 0x12340。"""
        text = (_ihex_line(0x02, b"\x12\x34") + "\n"
                + _ihex_line(0x00, b"\xaa", addr=0x0000) + "\n"
                + _ihex_line(0x01, b""))
        segs = core._parse_ihex(text)
        self.assertEqual(segs[0][0], 0x12340)

    def test_start_address_records_ignored(self):
        """类型 03/05 是起始地址记录，与内容地址无关——不许当数据段。"""
        text = (_ihex_line(0x05, b"\x08\x00\x00\xf9") + "\n"
                + _ihex_line(0x00, b"\xaa", addr=0x1000) + "\n"
                + _ihex_line(0x01, b""))
        segs = core._parse_ihex(text)
        self.assertEqual(segs, [(0x1000, b"\xaa")])

    def test_empty_data_record_type00_skipped(self):
        text = (_ihex_line(0x00, b"", addr=0x1000) + "\n"
                + _ihex_line(0x00, b"\xaa", addr=0x1000) + "\n"
                + _ihex_line(0x01, b""))
        self.assertEqual(core._parse_ihex(text), [(0x1000, b"\xaa")])

    def test_eof_stops_parsing(self):
        """EOF 之后的行（哪怕是垃圾）也不许再解析。"""
        text = (_ihex_line(0x00, b"\xaa", addr=0x0) + "\n"
                + _ihex_line(0x01, b"") + "\n"
                + "not a record at all\n")
        self.assertEqual(core._parse_ihex(text), [(0x0000, b"\xaa")])

    # ---- 错误路径：每条 ValueError 都必须有对应触发 ----
    def test_line_without_colon_rejected(self):
        with self.assertRaises(ValueError) as cm:
            core._parse_ihex("ZZ0000\n")
        self.assertIn("不是 Intel HEX 记录", str(cm.exception))

    def test_non_hex_content_rejected(self):
        with self.assertRaises(ValueError) as cm:
            core._parse_ihex(":GGGG\n")
        self.assertIn("不是合法十六进制", str(cm.exception))

    def test_too_short_rejected(self):
        with self.assertRaises(ValueError) as cm:
            core._parse_ihex(":0000\n")
        self.assertIn("太短", str(cm.exception))

    def test_length_field_mismatch_rejected(self):
        """长度字段说 2 实际只有 1 字节数据——这种 hex 是抄错的。"""
        bad = ":0200000010AA"          # n=2 但只带 1 字节数据，校验和也按错的来
        with self.assertRaises(ValueError) as cm:
            core._parse_ihex(bad + "\n")
        self.assertIn("长度字段", str(cm.exception))

    def test_bad_checksum_rejected(self):
        line = _ihex_line(0x00, b"\xaa", addr=0x1000)
        broken = line[:-2] + ("00" if line[-2:] != "00" else "01")
        with self.assertRaises(ValueError) as cm:
            core._parse_ihex(broken + "\n" + _ihex_line(0x01, b""))
        self.assertIn("校验失败", str(cm.exception))

    def test_unknown_record_type_rejected(self):
        with self.assertRaises(ValueError) as cm:
            core._parse_ihex(_ihex_line(0x0A, b"\x00\x00") + "\n")
        self.assertIn("不认识的记录类型", str(cm.exception))

    def test_empty_text_gives_no_segments(self):
        self.assertEqual(core._parse_ihex(""), [])
        self.assertEqual(core._parse_ihex(None), [])


class TestFirmwareSegments(unittest.TestCase):
    """_firmware_segments：.bin / .hex / .elf 三路 + 各种坏输入。"""

    def test_bin_default_base_is_stm32_flash(self):
        fd, p = tempfile.mkstemp(suffix=".bin")
        with os.fdopen(fd, "wb") as f:
            f.write(b"\x00\xbf")
        self.addCleanup(_rm, p)
        segs, note = core._firmware_segments(p)
        self.assertEqual(segs, [(0x08000000, b"\x00\xbf")])
        self.assertEqual(note, "")

    def test_bin_custom_base(self):
        fd, p = tempfile.mkstemp(suffix=".bin")
        with os.fdopen(fd, "wb") as f:
            f.write(b"\xaa")
        self.addCleanup(_rm, p)
        segs, _ = core._firmware_segments(p, base_addr="0x08010000")
        self.assertEqual(segs[0][0], 0x08010000)

    def test_bin_empty_rejected(self):
        fd, p = tempfile.mkstemp(suffix=".bin")
        self.addCleanup(_rm, p)
        with self.assertRaises(ValueError) as cm:
            core._firmware_segments(p)
        self.assertIn("空的", str(cm.exception))

    def test_hex_with_no_data_records_rejected(self):
        fd, p = tempfile.mkstemp(suffix=".hex")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_ihex_line(0x01, b"") + "\n")
        self.addCleanup(_rm, p)
        with self.assertRaises(ValueError) as cm:
            core._firmware_segments(p)
        self.assertIn("没有任何数据记录", str(cm.exception))

    def test_hex_real_addresses_used(self):
        fd, p = tempfile.mkstemp(suffix=".hex")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_ihex_line(0x04, b"\x08\x00") + "\n"
                    + _ihex_line(0x00, b"\xaa\xbb", addr=0x0100) + "\n"
                    + _ihex_line(0x01, b"") + "\n")
        self.addCleanup(_rm, p)
        segs, _ = core._firmware_segments(p)
        self.assertEqual(segs[0][0], 0x08000100)

    def test_elf_garbage_rejected(self):
        fd, p = tempfile.mkstemp(suffix=".elf")
        with os.fdopen(fd, "wb") as f:
            f.write(b"\x7fELF" + b"\x00" * 32)
        self.addCleanup(_rm, p)
        with self.assertRaises(ValueError) as cm:
            core._firmware_segments(p)
        self.assertIn("解析不了", str(cm.exception))

    def test_unknown_extension_rejected(self):
        fd, p = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("hello")
        self.addCleanup(_rm, p)
        with self.assertRaises(ValueError) as cm:
            core._firmware_segments(p)
        self.assertIn("只支持 .bin / .hex / .elf", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
