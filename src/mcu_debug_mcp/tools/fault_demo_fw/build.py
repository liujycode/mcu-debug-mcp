# -*- coding: utf-8 -*-
"""build.py —— 编译 HardFault 演示固件（STM32H563ZI / Cortex-M33）

用 msys64 ucrt64 里的 arm-none-eabi-gcc（16.1.0），不需要额外下载工具链。
产物：fault_demo.elf / .bin / .map
"""
import os
import subprocess
import sys
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = r"D:/tools/msys64/ucrt64/bin"

GCC = os.path.join(BIN, "arm-none-eabi-gcc.exe")
OBJCOPY = os.path.join(BIN, "arm-none-eabi-objcopy.exe")
SIZE = os.path.join(BIN, "arm-none-eabi-size.exe")
NM = os.path.join(BIN, "arm-none-eabi-nm.exe")

env = dict(os.environ)
env["PATH"] = BIN + os.pathsep + env.get("PATH", "")

ELF = os.path.join(HERE, "fault_demo.elf")
BINF = os.path.join(HERE, "fault_demo.bin")
MAP = os.path.join(HERE, "fault_demo.map")

CFLAGS = [
    "-mcpu=cortex-m33", "-mthumb", "-mfloat-abi=soft",
    "-O1", "-g3",
    "-ffreestanding", "-fno-builtin",
    "-Wall", "-Wextra",
]
LDFLAGS = [
    "-T", os.path.join(HERE, "link.ld"),
    "-nostdlib", "-lgcc",
    "-Wl,-Map=" + MAP, "-Wl,--cref",
    "-Wl,--gc-sections",
]

SRCS = [os.path.join(HERE, "startup.s"), os.path.join(HERE, "fault_demo.c")]


def run(cmd, label):
    print("=== %s ===" % label)
    print("  " + " ".join(os.path.basename(c) if c.startswith(BIN) else c for c in cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=HERE)
    out = (p.stdout or "") + (p.stderr or "")
    if out.strip():
        print(out.rstrip())
    print("  rc =", p.returncode)
    return p.returncode


def main():
    for f in (ELF, BINF, MAP):
        if os.path.exists(f):
            os.remove(f)

    rc = run([GCC] + CFLAGS + SRCS + ["-o", ELF] + LDFLAGS, "编译 + 链接")
    if rc != 0 or not os.path.exists(ELF):
        print("\n!! 编译失败")
        return 1

    rc = run([OBJCOPY, "-O", "binary", ELF, BINF], "转 .bin")
    if rc != 0:
        return 1

    print()
    run([SIZE, ELF], "体积")
    print()

    # 关键符号地址：栈回溯要靠它们
    print("=== 关键符号（供 symbol_lookup / stack_unwind 反查）===")
    p = subprocess.run([NM, "-n", ELF], capture_output=True, text=True, env=env)
    keys = ("mpu6050_read", "imu_task", "app_state", "main", "Reset_Handler",
            "HardFault_Handler", "HardFault_C", "raw_buf", "g_sink")
    for line in (p.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] in keys:
            print("  %s  %s  %s" % (parts[0], parts[1], parts[2]))

    print()
    data = open(BINF, "rb").read()
    print("=== 产物 ===")
    print("  %s  %d 字节  md5=%s" % (BINF, len(data), hashlib.md5(data).hexdigest()))
    print("  %s" % ELF)
    print("  %s" % MAP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
