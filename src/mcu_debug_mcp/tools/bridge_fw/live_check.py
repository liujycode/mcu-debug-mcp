# -*- coding: utf-8 -*-
"""MCUDBG/1 桥固件 · 真机闭环：烧录 → 逐工具验证 → 恢复原始固件。

流程（对应 tools/bridge_fw/README.md 的「验证」一节）：
  1) 编译（build.py）并核对 bin ≤ 8KB（扇区 0 边界，越界会碰到别的内容）
  2) 烧录到 0x08000000（SWD，verify）
  3) 用 mcu-debug-mcp 的 8 个总线工具逐个打真桥：
       bridge_probe   → 协议版本必须 ✓
       i2c_scan       → 空总线（本板没接 I2C 器件）也是有效结论
       spi_scan       → 两条 CS 都该是「无器件」（MISO 悬空）
       spi_read_reg   → 0x9F 读回全 1
       onewire_scan   → 无器件（PC7 没挂 1-Wire）
       rs485_send     → 桥如实回 -ERR NOBUS（本板没收发器）
  4) 恢复原始固件（orig_sector0.bin）并回读比对 md5

★ 为什么恢复：这块板是师兄的，烧进去的桥固件只是验证手段，
  验完必须把板子还原成他给我的样子（备份在 --backup 指向的文件）。

用法：
  python tools/bridge_fw/live_check.py                  # 全流程（默认 COM21）
  python tools/bridge_fw/live_check.py --port COM21 --backup <orig_sector0.bin>
"""
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
# 包的父目录（bridge_fw 在 src/mcu_debug_mcp/tools/bridge_fw 下，要往上三级；
# 源码树 = src/，安装后 = site-packages）——import mcu_debug_mcp 用
sys.path.insert(0, PKG_PARENT)


def _find_cube_prog():
    """定位 STM32_Programmer_CLI：环境变量 > PATH > 常见安装位置 glob。

    2026-10-13 去硬编码：此前钉死某台机器的一个版本目录，别人拿到仓库
    必然跑不通。找不到时给可读诊断，不静默用一个不存在的路径。
    """
    import glob
    env = os.environ.get("MCUDBG_CUBE_PROG")
    if env:
        if os.path.exists(env):
            return env
        sys.stderr.write("!! MCUDBG_CUBE_PROG=%s 不存在\n" % env)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.exists(os.path.join(d, "STM32_Programmer_CLI.exe")):
            return os.path.join(d, "STM32_Programmer_CLI.exe")
    local = os.environ.get("LOCALAPPDATA") or ""
    cands = []
    if local:
        cands += glob.glob(os.path.join(
            local, "stm32cube", "bundles", "programmer", "*", "bin",
            "STM32_Programmer_CLI.exe"))
    for root in ("C:/Program Files/STMicroelectronics",
                 "C:/Program Files (x86)/STMicroelectronics"):
        cands += glob.glob(os.path.join(
            root, "*", "*", "bin", "STM32_Programmer_CLI.exe"))
    for c in cands:
        if os.path.exists(c):
            return c
    return None


EXE = _find_cube_prog()
# 备份默认落在系统临时目录（不写死某台机器的盘符路径）；
# 恢复原固件用的是这个备份，烧录**之前**就会校验它存在且是 8192 字节整扇区。
DEFAULT_BACKUP = os.path.join(
    os.environ.get("TEMP") or os.environ.get("TMP") or ".",
    "mcudbg_orig_sector0.bin")

log_lines = []


def log(s):
    print(s)
    log_lines.append(s)


def sh(args, tmo=180):
    r = subprocess.run([EXE] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=tmo)
    out = ((r.stdout or "") + (r.stderr or ""))
    keep = [ln.rstrip() for ln in out.splitlines()
            if "\ufffd" not in ln and ln.strip()]
    log("$ STM32_Programmer_CLI " + " ".join(args))
    for k in keep:
        log("  " + k)
    log("  (rc=%s)" % r.returncode)
    return r.returncode, "\n".join(keep)


def md5(p):
    with open(p, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM21")
    ap.add_argument("--backup", default=DEFAULT_BACKUP,
                    help="原始扇区 0 备份（验证完恢复用）")
    ap.add_argument("--no-restore", action="store_true",
                    help="验证完不恢复原固件（桥固件留在板上）")
    args = ap.parse_args()

    if not EXE:
        log("找不到 STM32_Programmer_CLI，无法烧录/备份，终止（板子未被改动）。")
        log("设环境变量 MCUDBG_CUBE_PROG 指向它，例如：")
        log("  set MCUDBG_CUBE_PROG=%LOCALAPPDATA%\\stm32cube\\bundles\\"
            "programmer\\<版本>\\bin\\STM32_Programmer_CLI.exe")
        log("或安装 STM32CubeProgrammer（默认就落在上面这个 glob 路径）。")
        return 1

    fw_bin = os.path.join(HERE, "fw.bin")

    # ---------- 1) 编译 ----------
    log("=" * 64)
    log("【1】编译")
    r = subprocess.run([sys.executable, os.path.join(HERE, "build.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    log(r.stdout[-800:] if r.stdout else "")
    if r.returncode != 0:
        log(r.stderr[-2000:])
        log("编译失败，终止（板子没被动过）")
        return 1
    size = os.path.getsize(fw_bin)
    log("fw.bin = %d 字节" % size)
    if size > 8192:
        log("★ 超过扇区 0（8KB），会越界覆盖别的内容 —— 终止，板子没被动过")
        return 1

    # ---------- 2) 备份原固件（必须在烧录之前） ----------
    if not args.no_restore:
        ok_backup = (os.path.exists(args.backup)
                     and os.path.getsize(args.backup) == 8192)
        if ok_backup:
            log("【2】原固件备份已在（%s，8192 字节整扇区）" % args.backup)
        else:
            # 2026-10-13：旧版没有这一步——备份靠外部手动准备，默认路径不存在时
            # 烧完才在恢复阶段报错（板子已经是桥固件，原固件没了）。
            # 现在烧录前先 upload 原扇区 0，备不成就不烧。
            log("【2】先 upload 原扇区 0 → %s（烧录前做；备不好就不烧）" % args.backup)
            d = os.path.dirname(os.path.abspath(args.backup))
            if d:
                os.makedirs(d, exist_ok=True)
            rc, _ = sh(["-c", "port=SWD", "mode=hotplug", "mode=normal",
                        "-u", "0x08000000", "8192", args.backup])
            if rc != 0 or not os.path.exists(args.backup) \
                    or os.path.getsize(args.backup) != 8192:
                log("★ 原固件备份失败（rc=%s）——没有备份就不烧录，终止。"
                    "板子未被改动。" % rc)
                log("  排查：ST-Link 是否连上（CLI -l 看枚举）、板子是否上电。")
                return 1
            log("  备份完成（md5=%s）" % md5(args.backup))

    # ---------- 3) 烧录 ----------
    log("")
    log("【2】烧录桥固件到 0x08000000")
    rc, _ = sh(["-c", "port=SWD", "mode=hotplug", "mode=normal",
                "-w", fw_bin, "0x08000000", "-v", "-rst"])
    if rc != 0:
        log("★ 烧录失败，终止。板子状态：未改动成功——建议手动核对后重试")
        return 1
    time.sleep(1.5)                       # 等桥固件起来

    # ---------- 3) 逐工具验证 ----------
    from mcu_debug_mcp import core         # noqa: E402  （要在 env 变化前 import？不，guard 每次调用时读 env）
    results = []

    def check(name, fn):
        out = fn()
        log("-" * 64)
        log("【3.%s】" % name)
        log(out)
        return out

    log("")
    log("=" * 64)
    log("【3】用 mcu-debug-mcp 的总线工具打真桥（port=%s）" % args.port)

    out = check("1 bridge_probe", lambda: core.bridge_probe(args.port))
    results.append(("bridge_probe 协议握手", "✓ 与工具一致" in out))

    out = check("2 i2c_scan", lambda: core.i2c_scan(0, port=args.port))
    results.append(("i2c_scan 空总线=有效结论", "应答器件   : 0 个" in out))

    out = check("3 spi_scan", lambda: core.spi_scan(0, port=args.port))
    results.append(("spi_scan 两条 CS 都无器件", "无器件" in out))

    out = check("4 spi_read_reg(0x9F,3)",
                lambda: core.spi_read_reg(0x9F, 3, cs=0, port=args.port))
    results.append(("spi_read 读回全 1（MISO 悬空）",
                    "0xff,0xff,0xff" in out))

    out = check("5 onewire_scan", lambda: core.onewire_scan(0, port=args.port))
    results.append(("onewire_scan 无器件", "没有找到任何 1-Wire 器件" in out))

    # rs485_send 走 bus_write 闸门（write 档）——本脚本的目标板就是我们的桥，
    # 且桥没有 RS485 收发器、什么都不会发到线上，如实回 -ERR NOBUS。
    os.environ["MCU_DEBUG_MCP_MODE"] = "write"
    try:
        out = check("6 rs485_send",
                    lambda: core.rs485_send("48656c6c6f", port=args.port))
        results.append(("rs485_send 桥如实回 -ERR NOBUS", "-ERR NOBUS" in out))
    finally:
        os.environ.pop("MCU_DEBUG_MCP_MODE", None)

    # ---------- 4) 恢复原始固件 ----------
    log("")
    log("=" * 64)
    if args.no_restore:
        log("【4】跳过恢复（--no-restore）：桥固件留在板上")
    else:
        log("【4】恢复原始固件（%s）" % args.backup)
        if not os.path.isfile(args.backup):
            log("★ 备份文件不存在：%s —— 不恢复！板子上现在是桥固件，请手动处理" % args.backup)
            ok_all = all(x for _, x in results)
            log("验证结果：%s" % ("全过" if ok_all else "有失败"))
            return 1
        rc, _ = sh(["-c", "port=SWD", "mode=hotplug", "mode=normal",
                    "-w", args.backup, "0x08000000", "-v", "-rst"])
        if rc != 0:
            log("★ 恢复烧录失败！板子上现在是桥固件 —— 用同样的命令重烧备份即可")
            return 1
        rb = os.path.join(HERE, "restore_readback.bin")
        sh(["-c", "port=SWD", "mode=normal", "-u", "0x08000000", "8192", rb])
        same = md5(rb) == md5(args.backup)
        log("恢复回读 md5 %s（期望 %s）→ %s"
            % (md5(rb), md5(args.backup), "一致 ✓" if same else "★不一致！"))

    # ---------- 汇总 ----------
    log("")
    log("=" * 64)
    ok_all = True
    log("验证汇总：")
    for name, ok in results:
        ok_all &= ok
        log("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    log("结论：%s" % ("桥链路真机全通 ✓" if ok_all else "有失败项 ✗"))

    out_path = os.path.join(HERE, "live_check.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(log_lines))
    log("原始输出已存 %s" % out_path)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
