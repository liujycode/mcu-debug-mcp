# -*- coding: utf-8 -*-
"""SWD 直连五工具的离线专项断言：

    chip_info / reset_reason / flash_verify / timing_analysis / stream_capture

★ 为什么用「真机上抓下来的 CLI 原文」打桩，而不是连板子跑：
  这五件工具的价值全在**解析与判定**上 —— banner 里的字段、RCC_RSR 的每个位、
  回读比对的首个不一致偏移、时序的丢帧/卡顿/漂移。这些都能、也必须能离线复现
  （CI 上没有板子）。下面所有 banner / 寄存器输出 / 采集记录都是**实测原文**。
  真机那一层由 tools/live_acceptance.py 与 coverage.json 的 liveVerified 负责。

★ 这里刻意覆盖三层：
  L1 正向：真实输入 → 期望结论
  L3 判别力：错的东西必须被明确拒绝（未收录 / 没确认 / 不支持的格式 / 抽不到）
  L4 边界：None / "" / 0 / -1 / 非法正则 —— 一律只回文本，绝不抛异常
"""
import os
import struct
import tempfile
import unittest
import unittest.mock as mock

from mcu_debug_mcp import core
from isolation import relaxed_env

# ---------------------------------------------------------------- 实测原文
# NUCLEO-H563ZI / ST-Link V3 上抓的 hotplug 与 normal 两档 banner（原样，含空格）
BANNER_HOTPLUG = "\n".join([
    "ST-LINK SN  : 0046001C3234511737333934",
    "ST-LINK FW  : V3J10M3",
    "Board       : NUCLEO-H563ZI",
    "Voltage     : 3.28V",
    "SWD freq    : 8000 KHz",
    "Connect mode: Hot Plug",
    "Reset mode  : Software reset",
    "Device ID   : 0x484",
    "Revision ID : Rev X",
    "Error: Fail to read Flash Size",          # ★ hotplug 读不到 system memory，实测就有这行
    "Device name : STM32H56x/573",
    "Flash size  : 2 MBytes (default)",        # ★ 带 (default) 说明「没读到、是默认值」
    "Device type : MCU",
    "Device CPU  : Cortex-M33",
    "BL Version  : 0xE4",
    "SFSP Version: v2.5.0",
    "Debug in Low Power mode enabled",
])

BANNER_NORMAL = BANNER_HOTPLUG.replace("Hot Plug", "Normal").replace(
    "Error: Fail to read Flash Size\n", "").replace(
    "Flash size  : 2 MBytes (default)", "Flash size  : 2 MBytes")

OB_TEXT = "\n".join([
    "UPLOADING OPTION BYTES DATA ...",
    "OPTION BYTES BANK: 0",
    "   Product state:",
    "     PRODUCT_STATE: 0xED (Open) ",
    "   BOR Level:",
    "     BOR_LEV      : 0x0 (BOR Level 1, the threshold level is low (around 2.1 V)) ",
    "     IWDG_SW      : 0x1  (0x1) ",
    "OPTION BYTES BANK: 1",
    "     TZEN         : 0xC3 (Trust zone disabled) ",
    "OPTION BYTES BANK: 3",
    "   Bank1 - Flash watermark area definition:",
    "     SECWM1_STRT  : 0x0  (0x8000000) ",
    "     WRPSGn1      : 0xFFFFFFFF  (0x8000000) ",
])


def _rd_line(addr, *words):
    """CLI 的 -r32 输出：地址一行，多个字空格分隔挤在同一行（实测格式）。"""
    return "Reading 32-bit memory content\n  Size          : %d Bytes\n" \
           "  Address:      : 0x%08X\n0x%08X : %s\n" % (
               len(words) * 4, addr, addr, " ".join("%08X" % w for w in words))


DBGMCU = _rd_line(0x44024000, 0x10076484, 0x00000006, 0x00001800, 0x00000000)
RSR_SFT_PIN = _rd_line(0x44020CF4, 0x14000000)      # 实测：hotplug -rst 后就是 0x14000000
RSR_ZERO = _rd_line(0x44020CF4, 0x00000000)
RSR_IWDG = _rd_line(0x44020CF4, 0x20000000)         # IWDGRSTF
SCORE_RUN = "Core is running\n"
SCORE_HALT = "Core is halted\n"


def _cli(rules, default=(0, BANNER_HOTPLUG)):
    """造一个假 _sp_run：(args, timeout) -> (rc, text)。

    rules: [(判定函数, (rc, text)), ...]，第一条命中的胜出。

    ★ 真 CLI 在**同一次连接**里会把后面几条命令都执行掉（我们正是靠这个把
      `-r32` 和 `-score` 合成一次连接省 0.4s）。所以这里也照此模拟：
      args 里带 `-score` 就把状态行补进返回文本，否则测试会以为 -score 没输出。
    """
    calls = []

    def runner(args, timeout=90):
        args = [str(a) for a in args]
        calls.append(args)
        res = default
        for pred, r in rules:
            if pred(args):
                res = r
                break
        rc, text = res
        if "-score" in args and "Core is" not in text:
            text = text + "\n" + SCORE_RUN
        return rc, text
    runner.calls = calls
    return runner


def _has(flag):
    return lambda args: flag in args


def _which_ok(name):
    return "C:/fake/STM32_Programmer_CLI.exe"


# ---------------------------------------------------------------- chip_info

class ChipInfoTest(unittest.TestCase):

    def test_hotplug_reads_banner_and_dbgmcu(self):
        """hotplug 档：拿到型号/Device ID/CPU，并从 DBGMCU 读出更精确的 DEV_ID/REV_ID。"""
        fake = _cli([(_has("-r32"), (0, DBGMCU))])
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.chip_info()
        self.assertIn("NUCLEO-H563ZI", out)
        self.assertIn("0x484", out)
        self.assertIn("Cortex-M33", out)
        # ★ 关键：DBGMCU 里 DEV_ID 是低 12 位、REV_ID 是高 16 位，别把整个字当型号
        self.assertIn("DEV_ID=0x484", out)
        self.assertIn("REV_ID=0x1007", out)
        self.assertIn("STM32H56x", out)
        # hotplug 档必须明说「没有读到」而不是假装有
        self.assertIn("未读 UID", out)
        self.assertNotIn("已复位目标", out)

    def test_full_reads_uid_and_real_flash_size(self):
        """full 档：UID 12 字节 + FLASHSIZE 低 16 位 × 1KB；并给出选项字节里的读保护等级。"""
        uid = bytes.fromhex("200040001151333234383931")
        fs = bytes([0x00, 0x08, 0x04, 0x00])        # 低 16 位 0x0800 = 2048 KB

        def dl(addr, size, path, sn=None, mode=core._SP_MODE_READ):
            payload = {0x08FFF800: uid, 0x08FFF80C: fs}.get(int(addr))
            if payload is None:
                return False
            with open(path, "wb") as fh:
                fh.write(payload[:size])
            return True

        fake = _cli([(_has("-ob"), (0, OB_TEXT)), (_has("-r32"), (0, DBGMCU))])
        with relaxed_env(mode="write"), \
                mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake), \
                mock.patch.object(core, "_sp_download", dl):
            out = core.chip_info(mode="full")
        self.assertIn("200040001151333234383931", out)
        self.assertIn("2048 KB", out)
        self.assertIn("PRODUCT_STATE", out)
        self.assertIn("Open", out)
        # full 档必须如实告知会打扰目标
        self.assertIn("已复位目标", out)

    def test_flashsize_register_unprogrammed_falls_back(self):
        """FLASHSIZE 读到 0xFFFF/0x0000 时按官方规则取默认，并明说是默认值而不是实测。"""
        def dl(addr, size, path, sn=None, mode=core._SP_MODE_READ):
            payload = (b"\xFF\xFF\x04\x00" if int(addr) == 0x08FFF80C
                       else bytes.fromhex("200040001151333234383931"))
            with open(path, "wb") as fh:
                fh.write(payload[:size])
            return True

        fake = _cli([(_has("-ob"), (0, "nothing useful")), (_has("-r32"), (0, DBGMCU))])
        with relaxed_env(mode="write"), \
                mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake), \
                mock.patch.object(core, "_sp_download", dl):
            out = core.chip_info(mode="full")
        self.assertIn("默认 2MB", out)

    def test_refuses_bad_mode_and_unknown_device(self):
        """判别力：非法 mode 要拒；连不上/认不出芯片族也要明说，不能瞎猜地址。"""
        with mock.patch.object(core, "_which_tool", _which_ok):
            self.assertIn("只支持", core.chip_info(mode="quick"))
            self.assertIn("只支持", core.chip_info(mode=None))
        # 工具不在
        with mock.patch.object(core, "_which_tool", lambda n: None):
            self.assertIn("没找到", core.chip_info())
        # banner 里什么都没有 = 没连上
        fake = _cli([], default=(0, "some noise without any fields"))
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.chip_info()
        self.assertIn("连不上", out)

    def test_full_is_blocked_by_isolation_in_readonly(self):
        """判别力：full 档会复位目标 —— 默认 readonly 档必须被隔离层拦下。"""
        with mock.patch.object(core, "_which_tool", _which_ok):
            out = core.chip_info(mode="full")
        self.assertIn("隔离拦截", out)
        self.assertIn("MCU_DEBUG_MCP_MODE=write", out)


# ---------------------------------------------------------------- reset_reason

class ResetReasonTest(unittest.TestCase):

    def test_decodes_each_flag_and_warns_about_cumulative(self):
        """两个位同时为 1 时必须说明「累积」语义，不能让人以为是一次同时发生。"""
        fake = _cli([(_has("-r32"), (0, RSR_SFT_PIN)), (_has("-score"), (0, SCORE_RUN))])
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.reset_reason()
        self.assertIn("0x14000000", out)
        self.assertIn("PINRSTF", out)
        self.assertIn("SFTRSTF", out)
        self.assertIn("累积", out)
        self.assertIn("Core is running", out)
        # 只有这两位时不该顺带报看门狗/欠压的建议
        self.assertNotIn("看门狗复位", out)

    def test_all_zero_flags(self):
        fake = _cli([(_has("-r32"), (0, RSR_ZERO)), (_has("-score"), (0, SCORE_RUN))])
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.reset_reason()
        self.assertIn("全为 0", out)

    def test_watchdog_flag_gives_actionable_hint(self):
        fake = _cli([(_has("-r32"), (0, RSR_IWDG)), (_has("-score"), (0, SCORE_RUN))])
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.reset_reason()
        self.assertIn("IWDGRSTF", out)
        self.assertIn("喂狗", out)

    def test_refuses_unknown_chip_family(self):
        """判别力：没收录的芯片族绝不敢凭印象填寄存器地址。"""
        banner = BANNER_HOTPLUG.replace("Device ID   : 0x484", "Device ID   : 0x999").replace(
            "Device name : STM32H56x/573", "Device name : STM32X999")
        fake = _cli([], default=(0, banner))
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.reset_reason()
        self.assertIn("还没收录", out)
        self.assertIn("不敢凭印象", out)

    def test_clear_requires_confirm(self):
        """判别力：不可逆操作不靠「相信调用方」—— 没 confirm 必须被挡。"""
        fake = _cli([(_has("-r32"), (0, RSR_SFT_PIN)), (_has("-score"), (0, SCORE_RUN))])
        with relaxed_env(mode="write"), \
                mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.reset_reason(None, clear_flags=True, confirm=False)
        self.assertIn("二次确认", out)
        self.assertNotIn("已生效", out)

    def test_clear_blocked_in_readonly(self):
        fake = _cli([(_has("-r32"), (0, RSR_SFT_PIN)), (_has("-score"), (0, SCORE_RUN))])
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.reset_reason(None, clear_flags=True, confirm=True)
        self.assertIn("隔离拦截", out)

    def test_clear_then_reset_attributes_flags_to_its_own_command(self):
        """★ 这条是实测踩出来的：清零+复位分开做，读回 0 才是 RMVF 生效的硬证据；

        而复位后重新看到的 PINRSTF/SFTRSTF 是**命令自己**造成的 —— 必须这样归因，
        不能像第一版那样报成「✗ RMVF 没生效？」（那是误报，会把人带沟里）。
        """
        after_clear = _rd_line(0x44020CF4, 0x00000000, 0x00000000)
        fake = _cli([(lambda a: "-w32" in a, (0, after_clear)),
                     (lambda a: "-rst" in a, (0, BANNER_HOTPLUG)),
                     (_has("-r32"), (0, RSR_SFT_PIN)),
                     (_has("-score"), (0, SCORE_RUN))])
        with relaxed_env(mode="write"), \
                mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.reset_reason(None, clear_flags=True, confirm=True)
        self.assertIn("0x00000000 ✓ RMVF 已生效", out)
        self.assertIn("刚才这条 -rst 自己", out)
        self.assertNotIn("RMVF 没生效？", out)
        # 清零与复位必须是两条独立命令（合并会让读回被复位痕迹污染）
        clear_calls = [c for c in fake.calls if "-w32" in c]
        self.assertTrue(clear_calls, "没发出清零命令")
        self.assertNotIn("-rst", clear_calls[0],
                         "清零命令带上了 -rst：读回会被复位痕迹污染，RMVF 是否生效就无法判定")

    def test_clear_without_reset_says_core_is_halted(self):
        after_clear = _rd_line(0x44020CF4, 0x00000000, 0x00000000)
        fake = _cli([(lambda a: "-w32" in a, (0, after_clear)),
                     (_has("-r32"), (0, RSR_SFT_PIN)),
                     (_has("-score"), (0, SCORE_RUN))])
        with relaxed_env(mode="write"), \
                mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake):
            out = core.reset_reason(None, clear_flags=True, confirm=True, then_reset=False)
        self.assertIn("then_reset=False", out)
        self.assertIn("halt", out)
        self.assertNotIn("刚才这条 -rst 自己", out)


# ---------------------------------------------------------------- flash_verify

def _minimal_elf(data, vaddr=0x08000000, name=".text", typ=1):
    """造一个最小可用的 ELF32-LE，只含一个 PROGBITS 段。

    ★ 为什么要自己造：``_firmware_segments`` 走 elf 分支时，要靠**段在文件里的偏移**
      (sh_offset) 去 elf 里取「本该是什么」。而 ``_parse_elf`` 早期只导出了
      name/addr/size、**没有 off** —— 于是 elf 校验永远报「没有可校验段」，
      看起来像「这个 elf 里本来就没东西」，而不是配置错。这条测试就是钉死这个回归。
    """
    shstr = b"\x00" + name.encode() + b"\x00.shstrtab\x00"
    ehsize, shentsize, nsec = 52, 40, 3
    data_off = ehsize
    shstr_off = data_off + len(data)
    shoff = shstr_off + len(shstr)
    if shoff % 4:
        shoff += 4 - (shoff % 4)
    buf = bytearray(shoff + nsec * shentsize)
    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\x00" * 8
    struct.pack_into("<16sHHIIIIIHHHHHH", buf, 0, ident,
                     2, 40, 1, vaddr, 0, shoff, 0,
                     ehsize, 0, 0, shentsize, nsec, 2)
    buf[data_off:data_off + len(data)] = data
    buf[shstr_off:shstr_off + len(shstr)] = shstr
    struct.pack_into("<IIIIIIIIII", buf, shoff + 1 * shentsize,
                     1, typ, 0, vaddr, data_off, len(data), 0, 0, 4, 0)
    struct.pack_into("<IIIIIIIIII", buf, shoff + 2 * shentsize,
                     1 + len(name) + 1, 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0)
    return bytes(buf)


def _make_ihex(addr, data):
    """生成合法 Intel HEX（高 16 位走 0x04 线性地址扩展记录，校验和现算）。

    ★ 2026-10-13 修：旧版把 0x08000000 直接塞进 16 位记录地址字段
      （截成 0x0000）且不发 0x04 记录——夹具造假，解析器如实解出 0x00000000，
      测试却从来没发现（旧 flash_verify 回读失败也打印"✓ 完全一致"，
      这条测试就这么绿了两年）。Intel HEX 的地址字段只有 16 位，
      0x08000000 必须由 :020000040800F2 这类扩展记录提供。
    """
    out = []
    hi = (addr >> 16) & 0xFFFF
    if hi:
        body = bytes([2, 0, 0, 0x04, (hi >> 8) & 0xFF, hi & 0xFF])
        out.append(":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper())
    body = bytes([len(data), (addr >> 8) & 0xFF, addr & 0xFF, 0x00]) + data
    out.append(":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper())
    return "\n".join(out) + "\n"


class FlashVerifyTest(unittest.TestCase):

    @staticmethod
    def _dl_from(device_bytes, addr0=0x08000000):
        """假回读：从 device_bytes 里按地址切片返回（模拟设备上的真实内容）。"""
        def dl(addr, size, path, sn=None, mode=core._SP_MODE_READ):
            off = int(addr) - addr0
            chunk = device_bytes[off:off + size]
            if len(chunk) != size:
                return False
            with open(path, "wb") as fh:
                fh.write(chunk)
            return True
        return dl

    def _tmp(self, name, blob):
        d = tempfile.mkdtemp(prefix="mcp-verify-")
        p = os.path.join(d, name)
        with open(p, "wb") as fh:
            fh.write(blob)
        return p

    def test_bin_identical_passes(self):
        blob = bytes(range(256)) * 3            # 768 字节
        p = self._tmp("fw.bin", blob)
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_download", self._dl_from(blob)):
            out = core.flash_verify(p, chunk=128)     # 多分几块，验证分块拼接
        self.assertIn("✓ 完全一致", out)
        self.assertIn("768 字节全部通过", out)

    def test_bin_reports_first_mismatch_with_context(self):
        """判别力：不一致必须报出**首个**偏移与绝对地址，且是错的不是含糊的。"""
        blob = bytearray(bytes(range(256)) * 3)
        dev = bytearray(blob)
        dev[100] ^= 0xFF
        p = self._tmp("fw.bin", bytes(blob))
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_download", self._dl_from(bytes(dev))):
            out = core.flash_verify(p)
        self.assertIn("✗", out)
        self.assertIn("首个在偏移 +100", out)
        self.assertIn("0x08000064", out)
        self.assertIn("期望 0x64，实际 0x9B", out)

    def test_hex_uses_record_addresses(self):
        """hex 校验必须用记录里的地址（不是默认 0x08000000，更不是 0）。

        ★ 2026-10-13：此测试曾经是假绿灯——夹具 _make_ihex 把 0x08000000
          截成 16 位假记录，段地址实际解析成 0x00000000，回读失败，
          而旧 flash_verify 回读失败也打印"✓ 完全一致"，测试只看段行
          字符串就过。现在夹具合规 + 断言段地址，两条路都钉死。
        """
        blob = b"\xDE\xAD\xBE\xEF"
        p = self._tmp("fw.hex", (_make_ihex(0x08000000, blob) + ":00000001FF\n").encode())
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_download", self._dl_from(blob)):
            out = core.flash_verify(p)
        self.assertIn("0x08000000", out)          # 段地址来自记录，不是 0
        self.assertIn("✓ 完全一致", out)
        self.assertNotIn("未能校验", out)

    def test_elf_reads_section_bytes_by_file_offset(self):
        """★ 回归：elf 段必须按 sh_offset 取字节，否则永远报「没有可校验段」。"""
        blob = bytes(range(64))
        p = self._tmp("fw.elf", _minimal_elf(blob))
        segs, note = core._firmware_segments(p)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][0], 0x08000000)
        self.assertEqual(segs[0][1], blob)
        self.assertIn("ELF 只校验", note)
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_download", self._dl_from(blob)):
            out = core.flash_verify(p)
        self.assertIn("✓ 完全一致", out)

    def test_elf_truncated_is_rejected_not_silently_short_read(self):
        blob = bytes(range(200))
        full = _minimal_elf(blob)
        p = self._tmp("fw.elf", full[:120])          # 截断：段表说 200，文件里没那么多
        with self.assertRaises(ValueError) as cm:
            core._firmware_segments(p)
        self.assertTrue("段表" in str(cm.exception) or "解析不了" in str(cm.exception))

    def test_refuses_bad_inputs(self):
        """L3/L4：文件不存在 / 非路径 / 不支持的扩展名 / chunk 非法 —— 全部只回文本。"""
        with mock.patch.object(core, "_which_tool", _which_ok):
            self.assertIn("不存在", core.flash_verify("D:/definitely/not/here.bin"))
            self.assertIn("要给出固件文件路径", core.flash_verify(None))
            self.assertIn("要给出固件文件路径", core.flash_verify(""))
        p = self._tmp("fw.txt", b"x")
        with mock.patch.object(core, "_which_tool", _which_ok):
            self.assertIn("只支持", core.flash_verify(p))
        blob = b"\x01\x02"
        pb = self._tmp("fw.bin", blob)
        with mock.patch.object(core, "_which_tool", _which_ok):
            for bad in (0, -1, "abc", None):
                out = core.flash_verify(pb, None, None, bad)
                self.assertIn("chunk", out)              # 非整数 / 非正数都只回文本
                self.assertTrue("整数" in out)
        # 工具不在
        with mock.patch.object(core, "_which_tool", lambda n: None):
            self.assertIn("没找到", core.flash_verify(pb))

    def test_read_failure_is_reported_not_treated_as_match(self):
        """回读失败绝不能算通过 —— 那是「没验成」而不是「验过了」。

        ★ 2026-10-13 加强：旧版只断言了总结行有 ✗，可段行照样打印
          "✓ 完全一致"——一份自相矛盾的输出（段说一致、总结说不一致）。
          现在两头都钉：总结 ✗、段行必须"未能校验"且禁止出现"✓ 完全一致"。
        """
        blob = b"\x01\x02\x03\x04"
        p = self._tmp("fw.bin", blob)
        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_download", lambda *a, **k: False):
            out = core.flash_verify(p)
        self.assertIn("回读失败", out)
        self.assertIn("✗", out)
        self.assertIn("未能校验", out)
        self.assertNotIn("✓ 完全一致", out)


# ---------------------------------------------------------------- timing_analysis

class TimingAnalysisTest(unittest.TestCase):

    @staticmethod
    def _cap(rows, dt=0.050, skip=(), slow=()):
        """造一份采集记录（与 stream_capture 的落盘格式一致：[+秒] 行）。"""
        out, t = [], 0.0
        for i in rows:
            out.append("[+%.4f] %d TICK\n" % (t, i))
            t += dt * (6 if i in slow else 1)
            if i in skip:
                t += dt * 0                        # 跳号靠序号本身体现
        return "".join(out)

    def test_detects_dropped_frames_and_slowdown(self):
        txt = self._cap(list(range(1, 41)), skip=(16, 17, 18), slow=(37,))
        # 丢掉 16/17/18 这三行
        txt = "".join(l for l in txt.splitlines(True)
                      if l.split()[-2] not in ("16", "17", "18"))
        # 手工把序号跳号做出来：15 → 19
        out = core.timing_analysis(text=txt)
        self.assertIn("丢帧", out)
        self.assertIn("缺 3 个", out)
        self.assertIn("卡顿", out)

    def test_line_leading_seq_detected_without_extra_args(self):
        txt = "".join("[+%.4f] %d X\n" % (i * 0.05, i) for i in (1, 2, 3, 7, 8, 9))
        out = core.timing_analysis(text=txt)
        self.assertIn("缺 3 个", out)

    def test_seq_pattern_needed_for_non_leading_seq(self):
        """判别力：序号不在行首时要显式给 seq_pattern；不给则**明说查不了**，而不是假绿。"""
        txt = "".join("[+%.4f] TICK %d\n" % (i * 0.05, i) for i in (1, 2, 3, 7, 8, 9))
        out = core.timing_analysis(text=txt)
        self.assertIn("抽不到序号", out)
        out2 = core.timing_analysis(text=txt, seq_pattern=r"TICK\s+(\d+)")
        self.assertIn("缺 3 个", out2)

    def test_drift_real_vs_outlier(self):
        """真漂移要报；被个别离群点带偏的拟合不能报成「系统性漂移」。"""
        rows, t = [], 0.0
        for i in range(200):                        # 间隔从 50ms 线性涨到 ~250ms
            rows.append("[+%.4f] %d T\n" % (t, i))
            t += 0.050 + i * 0.001
        self.assertIn("⚠ 累计变化同时超过", core.timing_analysis(text="".join(rows)))

        rows, t = [], 0.0
        for i in range(200):                        # 基线平稳，只插两个大卡顿
            rows.append("[+%.4f] %d T\n" % (t, i))
            t += 0.500 if i in (50, 150) else 0.050
        out = core.timing_analysis(text="".join(rows))
        self.assertIn("没有系统性漂移", out)

    def test_refuses_bad_inputs(self):
        """L3/L4：无输入 / 无时间戳 / 路径不存在 / 非法正则 / 各类 None —— 只回文本。"""
        self.assertIn("要给输入", core.timing_analysis())
        self.assertIn("要给输入", core.timing_analysis(text="", path=""))
        self.assertIn("做不了时序分析", core.timing_analysis(text="hello\nworld\n"))
        self.assertIn("文件不存在", core.timing_analysis(path="D:/nope/never.log"))
        self.assertIn("要是文件路径字符串", core.timing_analysis(path=123))
        for bad in ("(", "[", "a{2,1}"):
            out = core.timing_analysis(text="[+0.1] a\n", pattern=bad)
            self.assertIn("不是合法的正则", out)
            out2 = core.timing_analysis(text="[+0.1] a\n", seq_pattern=bad)
            self.assertIn("不是合法的正则", out2)
        self.assertIn("不是合法的正则", core.timing_analysis(text="[+0.1] a\n", pattern=None,
                                                            seq_pattern="("))

    def test_single_sample_is_not_crashed(self):
        out = core.timing_analysis(text="[+0.1] only one\n")
        self.assertIn("做不了时序分析", out)


# ---------------------------------------------------------------- stream_capture

class _FakePort(object):
    """假的已打开串口：按脚本吐字节。"""

    def __init__(self, chunks, delay=0.0):
        self._chunks = list(chunks)
        self._delay = delay
        self.closed = False

    def read(self, _n=4096):
        if self._chunks:
            if self._delay:
                import time as _t
                _t.sleep(self._delay)
            return self._chunks.pop(0)
        return b""

    def close(self):
        self.closed = True


class StreamCaptureTest(unittest.TestCase):

    def _run(self, fake_port, **kw):
        port = kw.pop("port", "COM_TEST")
        # 2026-10-13：stream_capture 改登记为 write 档（长时独占口径），
        # 行为测试要在 write 档下跑（隔离本身由 test_isolation.py 专测）。
        with relaxed_env(mode="write"):
            with mock.patch.object(core, "_open_serial",
                                   lambda **k: fake_port):
                return core.stream_capture(port, **kw)

    def test_capture_writes_timestamped_file(self):
        d = tempfile.mkdtemp(prefix="mcp-cap-")
        out_path = os.path.join(d, "cap.log")
        fp = _FakePort([b"TICK 1\r\nTICK 2\r\n", b"TICK 3\r\n"])
        txt = self._run(fp, seconds=0.4, out_path=out_path, max_lines=3)
        self.assertIn("已落盘", txt)
        self.assertTrue(fp.closed)
        body = open(out_path, encoding="utf-8").read()
        self.assertIn("# 端口: COM_TEST @ 115200", body)
        for i in (1, 2, 3):
            self.assertIn("TICK %d" % i, body)
        # 每行都得带到达时间戳 —— 没有时间戳，时序信息就永久丢了
        for ln in body.splitlines():
            if ln.startswith("#") or not ln.strip():
                continue
            self.assertRegex(ln, r"^\[\+\d+\.\d{4}\] ")

    def test_capture_then_timing_analysis_chain(self):
        """L6 链路：采集 → 直接喂给 timing_analysis，两件工具必须咬得上。

        ★ 假串口**必须按行分次到达**（一次给一行 + 小延时）。若一次把全部字节吐出来，
          所有行会落在同一个时间戳刻度里 —— 那是真实存在的退化输入，工具会（正确地）
          拒绝给数据率，于是这条链路测试就测不到真正的分析了。
        """
        d = tempfile.mkdtemp(prefix="mcp-cap-")
        out_path = os.path.join(d, "chain.log")
        chunks = [b"TICK %d\r\n" % i for i in (1, 2, 3, 9, 10, 11)]
        fp = _FakePort(chunks, delay=0.03)
        with relaxed_env(mode="write"):
            with mock.patch.object(core, "_open_serial", lambda **k: fp):
                txt = core.stream_capture("COM_TEST", seconds=2.0, out_path=out_path)
        self.assertIn("已落盘", txt)
        self.assertIn("timing_analysis", txt)
        ana = core.timing_analysis(path=out_path, seq_pattern=r"TICK\s+(\d+)")
        self.assertIn("时序分析", ana)
        self.assertIn("缺 5 个", ana)          # 3 → 9 中间缺 4..8 共 5 个

    def test_burst_input_is_refused_instead_of_printing_garbage_rate(self):
        """★ 退化输入：全部行落在同一个时间戳刻度内 → 不许编数据率。

        实测踩到过：一次性回灌的日志会让「数据率」算出 6e9 行/秒这种垃圾数。
        正确行为是**明说窗口不够、这些指标一律不给**。
        """
        d = tempfile.mkdtemp(prefix="mcp-cap-")
        out_path = os.path.join(d, "burst.log")
        with relaxed_env(mode="write"):
            with mock.patch.object(core, "_open_serial",
                                   lambda **k: _FakePort([b"A\nB\nC\nD\nE\n"])):
                core.stream_capture("COM_TEST", seconds=0.3, out_path=out_path)
        ana = core.timing_analysis(path=out_path)
        self.assertIn("同一个刻度内", ana)
        self.assertIn("一律不给", ana)
        self.assertNotIn("行/秒", ana)

    def test_no_data_gives_actionable_text(self):
        d = tempfile.mkdtemp(prefix="mcp-cap-")
        txt = self._run(_FakePort([]), seconds=0.25,
                        out_path=os.path.join(d, "empty.log"))
        self.assertIn("一行都没收到", txt)

    def test_does_not_clobber_existing_file(self):
        d = tempfile.mkdtemp(prefix="mcp-cap-")
        p = os.path.join(d, "keep.log")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("原始现场，不许被覆盖\n")
        txt = self._run(_FakePort([b"A\r\n"]), seconds=0.25, out_path=p, max_lines=1)
        self.assertIn("keep_2.log", txt)
        self.assertEqual(open(p, encoding="utf-8").read(), "原始现场，不许被覆盖\n")

    def test_refuses_bad_port_and_surfaces_open_failure(self):
        """L3/L4：口名不合法要拒；口打不开要给可读原因（走笼子内的降级路径）。"""
        with relaxed_env(mode="readonly"):
            self.assertIn("要给出串口名", core.stream_capture(None, seconds=0.1))
            self.assertIn("要给出串口名", core.stream_capture("", seconds=0.1))
        # 打不开：把构造换成抛异常
        with relaxed_env(mode="off"):
            with mock.patch.object(core, "_open_serial",
                                   side_effect=OSError("Access is denied")):
                out = core.stream_capture("COM_TEST", seconds=0.1)
        self.assertIn("失败", out)
        self.assertIn("Access is denied", out)

    def test_refuses_bad_out_path_type(self):
        out = core.stream_capture("COM_TEST", seconds=0.1, out_path=123)
        self.assertIn("要是文件路径字符串", out)


# ---------------------------------------------------------------- 跨工具链路

class SwdChainTest(unittest.TestCase):
    """L6：一次会话里三件工具串着用，结论必须互相咬得上（同一次连接里的同一批数据）。"""

    def test_chip_info_then_reset_reason_then_verify(self):
        blob = bytes(range(48))
        d = tempfile.mkdtemp(prefix="mcp-chain-")
        p = os.path.join(d, "fw.bin")
        with open(p, "wb") as fh:
            fh.write(blob)

        fake = _cli([(lambda a: "-r32" in a and "0x44024000" in " ".join(a).lower(), (0, DBGMCU)),
                     (lambda a: "-r32" in a and "0x44020cf4" in " ".join(a).lower(), (0, RSR_ZERO)),
                     (_has("-score"), (0, SCORE_RUN))])

        def dl(addr, size, path, sn=None, mode=core._SP_MODE_READ):
            off = int(addr) - 0x08000000
            with open(path, "wb") as fh:
                fh.write(blob[off:off + size])
            return True

        with mock.patch.object(core, "_which_tool", _which_ok), \
                mock.patch.object(core, "_sp_run", fake), \
                mock.patch.object(core, "_sp_download", dl):
            info = core.chip_info()
            rst = core.reset_reason()
            ver = core.flash_verify(p)

        # 三份输出都在描述同一块 H5，且都不自相矛盾
        self.assertIn("0x484", info)
        self.assertIn("STM32H5", rst)
        self.assertIn("全为 0", rst)
        self.assertIn("✓ 完全一致", ver)
        # chip_info(hotplug) 明确说没打扰目标；reset_reason 用 hotplug 才能读到真实状态
        self.assertIn("不复位、不停内核", info)
        self.assertIn("Core is running", rst)


if __name__ == "__main__":
    unittest.main()
