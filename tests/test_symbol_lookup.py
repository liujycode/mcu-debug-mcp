# -*- coding: utf-8 -*-
"""符号反查 / 栈回溯 / ELF 解析的回归保护——**夹具是真工具链产出，不手编**。

★ 为什么这组测试用真 gcc 而不是手写几行 map 文本：
  2026-10-13 首次给 core.py 跑覆盖率，发现 ``_parse_elf`` 121 行里 22 行、
  ``_parse_map_keil`` 26/39、``_parse_map_iar`` 21/32 从未被执行过——这些
  是 advertised 能力（README 写"支持 .elf"），却是零回归保护。

  更关键的是真文件立刻暴露了一个文档没说的陷阱：**GNU ld 默认不往 .map 里
  写符号表**。同一个固件，fw.map 只解析出 3 个符号（含 g_vectors 这种段符
  号），fw.elf 有 31 个。拿 map 反查 0x08000054 会答「g_vectors（偏移 0x54）」
  ——真实函数是 uart_puts。**假名字比没名字更害人**，而原输出只有一句泛泛的
  "可能不准"，用户会直接采信。

  所以夹具必须来自真工具链：本文件用仓库里**已入库**的 ``bridge_fw/main.c``
  + ``linker.ld`` 现场编译（复用 bridge_fw/build.py 已有的工具链探测，
  不另写一套）。产物是真 Cortex-M33 ELF，与用户手上烧进板子的东西同构。

工具链缺失时整组 skip —— CI 上没有 arm-none-eabi-gcc 也能过，
这与既有"真机用例默认 skip"的规矩一致。
"""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from mcu_debug_mcp import core  # noqa: E402  (src 由 conftest 注入)

BRIDGE_DIR = os.path.join(ROOT, "src", "mcu_debug_mcp", "tools", "bridge_fw")


def _load_build_py():
    """复用 bridge_fw/build.py 的工具链探测（环境变量 > PATH > 常见安装位置）。

    不自己再写一遍探测逻辑：两处各写一套，迟早会有一处忘了更新路径。
    """
    p = os.path.join(BRIDGE_DIR, "build.py")
    spec = importlib.util.spec_from_file_location("_bridge_build", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_arm_gcc():
    try:
        d = _load_build_py()._find_gcc_bin()
    except Exception:
        return None
    return d if d and os.path.exists(os.path.join(d, "arm-none-eabi-gcc.exe")) else None


def _find_cmsis():
    try:
        b = _load_build_py()
    except Exception:
        return None, None
    return b._find_cmsis("MCUDBG_CMSIS_CORE"), b._find_cmsis("MCUDBG_CMSIS_DEV")


_FW = {}


def build_firmware(tmpdir):
    """编译仓库自带的桥固件，返回 (elf, map) 路径。失败抛 RuntimeError。"""
    gcc_dir = _find_arm_gcc()
    if not gcc_dir:
        raise RuntimeError("找不到 arm-none-eabi-gcc")
    core_inc, dev_inc = _find_cmsis()
    if not core_inc or not dev_inc:
        raise RuntimeError("找不到 CMSIS 头目录")
    cc = os.path.join(gcc_dir, "arm-none-eabi-gcc.exe")
    elf = os.path.join(tmpdir, "fw.elf")
    mp = os.path.join(tmpdir, "fw.map")
    # 参数与 bridge_fw/build.py 一致（-ffreestanding -nostdlib + 自带 linker.ld），
    # 只是输出到临时目录，不去污染仓库里的构建产物路径。
    r = subprocess.run(
        [cc, "-mcpu=cortex-m33", "-mthumb", "-O1", "-g",
         "-ffreestanding", "-nostdlib", "-nostartfiles",
         "-I" + core_inc, "-I" + dev_inc,
         "-T", os.path.join(BRIDGE_DIR, "linker.ld"),
         "-Wl,-Map=" + mp, "-o", elf, os.path.join(BRIDGE_DIR, "main.c")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if r.returncode != 0:
        raise RuntimeError("gcc 失败 rc=%s\n%s" % (r.returncode, r.stderr[-600:]))
    return elf, mp


class FirmwareFixture(unittest.TestCase):
    """整组共用一次编译：编一次，跑多个断言。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="mcu-sym-test-")
        try:
            cls.elf, cls.map_path = build_firmware(cls._tmp)
        except Exception as e:  # 工具链/CMSIS 缺失 → 整组 skip
            shutil.rmtree(cls._tmp, ignore_errors=True)
            raise unittest.SkipTest("需要 arm-none-eabi-gcc + CMSIS 头：%s" % e)
        cls.parsed_elf = core._parse_elf(cls.elf)
        cls.parsed_map = core._load_map_full(cls.map_path)

    @classmethod
    def tearDownClass(cls):
        tmp = getattr(cls, "_tmp", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


class TestParseElfReal(FirmwareFixture):
    """_parse_elf 处理真 Cortex-M ELF：段表、符号表、大小必须真。"""

    def test_elf_header_recognized(self):
        with open(self.elf, "rb") as f:
            head = f.read(6)
        self.assertEqual(head[:4], b"\x7fELF", "产物不是 ELF，工具链探测可能选错了")
        self.assertEqual(head[4], 1, "Cortex-M 是 32 位（e_ident[EI_CLASS]=1）")

    def test_parsed_not_none(self):
        self.assertIsNotNone(self.parsed_elf,
                             "真 Cortex-M ELF 解析失败——这是回归")
        self.assertEqual(self.parsed_elf["format"], "elf")

    def test_segments_have_flash_and_ram_ranges(self):
        segs = self.parsed_elf["segments"]
        self.assertTrue(segs, "段表为空")
        addrs = [v["addr"] for v in segs.values()]
        self.assertTrue(any(0x08000000 <= a < 0x08010000 for a in addrs),
                        "没有 flash 段（0x08000000~0x0800FFFF），段表解析有问题：%r" % addrs)
        self.assertTrue(any(0x20000000 <= a < 0x20010000 for a in addrs),
                        "没有 RAM 段（0x20000000~0x2000FFFF），段表解析有问题：%r" % addrs)

    def test_symbols_have_real_sizes(self):
        syms = self.parsed_elf["symbols"]
        self.assertGreater(len(syms), 5, "符号数过少：%d" % len(syms))
        sized = [s for s in syms if s["size"] > 0]
        self.assertGreater(len(sized), 3,
                           "带 size 的符号太少——ELF 的 st_size 没读出来")
        for s in sized:
            self.assertIsInstance(s["size"], int)
            self.assertGreater(s["size"], 0)

    def test_symbols_live_inside_known_segments(self):
        """符号地址必须落在某个已解析出的段内——抓「段表/符号表错位」这类静默错误。"""
        segs = list(self.parsed_elf["segments"].values())
        checked = 0
        for s in self.parsed_elf["symbols"]:
            a = s["addr"]
            if any(v["addr"] <= a < v["addr"] + max(v["size"], 1) for v in segs):
                checked += 1
        self.assertGreater(checked, 0, "没有一个符号落在段内，符号表与段表可能错位")

    def test_rejects_non_elf(self):
        """非 ELF（PE/COFF、文本）必须返回 None，不得假装解析成功。"""
        p = os.path.join(self._tmp, "not_elf.bin")
        with open(p, "wb") as f:
            f.write(b"MZ\x90\x00" + b"\x00" * 200)
        self.assertIsNone(core._parse_elf(p))

    def test_rejects_truncated(self):
        """截断文件不得抛异常——排障时喂半截文件很常见。"""
        p = os.path.join(self._tmp, "trunc.elf")
        with open(self.elf, "rb") as f:
            data = f.read()
        with open(p, "wb") as f:
            f.write(data[:40])
        self.assertIsNone(core._parse_elf(p))

    def test_missing_file(self):
        self.assertIsNone(core._parse_elf(os.path.join(self._tmp, "nope.elf")))


class TestGnuMapSymbolScarcity(FirmwareFixture):
    """★ 本轮修的核心缺陷：GNU ld 的 map 没有符号表，反查会给出假函数名。

  实测基线（真固件）：fw.map → 3 个符号；fw.elf → 31 个。
  修法不是「自动换源」（.elf 可能是另一次构建，版本不一致时地址会错位，
  比不反查更难查），而是**如实告知**：把同名 .elf 摆出来让用户自己选。
  """

    def test_map_symbol_count_is_sparse(self):
        """锁定「GNU ld 的 map 符号表确实很稀疏」这个前提本身。

        哪天上游默认行为变了、或者解析器被改坏，这条会先失败，
        后面的提示测试才不会被无声地跳过。
        """
        n_map = len(self.parsed_map["symbols"])
        n_elf = len(self.parsed_elf["symbols"])
        self.assertLess(n_map, n_elf,
                        "map 符号数(%d)不该多于 elf(%d)——解析行为变了？" % (n_map, n_elf))
        self.assertLess(n_map, 10,
                        "这份 GNU map 解析出 %d 个符号，预期是个位数（无符号表）" % n_map)

    def test_sibling_elf_detected(self):
        sib = core._sibling_elf_path(self.map_path, nsyms=len(self.parsed_map["symbols"]))
        self.assertIsNotNone(sib, "同目录有同名 .elf 却没探测到")
        self.assertEqual(os.path.basename(sib), "fw.elf")

    def test_no_sibling_when_symbols_plentiful(self):
        """符号表已经够全时不该提示——阈值 10 是为了不打扰 Keil/IAR 用户。"""
        self.assertIsNone(core._sibling_elf_path(self.map_path, nsyms=50))

    def test_no_sibling_when_elf_absent(self):
        p = os.path.join(self._tmp, "lonely.map")
        with open(p, "w", encoding="utf-8") as f:
            f.write("Archive member included\n")
        self.assertIsNone(core._sibling_elf_path(p, nsyms=1))

    def test_axf_and_out_recognized(self):
        """Keil 的 .axf、IAR 的 .out 同理——都是「同目录另一份更全符号」。

        探测顺序是 .elf → .axf → .out（ELF 优先），所以两种文件同时存在
        时先报 .elf；这里分目录各建一个，不制造「必须返回 .out」这种假约束。
        """
        for ext in (".axf", ".out"):
            d = os.path.join(self._tmp, "probe_" + ext.strip("."))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "probe" + ext), "wb") as f:
                f.write(b"\x7fELF" + b"\x00" * 100)
            sib = core._sibling_elf_path(os.path.join(d, "probe.map"), nsyms=1)
            self.assertEqual(os.path.basename(sib), "probe" + ext)

    def test_elf_takes_priority_over_axf(self):
        d = os.path.join(self._tmp, "probe_both")
        os.makedirs(d, exist_ok=True)
        for ext in (".elf", ".axf"):
            with open(os.path.join(d, "probe" + ext), "wb") as f:
                f.write(b"\x7fELF" + b"\x00" * 100)
        sib = core._sibling_elf_path(os.path.join(d, "probe.map"), nsyms=1)
        self.assertEqual(os.path.basename(sib), "probe.elf")

    def test_symbol_lookup_warns_on_sparse_map(self):
        out = core.symbol_lookup(self.map_path, [0x08000054])
        self.assertIn("GNU ld 默认", out,
                      "map 符号稀疏时必须说明原因，不能只给一句泛泛的'可能不准'")
        self.assertIn("fw.elf", out, "没告诉用户同目录有更全的符号来源")

    def test_symbol_lookup_message_admits_uncertainty(self):
        """稀疏 map 场景下，函数名本身必须仍标为不确定，不能装作确定结论。"""
        out = core.symbol_lookup(self.map_path, [0x08000054])
        self.assertTrue("△" in out or "✗" in out,
                        "稀疏 map 的反查结论必须带不确定标记（△/✗），不能是 ✓")

    def test_elf_lookup_gives_hard_answer(self):
        """给 .elf 时反查必须是硬结论（符号表来自 .symtab）。"""
        syms = {s["name"]: s for s in self.parsed_elf["symbols"]}
        target = next((s for s in syms.values() if s["size"] > 8), None)
        self.assertIsNotNone(target, "没有可用作探针的符号")
        out = core.symbol_lookup(self.elf, [target["addr"]])
        self.assertIn(".symtab", out, "elf 场景的来源说明应指向 .symtab")
        self.assertIn("✓", out, "elf 精确命中函数入口应给 ✓")

    def test_elf_lookup_rejects_out_of_range(self):
        """.elf 有真实 size，越界必须被拒——不给「+0xdeadbeef」这种假答案。

        探针取【最后一个符号】之后的地址：取中间符号 +0x1000 仍可能落在下一个
        符号范围内（那样测的是命中而不是越界，2026-10-13 第一次写时就踩了）。
        """
        syms = sorted((s for s in self.parsed_elf["symbols"] if s["size"] > 0),
                      key=lambda s: s["addr"])
        last = syms[-1]
        way_out = last["addr"] + last["size"] + 0x100000
        out = core.symbol_lookup(self.elf, [way_out])
        self.assertTrue("✗" in out or "△" in out,
                        "越界地址应被明确拒绝/标注，实测输出：\n%s" % out[:400])
        self.assertNotIn("✓", out, "越界地址不该给出 ✓ 的确定结论")

    def test_elf_lookup_inside_symbol_is_offset(self):
        """反向确认：符号内部的地址要给「+偏移」，不能误判成越界。"""
        syms = sorted((s for s in self.parsed_elf["symbols"] if s["size"] > 16),
                      key=lambda s: s["addr"])
        s0 = syms[0]
        inside = s0["addr"] + 4
        out = core.symbol_lookup(self.elf, [inside])
        self.assertIn(s0["name"], out)
        self.assertIn("✓", out)
        self.assertIn("偏移", out)

    def test_stack_unwind_also_warns(self):
        """栈回溯同样受害者于假符号名——提示也要出现在它输出里。"""
        frame = [0, 0, 0, 0, 0x08000123, 0x08000054, 0x08000040, 0x01000000]
        out = core.stack_unwind(self.map_path, frame=frame, exc_return=0xFFFFFFF9)
        self.assertIn("GNU ld 默认", out, "stack_unwind 未带上稀疏 map 提示")


class TestExcReturnBitSemantics(FirmwareFixture):
    """EXC_RETURN 位语义：2026-09-22 真机实测纠错过一次，锁死不许错回去。

    ★ 历史坑：bit2（SPSEL）与 bit3（Mode）曾被对调，工具输出过
      「bit3=1 → 异常前用 PSP」这种错话。修好后必须由测试钉住，
      否则下一个改这块的人很容易"照着自己记忆再写一遍"。
    """

    def _frame(self):
        return [0, 0, 0, 0, 0x08000123, 0x08000054, 0x08000040, 0x01000000]

    def test_bit4_1_standard_frame(self):
        out = core.stack_unwind(self.map_path, frame=self._frame(),
                                exc_return=0xFFFFFFF9)
        self.assertIn("bit4=1", out)
        self.assertIn("标准栈帧", out)

    def test_bit4_0_extended_frame(self):
        out = core.stack_unwind(self.map_path, frame=self._frame(),
                                exc_return=0xFFFFFFE9)
        self.assertIn("bit4=0", out)
        self.assertIn("扩展栈帧", out)

    def test_bit2_selects_stack(self):
        """bit2 = SPSEL：0 → MSP 主栈，1 → PSP 进程栈。"""
        msp = core.stack_unwind(self.map_path, frame=self._frame(),
                                exc_return=0xFFFFFFF1 & ~0x4)   # bit2=0
        psp = core.stack_unwind(self.map_path, frame=self._frame(),
                                exc_return=0xFFFFFFF1 | 0x4)    # bit2=1
        self.assertIn("bit2=0", msp)
        self.assertIn("bit2=1", psp)

    def test_bit3_selects_mode(self):
        """bit3 = Mode：0 → Handler，1 → Thread。"""
        handler = core.stack_unwind(self.map_path, frame=self._frame(),
                                    exc_return=0xFFFFFFF9 & ~0x8)
        thread = core.stack_unwind(self.map_path, frame=self._frame(),
                                   exc_return=0xFFFFFFF9 | 0x8)
        self.assertIn("bit3=0", handler)
        self.assertIn("bit3=1", thread)

    def test_no_exc_return_is_allowed(self):
        """不给 exc_return 也要能出结果——只少一段分析，不该崩。"""
        out = core.stack_unwind(self.map_path, frame=self._frame())
        self.assertIn("栈回溯", out)


class TestInferSizesGuards(unittest.TestCase):
    """_infer_sizes 的三条边界：每条都对应注释里写明的真实坑。"""

    def test_last_symbol_stays_zero(self):
        """最后一个符号没有下一个 → size 保持 0，不猜。"""
        syms = [{"name": "a", "addr": 0x1000, "size": 0},
                {"name": "b", "addr": 0x1010, "size": 0}]
        out = core._infer_sizes(syms, None)
        by = {s["name"]: s for s in out}
        self.assertEqual(by["a"]["size"], 0x10)
        self.assertEqual(by["b"]["size"], 0, "末符号不该被推断出大小")

    def test_alias_same_address_kept_zero(self):
        """同址多符号是别名，size 保持 0，避免把别名算成独立占用。"""
        syms = [{"name": "a", "addr": 0x1000, "size": 0},
                {"name": "a_alias", "addr": 0x1000, "size": 0},
                {"name": "b", "addr": 0x1020, "size": 0}]
        out = core._infer_sizes(syms, None)
        by = {s["name"]: s for s in out}
        self.assertEqual(by["a"]["size"], 0, "同址别名不该推断出大小")
        self.assertEqual(by["a_alias"]["size"], 0)

    def test_no_cross_segment_inference(self):
        """★ 跨段绝不推断：.text 末符号不能拿 .data 首地址当"下一个"。

        算错会印出 0x20000000 - 0x0800039c = 384MB 这种离谱数字。

        场景设计要点（2026-10-13 变异验证逼出来的）：必须造出【两个符号确实
        落在不同段】的组合。只放一个段尾符号时，它本来就没有"下一个"，正
        确实现和错误实现都返回 0，测不出判别力。
        """
        syms = [{"name": "text_fn", "addr": 0x08000300, "size": 0},
                {"name": "data_sym", "addr": 0x20000000, "size": 0}]
        segs = {".text": {"addr": 0x08000040, "size": 0x400},   # 末地址 0x08000440
                ".data": {"addr": 0x20000000, "size": 0x40}}
        out = core._infer_sizes(syms, segs)
        by = {s["name"]: s for s in out}
        # 两个符号分处不同段 → 谁都不该被推断出大小
        self.assertEqual(by["text_fn"]["size"], 0,
                         "跨段推断出的大小应为 0（实测 %s = %d 字节）"
                         % (hex(by["text_fn"]["size"]), by["text_fn"]["size"]))
        self.assertEqual(by["data_sym"]["size"], 0)
        # 离谱数字的直接判据：绝不能出现 >64KB 的"函数"
        for s in out:
            self.assertLess(s["size"], 0x10000,
                            "推断出 %d 字节的符号，明显跨段算错了" % s["size"])

    def test_same_segment_inference_still_works(self):
        """反向确认：同段内相邻符号【应该】能推断出大小。

        没有这条，跨段保护可能被"修"成永远不推断——那等于把功能砍了，
        测试还照样绿。
        """
        syms = [{"name": "a", "addr": 0x08000050, "size": 0},
                {"name": "b", "addr": 0x0800039C, "size": 0}]
        segs = {".text": {"addr": 0x08000040, "size": 0x400}}
        out = core._infer_sizes(syms, segs)
        by = {s["name"]: s for s in out}
        self.assertEqual(by["a"]["size"], 0x39C - 0x50)
        self.assertTrue(by["a"].get("_inferred"), "推断出的值应打 _inferred 标记")


class TestRegisterBitFieldParsing(unittest.TestCase):
    """decode_register 的字段定义解析分支——本轮覆盖率实测未执行过。

    ★ 断言必须打在【位宽标注】上，不能只断言"函数名出现了"：
      变异验证（2026-10-13）证实只断言名字的测试没有判别力——把区间/pos-width
      判断改成"一律当区间"后，名字照样出现，测试照样绿。
      两种解析的可见差别是位宽：bits:[0,20] → [19:0]（20 位），
      bits:[0,3] → [3:0]（4 位）。
    """

    def test_bits_interval_form(self):
        """[lsb, msb] 且差值小 → 当区间，位宽 = msb-lsb+1。"""
        out = core.decode_register(0b1111, [{"name": "F", "bits": [0, 3]}], width=8)
        self.assertIn("[3:0]", out, "bits:[0,3] 应解析成 4 位区间，实测：\n%s" % out)

    def test_bits_pos_width_form(self):
        """[pos, width] 差值大 → 当 pos+width，位宽 = 第二个数。"""
        out = core.decode_register(0xFFFFF, [{"name": "G", "bits": [0, 20]}], width=32)
        self.assertIn("[19:0]", out,
                      "bits:[0,20] 应解析成 20 位宽（pos=0,width=20），实测：\n%s" % out)

    def test_bits_extracted_value(self):
        """位宽对了，取出的值也得对——区间解 [4,7] 应取出高 4 位。"""
        out = core.decode_register(0xA5, [{"name": "HI", "bits": [4, 7]}], width=8)
        self.assertIn("10", out, "bits:[4,7] 应取出 0xA5 的高 4 位 = 0xA，实测：\n%s" % out)

    def test_fields_not_list_rejected(self):
        out = core.decode_register(1, "not-a-list", width=8)
        self.assertIn("fields", out)

    def test_non_dict_entry_skipped_not_crash(self):
        """列表里混进非 dict → 跳过并汇总，不得抛异常。"""
        out = core.decode_register(0x3, [{"name": "A", "bit": 0}, "junk", 42], width=8)
        self.assertIn("A", out)

    def test_field_missing_bit_spec_skipped(self):
        out = core.decode_register(0x3, [{"name": "A", "bit": 0},
                                        {"name": "B"}], width=8)
        self.assertIn("B", out)

    def test_lsb_width_form(self):
        """lsb+width 是第三种写法，必须与前两种并存。"""
        out = core.decode_register(0xFF, [{"name": "W", "lsb": 4, "width": 4}], width=8)
        self.assertIn("[7:4]", out, "lsb=4,width=4 应解析成 [7:4]，实测：\n%s" % out)

    def test_width_and_value_bounds(self):
        self.assertIn("width", core.decode_register(1, [{"name": "A", "bit": 0}], width=0))
        self.assertIn("超出", core.decode_register(256, [{"name": "A", "bit": 0}], width=8))
        self.assertIn("整数", core.decode_register(1, [{"name": "A", "bit": 0}], width="x"))


if __name__ == "__main__":
    unittest.main()
