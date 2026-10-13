# -*- coding: utf-8 -*-
"""编译 MCUDBG/1 桥固件（NUCLEO-H563ZI）。

工具链与 CMSIS 头的定位顺序（2026-10-13 去硬编码，换机器不用改代码）：

  1. 环境变量 ``MCUDBG_ARM_GCC`` / ``MCUDBG_CMSIS_CORE`` / ``MCUDBG_CMSIS_DEV``
  2. PATH 里直接找 ``arm-none-eabi-gcc``
  3. 常见安装位置 glob（VS Code STM32 扩展 bundle / STM32CubeIDE / msys64 ucrt64）
  4. 都没有 → 明确报错并说明怎么设，不静默用不存在的路径编译

CMSIS 头也可以来自任意 STM32Cube 工程（只 include，不改它一个字节）。
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _find_gcc_bin():
    """定位 arm-none-eabi-gcc 所在 bin 目录（环境变量 > PATH > glob）。"""
    env = os.environ.get("MCUDBG_ARM_GCC")
    if env:
        if os.path.exists(os.path.join(env, "arm-none-eabi-gcc.exe")):
            return env
        sys.stderr.write(
            "!! MCUDBG_ARM_GCC=%s 里没有 arm-none-eabi-gcc.exe\n" % env)
    # PATH
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.exists(os.path.join(d, "arm-none-eabi-gcc.exe")):
            return d
    # 常见安装位置
    cands = []
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        cands += glob.glob(os.path.join(
            local, "stm32cube", "bundles", "gnu-tools-for-stm32", "*", "bin"))
        cands += glob.glob(os.path.join(
            local, "Programs", "STM32CubeIDE_*", "*",
            "STM32CubeIDE", "plugins",
            "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*.*.*",
            "tools", "bin"))
    for d in ("C:/msys64/ucrt64/bin", "D:/tools/msys64/ucrt64/bin",
              "C:/Program Files (x86)/GNU Arm Embedded Toolchain/*/bin"):
        cands += glob.glob(d) or ([d] if os.path.isdir(d) else [])
    for c in cands:
        if os.path.exists(os.path.join(c, "arm-none-eabi-gcc.exe")):
            return c
    return None


def _find_cmsis(which):
    """定位 CMSIS 头目录：环境变量 > CubeMX 工程 glob（逐级加深，不全盘递归）。"""
    env = os.environ.get(which)
    if env and os.path.isdir(env):
        return env
    sub = ("Core/Include" if which == "MCUDBG_CMSIS_CORE"
           else "Device/ST/STM32H5xx/Include")
    cands = []
    for root in (os.environ.get("MCUDBG_CUBE_WORKSPACE"),
                 os.environ.get("USERPROFILE", ""),
                 "D:/work", "C:/work"):
        if not root or not os.path.isdir(root):
            continue
        # CubeMX 工程根相对这些 root 的深度不一（D:/work/2-IMU/6-stm32H5/CubeH5_full
        # 就是三层），逐级试 1~3 层，够用且不做全盘 recursive glob
        for depth in (1, 2, 3):
            cands += glob.glob(os.path.join(
                root, *(["*"] * depth), "Drivers", "CMSIS", sub))
    return _first_existing(cands)


def main():
    gcc = _find_gcc_bin()
    if not gcc:
        sys.stderr.write(
            "找不到 arm-none-eabi-gcc。设环境变量 MCUDBG_ARM_GCC 指向它的 bin 目录，\n"
            "例如 VS Code STM32 扩展：%LOCALAPPDATA%\\stm32cube\\bundles\\"
            "gnu-tools-for-stm32\\<版本>\\bin\n")
        return 1
    cmsis_core = _find_cmsis("MCUDBG_CMSIS_CORE")
    cmsis_dev = _find_cmsis("MCUDBG_CMSIS_DEV")
    if not cmsis_core or not cmsis_dev:
        sys.stderr.write(
            "找不到 CMSIS 头目录（core=%r dev=%r）。\n"
            "设 MCUDBG_CMSIS_CORE / MCUDBG_CMSIS_DEV，或设 MCUDBG_CUBE_WORKSPACE "
            "指向 CubeMX 工程根（含 Drivers/CMSIS/ 的目录）。\n"
            "任一 STM32Cube 工程的 Drivers/CMSIS 都能用（只 include，不修改）。\n"
            % (cmsis_core, cmsis_dev))
        return 1
    print("== 工具链: %s" % gcc)
    print("== CMSIS : %s" % cmsis_core)

    cc = os.path.join(gcc, "arm-none-eabi-gcc.exe")
    oc = os.path.join(gcc, "arm-none-eabi-objcopy.exe")
    sz = os.path.join(gcc, "arm-none-eabi-size.exe")
    cflags = ["-mcpu=cortex-m33", "-mthumb", "-O1", "-g",
              "-ffreestanding", "-nostdlib", "-nostartfiles",
              "-Wall", "-Wextra",
              "-I" + cmsis_core, "-I" + cmsis_dev]

    elf = os.path.join(HERE, "fw.elf")
    bin_ = os.path.join(HERE, "fw.bin")
    map_ = os.path.join(HERE, "fw.map")

    r = run([cc] + cflags + ["-T", os.path.join(HERE, "linker.ld"),
                             "-Wl,-Map=" + map_, "-o", elf,
                             os.path.join(HERE, "main.c")])
    print("== gcc rc=%s ==" % r.returncode)
    print(r.stdout)
    print(r.stderr)
    if r.returncode != 0:
        return 1

    r = run([oc, "-O", "binary", elf, bin_])
    print("== objcopy rc=%s ==" % r.returncode)
    print(r.stdout)
    print(r.stderr)
    if r.returncode != 0:
        return 1

    r = run([sz, elf])
    print("== size ==")
    print(r.stdout)

    print("== bin size = %d bytes ==" % os.path.getsize(bin_))
    print("（扇区 0 = 8KB：只要 bin 不超过 8192 字节，烧进 0x08000000 就不会")
    print("  越界到别的内容；用 live_check.py 烧录，它会先核对这条。）")
    return 0


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, **kw)
    return r


if __name__ == "__main__":
    sys.exit(main())
