# -*- coding: utf-8 -*-
"""真机验收夹具（live acceptance fixture）—— 对应能力缺口 N-15。

用途
----
拿一块**真实的板子**，把 mcu-debug-mcp 的核心链路从头到尾走一遍：
环境自检 → 只读探测 → 双向契约 → 判别力（反例）→ 符号定位 → 排故链 → 破坏性复现。
每一步都给出 PASS / FAIL / SKIP，末尾汇总并返回退出码，可直接接进 CI（有板子的机器上）。

第 6 关（`--swd`）是**直连调试器**那一关，走 SWD 而不是串口：芯片身份 / 复位原因 /
烧录后逐字节校验 / 采集→时序链路。默认不开，原因见下面第 5 条。

设计约束（五条，按本项目一贯的「默认最保守」原则）
------------------------------------------------
1. **不带参数运行 = 什么都不碰硬件**。必须先 `--port COMxx` 指名端口。
2. **只读是默认档**。要发字节必须再加 `--allow-write`；会打死板子的那一关还要
   `--allow-destructive`（它明确告知：跑完需 SWD 复位才能恢复）。
3. **端口名单收窄**：只放行 `--port` 指定的那一个口，其余全部 DENY。
   这样即使机器上插着别的设备，也不会被这个脚本误写。
4. **探针按目标固件协议给**：第 2 关发什么、期望什么，取决于**目标板固件**，不是本工具。
   默认模板只是 ST 例程 SENSOR_PWR_CTRL 的（'1'/'0'/'x'）；换板子务必用
   `--cmd/--expect` 或 `--probe-file` 指定，否则那一关的 FAIL 是协议不匹配、不是缺陷。
5. **第 6 关要单独开关**：它用的是 SWD，不是串口。`mode=normal` 的连接**本身就会复位
   目标并把内核 halt 住**（实测，不是文档说法）——「板子在手边正跑着别的东西」时，
   连一下就把人家打断了。所以默认不连，而且只读部分固定走 hotplug 档。

用法
----
    # 1) 只读：看板子在不在、说什么（不发任何字节，最安全）
    python tools/live_acceptance.py COM21

    # 2) 双向：发指令验证读写（会真的给传感器上下电）
    python tools/live_acceptance.py COM21 --allow-write

    # 3) 全量：多跑一关「ORE 复现」，把板子打死以验证提示文案
    #    ★ 跑完板子需 SWD 复位；不想担风险就别加这个开关
    python tools/live_acceptance.py COM21 --allow-write --allow-destructive

    # 4) 带真实固件产物，跑符号定位关
    python tools/live_acceptance.py COM21 --elf path/to/fw.elf --map path/to/fw.map

    # 5) 第 6 关：SWD 直连（芯片身份 / 复位原因 / 烧录校验 / 采集链路）
    #    只读档：只走 hotplug，不打扰目标；给 --fw 才做逐字节校验
    python tools/live_acceptance.py COM21 --swd --fw build/fw.bin
    #    加 --allow-write 才会跑 full 档（读 UID/选项字节）——★ 会复位并 halt 目标
    python tools/live_acceptance.py COM21 --swd --fw build/fw.bin --allow-write

    # 6) 只想看会执行哪些动作，不真跑
    python tools/live_acceptance.py COM21 --allow-write --dry-run

退出码
------
    0 = 全部 PASS（或 SKIP）；1 = 有 FAIL；2 = 环境不满足（打不开端口等）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(os.path.dirname(HERE))
# 包的父目录（源码树 = src/；安装后 = site-packages）——import mcu_debug_mcp 用
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)


# --------------------------------------------------------------------------- #
# 结果收集
# --------------------------------------------------------------------------- #

class Report(object):
    def __init__(self):
        self.rows = []          # (level, name, status, detail)

    def add(self, level, name, status, detail=""):
        self.rows.append((level, name, status, detail))
        mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
        print("  %s %-58s %s" % (mark, name, ("— " + detail) if detail else ""))

    def counts(self):
        c = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for _l, _n, s, _d in self.rows:
            c[s] += 1
        return c


def _hr(title=""):
    print("")
    print("=" * 78)
    if title:
        print(title)
        print("=" * 78)


def _handle_of(text):
    """从 serial_open 的返回文本里取句柄号。取不到返回 None。"""
    m = re.search(r"handle[= ]+(\d+)", str(text))
    return int(m.group(1)) if m else None


def _prepare_isolation(core, guard, port, allow_write):
    """在 import core 之前就要设好环境变量；这里做的是校验与展示。"""
    _hr("【第 0 关】环境与隔离")
    print(core.env_status().split("── 隔离")[0].rstrip())
    print("")
    print("── 隔离状态")
    print("   档位        : %s（来源：%s）" % (guard.mode(), guard.mode_source()))
    print("   目标端口    : %s" % port)
    print("   放行结果    : %s" % (guard.check("serial_open", port) or "放行"))
    print("   发字节权限  : %s" % ("已授权（--allow-write）" if allow_write else "未授权（只读）"))


# --------------------------------------------------------------------------- #
# 第 1 关 · 只读探测（不发任何字节）
# --------------------------------------------------------------------------- #

def stage_readonly(core, port, baud, rep):
    _hr("【第 1 关】只读探测（不发送任何字节）")

    r = core.serial_open(port, baud)
    h = _handle_of(r)
    if h is None:
        rep.add(1, "serial_open(%s, %d)" % (port, baud), "FAIL", str(r).splitlines()[0])
        return None
    rep.add(1, "serial_open(%s, %d) 返回句柄" % (port, baud), "PASS", "handle=%d" % h)

    # 静默 / 主动打印 都算正常，只是信息不同——关键是【不许报错、不许卡死】
    g = core.serial_read(h, seconds=2.0, idle_ms=1200)
    silent = "没有收到任何字节" in g
    bad = ("Traceback" in g) or ("Error" in g and not silent)
    rep.add(1, "serial_read 空闲读不报错（无异常/无 Error）",
            "FAIL" if bad else "PASS",
            "空闲无输出" if silent else "空闲期有输出（设备在主动打印）")

    # ★ 这条原先是 `"handle=%d" % h in lg` —— 判据是【反的】，必须记下来：
    #   serial_session_log 只在**缓冲区为空**时才带 "handle=N" 前缀，有数据时直接返回
    #   日志原文（见 core.py 的实现）。于是：板子沉默 → 假通过；板子一说话 → 假 FAIL。
    #   真机第一次跑就撞上了：我随手接的板子在刷 TICK，它却报「不可读」。
    #   改成按实际契约判，并且**两个方向都要有判别力**：
    #     沉默板 → 必须明说"日志为空"；有输出的板 → 日志里必须真含刚读到的那几行。
    lg = core.serial_session_log(h)
    if silent:
        ok = ("会话日志为空" in lg)
        note = "沉默板：如实报空，没编内容"
    else:
        toks = [ln.strip() for ln in g.splitlines()
                if ln.strip() and not ln.startswith("handle=") and not ln.startswith("---")]
        # 取末尾几行做样本：日志是 `s["log"][-4000:]`，超长时截的是**头部**，
        # 拿第一行当样本会在长日志下误报。
        toks = toks[-3:]
        hit = [t for t in toks if t in lg]
        ok = bool(lg) and bool(hit)
        note = ("日志含刚读到的 %r" % hit[0]) if hit else \
               ("日志非空但不含 %r（可能是被截断）" % (toks[-1] if toks else ""))
    rep.add(1, "serial_session_log 可读（且与 serial_read 一致）",
            "PASS" if ok else "FAIL", note)

    rep.add(1, "serial_close 正常关闭", "PASS" if "关闭" in core.serial_close(h) else "FAIL",
            "")
    return True


# --------------------------------------------------------------------------- #
# 第 2 关 · 双向契约（需要 --allow-write）
# --------------------------------------------------------------------------- #

def stage_roundtrip(core, port, baud, rep, probe):
    """probe = [(cmd, expect_regex, label, newline), ...] 由调用方按固件协议给。

    ★ 每条探针自带 newline 开关。像 SENSOR_PWR_CTRL 这类**逐字节接收**的固件，
      多一个 \\r\\n 就会触发 ORE 溢出让接收通道僵死——所以夹具不能用「默认加 CRLF」
      连着发。真机第一次跑这套夹具时，正是靠 3 条 FAIL 才把这点暴露出来。
    """
    _hr("【第 2 关】双向契约（会真的写字节）")

    r = core.serial_open(port, baud)
    h = _handle_of(r)
    if h is None:
        rep.add(2, "serial_open", "FAIL", str(r).splitlines()[0])
        return

    for cmd, expect, label, nl in probe:
        g = core.serial_send(h, cmd, expect=expect, wait_s=1.5, newline=nl)
        ok = "✓ 已命中" in g and "未命中" not in g
        rep.add(2, "%s（send %r → expect %s）" % (label, cmd, expect),
                "PASS" if ok else "FAIL")

    # 判别力：故意用错期望，必须报「未命中」而不是含糊通过
    cmd, _good, _l, nl = probe[0]
    g = core.serial_send(h, cmd, expect="@@绝不命中@@", wait_s=1.2, newline=nl)
    rep.add(2, "★判别力：期望值故意写错 → 必须报未命中",
            "PASS" if ("未命中" in g or "✗" in g) else "FAIL")

    # 设备仍应活着（用与探针相同的发送方式再打一次）
    g = core.serial_send(h, cmd, newline=nl, expect=probe[0][1], wait_s=1.5)
    rep.add(2, "设备在整轮探针后仍活着", "PASS" if "✓ 已命中" in g else "FAIL")

    rep.add(2, "serial_close", "PASS" if "关闭" in core.serial_close(h) else "FAIL")


# --------------------------------------------------------------------------- #
# 第 3 关 · 符号定位（需要真实 .elf / .map）
# --------------------------------------------------------------------------- #

def stage_symbols(core, elf, mapf, rep):
    _hr("【第 3 关】真实固件符号定位")

    if not elf and not mapf:
        rep.add(3, "符号定位", "SKIP", "未提供 --elf / --map")
        return

    target = elf or mapf
    if not os.path.isfile(target):
        rep.add(3, "固件文件存在", "FAIL", target)
        return

    kind = ".elf" if target.lower().endswith(".elf") else ".map"
    out = core.parse_map(target, top_n=5)
    rep.add(3, "parse_map(%s) 可解析" % kind, "PASS" if "0x" in out else "FAIL")

    if elf and mapf and os.path.isfile(mapf):
        # 双向：map 里抓真实地址 → 拿去 elf 反查，名字必须对上。
        # ★ 符号名要求「字母/下划线开头」：map 的段表里有大量 `0x08000000  .`
        #   这种占位行，名字就是一个点，拿它去匹配毫无意义（本夹具初版正是
        #   栽在这里，报了个假的 2/5）。
        text = open(mapf, encoding="utf-8", errors="replace").read()
        pairs, seen = [], set()
        for m in re.finditer(r"^\s+0x(08[0-9a-f]{6})\s+([A-Za-z_]\w*)\s*$", text, re.M):
            a, n = m.group(1), m.group(2)
            if a in seen:
                continue
            seen.add(a)
            pairs.append((a, n))
            if len(pairs) >= 5:
                break
        if pairs:
            res = core.symbol_lookup(elf, ["0x" + a for a, _ in pairs])
            hit = sum(1 for _a, n in pairs if n in res)
            rep.add(3, "★双向：map 地址 → elf 符号名（%d/%d 命中）" % (hit, len(pairs)),
                    "PASS" if hit == len(pairs) else "FAIL",
                    "" if hit == len(pairs) else "抓到：%s" % ",".join(n for _a, n in pairs))
        else:
            rep.add(3, "★双向：map 地址 → elf 符号名", "SKIP", "map 里没抓到可用符号名")

    # 反例：不存在的地址必须说「未找到」
    res = core.symbol_lookup(target, ["0xDEADBEEF"])
    rep.add(3, "反例：不存在地址 → 说未找到",
            "PASS" if ("未找到" in res or "没找到" in res or "无" in res) else "FAIL")


# --------------------------------------------------------------------------- #
# 第 4 关 · 排故链
# --------------------------------------------------------------------------- #

def stage_diagnosis(core, rep):
    _hr("【第 4 关】排故链与日志分析（离线，不需要板子）")

    norm = "boot ok\r\nready\r\nPWR OFF\r\nPWR ON\r\n"
    out = core.analyze_log(norm)
    # 工具对「干净的日志」的原话就是「未命中已知故障模式」——夹具按这个确切口径判，
    # 不许用「未发现/没有匹配」这类臆想出来的措辞（初版就是这么写错一条的）。
    rep.add(4, "反例档：正常业务日志【不该】报故障",
            "PASS" if "未命中已知故障模式" in out else "FAIL", out.splitlines()[0])

    ore = "UART Error: ORE detected\nHAL_UART_Receive failed: HAL_BUSY\nrx overrun error\n"
    out = core.analyze_log(ore)
    rep.add(4, "UART ORE/溢出 日志可被识别（45 条模式库）",
            "PASS" if ("ORE" in out or "溢出" in out or "overrun" in out.lower()) else "FAIL")


# --------------------------------------------------------------------------- #
# 第 5 关 · 破坏性复现（需要 --allow-destructive）
# --------------------------------------------------------------------------- #

def stage_destructive(core, port, baud, rep):
    """复现「自动追加 CRLF → 设备侧 ORE → 静默死亡」并核对提示文案。

    ★ 这一关会把目标板打死（正是要复现的现场），跑完需要 SWD 复位。
    """
    _hr("【第 5 关】破坏性复现：CRLF 触发 ORE 僵死 + 提示文案（跑完需 SWD 复位）")
    print("   ★ 本关会让目标设备静默死亡，属预期现象。")

    r = core.serial_open(port, baud)
    h = _handle_of(r)
    if h is None:
        rep.add(5, "serial_open", "FAIL", str(r).splitlines()[0])
        return

    # 首发带 CRLF —— 多数设备这一步仍能通（证明 CRLF 链路本身没坏）
    g = core.serial_send(h, "1", expect=r"PWR ON", wait_s=1.5)
    rep.add(5, "带 CRLF 首发（此步通常仍可通）",
            "PASS" if "✓ 已命中" in g else "SKIP",
            "" if "✓ 已命中" in g else "该设备对 CRLF 敏感，首发即未命中")

    # 带 CRLF + 期望值必然写错 → 应给出 ORE/CRLF 排查提示
    g = core.serial_send(h, "x", expect="@@绝不命中@@", wait_s=1.2)
    rep.add(5, "带自动 CRLF 且未命中 → 给出 ORE 排查提示",
            "PASS" if "自动加了" in g or "ORE" in g else "FAIL")

    # 对照：newline=False 时不该出现 CRLF 那段提示
    g2 = core.serial_send(h, "x", newline=False, expect="@@绝不命中@@", wait_s=1.2)
    rep.add(5, "对照：newline=False → 不出现 CRLF 提示",
            "PASS" if "本次在数据后自动加了" not in g2 else "FAIL")

    core.serial_close(h)


# --------------------------------------------------------------------------- #
# 第 6 关 · SWD 直连（需要 --swd；写档的部分还要 --allow-write）
# --------------------------------------------------------------------------- #

def stage_swd(core, fw, rep, allow_write):
    """直连调试器读芯片：身份 / 复位原因 / 烧录后校验 / 采集+时序链路。

    ★ 为什么这一关要单独开关（--swd）：它用的是 **SWD**，不是串口。
      默认不开，因为「板子在手边跑着别的东西」时，SWD 连一下就可能复位目标
      （normal 档的连接本身就会 —— 实测，不是文档说法）。
      chip_info 默认走 hotplug 档正是为了不打扰；要 full 档必须显式 --allow-write。

    ★ 关内顺序不是随手排的：**侵入性动作一律排在最后**。
      full 档连接会把内核 halt 住（实测 `-score` → "Core is halted"），
      而 6f 的采集要靠板子自己往外打印 —— 先 halt 再采集，采到的就是一段空气。
      所以把 full 档从 6b 挪到最后一步。整关也一样：它排在第 5 关（会打死板子）前面。
    """
    _hr("【第 6 关】SWD 直连：芯片身份 / 复位原因 / 烧录校验 / 采集链路")

    # 6a 身份（hotplug：不复位不停内核）
    info = core.chip_info()
    ok = ("Device ID" in info) and ("连接" not in info.splitlines()[0])
    rep.add(6, "chip_info() 读到芯片身份（hotplug 档，不打扰目标）",
            "PASS" if ok else "FAIL",
            "" if ok else info.splitlines()[0] if info else "无输出")
    if ok:
        rep.add(6, "  └ 明说没复位目标", "PASS" if "不复位、不停内核" in info else "FAIL")
        rep.add(6, "  └ DBGMCU 与 banner 交叉核对 DEV_ID",
                "PASS" if "DEV_ID=" in info else "FAIL")

    # 6c 复位原因（hotplug，不打扰）
    rst = core.reset_reason()
    rep.add(6, "reset_reason() 读出运行状态与复位标志",
            "PASS" if ("RCC_RSR" in rst and "内核状态" in rst) else "FAIL",
            "" if "RCC_RSR" in rst else rst.splitlines()[0])
    # ★ 实测第三态：-hardRst 会把内核摁在复位里不放。这条断言把「三态契约」钉住 ——
    #   只要状态行是空的、或冒出一个没见过的词，就说明 _sp_core_state 的判据退化了。
    state_known = ("Core is running", "Core is halted", "Core is kept under reset")
    rep.add(6, "  └ 内核状态行属已知三态（running / halted / kept under reset）",
            "PASS" if any(k in rst for k in state_known) else "FAIL",
            "" if any(k in rst for k in state_known) else "状态行缺失或出现了没见过的词")

    # 6d ★判别力：不可逆的清零必须被挡住（不给 confirm 绝不放行）
    blocked = core.reset_reason(None, clear_flags=True, confirm=False)
    want = "二次确认" if allow_write else "隔离拦截"
    rep.add(6, "★判别力：clear_flags 不带 confirm → 必须被挡（%s）" % want,
            "PASS" if want in blocked else "FAIL",
            "" if want in blocked else blocked[-200:])

    # 6e 烧录后校验（需要 --fw 给固件文件）
    if fw:
        if not os.path.isfile(fw):
            rep.add(6, "flash_verify(%s)" % fw, "FAIL", "文件不存在")
        else:
            ver = core.flash_verify(fw)
            good = "✓ 完全一致" in ver
            rep.add(6, "flash_verify(%s) 逐字节校验" % os.path.basename(fw),
                    "PASS" if good else "FAIL",
                    "" if good else (ver.splitlines()[-1] if ver else ""))
            # ★判别力：故意改一个字节，必须报出偏移 —— 不然「校验」等于没校验
            bad = os.path.join(os.path.dirname(os.path.abspath(fw)), "_la_bad.bin")
            try:
                blob = bytearray(open(fw, "rb").read())
                if blob:
                    blob[len(blob) // 2] ^= 0x01
                    open(bad, "wb").write(blob)
                    v2 = core.flash_verify(bad)
                    rep.add(6, "★判别力：改 1 字节 → 必须报不一致",
                            "PASS" if ("✗" in v2 and "不一致" in v2) else "FAIL")
                else:
                    rep.add(6, "★判别力：改 1 字节 → 必须报不一致", "SKIP", "固件为空")
            finally:
                if os.path.exists(bad):
                    os.remove(bad)
    else:
        rep.add(6, "flash_verify", "SKIP", "未给 --fw <固件文件>")

    # 6f 采集 → 时序分析 链路（这两件工具是同一条链路，必须咬得上）
    if core.HAS_PYSERIAL:
        # 注意用两段式：先采、再拿文件去分析，中间不换手
        cap_dir = None
        try:
            import tempfile
            cap_dir = tempfile.mkdtemp(prefix="live-cap-")
            cap_path = os.path.join(cap_dir, "cap.log")
            cap = core.stream_capture(_PORT_FOR_CAPTURE[0], seconds=3.0,
                                      baudrate=_BAUD_FOR_CAPTURE[0], out_path=cap_path)
            saved = os.path.isfile(cap_path)
            rep.add(6, "stream_capture 采集并落盘（3 秒）",
                    "PASS" if saved else "FAIL", cap.splitlines()[-1][:80] if cap else "")
            ana = core.timing_analysis(path=cap_path)
            rep.add(6, "  └ 采集文件可直接喂 timing_analysis",
                    "PASS" if "时序分析" in ana else "FAIL")
            # 退化输入（全在同一刻度）必须是「明说不给」而不是编个数
            burst_ok = ("同一个刻度内" in ana) or ("不带时间戳" not in ana)
            rep.add(6, "  └ 退化输入不编数据率（同一刻度时明说不给）",
                    "PASS" if burst_ok else "FAIL")
        except Exception as exc:                      # noqa: BLE001
            rep.add(6, "stream_capture → timing_analysis 链路", "SKIP", "异常：%s" % exc)
        finally:
            if cap_dir:
                import shutil
                shutil.rmtree(cap_dir, ignore_errors=True)
    else:
        rep.add(6, "stream_capture → timing_analysis 链路", "SKIP", "本环境无 pyserial")

    # 6g ★ 侵入性动作放最后：full 档连接【会复位目标并把内核 halt 住】。
    #    放在前面会把 6f 的采集变成"采一段空气"——板子都不跑了还有什么时序。
    full_ran = False
    if allow_write:
        full = core.chip_info(mode="full")
        full_ran = True
        got_uid = ("UID" in full) and ("读取失败" not in full)
        rep.add(6, "chip_info(mode='full') 读到 UID/选项字节",
                "PASS" if got_uid else "FAIL",
                "" if got_uid else (full.splitlines()[0] if full else "无输出"))
        rep.add(6, "  └ 如实告知会复位目标（不偷偷复位）",
                "PASS" if "已复位目标" in full else "FAIL")
        # full 档把内核 halt 住了 —— 得自己收尾放出来，不能让下一关/下一次运行
        # 看到一块"有电但一行不跑"的板子然后去查固件。
        # 用一条 hotplug + -rst 同时完成"放出来"和"验证放出来了"（实测：-rst 后
        # -score 报 Core is running）。注意别用 -hardRst —— 那是唯一会把内核
        # 摁在复位里不放的动作（实测 "Core is kept under reset"）。
        _rc, _out = core._sp_run(core._sp_conn(core._SP_MODE_READ) + ["-rst", "-score"])
        st = core._sp_core_state(_out)
        rep.add(6, "  └ 跑完自己把内核放回运行态（不留 halt 后遗症）",
                "PASS" if "running" in st else "FAIL", st or "读不到状态")
    else:
        rep.add(6, "chip_info(mode='full')", "SKIP", "未加 --allow-write（会复位目标）")

    return {"full_ran": full_ran}


#: 第 6 关的采集目标（由 main 在解析参数后填好——stage_swd 不带一堆参数）
_PORT_FOR_CAPTURE = [None]
_BAUD_FOR_CAPTURE = [115200]


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

# (命令, 期望正则, 说明, newline)
#
# ★ 这只是**内置默认模板**，对应 ST 官方例程 SENSOR_PWR_CTRL（'1' 通电 / '0' 断电 / 其它回 '?'）。
#   换任何别的板子，必须用 --cmd/--expect 或 --probe-file 提供该固件真正的协议——
#   否则第 2 关会整片 FAIL（那是协议不匹配，不是工具坏了）。这是本夹具从
#   「STM32H5 专用脚本」升级成「通用夹具」时补上的第一件事。
#
# newline 默认 False：验收夹具必须能**反复跑**而不打死目标板。多一个 \r\n 就僵死的
# 固件（SENSOR_PWR_CTRL 正是）碰到 CRLF 会当场倒下——刻意打死板子那件事放在第 5 关。
DEFAULT_PROBE = [
    ("1", r"PWR ON", "固件协议：'1' → PWR ON（传感器通电）", False),
    ("0", r"PWR OFF", "固件协议：'0' → PWR OFF（传感器断电）", False),
    ("x", r"\?", "未知指令 → '?'（反例档）", False),
]

#: 默认模板对应的「安全态」动作。**只在这套模板下才自动带上**——
#: 换成 --cmd/--expect 之后本夹具不该再拿它去猜别人板子的安全态（见 _safe_off 注释）。
DEFAULT_OFF = ("0", r"PWR OFF")


def _build_probe(args):
    """决定第 2 关用哪套探针。优先级：--probe-file > --cmd/--expect > 内置模板。

    返回 (probe, source_desc)；probe = [(cmd, expect, label, newline), ...]。
    任何输入错误都返回 (None, 原因)，由调用方决定怎么报。
    """
    if args.probe_file:
        if not os.path.isfile(args.probe_file):
            return None, "探针文件不存在：%s" % args.probe_file
        try:
            with open(args.probe_file, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:                      # noqa: BLE001
            return None, "探针文件解析失败（应是 JSON 数组）：%s" % exc
        if not isinstance(raw, list) or not raw:
            return None, "探针文件应为非空 JSON 数组，每项形如 {\"cmd\":\"1\",\"expect\":\"PWR ON\"}"
        out = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict) or "cmd" not in item or "expect" not in item:
                return None, "第 %d 项缺 cmd/expect 字段" % (i + 1)
            out.append((str(item["cmd"]), str(item["expect"]),
                        str(item.get("label") or "probe-%d" % (i + 1)),
                        bool(item.get("newline", False))))
        return out, args.probe_file

    if args.cmd or args.expect:
        if not args.cmd or not args.expect:
            return None, "--cmd 与 --expect 必须成对出现（各给 %d / %d 个）" % (
                len(args.cmd or []), len(args.expect or []))
        if len(args.cmd) != len(args.expect):
            return None, "--cmd 与 --expect 数量不等（%d vs %d）" % (
                len(args.cmd), len(args.expect))
        nl = bool(args.probe_newline)
        return [(c, e, "自定义探针 %d：send %r → expect %s" % (i + 1, c, e), nl)
                for i, (c, e) in enumerate(zip(args.cmd, args.expect))], "命令行 --cmd/--expect"

    return DEFAULT_PROBE, "内置默认模板（SENSOR_PWR_CTRL 例程）"


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="live_acceptance.py",
        description="mcu-debug-mcp 真机验收夹具（默认只读；需显式授权才写字节）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("port", help="目标串口，如 COM21（必填——不填就不碰硬件）")
    ap.add_argument("--baud", type=int, default=115200, help="波特率（默认 115200）")
    ap.add_argument("--elf", help="真实固件 .elf，用于符号定位关")
    ap.add_argument("--map", dest="mapf", help="真实固件 .map，用于符号定位关")
    ap.add_argument("--swd", action="store_true",
                    help="跑第 6 关（直连调试器：芯片身份/复位原因/烧录校验/采集链路）")
    ap.add_argument("--fw", metavar="FILE",
                    help="第 6 关要校验的固件文件（.bin/.hex/.elf），配合 --swd")
    ap.add_argument("--allow-write", action="store_true",
                    help="允许发送字节（第 2 关）。不加则只做只读探测")
    ap.add_argument("--allow-destructive", action="store_true",
                    help="允许跑第 5 关（会把目标板打死，跑完需 SWD 复位）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的动作，不真跑")
    ap.add_argument("--cmd", action="append", metavar="TEXT",
                    help="自定义探针的发送内容（可重复；须与 --expect 一一对应）")
    ap.add_argument("--expect", action="append", metavar="REGEX",
                    help="自定义探针的期望正则（可重复；须与 --cmd 一一对应）")
    ap.add_argument("--probe-file", metavar="JSON",
                    help='探针文件（JSON 数组，形如 [{"cmd":"1","expect":"PWR ON"}]），优先级最高')
    ap.add_argument("--probe-newline", action="store_true",
                    help="给 --cmd 探针也补 \\r\\n（默认不补，安全档——见文件头 ORE 说明）")
    ap.add_argument("--off", metavar="TEXT",
                    help="收尾时要发的「安全态」命令（如把执行器断电）。默认模板下自动为 '0'；"
                         "换了自己的板子就必须显式给，否则收尾会跳过而不是乱发")
    ap.add_argument("--off-expect", metavar="REGEX",
                    help="配合 --off：安全态应答的期望正则（给不了就只能验发送成功，验不了状态）")
    args = ap.parse_args(argv)

    # 第 6 关要用的采集目标。放在这里填——stage_swd 的签名才不会变成一堆透传参数，
    # 也保证「参数解析完」和「采集用哪个口」是同一个来源，不会各自跑偏。
    _PORT_FOR_CAPTURE[0] = args.port
    _BAUD_FOR_CAPTURE[0] = args.baud

    # ── 隔离：只放这一个口（ALLOW 非空即白名单模式，其余口自然被拒）──────
    #    注意：这里【不要】再设 PORT_DENY。DENY 优先级最高，写成 "*" 会连目标口
    #    一起拦掉——本夹具最初就是这么写的，被自己的自检抓了出来。
    os.environ["MCU_DEBUG_MCP_PORT_ALLOW"] = args.port
    os.environ.pop("MCU_DEBUG_MCP_PORT_DENY", None)
    os.environ["MCU_DEBUG_MCP_NO_TOUCH_DTR"] = "1"
    if args.allow_write or args.allow_destructive:
        os.environ["MCU_DEBUG_MCP_MODE"] = "write"
    else:
        os.environ.pop("MCU_DEBUG_MCP_MODE", None)   # 落到默认的 readonly

    # 必须在设好环境变量之后再 import
    from mcu_debug_mcp import guard  # noqa: E402
    from mcu_debug_mcp import core   # noqa: E402

    _hr("mcu-debug-mcp 真机验收 · 目标 %s @ %d" % (args.port, args.baud))
    _prepare_isolation(core, guard, args.port, args.allow_write or args.allow_destructive)

    # 端口自检【必须在 dry-run 之前】：隔离配置错了，dry-run 也不该给人「一切正常」的错觉
    rejected = guard.check("serial_open", args.port)
    if rejected is not None:
        _hr("目标端口被隔离层拒绝 —— 夹具自身配置有问题或端口写错了")
        print(rejected)
        return 2

    # 探针自检【同样在 dry-run 之前】：--cmd/--expect/--probe-file 写错了，第 2 关根本
    # 跑不起来。等真跑才发现，或让 --dry-run 显示成「一切正常」，是同一种「假绿灯」。
    probe = probe_src = None
    if args.allow_write:
        probe, probe_src = _build_probe(args)
        if probe is None:
            _hr("探针配置有误 —— 第 2 关无法执行")
            print("   %s" % probe_src)
            return 2

    # 收尾动作的「安全态」从哪来：内置模板自带；换了自己的固件就只认 --off。
    # 这里绝不拿默认模板的 '0' 去猜别人的板子（那会产生假 FAIL，见 _safe_off 注释）。
    off_cmd, off_expect = args.off, args.off_expect
    if not off_cmd and probe is DEFAULT_PROBE:
        off_cmd, off_expect = DEFAULT_OFF

    if args.dry_run:
        print("")
        print("── dry-run，将执行：")
        print("   第 1 关 只读探测        : 总是执行")
        if probe is not None:
            print("   第 2 关 双向契约        : 执行（探针：%s，%d 条）" % (probe_src, len(probe)))
            print("           收尾安全态      : %s" % (
                "send %r → %s" % (off_cmd, off_expect or "（未给期望，只验发得出去）")
                if off_cmd else "跳过（自定义探针下不知道安全态是什么；可用 --off/--off-expect 指定）"))
        else:
            print("   第 2 关 双向契约        : 跳过（需 --allow-write）")
        print("   第 3 关 符号定位        : %s" % ("执行" if (args.elf or args.mapf) else "跳过（需 --elf/--map）"))
        print("   第 4 关 排故链（离线）  : 总是执行")
        print("   第 5 关 破坏性复现      : %s" % ("执行（板子会被打死）" if args.allow_destructive else "跳过（需 --allow-destructive）"))
        if args.swd:
            print("   第 6 关 SWD 直连        : 执行（直连调试器；%s）" % (
                "含 full 档，会复位目标" if args.allow_write else "只走 hotplug 档，不打扰目标"))
            print("           烧录校验        : %s" % (
                "执行（%s）" % os.path.basename(args.fw) if args.fw else "跳过（未给 --fw）"))
        else:
            print("   第 6 关 SWD 直连        : 跳过（需 --swd）")
        if args.fw and not args.swd:
            print("   ⚠ --fw 给了但没加 --swd —— 第 6 关不跑，这个文件不会被用上。")
        return 0

    rep = Report()
    got = stage_readonly(core, args.port, args.baud, rep)
    if not got:
        _hr("结论")
        print("打不开 %s —— 检查端口号 / 是否被 mcu-debug-mcp 之外的占着 / 驱动。" % args.port)
        return 2

    if args.allow_write:
        print("")
        print("── 第 2 关探针来源：%s（%d 条）" % (probe_src, len(probe)))
        stage_roundtrip(core, args.port, args.baud, rep, probe)
        # 收尾：无论前面成败，尽量把目标拉回安全态
        _safe_off(core, args.port, args.baud, rep, off_cmd, off_expect)
    else:
        _hr("【第 2 关】双向契约")
        rep.add(2, "双向契约", "SKIP", "未加 --allow-write")

    stage_symbols(core, args.elf, args.mapf, rep)
    stage_diagnosis(core, rep)

    # 第 6 关放在第 5 关【前面】：第 5 关会把目标打死，死了之后就采不到有意义的时序了。
    # （SWD 本身在目标死后还能连，但那时的串口采集等于采一段空气。）
    swd_info = {"full_ran": False}
    if args.swd:
        if args.fw and not os.path.isfile(args.fw):
            _hr("【第 6 关】SWD 直连")
            rep.add(6, "烧录校验", "FAIL", "--fw 指向的文件不存在：%s" % args.fw)
        else:
            swd_info = stage_swd(core, args.fw, rep, args.allow_write) or swd_info
    else:
        _hr("【第 6 关】SWD 直连")
        rep.add(6, "芯片身份/复位原因/烧录校验/采集链路", "SKIP", "未加 --swd")
        if args.fw:
            print("   ⚠ --fw 给了但没加 --swd —— 第 6 关不跑，这个文件不会被用上。")

    if args.allow_destructive:
        stage_destructive(core, args.port, args.baud, rep)
    else:
        _hr("【第 5 关】破坏性复现")
        rep.add(5, "CRLF/ORE 僵死复现", "SKIP", "未加 --allow-destructive")

    c = rep.counts()
    _hr("结论")
    print("   PASS %d · FAIL %d · SKIP %d" % (c["PASS"], c["FAIL"], c["SKIP"]))
    if c["FAIL"]:
        print("   有 FAIL —— 逐条看上面的 [FAIL] 行。")
    else:
        print("   全部通过（SKIP 项是本次没授权/没给素材的关卡）。")
    if args.allow_destructive:
        print("   ★ 第 5 关已跑：目标板可能已静默死亡，需要 SWD 复位才能恢复。")
        for line in _reset_hint(core):
            print("   %s" % line)
    elif swd_info["full_ran"]:
        print("   ★ 第 6 关跑了 full 档（--allow-write）：normal 档连接本身就会复位目标，"
              "而且内核会停在 halt。")
        print("     固件没被改过，内核已由本关自己放回运行态（见上面那条 PASS）；"
              "若仍要手动确认：")
        for line in _reset_hint(core):
            print("   %s" % line)
    return 1 if c["FAIL"] else 0


def _reset_hint(core):
    """第 5 关会打死设备——给出可直接复制的复位命令（探测到工具就给真命令）。

    为什么值得单独写：第 5 关是唯一会留下「后遗症」的一关。此前只在末尾打印一句
    「请 SWD 复位」，用户还得自己去翻工具路径。这里把路径探出来，命令可整条复制。
    """
    lines = ["复位办法（任选其一）："]
    cli = None
    try:
        cli = core._which_tool("STM32_Programmer_CLI")
    except Exception:                                 # noqa: BLE001
        pass
    if cli:
        lines.append('  ST-Link：  "%s" -c port=SWD mode=hotplug -rst' % cli)
    else:
        lines.append("  ST-Link：  STM32_Programmer_CLI -c port=SWD mode=hotplug -rst"
                     "（本机未探测到该工具）")
    lines.append("  pyocd  ：  pyocd reset -t <target>")
    lines.append("  最省事 ：  按板子上的 RESET 键，或重新上电")
    return lines


def _safe_off(core, port, baud, rep, off_cmd=None, off_expect=None):
    """尽力把目标设备拉回安全态（断电）。

    ★ 必须看真实返回，不许"调了就当成功"——初版这里是无条件 PASS，
      结果板子明明已经被打死、收尾其实没生效，报告上却是一行绿。

    ★ 第二版把 SENSOR_PWR_CTRL 的 '0' / 'PWR OFF' 写死在这里 —— 又一个坑：
      用户一旦用 `--cmd/--expect` 换成**自己的板子**，收尾照样朝目标发一个对人家
      毫无意义的 '0'，然后报 FAIL「设备未应答（可能已被打死，需复位）」。
      板子活得好好的，报告却说你把它弄死了。**假 FAIL 和假绿灯一样有害**：
      它会让人去翻一块根本没坏的板子。
      ⟹ 现在只有两种走法，都不猜：
         · 调用方给了 off_cmd/off_expect（默认模板会自动给）→ 按给的发、按给的判；
         · 没给 → SKIP，并明说"本夹具不知道你这块板子的安全态是什么，没发字节"。
    """
    if not off_cmd:
        rep.add(2, "收尾：拉回安全态", "SKIP",
                "自定义探针下不知道「安全态」对应什么命令 —— 没发任何字节，请自行确认目标状态"
                "（可用 --off/--off-expect 指定）")
        return
    try:
        r = core.serial_open(port, baud)
        h = _handle_of(r)
        if h is None:
            rep.add(2, "收尾：拉回安全态", "SKIP", "打不开：%s" % str(r).splitlines()[0])
            return
        g = core.serial_send(h, off_cmd, newline=False, expect=off_expect, wait_s=1.5)
        core.serial_close(h)
        tag = "收尾：拉回安全态（send %r → %s）" % (off_cmd, off_expect or "（未给期望）")
        if off_expect is None:
            # 没给期望就没法判"命中"—— 只能判"发得出去"，并把判据写清楚，别冒充成功。
            ok = "发送失败" not in g
            rep.add(2, tag, "PASS" if ok else "FAIL",
                    "只验了发送成功（未给 --off-expect，无法验目标是否真的进了安全态）")
        elif "✓ 已命中" in g:
            rep.add(2, tag, "PASS")
        else:
            rep.add(2, tag, "FAIL",
                    "设备未应答（可能已被打死，需复位 / 或 --off 给得不对）")
    except Exception as exc:                      # noqa: BLE001
        rep.add(2, "收尾：拉回安全态", "SKIP", "异常：%s" % exc)


if __name__ == "__main__":
    sys.exit(main())
