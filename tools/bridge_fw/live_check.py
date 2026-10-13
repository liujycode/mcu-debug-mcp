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
ROOT = os.path.dirname(os.path.dirname(HERE))          # 仓库根（含 core.py）
sys.path.insert(0, ROOT)

EXE = (r"C:\Users\ljyit\AppData\Local\stm32cube\bundles\programmer"
       r"\2.21.0\bin\STM32_Programmer_CLI.exe")

DEFAULT_BACKUP = r"D:\work\_tmp\h563_probe_fw\orig_sector0.bin"

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

    # ---------- 2) 烧录 ----------
    log("")
    log("【2】烧录桥固件到 0x08000000")
    rc, _ = sh(["-c", "port=SWD", "mode=hotplug", "mode=normal",
                "-w", fw_bin, "0x08000000", "-v", "-rst"])
    if rc != 0:
        log("★ 烧录失败，终止。板子状态：未改动成功——建议手动核对后重试")
        return 1
    time.sleep(1.5)                       # 等桥固件起来

    # ---------- 3) 逐工具验证 ----------
    import core                            # noqa: E402  （要在 env 变化前 import？不，guard 每次调用时读 env）
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
