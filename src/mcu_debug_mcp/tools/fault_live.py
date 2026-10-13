# -*- coding: utf-8 -*-
"""fault_live.py —— HardFault 真机闭环验收（ROADMAP §6 收口判据 1）

把「人为触发 HardFault → 抓寄存器 → 反查出错函数」这条链在**真实板子**上跑一遍，
全程只用 mcu-debug-mcp 自己的能力（SWD 读内存 + decode_fault / stack_unwind /
symbol_lookup）。

为什么需要它
------------
离线夹具（tests/test_fault_chain.py）用的是**编出来的** exc_return 与寄存器值。
判据 1 要的是真机。而且这套流程第一次跑就抓到一个只有真机才暴露的错误：
``stack_unwind`` 把 EXC_RETURN 的 bit2/bit3 含义对调了（ARMv7-M 的标准
EXC_RETURN 值里这两位恒同向，所以旧测试永远假通过；ARMv8-M 的 M33 才露出来）。
详见 core.stack_unwind 内注释与 tests/test_fault_chain.py::
test_exc_return_bit2_is_stack_not_mode。

⚠️ 会覆盖目标 0x08000000 起的固件
--------------------------------
本脚本要烧一块**故意会崩**的固件，跑完目标停在 HardFault 死循环里。
- 默认（不带 --flash）**不碰硬件**：只编译固件 + 打印计划。
- 要真跑必须显式 ``--flash``；跑完想恢复，请自己重烧原固件。
- 目标断电或复位不会损坏 —— 它只是卡在 handler 里，SWD 随时能连。

用法
----
    # 1) 只编译 + 自检 + 看计划（安全，不碰板子）
    python tools/fault_live.py

    # 2) 真跑（会烧掉目标当前固件！）
    python tools/fault_live.py --flash

    # 3) 指定调试器
    python tools/fault_live.py --flash --sn 0046001C3234511737333934

退出码
------
    0 = 闭环走通（7 项判定全 PASS）；1 = 有 FAIL；2 = 环境不满足（缺固件/烧不动等）
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))   # 包的父目录（源码树 = src/；安装后 = site-packages）
sys.path.insert(0, PROJ)
from mcu_debug_mcp import core  # noqa: E402

FW_DIR = os.path.join(HERE, "fault_demo_fw")
BUILD = os.path.join(FW_DIR, "build.py")
FW_BIN = os.path.join(FW_DIR, "fault_demo.bin")
FW_ELF = os.path.join(FW_DIR, "fault_demo.elf")

# 故障现场结构 / SCB 寄存器地址（与 fault_demo.c、startup.s 约定一致）
FI_ADDR = 0x20000000
FI_WORDS = 10                    # msp psp exc_return magic cfsr hfsr bfar mmfar shcsr stack
SCB = {"CFSR": 0xE000ED28, "HFSR": 0xE000ED2C, "MMFAR": 0xE000ED34, "BFAR": 0xE000ED38}
SRAM_LO, SRAM_HI = 0x20000000, 0x200A0000
MAGIC = 0x5A5A5A5A

def _find_cube_prog():
    """定位 STM32_Programmer_CLI：环境变量 > PATH > 常见安装位置 glob。

    2026-10-13 去硬编码：此前钉死 `programmer\\2.21.0` 一个版本目录，
    换个 CubeProgrammer 版本就跑不通。找不到时由调用方给可读诊断。
    """
    import glob
    env = os.environ.get("MCUDBG_CUBE_PROG")
    if env:
        return env if os.path.exists(env) else None
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.exists(os.path.join(d, "STM32_Programmer_CLI.exe")):
            return os.path.join(d, "STM32_Programmer_CLI.exe")
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        for c in glob.glob(os.path.join(
                local, "stm32cube", "bundles", "programmer", "*", "bin",
                "STM32_Programmer_CLI.exe")):
            if os.path.exists(c):
                return c
    return None


STM32_CLI = _find_cube_prog()


def hr(t):
    print("")
    print("=" * 78)
    print(t)
    print("=" * 78)


def build_firmware():
    """编译演示固件（需要 arm-none-eabi-gcc；找不到就报错，不静默跳过）。"""
    hr("【0】编译演示固件")
    if not os.path.isfile(BUILD):
        print("  缺 build.py：%s" % BUILD)
        return False
    p = subprocess.run([sys.executable, BUILD], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=FW_DIR)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0 or not os.path.isfile(FW_BIN):
        print(out[-2500:])
        print("  !! 编译失败")
        return False
    for ln in out.splitlines():
        s = ln.strip()
        if s.startswith(("text", "   ")) or "md5=" in s or s.startswith(("arm-none-eabi-gcc.exe",)):
            continue
        if any(k in s for k in ("字节", "错误", "warning", "Error")):
            print("  " + s)
    data = open(FW_BIN, "rb").read()
    print("  固件 %s (%d 字节, md5=%s)" % (FW_BIN, len(data), hashlib.md5(data).hexdigest()))
    return True


def flash_and_capture(sn):
    """烧录 + 抓现场 + 走全链。返回 (ok, fail_list)。"""
    args = ["-c", "port=SWD", "mode=hotplug"]
    if sn:
        args += ["sn=" + sn]
    args += ["-w", FW_BIN, "0x08000000", "-v", "-rst"]

    hr("【1】烧录会崩的固件并复位（★ 覆盖 0x08000000 起）")
    p = subprocess.run([STM32_CLI] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    out = (p.stdout or "") + (p.stderr or "")
    for ln in out.splitlines():
        s = ln.strip()
        if any(k in s for k in ("Download verified", "Error", "error", "Memory programming")):
            print("  " + s)
    print("  rc = %d" % p.returncode)
    if p.returncode != 0:
        return False, ["烧录失败"]

    print("\n  等 2 秒，让 main() 跑起来并炸在 HardFault…")
    time.sleep(2.0)

    hr("【2】SWD hotplug 抓现场（不复位、不停内核）")
    words, raw = core._sp_fetch(FI_ADDR, FI_WORDS * 4)
    if len(words) < FI_WORDS:
        print("  读不到现场结构（读到 %d 个字）。SCB 直读：CFSR=0x%08X"
              % (len(words), (core._sp_fetch(SCB["CFSR"], 4)[0] or [0])[0]))
        print("  原始输出尾部：")
        print(raw[-800:])
        return False, ["读现场失败"]
    (msp, psp, exc_ret, magic, cfsr, hfsr, bfar, mmfar, shcsr, stack) = words[:FI_WORDS]

    print("  现场结构 @ 0x%08X" % FI_ADDR)
    for k, v in (("MSP(fault 时)", msp), ("PSP(fault 时)", psp),
                 ("EXC_RETURN (LR)", exc_ret), ("magic", magic),
                 ("CFSR", cfsr), ("HFSR", hfsr), ("BFAR", bfar),
                 ("MMFAR", mmfar), ("SHCSR", shcsr)):
        print("    %-18s = 0x%08X" % (k, v))
    print("    承载压栈帧的栈      = 0x%08X  (%s)"
          % (stack, "PSP" if (exc_ret & 0x4) else "MSP"))

    hr("【3】交叉核对：绕过固件，直接读 SCB")
    for name, addr in SCB.items():
        w, _ = core._sp_fetch(addr, 4)
        direct = w[0] if w else None
        got = {"CFSR": cfsr, "HFSR": hfsr, "MMFAR": mmfar, "BFAR": bfar}[name]
        print("  %-6s @0x%08X 直读 = 0x%08X   %s"
              % (name, addr, direct or 0, "✓ 一致" if direct == got else "✗ 不一致"))

    hr("【4】读压栈帧（R0 R1 R2 R3 R12 LR PC xPSR）")
    if not (SRAM_LO <= stack < SRAM_HI):
        print("  !! 栈指针 0x%08X 不在 SRAM 范围，拒绝乱读" % stack)
        return False, ["栈指针越界"]
    print("  压栈帧 @ 0x%08X" % stack)
    fw_words, _ = core._sp_fetch(stack, 32)
    frame = list(fw_words)
    if len(frame) < 8:
        print("  !! 只读到 %d 个字" % len(frame))
        return False, ["压栈帧读不全"]
    for n, v in zip(["R0", "R1", "R2", "R3", "R12", "LR", "PC", "xPSR"], frame):
        print("    %-5s = 0x%08X" % (n, v))

    hr("【5】走工具全链：寄存器 → 故障类型 → 出错函数")
    print("\n----- core.decode_fault() -----")
    print(core.decode_fault(cfsr=cfsr, hfsr=hfsr, bfar=bfar, mmfar=mmfar))
    print("\n----- core.stack_unwind() -----")
    print(core.stack_unwind(FW_ELF, frame=frame, exc_return=exc_ret, sp=stack))
    print("\n----- core.symbol_lookup() 交叉验证 -----")
    print(core.symbol_lookup(FW_ELF, [frame[6], frame[5]]))

    hr("【6】判定")
    checks = [
        ("magic 写进去了（汇编侧抢在 C 之前存现场）", magic == MAGIC),
        ("CFSR 非 0（确实发生了故障）", cfsr != 0),
        ("HFSR.FORCED=1（这是被升级上来的故障）", bool(hfsr & (1 << 30))),
        ("CFSR.PRECISERR=1（精确总线错误）", bool(cfsr & (1 << 9))),
        ("CFSR.BFARVALID=1（BFAR 可信）", bool(cfsr & (1 << 15))),
        ("BFAR = 0xFFFFFFF0（函数里故意读的那个非法地址）", bfar == 0xFFFFFFF0),
        ("压栈帧 PC 落在 Flash 内", 0x08000000 <= frame[6] < 0x08200000),
        ("PC 反查得到 mpu6050_read（出错函数名对得上）",
         "mpu6050_read" in core.symbol_lookup(FW_ELF, [frame[6]])),
        ("EXC_RETURN bit2=0 判成 MSP（★ 真机暴露过的错位点）",
         "MSP" in core.stack_unwind(FW_ELF, frame=frame, exc_return=exc_ret)),
    ]
    for name, ok in checks:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    bad = [n for n, ok in checks if not ok]
    print("")
    print("  → %s" % ("全部通过" if not bad else "失败 %d 项：%s" % (len(bad), bad)))
    return (not bad), bad


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="HardFault 真机闭环验收（默认只编译+打印计划，不碰板子）")
    ap.add_argument("--flash", action="store_true",
                    help="真烧录目标 —— ★ 会覆盖 0x08000000 起的目标固件")
    ap.add_argument("--sn", metavar="SN", help="指定 ST-Link 序列号（多探针时用）")
    args = ap.parse_args(argv)

    print("mcu-debug-mcp · HardFault 真机闭环（ROADMAP 收口判据 1）")
    print("调试器 CLI：%s" % ("找到" if os.path.isfile(STM32_CLI) else "★ 没找到 " + STM32_CLI))

    if not build_firmware():
        return 2

    if not args.flash:
        hr("【计划】（未加 --flash，不碰硬件）")
        print("  1. 烧 fault_demo.bin 到 0x08000000，复位 → main() 读非法地址 0xFFFFFFF0 → HardFault")
        print("  2. 现场写进固定结构 0x20000000，内核停在 handler 死循环")
        print("  3. SWD hotplug 连上（不复位），读现场 + 直读 SCB 交叉核对")
        print("  4. 读压栈帧 → decode_fault / stack_unwind / symbol_lookup 走全链")
        print("  5. 9 项判定")
        print("")
        print("  ★ 会覆盖目标当前固件；跑完请自行重烧原固件恢复。")
        print("  要真跑：python tools/fault_live.py --flash")
        return 0

    if not STM32_CLI or not os.path.isfile(STM32_CLI):
        print("\n!! 找不到 STM32_Programmer_CLI，无法烧录")
        print("  设环境变量 MCUDBG_CUBE_PROG 指向它（任意版本），例如：")
        print("    set MCUDBG_CUBE_PROG=%LOCALAPPDATA%\\stm32cube\\bundles\\"
              "programmer\\<版本>\\bin\\STM32_Programmer_CLI.exe")
        return 2

    ok, bad = flash_and_capture(args.sn)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
