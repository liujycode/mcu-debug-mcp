# -*- coding: utf-8 -*-
"""真机实测发现固化（2026-09-22，NUCLEO-H563ZI / STM32H563 @ COM21）。

本文件每一条都对应一次**在真实硬件上跑出来**的问题，不是想象的边界。
留在这里是为了防回退 —— 这些坑的共同点是「离线单测永远碰不到」：

  D1  ST-Link V3 板载 VCP（0483:374E）曾被报「未收录，装对应厂商驱动」
      —— 对一块官方 ST 板 + 官方调试器，这条建议是错的（它随 ST-Link 驱动免驱）。
  D2  pip 安装态下会话日志写进了安装目录（site-packages 通常也可写）
      —— 卸载残留 / 多 venv 互相串 / 只读镜像静默失败都从这里来。
  D3  flash_tool_detect() 漏报不在 PATH 的工具
      —— 本机装了 STM32_Programmer_CLI(×2) / pyocd / openocd，却报「无」，
         还让用户去装 stlink。
  D4  serial_send 默认补 CRLF、at_sequence 硬编码 CRLF
      —— 对「逐字节轮询接收」的固件，多发的字节触发 UART 溢出(ORE)，
         接收端永久僵死（实测：单字节反复正常，"1\\r\\n" 只成功第一次）。
  D5  .elf 曾被当成「认不出的 map」，还把 355KB 二进制当「前几行」打给用户
      —— 而文档一直写着 symbol_lookup 接受 .elf。真解析后发现还有 Thumb 位问题。
  D6  ARM 固件里的 __do_global_dtors_aux 被当成「主机程序特征」
      —— 该符号 arm-none-eabi-gcc 的 crtstuff 也会生成（实测固件里就有），
         收进主机判据会把正常 STM32 固件误判。
"""
import inspect
import io
import json
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import analyzer  # noqa: E402
import core      # noqa: E402
import paths     # noqa: E402


# ---------------------------------------------------------------------------
# 测试用最小 ELF32（Cortex-M 风格）构造器
# ---------------------------------------------------------------------------
def _make_elf32(symbols, text_addr=0x08000000, text_size=0x400):
    """造一个最小可用的 ELF32 LE（EM_ARM），含 .text/.symtab/.strtab/.shstrtab。

    symbols: [(name, addr, size, is_func)]
    ★ 刻意不依赖本机 arm-none-eabi 工具链：CI 上没有它。
    """
    shstr = bytearray(b"\0")

    def sh_name(n):
        off = len(shstr)
        shstr.extend(n.encode() + b"\0")
        return off

    i_text = sh_name(".text")
    i_symtab = sh_name(".symtab")
    i_strtab = sh_name(".strtab")
    i_shstrtab = sh_name(".shstrtab")

    strtab = bytearray(b"\0")

    def st_name(n):
        off = len(strtab)
        strtab.extend(n.encode() + b"\0")
        return off

    sym = bytearray(b"\0" * 16)          # sym[0] = 保留的 NULL 符号
    for name, addr, size, is_func in symbols:
        nm = st_name(name)
        info = (1 << 4) | (2 if is_func else 1)   # GLOBAL | (FUNC|OBJECT)
        sym.extend(struct.pack("<IIIBBH", nm, addr, size, info, 0, 1))

    ehsize, shentsize, shnum = 52, 40, 5
    text_off = ehsize
    symtab_off = text_off + text_size
    strtab_off = symtab_off + len(sym)
    shstrtab_off = strtab_off + len(strtab)
    shoff = shstrtab_off + len(shstr)

    shs = b"\0" * 40
    shs += struct.pack("<IIIIIIIIII", i_text, 1, 6, text_addr, text_off,
                       text_size, 0, 0, 4, 0)
    shs += struct.pack("<IIIIIIIIII", i_symtab, 2, 0, 0, symtab_off,
                       len(sym), 3, 1, 4, 16)     # link = 3 (.strtab)
    shs += struct.pack("<IIIIIIIIII", i_strtab, 3, 0, 0, strtab_off,
                       len(strtab), 0, 0, 1, 0)
    shs += struct.pack("<IIIIIIIIII", i_shstrtab, 3, 0, 0, shstrtab_off,
                       len(shstr), 0, 0, 1, 0)

    ehdr = b"\x7fELF" + bytes([1, 1, 1, 0, 0]) + b"\0" * 7
    ehdr += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, text_addr, 0, shoff,
                        0x05000000, ehsize, 0, 0, shentsize, shnum, 4)

    data = bytes(ehdr) + b"\0" * text_size + bytes(sym) + bytes(strtab) + bytes(shstr) + shs
    assert len(data) == shoff + len(shs)
    return data


class _TmpFile(object):
    """with 语句里落一个临时文件。"""

    def __init__(self, data, suffix=".elf", binary=True):
        self.data, self.suffix, self.binary = data, suffix, binary
        self.path = None

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(suffix=self.suffix)
        os.close(fd)
        mode = "wb" if self.binary else "w"
        kw = {} if self.binary else {"encoding": "utf-8", "newline": ""}
        with open(self.path, mode, **kw) as fh:
            fh.write(self.data)
        return self.path

    def __exit__(self, *a):
        try:
            os.remove(self.path)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# D1 —— ST-Link V3 的 VCP
# ---------------------------------------------------------------------------
class TestUsbSerialIdentify(unittest.TestCase):
    def test_stlink_v3_vcp_is_identified(self):
        """0483:374E 必须认出来，且建议里不能出现「装串口驱动」。"""
        got = analyzer.identify_usb_serial("0483:374E")
        self.assertIsNotNone(got, "0483:374E 是 ST-Link V3 的 VCP，不该落入「未收录」")
        name, advice = got
        self.assertIn("ST-Link", name)
        self.assertIn("免驱", advice)
        self.assertNotIn("装对应厂商驱动", advice)

    def test_stlink_any_pid_falls_back(self):
        """0483 下任何没逐条录入的 PID 都要兜底成 ST-Link，别让新板子退化成「未收录」。"""
        name, _advice = analyzer.identify_usb_serial("0483:ABCD")
        self.assertIn("ST-Link", name)

    def test_unknown_vid_still_none(self):
        """★ 反例：非 0483 的未收录 ID 仍然返回 None，别把兜底做成一锅粥。"""
        self.assertIsNone(analyzer.identify_usb_serial("DEAD:BEEF"))
        self.assertIsNone(analyzer.identify_usb_serial(""))
        self.assertIsNone(analyzer.identify_usb_serial(None))

    def test_known_entries_still_win(self):
        """表里明确有的条目优先于兜底。"""
        name, _ = analyzer.identify_usb_serial("1A86:7523")
        self.assertIn("CH340", name)


# ---------------------------------------------------------------------------
# D2 —— 数据目录不许是程序目录
# ---------------------------------------------------------------------------
class TestDataRootSeparation(unittest.TestCase):
    def test_writable_root_is_not_package_dir(self):
        """包目录即使可写也不能当选数据根（否则 pip 安装态日志落 site-packages）。"""
        old = os.environ.pop(paths.ENV_HOME, None)
        paths.reset_cache()
        try:
            wr = paths.writable_root()
        finally:
            if old is not None:
                os.environ[paths.ENV_HOME] = old
            paths.reset_cache()
        self.assertNotEqual(
            os.path.normcase(wr), os.path.normcase(paths.PKG_DIR),
            "数据根落到了包目录：%s" % wr)

    def test_env_home_wins(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get(paths.ENV_HOME)
            os.environ[paths.ENV_HOME] = d
            paths.reset_cache()
            try:
                self.assertEqual(paths.writable_root(), os.path.abspath(d))
                self.assertTrue(paths.logs_dir().startswith(os.path.abspath(d)))
            finally:
                if old is None:
                    os.environ.pop(paths.ENV_HOME, None)
                else:
                    os.environ[paths.ENV_HOME] = old
                paths.reset_cache()

    def test_is_source_tree_detects_repo(self):
        """本仓是源码树（有 pyproject.toml）——它只影响兜底顺序，不影响数据根选择。"""
        self.assertTrue(paths.is_source_tree())


# ---------------------------------------------------------------------------
# D3 —— 烧录工具探测（不能只看 PATH）
# ---------------------------------------------------------------------------
class TestFlashToolDetection(unittest.TestCase):
    def test_detects_tool_outside_path(self):
        """IDE/扩展自带的工具不在 PATH，也要能搜到（真机实测漏报过）。"""
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "STM32_Programmer_CLI.exe")
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write("")
            saved = core._TOOL_SEARCH_DIRS
            core._TOOL_SEARCH_DIRS = [d]
            try:
                self.assertEqual(core._which_tool("STM32_Programmer_CLI"), fake)
                self.assertIn("STM32_Programmer_CLI", core.flash_tool_detect())
            finally:
                core._TOOL_SEARCH_DIRS = saved

    def test_found_detects_real_tool_when_absent(self):
        """搜不到就给「搜过哪些位置」的说明，不许只丢一句「没有」。"""
        saved = core._TOOL_SEARCH_DIRS
        core._TOOL_SEARCH_DIRS = [os.path.join(tempfile.gettempdir(), "definitely-not-here-xyz")]
        try:
            core.flash_tool_detect()
            hint = core.flash_tool_hint()
        finally:
            core._TOOL_SEARCH_DIRS = saved
        self.assertIn("已搜过", hint)
        self.assertIn("STM32CubeProgrammer", hint)

    def test_stm32_cli_in_tool_list(self):
        """STM32_Programmer_CLI 必须留在候选名单里（它才是 STM32 实际在用的那个）。"""
        self.assertIn("STM32_Programmer_CLI", core._FLASH_TOOLS)


# ---------------------------------------------------------------------------
# D4 —— CRLF / ORE
# ---------------------------------------------------------------------------
class TestOreTrapHints(unittest.TestCase):
    def setUp(self):
        import serial  # noqa: F401  确保依赖在（本类需要 loop://）
        core._SESSIONS.clear()

    def tearDown(self):
        for h in list(core._SESSIONS):
            core.serial_close(h)
        core._SESSIONS.clear()

    def _open_loop(self):
        r = core.serial_open("loop://")
        import re as _re
        m = _re.search(r"handle[= ]+(\d+)", r)
        self.assertIsNotNone(m, "loop:// 打不开：%s" % r)
        return int(m.group(1))

    def test_hint_when_expect_missed_with_auto_crlf(self):
        """★ 自动补了 CRLF 且 expect 未命中 → 必须提示 ORE 陷阱。

        真机场景：固件回了 "?"（所以「有响应」分支也会走），然后设备就僵死了。
        提示必须在这种「有响应但没命中」的情况下也出现，否则永远看不到。
        """
        h = self._open_loop()
        out = core.serial_send(h, "x", expect="绝对不会出现的字符串", wait_s=0.3)
        self.assertIn("另一常见原因", out)
        self.assertIn("ORE", out)
        self.assertIn("newline=False", out)

    def test_no_hint_when_newline_disabled(self):
        """★ 反例：newline=False 时不该出现这段提示（没有多余字节，别乱指方向）。"""
        h = self._open_loop()
        out = core.serial_send(h, "x", newline=False,
                               expect="绝对不会出现的字符串", wait_s=0.3)
        self.assertNotIn("另一常见原因", out)

    def test_no_hint_when_matched(self):
        """★ 反例：命中了就别啰嗦。"""
        h = self._open_loop()
        out = core.serial_send(h, "OK", expect="OK", wait_s=0.3)
        self.assertIn("已命中", out)
        self.assertNotIn("另一常见原因", out)

    def test_no_hint_when_no_expect(self):
        """★ 反例：没给 expect 就没有「未命中」可言，不该提示。"""
        h = self._open_loop()
        out = core.serial_send(h, "x", wait_s=0.2)
        self.assertNotIn("另一常见原因", out)

    def test_at_sequence_exposes_newline(self):
        """at_sequence 必须暴露 newline —— 以前写死 +"\\r\\n"，用户无法规避死锁。"""
        self.assertIn("newline", inspect.signature(core.at_sequence).parameters)

    def test_at_sequence_no_hardcoded_crlf(self):
        """源码里不许再出现写死的 (cmd + "\\r\\n")。"""
        src = io.open(os.path.join(ROOT, "core.py"), encoding="utf-8").read()
        self.assertNotIn('s.write((cmd + "\\r\\n").encode())', src)
        self.assertIn('step.get("newline", newline)', src)

    def test_at_sequence_newline_false_over_loopback(self):
        """newline=False 时，loopback 上应当收到不含自动 CRLF 的命令。

        ★ 2026-10-13：cmd 自带 "\\r\\n" —— newline=False 的真实用法就是
          「终止符我自己给」。此前 cmd="AAA" 不带换行也能 PASS，靠的是
          _at_line_match 把未终结的半行当整行（半包假 PASS，已修）；
          现在整行匹配只认被 \\n 终结的行，命令就得自己带终止符。
        """
        res = core.at_sequence("loop://", [{"cmd": "AAA\r\n", "expect": "AAA",
                                            "timeout_s": 1.0}],
                               newline=False)
        self.assertIn("PASS", res)

    def test_at_line_match_ignores_unterminated_tail(self):
        """半包假 PASS 钉子（2026-10-13）：未终结的尾段不是整行。

        缓冲以 "OK" 结尾、\\r\\n 还在线上时，旧版判 PASS——设备实际要回
        "OKX" 或 "ERROR" 也会中。现在必须等换行终结；设备不吐换行的场合
        用 loose=True 显式放宽（比假通过诚实）。
        """
        import re
        rx = re.compile("OK")
        self.assertFalse(core._at_line_match("log line\r\nOK", rx)[0],
                         "半行 OK 被当成整行命中——半包假 PASS 复发")
        self.assertTrue(core._at_line_match("log line\r\nOK\r\n", rx)[0])
        self.assertEqual(core._at_line_match("log line\r\nOK\r\n", rx)[1], "OK")
        # loose 仍是显式逃生门
        self.assertTrue(core._at_line_match("log line\r\nOK", rx, loose=True)[0])


# ---------------------------------------------------------------------------
# D5 —— .elf 支持 + Thumb 位
# ---------------------------------------------------------------------------
class TestElfParsing(unittest.TestCase):
    SYMS = [
        ("frame_dummy", 0x08000274 | 1, 0, True),          # 带 Thumb 位、size 0
        ("SystemClock_Config", 0x08000290 | 1, 0x9C, True),
        ("Error_Handler", 0x08000330 | 1, 2, True),
        ("big_buf", 0x20000000, 4096, False),
    ]

    def test_is_elf(self):
        with _TmpFile(_make_elf32(self.SYMS)) as p:
            self.assertTrue(core._is_elf(p))
        with _TmpFile("Memory Configuration\n", suffix=".map", binary=False) as p:
            self.assertFalse(core._is_elf(p))

    def test_thumb_bit_stripped(self):
        """★ FUNC 符号的 Thumb 位必须清掉：不清会让整张函数表偏 1 字节。"""
        with _TmpFile(_make_elf32(self.SYMS)) as p:
            parsed = core._parse_elf(p)
        self.assertIsNotNone(parsed)
        by = {s["name"]: s for s in parsed["symbols"]}
        self.assertEqual(by["SystemClock_Config"]["addr"], 0x08000290)
        self.assertEqual(by["frame_dummy"]["addr"], 0x08000274)

    def test_symbol_lookup_on_elf(self):
        """反查必须命中正确函数 —— 真机现场就是这个例子。"""
        with _TmpFile(_make_elf32(self.SYMS)) as p:
            out = core.symbol_lookup(p, ["0x08000290", "0x08000330"])
        self.assertIn("SystemClock_Config", out)
        self.assertIn("Error_Handler", out)

    def test_elf_size_used_for_bounds(self):
        """★ .elf 的价值：带真实 size，越界要能判出来（.map 只能估计）。"""
        with _TmpFile(_make_elf32(self.SYMS)) as p:
            out = core.symbol_lookup(p, ["0x08000333"])   # Error_Handler 只有 2 字节
        self.assertIn("超出", out)

    def test_parse_map_accepts_elf(self):
        """parse_map 现在要认 .elf（以前是「认不出的格式」）。"""
        with _TmpFile(_make_elf32(self.SYMS)) as p:
            out = core.parse_map(p, top_n=2)
        self.assertIn("ELF", out)
        self.assertIn("SystemClock_Config", out)
        self.assertNotIn("认不出", out)

    def test_binary_not_dumped_as_text(self):
        """★ 认不出时也不许把二进制当「前几行」打给用户（以前就是这样）。"""
        with _TmpFile(b"\x7fELF\x02\x01" + os.urandom(200)) as p:
            out = core.parse_map(p)
        self.assertLess(len(out), 600, "输出过短说明没把二进制倒出来")
        self.assertNotIn("\x00", out)

    def test_text_map_not_treated_as_elf(self):
        """★ 反例：普通 .map 不该走 ELF 分支。"""
        self.assertFalse(core._is_elf(os.path.join(ROOT, "README.md")))


# ---------------------------------------------------------------------------
# D6 —— 主机程序判据（不许误伤 ARM 固件）
# ---------------------------------------------------------------------------
class TestHostTargetCheck(unittest.TestCase):
    def test_arm_runtime_symbols_do_not_trigger(self):
        """★ __do_global_dtors_aux 是 ARM 也有的，不能当主机特征（实测误报过）。"""
        syms = [{"name": "__do_global_dtors_aux", "addr": 0x8000250, "size": 10},
                {"name": "__do_global_dtors_aux_fini_array_entry", "addr": 0x800b974, "size": 4}]
        segs = {".text": {"name": ".text", "addr": 0x8000000, "size": 0x100}}
        level, _ = core._map_target_check(segs, syms)
        self.assertIsNone(level)

    def test_debug_frame_does_not_trigger(self):
        """★ .debug_frame 在 ARM ELF 里是常规段。"""
        segs = {".debug_frame": {"name": ".debug_frame", "addr": 0, "size": 100}}
        level, _ = core._map_target_check(segs, [])
        self.assertIsNone(level)

    def test_host_signatures_still_detected(self):
        """★ 反向：真正的主机特征必须继续报出来，别把守卫改废了。"""
        syms = [{"name": "__mingw_vfprintf", "addr": 0x401000, "size": 10},
                {"name": "GetLastError", "addr": 0x401010, "size": 10},
                {"name": "LdrpInitialize", "addr": 0x401020, "size": 10}]
        segs = {".idata": {"name": ".idata", "addr": 0x401000, "size": 0x100}}
        level, reasons = core._map_target_check(segs, syms)
        self.assertEqual(level, "host")
        self.assertTrue(reasons)


# ---------------------------------------------------------------------------
# 知识库：新规则必须真的能用
# ---------------------------------------------------------------------------
class TestOreKnowledgeRule(unittest.TestCase):
    def _rule(self):
        p = os.path.join(ROOT, "knowledge", "fault_patterns.json")
        with io.open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        for r in d["rules"]:
            if r["id"] == "UART_ORE_RX_LATCH":
                return r
        return None

    def test_rule_exists_with_source(self):
        r = self._rule()
        self.assertIsNotNone(r, "真机实测的 ORE 规则没了")
        self.assertTrue(r.get("source"), "知识库条目必须有出处")
        self.assertIn("NUCLEO-H563ZI", r["source"], "出处要写明是哪次实测")
        self.assertTrue(r.get("next_steps"))

    def test_rule_matches_ore_log(self):
        out = core.analyze_log("UART Error: ORE detected\nHAL_UART_Receive failed: HAL_BUSY")
        self.assertIn("UART_ORE_RX_LATCH", out)

    def test_rule_silent_on_normal_log(self):
        """★ 反例：真机正常输出不能触发它（否则等于天天误报）。"""
        out = core.analyze_log("PWR OFF\r\nPWR ON\r\nPWR OFF\r\n")
        self.assertNotIn("UART_ORE_RX_LATCH", out)

    def test_ore_pattern_does_not_match_words_containing_ore(self):
        """★ 反例：`\\bORE\\b` 不该命中 STORE / BEFORE 这类词。"""
        import re
        pats = [re.compile(p) for p in self._rule()["patterns"]]
        text = "STORE the value BEFORE you read it"
        hits = [p.pattern for p in pats if p.search(text)]
        self.assertEqual(hits, [], "ORE 的词边界没写对，命中了 %s" % hits)


if __name__ == "__main__":
    unittest.main()
