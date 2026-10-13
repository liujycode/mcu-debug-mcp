# -*- coding: utf-8 -*-
"""编译 MCUDBG/1 桥固件（NUCLEO-H563ZI）。

工具链取自 VS Code 的 STM32 扩展 bundle（不在 PATH）；CMSIS 头取自只读参考
目录 D:\\work\\2-IMU\\6-stm32H5（只 include，不改它一个字节）。换机器时改
这两处路径即可。
"""
import os
import subprocess
import sys

GCC = (r"C:\Users\ljyit\AppData\Local\stm32cube\bundles"
       r"\gnu-tools-for-stm32\14.3.1+st.2\bin")
CMSIS_CORE = (r"D:\work\2-IMU\6-stm32H5\CubeH5_full\Drivers\CMSIS\Core\Include")
CMSIS_DEV = (r"D:\work\2-IMU\6-stm32H5\CubeH5_full\Drivers\CMSIS\Device"
             r"\ST\STM32H5xx\Include")
HERE = os.path.dirname(os.path.abspath(__file__))

CC = os.path.join(GCC, "arm-none-eabi-gcc.exe")
OC = os.path.join(GCC, "arm-none-eabi-objcopy.exe")
SZ = os.path.join(GCC, "arm-none-eabi-size.exe")

CFLAGS = ["-mcpu=cortex-m33", "-mthumb", "-O1", "-g",
          "-ffreestanding", "-nostdlib", "-nostartfiles",
          "-Wall", "-Wextra",
          "-I" + CMSIS_CORE, "-I" + CMSIS_DEV]


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, **kw)
    return r


def main():
    elf = os.path.join(HERE, "fw.elf")
    bin_ = os.path.join(HERE, "fw.bin")
    map_ = os.path.join(HERE, "fw.map")

    r = run([CC] + CFLAGS + ["-T", os.path.join(HERE, "linker.ld"),
                             "-Wl,-Map=" + map_, "-o", elf,
                             os.path.join(HERE, "main.c")])
    print("== gcc rc=%s ==" % r.returncode)
    print(r.stdout)
    print(r.stderr)
    if r.returncode != 0:
        return 1

    r = run([OC, "-O", "binary", elf, bin_])
    print("== objcopy rc=%s ==" % r.returncode)
    print(r.stdout)
    print(r.stderr)
    if r.returncode != 0:
        return 1

    r = run([SZ, elf])
    print("== size ==")
    print(r.stdout)

    print("== bin size = %d bytes ==" % os.path.getsize(bin_))
    print("（扇区 0 = 8KB：只要 bin 不超过 8192 字节，烧进 0x08000000 就不会")
    print("  越界到别的内容；用 live_check.py 烧录，它会先核对这条。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
