# -*- coding: utf-8 -*-
"""
排故链（fault 解码 / .map 解析 / 符号反查 / 栈回溯）测试。

设计原则：本项目的血泪教训是「自证式测试」——只测通过路径，等于没测。
每个工具都必须有【双向测试】：
  · 该给结论的时候给对结论
  · 不该给结论的时候【必须拒绝给】，不能给看起来合理的假答案
本文件里的"假答案防线"是最重要的部分（fault 地址可信性、符号未找到）。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
# ★测试产物一律写系统临时目录，绝不落项目目录：
#   以前写 ROOT/build_tmp，导致「跑一次测试就产一堆 .exe/.map」，
#   然后又要清理、又要过平台批量删除闸门——源头断掉才算真的干净。
BUILD_TMP = os.path.join(tempfile.gettempdir(), "mcu_debug_mcp_build")
MAP_ARM = os.path.join(FIXTURES, "arm_mcu.map")          # 目标 MCU map（按真实 GNU ld 格式固化）
MAP_DEMO = os.path.join(BUILD_TMP, "fault_demo.map")     # 主机 x86 map（现场用 gcc 生成）


def _find_gcc():
    """找可用的 gcc，并返回「必须补进 PATH 的前缀」。

    ★踩过的坑：msys2 的 gcc.exe 即使能在 PATH 里被 which 找到，
    编译时仍会因为找不到 cc1/as/ld 而以 rc≠0 + 空输出【静默失败】。
    所以只要 gcc 来自 msys2，就必须把它的 bin 目录显式前置到 PATH——
    不能因为 which 成功就以为不用补。
    """
    msys = [r"D:\tools\msys64\mingw64\bin", r"D:\tools\msys64\usr\bin"]
    prefix = os.pathsep.join(d for d in msys if os.path.isdir(d)) or None
    gcc = shutil.which("gcc")
    if gcc:
        return gcc, prefix        # ★即便 which 成功，也要带上前缀
    for d in msys:
        cand = os.path.join(d, "gcc.exe")
        if os.path.isfile(cand):
            return cand, prefix
    return None, None


def _gcc_works(gcc, prefix):
    """真跑一次 gcc --version：确认它能被执行，而不是只会被找到。"""
    env = dict(os.environ)
    if prefix:
        env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    try:
        r = subprocess.run([gcc, "--version"], capture_output=True, text=True,
                           env=env, timeout=30)
        return r.returncode == 0 and "gcc" in (r.stdout or "").lower()
    except Exception:
        return False


def setUpModule():
    """确保 build_tmp/fault_demo.map 存在——用 fixtures 里的 .c 现场编译。

    ★不用"没文件就 skip"糊过去：那样在干净环境里整组测试会静默失效
    （本项目明令禁止"看起来跑了其实没跑"）。有 gcc 就真编译，没有才 skip。
    """
    if os.path.isfile(MAP_DEMO):
        return
    src = os.path.join(FIXTURES, "fault_demo.c")
    if not os.path.isfile(src):
        return
    gcc, prefix = _find_gcc()
    if not gcc or not _gcc_works(gcc, prefix):
        return
    os.makedirs(BUILD_TMP, exist_ok=True)
    env = dict(os.environ)
    if prefix:
        env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    out = os.path.join(BUILD_TMP, "fault_demo.exe")
    r = subprocess.run(
        [gcc, "-g", "-O0", src, "-o", out,
         "-Wl,-Map=" + MAP_DEMO],
        capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0 or not os.path.isfile(MAP_DEMO):
        # 编译失败不静默：打印出来，让"环境问题"可见而不是伪装成 skip
        sys.stderr.write("[test_fault_chain] gcc 生成 fault_demo.map 失败 rc=%s\n%s\n%s\n"
                         % (r.returncode, r.stdout, r.stderr))


def _have_demo():
    """运行时判断：主机 map 是否可用。

    ★必须做成函数、在测试方法里调用，不能用 @skipUnless(os.path.isfile(...))：
    装饰器在【类定义时】求值，而 setUpModule 在之后才跑——用装饰器会导致
    「刚生成完 map，测试却已经被判为 skip」的静默失效（本项目踩过）。
    """
    if os.path.isfile(MAP_DEMO):
        return True
    gcc, prefix = _find_gcc()
    return bool(gcc and _gcc_works(gcc, prefix))


def _have_arm():
    return os.path.isfile(MAP_ARM)


# ============================================================
# 一、fault 位域表与加载
# ============================================================

class TestFaultBitsKnowledge(unittest.TestCase):

    def test_cortex_m_listed(self):
        self.assertIn("cortex_m", core.fault_bits_list())

    def test_load_cortex_m(self):
        d = core._load_fault_bits("cortex-m")
        self.assertIsNotNone(d)
        self.assertEqual(d.get("arch"), "cortex-m")

    def test_unknown_arch_returns_none(self):
        self.assertIsNone(core._load_fault_bits("riscv-nonexist"))

    def test_all_bits_have_source_and_hint(self):
        """位域表是诊断的主体，任何一条缺 source/hint 都等于没做诊断。"""
        d = core._load_fault_bits("cortex-m")
        self.assertTrue(d.get("source"), "缺 source")
        for reg_name, reg in d["registers"].items():
            for bit_name, info in reg.get("bits", {}).items():
                self.assertTrue(info.get("meaning"),
                                "%s.%s 缺 meaning" % (reg_name, bit_name))
                self.assertTrue(info.get("hint"),
                                "%s.%s 缺 hint" % (reg_name, bit_name))

    def test_cfsr_has_three_segments(self):
        d = core._load_fault_bits("cortex-m")
        segs = d["registers"]["CFSR"].get("segments") or {}
        for s in ("MMFSR", "BFSR", "UFSR"):
            self.assertIn(s, segs, "CFSR 缺 %s 段" % s)

    def test_no_bit_overlap_in_cfsr(self):
        """位域重叠会给出冲突诊断——这是 fault 表最致命的错。"""
        d = core._load_fault_bits("cortex-m")
        for reg_name, reg in d["registers"].items():
            occupied = {}
            for bit_name, info in reg.get("bits", {}).items():
                bit = info.get("bit")
                w = info.get("width", 1)
                if bit is None:
                    continue
                for b in range(bit, bit + w):
                    self.assertNotIn(b, occupied,
                                     "%s bit %d 被 %s 和 %s 同时占用"
                                     % (reg_name, b, occupied.get(b), bit_name))
                    occupied[b] = bit_name

    def test_valid_flag_references_exist(self):
        """BFAR.valid_flag 必须能解析到实际存在的位域名。"""
        d = core._load_fault_bits("cortex-m")
        all_bits = set()
        for reg in d["registers"].values():
            all_bits.update(reg.get("bits", {}).keys())
        for reg_name, reg in d["registers"].items():
            vf = reg.get("valid_flag")
            if not vf:
                continue
            name = vf.split("（")[0].strip()
            self.assertIn(name, all_bits,
                          "%s.valid_flag 引用的 %s 不存在" % (reg_name, name))


# ============================================================
# 二、decode_fault：给对结论 + 拒绝给假结论
# ============================================================

class TestDecodeFault(unittest.TestCase):

    def test_precise_bus_error_gives_trustworthy_addr(self):
        """PRECISERR + BFARVALID=1 → 地址可信，必须明说可用。"""
        # PRECISERR=bit9, BFARVALID=bit15
        cfsr = (1 << 9) | (1 << 15)
        out = core.decode_fault(cfsr=cfsr, bfar=0x40021000)
        self.assertIn("PRECISERR", out)
        self.assertIn("可信", out)
        self.assertIn("0x40021000", out)
        self.assertNotIn("不可信", out)

    def test_imprecise_bus_error_flags_addr_untrustworthy(self):
        """★核心防线：IMPRECISERR 时地址不可信，必须显式拒绝使用。"""
        # IMPRECISERR=bit10, BFARVALID=bit15 —— 两者都置位，但地址仍不可信
        cfsr = (1 << 10) | (1 << 15)
        out = core.decode_fault(cfsr=cfsr, bfar=0x40021000)
        self.assertIn("IMPRECISERR", out)
        self.assertIn("不可信", out)
        # 必须引导到替代手段，不能只说"不可信"就完了
        self.assertTrue("二分" in out or "读回" in out,
                        "IMPRECISERR 必须给替代定位手段，不能只说不可信")

    def test_bfar_valid_zero_refuses_to_decode(self):
        """★核心防线：valid=0 时地址是垃圾值，必须拒绝解读。"""
        # PRECISERR 置位但 BFARVALID=0
        cfsr = (1 << 9)
        out = core.decode_fault(cfsr=cfsr, bfar=0xDEADBEEF)
        self.assertIn("BFARVALID=0", out)
        self.assertIn("垃圾值", out)
        # 关键：不能把垃圾地址当成可用地址推荐去做符号反查
        self.assertIn("不要拿它去反查", out)

    def test_mmar_valid_zero_refuses(self):
        cfsr = (1 << 1)  # DACCVIOL 置位，但 MMARVALID=0
        out = core.decode_fault(cfsr=cfsr, mmfar=0xCAFEBABE)
        self.assertIn("MMARVALID=0", out)
        self.assertIn("垃圾值", out)

    def test_forced_hfsr_prompts_to_read_cfsr(self):
        """★核心防线：FORCED=1 只看 HFSR 会漏根因，必须引导去 CFSR。"""
        out = core.decode_fault(hfsr=0x40000000)   # 只有 FORCED
        self.assertIn("FORCED", out)
        self.assertIn("CFSR", out)
        self.assertIn("还没传 CFSR", out)

    def test_forced_with_empty_cfsr_warns(self):
        """FORCED=1 但 CFSR=0：要指出可能原因，而不是当没事。"""
        out = core.decode_fault(cfsr=0, hfsr=0x40000000)
        self.assertIn("FORCED", out)
        self.assertTrue("没有故障位" in out or "清过" in out or "时机" in out)

    def test_stack_error_bits_lead_to_stack_conclusion(self):
        """STKERR/UNSTKERR 命中 → 必须引导到栈问题。"""
        # STKERR=bit12
        cfsr = (1 << 12)
        out = core.decode_fault(cfsr=cfsr)
        self.assertIn("STKERR", out)
        self.assertTrue("栈" in out)

    def test_no_input_asks_for_input(self):
        out = core.decode_fault()
        self.assertIn("至少要给一个寄存器值", out)

    def test_bad_value_rejected(self):
        out = core.decode_fault(cfsr="not-a-number")
        self.assertIn("无法解析", out)

    def test_out_of_range_rejected(self):
        out = core.decode_fault(cfsr=0x1FFFFFFFF)
        self.assertIn("超出 32 位", out)

    def test_unknown_arch_lists_available(self):
        out = core.decode_fault(cfsr=1, arch="riscv")
        self.assertIn("没有收录", out)
        self.assertIn("cortex_m", out)

    def test_valid_bit_without_addr_asks_for_addr(self):
        """valid=1 但没给地址 → 要提示补上，不能静默。"""
        cfsr = (1 << 9) | (1 << 15)
        out = core.decode_fault(cfsr=cfsr)
        self.assertIn("没传 BFAR", out)

    def test_m0_caveat_present(self):
        """M0 无 CFSR 分项，这个限制必须出现在输出里。"""
        out = core.decode_fault(cfsr=0)
        self.assertIn("M0", out)

    # ---- M0 专门路径（不是「没收录」）----

    def test_m0_arch_not_treated_as_missing(self):
        """M0 是已知架构但没有 CFSR，不能报「没收录」，要给替代路径。"""
        out = core.decode_fault(bfar=0x1234, arch="cortex-m0")
        self.assertNotIn("没有收录", out)
        self.assertIn("没有 CFSR", out)
        self.assertIn("symbol_lookup", out)   # 必须指到还能用的工具
        self.assertIn("stack_unwind", out)

    def test_m0plus_alias_also_routed(self):
        for a in ("cortex-m0+", "cortex-m0plus"):
            out = core.decode_fault(arch=a)
            self.assertNotIn("没有收录", out, a)
            self.assertIn("没有 CFSR", out)

    def test_truly_unknown_arch_still_says_missing(self):
        """真没收录的架构还是要说「没收录」——别把 M0 特例扩大到所有架构。"""
        out = core.decode_fault(cfsr=1, arch="cortex-m99")
        self.assertIn("没有收录", out)

    # ---- ★哨兵地址：valid=1 也不能无脑放行 ----

    def test_sentinel_address_warned_even_when_valid(self):
        """BFARVALID=1+PRECISERR=1 但地址是 DEADBEEF → 必须提示可疑。"""
        cfsr = (1 << 9) | (1 << 15)
        out = core.decode_fault(cfsr=cfsr, bfar=0xDEADBEEF)
        self.assertIn("0xdeadbeef", out.lower().replace("0x", "0x"))
        self.assertIn("哨兵", out)
        # 仍要说「可信」（位域语义没错），但加了警告
        self.assertIn("可信", out)

    def test_null_and_erased_address_warned(self):
        cfsr = (1 << 9) | (1 << 15)
        self.assertIn("NULL", core.decode_fault(cfsr=cfsr, bfar=0))
        self.assertIn("0xFFFFFFFF", core.decode_fault(cfsr=cfsr, bfar=0xFFFFFFFF))

    def test_repeating_byte_address_warned(self):
        cfsr = (1 << 9) | (1 << 15)
        self.assertIn("全同", core.decode_fault(cfsr=cfsr, bfar=0xAAAAAAAA))

    def test_normal_peripheral_addr_not_false_flagged(self):
        """正常外设地址不能误报哨兵——这是判别力另一侧。"""
        cfsr = (1 << 9) | (1 << 15)
        out = core.decode_fault(cfsr=cfsr, bfar=0x40021000)
        self.assertNotIn("哨兵", out)
        self.assertNotIn("全同", out)

    def test_sentinel_helper_direct(self):
        self.assertIsNone(core._looks_like_sentinel(0x40021000))
        self.assertIsNone(core._looks_like_sentinel(0x20000000))
        self.assertIsNotNone(core._looks_like_sentinel(0))
        self.assertIsNotNone(core._looks_like_sentinel(0xFFFFFFFF))
        self.assertIsNotNone(core._looks_like_sentinel(0x55))
        self.assertIsNotNone(core._looks_like_sentinel(0xDEADBEEF))
        self.assertIsNotNone(core._looks_like_sentinel(0xCAFEBABE))
        self.assertIsNotNone(core._looks_like_sentinel(0xAAAAAAAA))
        self.assertIsNone(core._looks_like_sentinel(None))

    # ---- valid=0 且没传地址值时的措辞准确性 ----

    def test_valid_zero_without_addr_value_worded_correctly(self):
        """只传 CFSR（valid=0）没传 BFAR → 不能说「本次读到的 BFAR 是垃圾值」，因为根本没读到。"""
        out = core.decode_fault(cfsr=0)   # 无任何位
        self.assertNotIn("本次读到的 BFAR", out)
        self.assertIn("别拿它反查", out)


# ============================================================
# 三、parse_map：格式识别 + 拒绝猜
# ============================================================

class TestParseMap(unittest.TestCase):

    def test_missing_file(self):
        out = core.parse_map("D:/nope/not-exist.map")
        self.assertIn("不存在", out)

    def test_empty_file(self):
        os.makedirs(BUILD_TMP, exist_ok=True)
        p = os.path.join(BUILD_TMP, "_empty_test.map")
        with open(p, "w", encoding="utf-8") as f:
            f.write("")
        try:
            out = core.parse_map(p)
            self.assertIn("空", out)
        finally:
            os.remove(p)

    def test_unknown_format_refuses_to_guess(self):
        """★核心防线：认不出的格式必须明说，不能猜出错的段占用。"""
        os.makedirs(BUILD_TMP, exist_ok=True)
        p = os.path.join(BUILD_TMP, "_unknown_test.map")
        with open(p, "w", encoding="utf-8") as f:
            f.write("this is not any known map format\nrandom text\n")
        try:
            out = core.parse_map(p)
            self.assertIn("认不出", out)
            self.assertIn("没有猜", out)
        finally:
            os.remove(p)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_real_gnu_map_parsed(self):
        out = core.parse_map(MAP_DEMO)
        self.assertIn("GNU", out)
        self.assertIn("段占用", out)
        self.assertIn("符号表", out)
        self.assertIn(".text", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_gnu_map_symbols_have_inferred_sizes(self):
        """GNU map 不记 size——必须靠相邻地址推断，否则大符号视图是空的。"""
        syms = core._load_map_symbols(MAP_DEMO)
        self.assertTrue(syms)
        with_size = [s for s in syms if s["size"] > 0]
        self.assertTrue(with_size, "符号大小全为 0——推断没生效")
        # 至少有些是推断出来的
        self.assertTrue(any(s.get("_inferred") for s in with_size),
                        "没有任何符号被标为推断——来源不透明")

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_inferred_size_marked_in_output(self):
        """推断出来的大小必须标注，不能让人当实测值。"""
        out = core.parse_map(MAP_DEMO)
        self.assertIn("推断", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_last_symbol_not_guessed(self):
        """最后一个符号没有下一个可参照，不能猜大小。"""
        syms = core._load_map_symbols(MAP_DEMO)
        addrs = sorted({s["addr"] for s in syms if s["addr"]})
        last_addr = addrs[-1]
        last = [s for s in syms if s["addr"] == last_addr]
        for s in last:
            self.assertEqual(s.get("size", 0), 0,
                             "最后一个符号 %s 的大小被猜出来了" % s["name"])

    # ---- ★跨段推断保护（真实踩过的坑）----

    @unittest.skipUnless(_have_arm(), "需要 tests/fixtures/arm_mcu.map")
    def test_no_cross_segment_size_inference(self):
        """★.text 末符号绝不能拿 .data 首地址当"下一个"——
        否则会算出 0x20000000-0x0800039c=384MB 这种离谱值并被当成真大小印出来。
        """
        syms = core._load_map_symbols(MAP_ARM)
        big = [s for s in syms if s["size"] > 0x100000]   # >1MB 在 MCU 上不可能
        self.assertEqual(big, [],
                         "出现了跨段推断的离谱大小：%s"
                         % [(s["name"], s["size"]) for s in big])

    @unittest.skipUnless(_have_arm(), "需要 tests/fixtures/arm_mcu.map")
    def test_inferred_size_within_segment_bounds(self):
        """任何推断值都不能超过所属段的总大小。"""
        syms = core._load_map_symbols(MAP_ARM)
        p = core._parse_map_gnu(open(MAP_ARM, encoding="utf-8").read())
        segs = p["segments"]
        max_seg = max(v["size"] for v in segs.values())
        for s in syms:
            if s.get("_inferred"):
                self.assertLessEqual(s["size"], max_seg,
                                     "%s 推断值超过最大段" % s["name"])

    # ---- ★目标平台自检：主机 map 要报警，MCU map 不能误报 ----

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_host_map_flagged(self):
        """x86 mingw 的 map 必须被识别为「不是 MCU 固件」。"""
        out = core.parse_map(MAP_DEMO)
        self.assertIn("不是 MCU 固件", out)
        self.assertIn("arm-none-eabi", out)

    @unittest.skipUnless(_have_arm(), "需要 tests/fixtures/arm_mcu.map")
    def test_mcu_map_not_false_flagged(self):
        """★判别力另一侧：正常的 ARM MCU map 绝不能报警。"""
        out = core.parse_map(MAP_ARM)
        self.assertNotIn("不是 MCU 固件", out)
        self.assertNotIn("主机（PC）程序", out)

    def test_map_target_check_helper(self):
        """直接测判定函数：主机特征 vs MCU 特征。"""
        host_segs = {".text": {"size": 100}, ".idata": {"size": 10},
                     ".rsrc": {"size": 10}, ".reloc": {"size": 10}}
        host_syms = [{"name": n, "addr": i, "size": 1} for i, n in enumerate(
            ["__mingw_module_is_dll", "_pei386_runtime_relocator", "_matherr", "main"])]
        lvl, reasons = core._map_target_check(host_segs, host_syms)
        self.assertEqual(lvl, "host")
        self.assertTrue(reasons)

        mcu_segs = {".text": {"size": 4608}, ".data": {"size": 456}, ".bss": {"size": 1472}}
        mcu_syms = [{"name": n, "addr": i, "size": 1} for i, n in enumerate(
            ["g_pfnVectors", "main", "mpu6050_read", "imu_task"])]
        lvl2, _ = core._map_target_check(mcu_segs, mcu_syms)
        self.assertIsNone(lvl2, "正常 MCU map 被误判成主机")

    # ---- 应用符号优先排序 ----

    @unittest.skipUnless(_have_arm(), "需要 tests/fixtures/arm_mcu.map")
    def test_app_symbol_ranked_above_runtime(self):
        """查大缓冲时，用户的应用符号要排在运行时符号前面。"""
        out = core.parse_map(MAP_ARM)
        # raw_buf 是应用里最大的，应该出现在列表里且靠前
        self.assertIn("raw_buf", out)
        lines = [l for l in out.split("\n") if "raw_buf" in l or "__aeabi" in l]
        if any("__aeabi" in l for l in lines):
            ri = next(i for i, l in enumerate(lines) if "raw_buf" in l)
            ri_rt = next(i for i, l in enumerate(lines) if "__aeabi" in l)
            self.assertLess(ri, ri_rt, "运行时符号排到了应用符号前面")


# ============================================================
# 四、symbol_lookup：反查正确 + 不猜名字
# ============================================================

class TestSymbolLookup(unittest.TestCase):

    def test_bad_path(self):
        out = core.symbol_lookup("D:/nope.map", [0x1000])
        self.assertIn("无法读取符号表", out)

    def test_no_addresses(self):
        # 用固化夹具：这条不需要真机 map 内容，不该依赖 gcc 生成的演示文件
        out = core.symbol_lookup(MAP_ARM, [])
        self.assertIn("没给地址", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_exact_function_entry(self):
        syms = core._load_map_symbols(MAP_DEMO)
        target = [s for s in syms if s["name"] == "mpu6050_read"]
        self.assertTrue(target, "演示工程里应有 mpu6050_read")
        out = core.symbol_lookup(MAP_DEMO, [target[0]["addr"]])
        self.assertIn("mpu6050_read", out)
        self.assertIn("正好是函数入口", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_offset_within_function(self):
        syms = core._load_map_symbols(MAP_DEMO)
        t = [s for s in syms if s["name"] == "imu_task"][0]
        out = core.symbol_lookup(MAP_DEMO, [t["addr"] + 0xD])
        self.assertIn("imu_task", out)
        self.assertIn("偏移", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_unfound_address_reports_not_found(self):
        """★核心防线：找不到就说找不到，不能返回一个碰巧相邻的函数名。"""
        out = core.symbol_lookup(MAP_DEMO, [0x10])   # 远低于符号表最小地址
        self.assertIn("未找到符号", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_bad_address_string_reported(self):
        out = core.symbol_lookup(MAP_DEMO, ["zzz"])
        self.assertIn("无法解析", out)

    # ---- ★假答案防线（高地址侧，实测踩过）----

    @unittest.skipUnless(_have_arm(), "需要 tests/fixtures/arm_mcu.map")
    def test_far_above_symbols_refuses_to_name(self):
        """★0xdeadbeef 曾被打成「app_state+0xbeadb817 ✓」——这必须被拦住。
        高地址落在符号表空隙里 = 野指针，不能硬套最近的前置符号。
        """
        out = core.symbol_lookup(MAP_ARM, [0xDEADBEEF])
        self.assertNotIn("✓", out)
        self.assertTrue("✗" in out)
        self.assertTrue("不属于" in out or "未找到可信符号" in out)

    @unittest.skipUnless(_have_arm(), "需要 tests/fixtures/arm_mcu.map")
    def test_addr_in_gap_between_symbols_refused(self):
        """bss 最后一个符号之后、段尾之前…越出段就拒。"""
        # app_state 在 0x200006d8，大小 0x40+2倍…段尾 0x20000788
        out = core.symbol_lookup(MAP_ARM, [0x20000800])   # 超过 .bss 段尾
        self.assertNotIn("✓", out)

    @unittest.skipUnless(_have_arm(), "需要 tests/fixtures/arm_mcu.map")
    def test_addr_inside_sizeless_symbol_marked_tentative(self):
        """无 size 记录的符号内地址 → 用 △ 标注，不能给 ✓ 冒充确定。"""
        out = core.symbol_lookup(MAP_ARM, [0x200006f0])   # app_state 内
        self.assertIn("app_state", out)
        self.assertIn("△", out)

    @unittest.skipUnless(_have_arm(), "需要 tests/fixtures/arm_mcu.map")
    def test_normal_text_addr_still_resolves(self):
        """★判别力另一侧：正常 .text 地址必须照常 ✓，别被防假答案的逻辑误伤。"""
        out = core.symbol_lookup(MAP_ARM, [0x08000180, 0x08000130])
        self.assertIn("✓", out)
        self.assertIn("mpu6050_read", out)
        self.assertIn("main", out)


# ============================================================
# 五、stack_unwind：定位正确 + 跑飞识别
# ============================================================

class TestStackUnwind(unittest.TestCase):

    def test_missing_file(self):
        out = core.stack_unwind("D:/nope.map", frame=[0] * 8)
        self.assertIn("无法读取符号表", out)

    def test_no_frame_gives_usage(self):
        # 用固化夹具：不依赖 gcc 生成物
        out = core.stack_unwind(MAP_ARM)
        self.assertIn("没给栈帧内容", out)
        self.assertIn("R0", out)

    def test_short_frame_rejected(self):
        """少于 8 个字的帧没有 PC，无法定位——必须拒绝。"""
        out = core.stack_unwind(MAP_ARM, frame=[1, 2, 3])
        self.assertIn("需要 8 个字", out)

    def test_bad_frame_value(self):
        out = core.stack_unwind(MAP_ARM, frame=[0, 0, 0, 0, 0, 0, "zzz", 0])
        self.assertIn("无法解析", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_pc_resolves_to_function(self):
        syms = core._load_map_symbols(MAP_DEMO)
        t = [s for s in syms if s["name"] == "imu_task"][0]
        frame = [0, 0, 0, 0, 0, 0, t["addr"] + 4, 0x61000000]
        out = core.stack_unwind(MAP_DEMO, frame=frame)
        self.assertIn("imu_task", out)
        self.assertIn("出错函数", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_wild_pc_detected_as_runaway(self):
        """★核心防线：PC 落在无符号区必须判为「跑飞」，并给排查方向。"""
        frame = [0, 0, 0, 0, 0, 0, 0xDEADBEEF, 0x61000000]
        out = core.stack_unwind(MAP_DEMO, frame=frame)
        self.assertIn("未找到符号", out)
        self.assertIn("跑飞", out)
        # 必须给可行动方向，不能只说"跑飞了"
        self.assertTrue("函数指针" in out or "栈" in out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_exc_return_psp_vs_msp(self):
        """exc_return 决定去哪个栈找帧——判错就找到垃圾数据。"""
        frame = [0] * 6 + [0x140001520, 0x61000000]
        out_psp = core.stack_unwind(MAP_DEMO, frame=frame, exc_return=0xFFFFFFFD)
        self.assertIn("PSP", out_psp)
        out_msp = core.stack_unwind(MAP_DEMO, frame=frame, exc_return=0xFFFFFFF9)
        self.assertIn("MSP", out_msp)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_exc_return_bit2_is_stack_not_mode(self):
        """★ 真机暴露的错位：bit2 = 栈选择(SPSEL)，bit3 = 模式(Mode)。

        真机证据（2026-09-22，NUCLEO-H563ZI / STM32H563ZI / Cortex-M33）：
          故意读非法地址 0xFFFFFFF0 → HardFault；handler 入口的 LR 实测
          = 0xFFFFFFB8（ARMv8-M / Secure / 标准帧 / Thread / 栈在 MSP）。
          压栈帧确实在 MSP = 0x2009FFC8，此时 PSP = 0。
        0xFFFFFFB8 的 bit2=0 而 bit3=1 —— 两位**不同向**，是 ARMv7-M 标准
        EXC_RETURN（0xFFFFFFF1/F9/FD/E1/E9/ED）永远给不出的组合；旧实现把两位
        含义对调，在只用标准值的旧测试下一直假通过。
        """
        frame = [0] * 6 + [0x140001520, 0x61000000]

        out = core.stack_unwind(MAP_DEMO, frame=frame, exc_return=0xFFFFFFB8)
        b2 = [l for l in out.splitlines() if "EXC_RETURN bit2=" in l]
        b3 = [l for l in out.splitlines() if "EXC_RETURN bit3=" in l]
        self.assertTrue(b2 and b3, "bit2/bit3 的解读必须都输出")
        self.assertIn("MSP", b2[0])
        self.assertNotIn("PSP", b2[0],
                         "bit2=0 是 MSP；误报 PSP 会让用户去 PSP 取到一整帧 0")
        self.assertIn("Thread", b3[0])

        # 对照方向：bit2=1 → PSP（ARMv7-M 的 0xFFFFFFFD 也是这个方向）
        out2 = core.stack_unwind(MAP_DEMO, frame=frame, exc_return=0xFFFFFFBD)
        b2b = [l for l in out2.splitlines() if "EXC_RETURN bit2=" in l]
        self.assertTrue(b2b)
        self.assertIn("PSP", b2b[0])

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_exc_return_extended_frame_warned(self):
        """bit4=0 是 FPU 扩展帧，栈内容偏移不同——必须警告。"""
        frame = [0] * 6 + [0x140001520, 0x61000000]
        out = core.stack_unwind(MAP_DEMO, frame=frame, exc_return=0xFFFFFFE1)
        self.assertIn("扩展栈帧", out)

    @unittest.skipUnless(_have_demo(), "需要 gcc 生成 build_tmp/fault_demo.map")
    def test_ipsr_reported(self):
        """xPSR 的 IPSR 反映当前异常号。"""
        frame = [0, 0, 0, 0, 0, 0, 0x140001520, 0x00000003]  # IPSR=3 HardFault
        out = core.stack_unwind(MAP_DEMO, frame=frame)
        self.assertIn("HardFault", out)


# ============================================================
# 六、知识库校验器自身的判别力（防止校验器形同虚设）
# ============================================================

class TestValidatorCanCatchErrors(unittest.TestCase):
    """校验 fault 表的校验器必须【真的能抓到错】——否则是自证式测试。"""

    def _run_validator(self):
        import subprocess
        r = subprocess.run(
            [sys.executable if os.path.basename(sys.executable).startswith("python") else sys.executable,
             os.path.join(ROOT, "tests", "validate_knowledge.py")],
            capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def test_validator_passes_on_clean_data(self):
        rc, out = self._run_validator()
        self.assertEqual(rc, 0, "当前知识库应通过校验：%s" % out)

    def _inject_and_run(self, relpath, mutate):
        """注入改动 → 跑校验 → **逐字**恢复原文。

        ★恢复必须写回原始文本，不能用 json.dump：知识库里 cme.json / pinmap/*.json
        是「一行一条」的手写排版，json.dump 会把它们改写成标准缩进——数据没丢但
        排版毁了，属于【测试污染生产文件】，下一次 diff 整篇变色。
        """
        import json
        src = os.path.join(ROOT, "knowledge", relpath)
        orig_text = open(src, encoding="utf-8").read()
        data = json.loads(orig_text)
        mutate(data)
        try:
            json.dump(data, open(src, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            return self._run_validator()
        finally:
            open(src, "w", encoding="utf-8").write(orig_text)

    def test_validator_catches_bit_overlap(self):
        """注入位域重叠，校验器必须报错。"""
        def mutate(d):
            d["registers"]["CFSR"]["bits"]["DIVBYZERO"]["bit"] = 24  # 与 UNALIGNED 撞
        rc, out = self._inject_and_run("fault_bits/cortex_m.json", mutate)
        self.assertNotEqual(rc, 0, "位域重叠没被抓到")
        self.assertIn("重叠", out)

    def test_validator_catches_dangling_valid_flag(self):
        """注入悬空的 valid_flag 引用，校验器必须报错。"""
        def mutate(d):
            d["registers"]["BFAR"]["valid_flag"] = "NONEXIST（CFSR 的 bit99）"
        rc, out = self._inject_and_run("fault_bits/cortex_m.json", mutate)
        self.assertNotEqual(rc, 0, "悬空 valid_flag 没被抓到")
        self.assertIn("悬空", out)

    def test_validator_catches_missing_item_source(self):
        """注入「条目缺 source」，校验器必须报错——护城河不能靠自觉维护。"""
        def mutate(d):
            del d["entries"]["3"]["source"]
        rc, out = self._inject_and_run("error_codes/cme.json", mutate)
        self.assertNotEqual(rc, 0, "缺条目级 source 没被抓到")
        self.assertIn("条目级 source", out)

    def test_validator_catches_missing_toplevel_source(self):
        """注入「文件顶层缺 source」，校验器必须报错。"""
        def mutate(d):
            del d["source"]
        rc, out = self._inject_and_run("fault_patterns.json", mutate)
        self.assertNotEqual(rc, 0, "缺顶层 source 没被抓到")
        self.assertIn("顶层 source", out)

    def test_injection_leaves_file_byte_for_byte_intact(self):
        """注入测试跑完后文件必须逐字复原——防止测试污染知识库排版。"""
        import hashlib
        src = os.path.join(ROOT, "knowledge", "error_codes", "cme.json")
        before = hashlib.sha256(open(src, "rb").read()).hexdigest()

        def mutate(d):
            del d["entries"]["3"]["source"]
        self._inject_and_run("error_codes/cme.json", mutate)
        after = hashlib.sha256(open(src, "rb").read()).hexdigest()
        self.assertEqual(before, after, "注入测试改动了知识库文件内容/排版")


if __name__ == "__main__":
    unittest.main(verbosity=2)
