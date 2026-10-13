# -*- coding: utf-8 -*-
"""
mcu-debug-mcp · 核心逻辑层（core.py）

所有工具的实现都在这里，零 MCP 框架依赖——可以直接单元测试，server.py 只是薄壳。
依赖策略：pyserial/pyocd 缺失时不炸，对应函数返回安装指引（优雅降级）。
"""

import glob
import json
import os
import re
import shutil
import subprocess
import time

# ---- 依赖可用性探测 ----
try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False

try:
    from pyocd.probe.pydapaccess import DAPAccess
    HAS_PYOCD = True
except Exception:
    HAS_PYOCD = False

# ★ 2026-10-13 C 档重构：src 正规包布局，包内绝对导入原生可用。
#   平铺布局时代的「把自己目录插进 sys.path」hack 退役（os.sys.path 那两行删了）。
#   知识库 / 驱动库 / 日志一律走 paths.* 定位（源码运行 = 包内，pip 安装 = 包内）。
#   历史坑：曾拿代码目录当数据目录，于是 pip 装出来的包里没有 knowledge/drivers，
#   知识类工具集体返回「空」——见 PLAN.md §2（N-01）。

from mcu_debug_mcp import analyzer, guard, paths  # noqa: E402
# analyzer —— 异常模式库；guard —— 运行时隔离层（默认禁写硬件，见 guard.py）；
# paths —— 数据根定位（代码目录 ≠ 数据目录）

# ---------------------------------------------------------------
# 串口会话管理
# ---------------------------------------------------------------
_SESSIONS = {}          # handle -> {"port": Serial, "log": str, "portname": str, ...}
_NEXT_HANDLE = [1]
# 会话日志落盘目录（"记得住"支柱）。
# ★ 不用 os.path.join(sys_dir, "logs")：pip 装到只读的 site-packages 时写不进去，
#   且那等于把代码目录当数据目录。真实落点由 paths.logs_dir() 决定
#   （外置数据根 > 包目录 > ~/.mcu-debug-mcp）。
#   本变量保留为**覆盖点**：非 None 时优先——测试靠它把日志指到临时目录。
_LOGS_DIR = None


def _logs_dir():
    """会话日志目录：显式覆盖 > paths.logs_dir()。"""
    return _LOGS_DIR or paths.logs_dir()


def _deps_msg():
    return ("本环境未安装 pyserial，串口功能不可用。\n"
            "安装：pip install pyserial\n"
            "（分析/驱动/校验类工具不受影响，可继续使用）")


_NO_HANDLE_MSG = "无效的串口句柄 %s（可能已关闭）。请先用 serial_list/serial_open。"


def _open_serial(**kw):
    """打开串口的**唯一入口**（DTR/RTS「不打扰」在这里生效）。

    ★为什么不用 serial.Serial(port=...) 一步到位：
      构造时带 port 会**立刻打开**端口，而端口一开，驱动就按默认把 DTR/RTS 拉起来
      —— 对 ESP32/STM32 这类把 DTR/RTS 接成自动复位电路的板子，等于「只想看一眼，
      设备却重启了」。改成「先构造空实例 → 摆好参数与 DTR/RTS → 再 open()」，
      就能在拉线之前把控制线状态摆对，尽量不打扰正在运行的设备。
      开关：MCU_DEBUG_MCP_NO_TOUCH_DTR=1（默认关，保持驱动原行为，避免「悄悄改了
      行为却没人知道」）。

    ★成立范围：调用契约（构造 → 设属性 → open 的次序）已离线验证；
      「真机上到底会不会复位」需要板卡实测——未验前不要在文档里宣称已解决。
    """
    port = kw.pop("port")
    if str(port).startswith("loop://") or "://" in str(port):
        # pyserial 的 URL 端口（loop:// 回环、socket:// 等）必须走 serial_for_url；
        # Serial(port=...) 不认 URL。这类 URL 的外部副作用由 guard.is_virtual_port 判定。
        return serial.serial_for_url(port, **kw)
    s = serial.Serial()
    s.port = port
    for k, v in kw.items():
        setattr(s, k, v)
    if guard.suppress_dtr():
        try:
            s.dtr = False
            s.rts = False
        except Exception:
            pass
    s.open()
    return s


def get_session(handle):
    """取会话；句柄不存在时返回 None（**不抛异常**）。

    MCP 工具层不能把「用户给了个没用的句柄」当异常抛出去——客户端会收到一句
    Python 内部错误（ToolError），而不是能读懂的排查提示。
    本函数只如实回答「有没有这个句柄」，怎么讲由调用方定（统一用 _NO_HANDLE_MSG）。
    历史坑：原来这里 raise ValueError，导致 serial_read / serial_send /
    serial_session_log 三个工具对无效句柄抛异常，而 serial_close 却友好返回
    ——同一件事两套口径（与 USB VID:PID 两表脱节同型）。
    """
    return _SESSIONS.get(handle)


# ---------------------------------------------------------------
# 串口层实现
# ---------------------------------------------------------------

def serial_list():
    if not HAS_PYSERIAL:
        return _deps_msg()
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return "没有发现任何串口设备。检查：1) USB 线是否插好（是否为数据线而非纯充电线）；2) 驱动是否安装。"
    lines = ["共 %d 个串口：" % len(ports)]
    for p in ports:
        lines.append("- %s：%s" % (p.device, (p.description or "未知设备")))
        if p.vid is not None and p.pid is not None:
            key = "%04X:%04X" % (p.vid, p.pid)
            info = analyzer.identify_usb_serial(key)
            if info:
                lines.append("  芯片 %s（%s）" % (info[0], info[1]))
            else:
                lines.append("  USB ID %s（未收录，装对应厂商驱动）" % key)
    return "\n".join(lines)


def serial_open(port, baudrate=115200, data_bits=8, parity="N", stop_bits=1):
    if not HAS_PYSERIAL:
        return _deps_msg()
    # 隔离闸门：档位 + 端口黑/白名单（拒绝时返回可读说明，不抛异常）
    blocked = guard.check("serial_open", port)
    if blocked:
        return blocked
    par = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}.get(parity.upper())
    if par is None:
        return "校验位只支持 N/E/O，收到：%s" % parity
    try:
        s = _open_serial(
            port=port, baudrate=baudrate,
            bytesize={8: serial.EIGHTBITS, 7: serial.SEVENBITS}.get(data_bits, serial.EIGHTBITS),
            parity=par,
            stopbits={1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}.get(stop_bits, serial.STOPBITS_ONE),
            timeout=0.1,
        )
    except serial.SerialException as e:
        return "打开 %s 失败：%s\n排查：1) 是否被其他软件占用（关掉串口助手）；2) 端口号是否正确。" % (port, e)
    h = _NEXT_HANDLE[0]
    _NEXT_HANDLE[0] += 1
    sess = {"port": s, "log": "", "portname": port, "fh": None, "logfile": None}
    # 会话日志自动落盘（logs/ 目录；只读文件系统等场景静默降级，不影响主流程）
    try:
        logs_dir = _logs_dir()
        os.makedirs(logs_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", port) or "port"
        path = os.path.join(logs_dir, "%s_%s_h%d.log" % (time.strftime("%Y%m%d_%H%M%S"), safe, h))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# mcu-debug-mcp 会话日志\n# 端口: %s @ %d %d%s%d\n# 开启: %s\n"
                     % (port, baudrate, data_bits, parity.upper(), stop_bits,
                        time.strftime("%Y-%m-%d %H:%M:%S")))
        sess["logfile"] = path
    except Exception:
        pass
    _SESSIONS[h] = sess
    msg = ("已打开 %s @ %d 8%s%d，句柄 handle=%d。用 serial_read 读数据。"
           % (port, baudrate, parity.upper(), stop_bits, h))
    # USB CDC 口：提示 baudrate 参数不参与数据路径，免得用户读不到数据时
    # 反复换波特率（那是无效动作），或者以为"波特率对了所以能读到"。
    kind, desc = _port_baud_relevance(port)
    if kind == "usb_cdc":
        msg += ("\n  ⓘ %s 是%s，该口【波特率不影响数据】：打开成功不代表波特率选对了，"
                "读不到数据时别去换波特率，查设备是否在运行/输出。" % (port, desc))
    return msg


def _sess_append(sess, text):
    """会话日志追加（内存累积 + 落盘，落盘失败不影响读数）。"""
    sess["log"] += text
    if sess.get("logfile"):
        try:
            with open(sess["logfile"], "a", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass


def serial_close(handle):
    s = _SESSIONS.pop(handle, None)
    if s is None:
        return "句柄 %s 不存在或已关闭。" % handle
    try:
        s["port"].close()
    except Exception:
        pass
    saved = ""
    if s.get("logfile"):
        saved = "会话日志已落盘：%s（sessions_list 可回看）" % os.path.basename(s["logfile"])
    return "已关闭 %s（handle=%d）。%s" % (s["portname"], handle, saved)


# ---------------------------------------------------------------
# 串口增强：自动波特率 / AT 序列 / 双口对录（依赖 pyserial，缺失优雅降级）
# ---------------------------------------------------------------

def _printable_score(text):
    """一段字节流有多"像话"：可打印比例 + 字母数字占比综合分（0-1）。

    ★只回答"像不像文本"，不回答"是不是正确波特率"——ROM 启动日志与真实应用
      日志在这个分数上几乎不可分（实测 0.974 vs 0.990）。用它单独下结论必然
      翻车，所以 baudrate_autodetect 另加判别力门禁（见该函数）。
    """
    if not text:
        return 0.0
    ok = sum(1 for c in text if 32 <= ord(c) < 127 or c in "\r\n\t")
    if not ok:
        return 0.0
    printable = ok / len(text)
    alnum = sum(1 for c in text if c.isalnum() or c in " :.,-=[]()\r\n") / len(text)
    return printable * 0.4 + alnum * 0.6


# ---------------------------------------------------------------
# 波特率自动探测：取样 → 评分 → ★判别力门禁 → ★USB CDC 短路
#
# 【第一优先】USB CDC 口上这个工具【原理上无意义】，必须先识别出来并短路。
#   2026-09-21 师兄指出：ESP32 用的是 USB CDC，波特率参数不参与数据路径。
#   实测佐证（COM4 / ESP32-S3 原生 USB）：
#     · 同一波特率(115200)连测 3 轮 ⟹ 全 0 字节；而另一次 9600 却拿到 88064 字节。
#       同一波特率的结果从 88KB 到 0 —— 差异只可能来自设备状态/打开时机。
#     · 9 档同分 0.99 的主因也不是"采样不够"，而是 USB CDC 下各档收到的
#       本来就是【完全相同的字节流】，评分当然一样。
#   ⟹ USB CDC 上扫档 = 在测"这次打开时设备有没有在说话"，纯随机。
#     必须直接告诉用户"该口不需要波特率"，而不是给一个看着有区分度的排序。
#
# 【第二】非 USB CDC（真 UART 桥）上才轮到判别力门禁：
#   在 ESP32-S3 原生 USB 口（COM4）上扫 11 档，2400~460800 九档全部 0.99 分，
#   工具于是"推荐"了候选表里的最高档 460800 —— 而板上真实波特率是 115200。
#   根因链（三条缺一不可）：
#     ① 打开串口会翻转 DTR/RTS ⟹ ESP32 复位 ⟹ 每个档位都先收到同一段
#        ROM bootloader 日志（纯 ASCII：rst:0x15 ... boot:0x9 ...）；
#     ② 采样在 len(buf) > 2048 时就 break，真实应用日志(二进制/混合)还没来；
#     ③ scores 全平 ⟹ sort 退化成候选表顺序（降序）⟹ 永远推最高档。
#   ★更坏的情况：正确档反而被压低。同一台机器上 115200 曾因恰好捞到二进制段
#     得 0.071 分，排在"错误档"后面。扣分方向完全反了。
#
#   所以：分数没有区分度时，必须说"判不出来"，绝不输出确定性推荐。
#   这与本项目"禁止看似合理的假数字"是同一件事——一个带推荐值的错误答案，
#   比"我判不出来"危害大得多（后者用户会自己再查，前者会照做）。
# ---------------------------------------------------------------

# 判别力门禁：最高分与次高分的最小差距。低于它 ⟹ 拒绝推荐。
# 取 0.10 的依据：健康样本里正确档(0.99)与错位档(<0.60)天然差 0.4 以上；
# 而同一段文本在不同档下重复出现的"平局"差距恒为 0。0.10 远远分开这两种情形，
# 又不至于因为采样抖动把真实可判的场合误判成"判不出"。
_BAUD_MIN_MARGIN = 0.10
# 采样累计上限：太低会停在 ROM 日志里（见根因②），太高则单档耗时膨胀。
_BAUD_SAMPLE_CAP = 8192

# 已知走 USB CDC（自带 USB 外设，波特率不参与数据路径）的芯片 USB VID/PID。
# 这些口上 baudrate 参数无意义，扫档会得出随机结果。
_USB_CDC_IDS = {
    (0x303A, 0x1001),   # Espressif 原生 USB-Serial-JTAG（ESP32-S3/C3/C6 等）
    (0x303A, 0x0002),   # Espressif USB JTAG/serial debug unit（备用 PID）
    (0x2E8A, 0x0005),   # Raspberry Pi Pico / RP2040 CDC
    (0x2E8A, 0x000A),   # RP2040 MicroPython CDC
    (0x2341, 0x005E),   # Arduino (SAMD CDC)
    (0x2341, 0x804D),
}
# 已知是【独立 UART 桥】（波特率真实生效，扫档有意义）的 VID/PID。
_USB_UART_BRIDGE_IDS = {
    (0x0403, 0x6010),   # FTDI FT2232H（双通道）
    (0x0403, 0x6001),   # FTDI FT232
    (0x10C4, 0xEA60),   # Silicon Labs CP210x
    (0x1A86, 0x7523),   # CH340
    (0x1A86, 0x55D4),   # CH9102
    (0x1366, 0x0105),   # SEGGER J-Link CDC UART
}


def _port_baud_relevance(port):
    """判断一个串口上"波特率是否对数据有影响"。

    返回 (kind, desc)：
      "usb_cdc" ⟹ 自带 USB 外设，baudrate 不参与数据路径，扫档无意义
      "bridge"  ⟹ 独立 UART 桥，baudrate 真实生效
      "serial"  ⟹ 原生/板载 UART（含蓝牙虚拟口），baudrate 通常生效
      "unknown" ⟹ 查不到 VID/PID，无法判定（不阻挡流程，只提示）
    """
    try:
        from serial.tools import list_ports
    except Exception:
        return "unknown", ""
    try:
        for p in list_ports.comports():
            if str(p.device).upper() != str(port).upper():
                continue
            hwid = (p.hwid or "").upper()
            # 蓝牙虚拟串口：无真实 UART，波特率无意义
            if "BTHENUM" in hwid or "BLUETOOTH" in hwid:
                return "usb_cdc", "蓝牙虚拟串口（无物理 UART，波特率不生效）"
            m = None
            if "VID:PID=" in hwid:
                try:
                    vid_pid = hwid.split("VID:PID=")[1].split()[0]
                    a, b = vid_pid.split(":")
                    m = (int(a, 16), int(b, 16))
                except Exception:
                    m = None
            if m in _USB_CDC_IDS:
                return "usb_cdc", "原生 USB CDC（芯片自带 USB 外设，%04X:%04X）" % m
            if m in _USB_UART_BRIDGE_IDS:
                return "bridge", "独立 UART 桥（%04X:%04X）" % m
            return "unknown", (p.description or "")[:40]
    except Exception:
        return "unknown", ""
    return "unknown", ""


def baudrate_autodetect(port, candidates=None, sample_s=0.6, log_text=None):
    """扫描候选波特率，按"最像正常日志"打分排序。

    波特率错了的表现是整段乱码（MOJIBAKE）：换一档读同一段就是可读文本。
    candidates 默认覆盖 STM32/ESP 常用档（74880 是 ESP bootloader 日志专用）。
    有现成日志样本（log_text）时直接离线打分，不占串口。

    ★返回里可能明确写「判不出来」——这是有意的。评分没有区分度时（例如板上
      每个档位都先回同一段 ROM 启动日志），工具拒绝给确定性推荐，免得用户照着
      一个"看着很正常"的错误档位去改代码。判据见模块内 _BAUD_MIN_MARGIN 注释。

    ★★USB CDC 口（如 ESP32-S3 原生 USB、RP2040）上本工具【原理上无意义】：
      波特率不参与数据路径，各档收到的字节流完全相同。此时直接短路返回，
      告诉用户该口不需要波特率，而不是给一个看着有区分度、实则随机的排序。
    """
    if not HAS_PYSERIAL and not log_text:
        return _deps_msg()

    # ---- USB CDC 短路：该口上扫档测的是"这次打开设备有没有在说话"，纯随机 ----
    # 但用户【显式传了 candidates】时视为"我知道我在干什么，强行扫"，放行。
    if not log_text and candidates is None:
        kind, desc = _port_baud_relevance(port)
        if kind == "usb_cdc":
            return (
                "%s 是%s：这个口上【波特率不参与数据路径】，扫档没有意义。\n"
                "  原因：数据走芯片自带的 USB 外设（CDC ACM），收发的字节流与 baudrate 无关；\n"
                "  各档「评分」其实只是同一批字节被反复读了一遍——实测同一波特率连测两轮，\n"
                "  一轮 0 字节、一轮 88064 字节，差异全来自设备当时有没有在输出。\n"
                "  该怎么做：\n"
                "    1) 直接用 serial_open(%r) 不传 baudrate（或随便传），读数据即可；\n"
                "    2) 读不到内容时【不要】换波特率，去查：设备是否在运行/是否复位/固件是否在打印；\n"
                "    3) 若真需要判波特率，得接【独立 UART 桥】的 RX（如 FT2232H/CH340/CP210x）\n"
                "       去听芯片的 UART TX 脚，CPU 自带的 USB 口没有这个过程。\n"
                "  想强行扫（例如怀疑枚举成 CDC 但实际是桥）：传 candidates 参数仍会执行。"
                % (port, desc, port))
    if candidates is None:
        candidates = [921600, 460800, 230400, 115200, 74880, 57600, 38400,
                      19200, 9600, 4800, 2400]
    candidates = [int(b) for b in candidates]

    results = []
    if log_text:
        # 离线模式：对现成日志逐档按"乱码修复视角"打分没有意义，
        # 只给可读性评分作为参考——主要用于测试与教学
        for b in candidates:
            results.append((b, _printable_score(log_text[:2000])))
    else:
        # 硬件分支：会反复开合端口并写探测字节 ⟹ 过隔离闸门（离线分支不碰硬件，不拦）
        blocked = guard.check("baud_probe", port)
        if blocked:
            return blocked
        for b in candidates:
            try:
                s = _open_serial(port=port, baudrate=b, timeout=0.2)
            except serial.SerialException:
                results.append((b, -1.0))
                continue
            try:
                time.sleep(0.05)
                s.write(b"\r\n")            # 捅一下：有些设备收到输入才说话
                buf = b""
                t0 = time.time()
                # ★采样上限抬到 8192：原来 2048 会在 ROM bootloader 日志里就
                #   退出，真实应用日志根本进不了样本（见模块内根因②）。
                while time.time() - t0 < sample_s:
                    chunk = s.read(256)
                    if chunk:
                        buf += chunk
                        if len(buf) > _BAUD_SAMPLE_CAP:
                            break
                text = buf.decode("utf-8", errors="replace")
                results.append((b, _printable_score(text)))
            except Exception:
                results.append((b, -1.0))
            finally:
                try:
                    s.close()
                except Exception:
                    pass

    good = [(b, sc) for b, sc in results if sc >= 0]
    good.sort(key=lambda x: -x[1])
    lines = ["波特率扫描结果（%s）：" % port]
    for b, sc in good:
        # 推荐标记只在【判别力成立】时才打，且只打给真正的第一名
        tag = ""
        if len(good) >= 2:
            margin = good[0][1] - good[1][1]
            if margin >= _BAUD_MIN_MARGIN and sc >= 0.8 and b == good[0][0]:
                tag = " ← 推荐"
        lines.append("  %-7d 评分 %.2f%s" % (b, sc, tag))
    failed = [b for b, sc in results if sc < 0]
    if failed:
        lines.append("  打不开的口（跳过）：%s" % ", ".join(str(b) for b in failed))

    # ---- 判别力门禁：分不出胜负时，拒绝给推荐 ----
    margin = (good[0][1] - good[1][1]) if len(good) >= 2 else 0.0
    confident = bool(good) and good[0][1] >= 0.8 and margin >= _BAUD_MIN_MARGIN

    if not good:
        # ★全部候选都打不开时【必须给结论】：只甩一串"打不开的口"，用户分不清
        #   是口名写错、被别的程序占用、还是驱动没装——那等于没给答案。
        lines.append("★结论：所有候选波特率都打不开 %s——问题【不在波特率】。按顺序查："
                     "① 口名对不对（先用 serial_list 看当前有哪些口）；"
                     "② 口是否被别的程序占用（串口助手/Arduino IDE/另一个终端）；"
                     "③ 驱动是否装好（CH340/CP210x/FTDI 要装对应驱动，设备管理器看有无黄叹号）。"
                     % port)
    elif good[0][1] < 0.5:
        lines.append("注意：最高分也 < 0.5——设备可能没在说话（只上电不输出），"
                     "先复位一次再扫，或它本来就是静默的。")
    elif confident:
        lines.append("建议用 %d 打开：serial_open(port, %d)" % (good[0][0], good[0][0]))
    elif len(good) >= 2:
        # ★这是本工具最容易骗到人的场合：分数排出来了、看着都挺正常，但彼此
        #   差不多 ⟹ 排序没有意义，冠军只是候选表顺序的产物。
        #   （2026-10-13：旧版只判 good 非空，单元素候选表会在 good[1][1] 上
        #    IndexError——baudrate_autodetect(candidates=[115200]) 实测崩过）
        lines.append(
            "★判不出来：最高分 %.2f 与次高分 %.2f 只差 %.2f（门 %.2f），"
            "这批数据没有区分度，上面那个顺序【不代表真实波特率】，别照它改代码。"
            % (good[0][1], good[1][1], margin, _BAUD_MIN_MARGIN))
        lines.append(
            "  典型成因：① 打开串口会翻转 DTR/RTS ⟹ 设备复位 ⟹ 每个档位都先回"
            "同一段 ROM/上电日志（纯 ASCII，各档同分）；② 设备输出以二进制为主，"
            "可读性评分天然分不出对错。")
        lines.append(
            "  改用手工确认：把串口按已知档位打开读一次（serial_open + serial_read），"
            "看是否出现完整可读的日志行；不确定就试 115200（STM32 HAL 默认）"
            "或 74880（ESP bootloader 专用）。")
    return "\n".join(lines)


def _at_line_match(buf, rx, loose=False):
    """在串口累积缓冲里判定 expect 是否命中。

    默认【整行】匹配：把缓冲按行切开，去掉行首尾空白后要求整行被正则覆盖
    （re.fullmatch）。这样设备日志中偶然出现的 "OK" 不会被当成 AT 应答。
    loose=True 时退化为全文子串搜索（旧行为，仅在你明确知道自己在干什么时用）。

    返回 (是否命中, 命中的那一行文本)。
    """
    if loose:
        m = rx.search(buf)
        if m:
            # 找到命中位置所在行，方便回显定位
            start = buf.rfind("\n", 0, m.start()) + 1
            end = buf.find("\n", m.end())
            line = buf[start:end if end != -1 else len(buf)]
            return True, line.strip()
        return False, ""
    # 2026-10-13：半包假 PASS。旧版把 split("\n") 的**最后一段**也当整行匹配，
    # 而它可能是设备还没发完的半行：缓冲以 "OK" 结尾、"\r\n" 还在线上时，
    # 旧代码判 PASS——设备实际要回 "OKX" 或 "ERROR" 也会中。
    # 修法：整行模式下，未被 \n 终结的尾段不参与匹配（设备不吐换行时
    # 会走到超时分支，届时 loose=true 是显式逃生门，比假 PASS 诚实）。
    parts = buf.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for raw in parts[:-1]:
        line = raw.strip()
        if line and rx.fullmatch(line):
            return True, line
    return False, ""


def at_sequence(port, steps, default_baud=115200, total_timeout_s=60, newline=True):
    """批量执行 AT 指令序列并逐条断言（半自动回归测试）。

    steps 形如：
      [{"cmd": "AT", "expect": "OK", "timeout_s": 2},
       {"cmd": "AT+CWJAP?", "expect": "WIFI", "timeout_s": 5},
       {"cmd": "AT+CIPMUX=1", "expect": "OK|ERROR"}]   # expect 是正则，可多选一
    返回每步 PASS/FAIL/TIMEOUT + 汇总，中途失败不中断（方便一次看全）。

    ★判定口径：expect 默认按【整行】匹配（行首行尾允许空白），不是全文子串搜索。
    原因：真实串口上除 AT 应答外还混着设备日志，若用全文搜索，日志里随便出现一个
    "OK" 就会被判成 PASS——那是典型的"看似合理的假通过"。实测在 ESP32(BQ27421 日志)
    上 expect="OK" 曾命中日志文本里的 OK 而误报 PASS。
    若确实想恢复宽松子串匹配，在该步骤里显式写 {"cmd": ..., "expect": "OK", "loose": true}。

    ★ 换行：默认每条指令后补 \r\n（AT 设备必须如此）。但若目标**不是** AT 设备，
      而是逐字节接收的单字符协议（STM32 用 HAL_UART_Receive 轮询一次收 1 字节即
      典型），多补这两个字节会触发 UART 溢出(ORE) 并让接收端僵死。真机实测过：
      同一块板子上 "1" 单字节反复正常，改成 "1\r\n" 后只成功第一次、**之后永久
      无响应**（需复位恢复）。这类设备要传 newline=False，或在单步里写
      {"cmd": "1", "expect": "PWR ON", "newline": false}。
      注意：reset_input_buffer() 会在发指令前清空接收缓冲，对"发完立刻回、且分多
      次吐字节"的裸协议可能丢掉第一段响应 —— 那也是 AT 语义之外的用法，谨慎。
    """
    if not HAS_PYSERIAL:
        return _deps_msg()
    if not steps or not isinstance(steps, list):
        return "steps 需要是列表，如 [{\"cmd\": \"AT\", \"expect\": \"OK\"}]"
    # 会向设备写真实 AT 指令（可能改配置/重启）⟹ 过隔离闸门
    blocked = guard.check("at_sequence", port)
    if blocked:
        return blocked
    try:
        s = _open_serial(port=port, baudrate=int(default_baud), timeout=0.1)
    except serial.SerialException as e:
        return "打开 %s 失败：%s" % (port, e)

    results = []
    t_all = time.time()
    try:
        for i, step in enumerate(steps, 1):
            if not isinstance(step, dict) or not step.get("cmd"):
                results.append((i, "", "SKIP", "步骤格式错（缺 cmd）"))
                continue
            cmd = str(step["cmd"])
            expect = str(step.get("expect") or "OK")
            timeout_s = float(step.get("timeout_s", 3.0))
            loose = bool(step.get("loose", False))
            if time.time() - t_all > total_timeout_s:
                results.append((i, cmd, "SKIP", "总超时 %ds 已到，后面的步骤放弃" % total_timeout_s))
                continue
            nl = bool(step.get("newline", newline))
            try:
                s.reset_input_buffer()
                s.write((cmd + ("\r\n" if nl else "")).encode())
            except Exception as e:
                results.append((i, cmd, "FAIL", "写入失败：%s" % e))
                continue
            try:
                rx = re.compile(expect)
            except re.error as e:
                results.append((i, cmd, "SKIP", "expect 正则无效：%s" % e))
                continue
            buf = ""
            hit_line = ""
            t0 = time.time()
            status, note = "TIMEOUT", "期望 %r 未出现" % expect
            while time.time() - t0 < timeout_s:
                try:
                    chunk = s.read(128)
                except Exception as e:
                    status, note = "FAIL", "读失败：%s" % e
                    break
                if chunk:
                    buf += chunk.decode("utf-8", errors="replace")
                    matched, hit_line = _at_line_match(buf, rx, loose)
                    if matched:
                        status = "PASS"
                        note = "命中 %r（%.1fs）" % (expect, time.time() - t0)
                        break
            tail = buf[-80:].replace("\r", "").replace("\n", " ").strip()
            if status == "PASS" and hit_line:
                note += "｜命中行: %s" % hit_line[:50]
            elif status != "PASS":
                note += "｜回显尾: %s" % tail[:60]
            results.append((i, cmd, status, note))
    finally:
        try:
            s.close()
        except Exception:
            pass

    n_pass = sum(1 for r in results if r[2] == "PASS")
    lines = ["AT 序列结果：%d/%d 通过（%s @ %d）" % (n_pass, len(steps), port, default_baud)]
    for i, cmd, status, note in results:
        lines.append("  [%-7s] %d) %s  → %s" % (status, i, cmd, note))
    if n_pass < len(steps):
        lines.append("失败的步骤单独用 serial_open + serial_send 复现细查。")
    return "\n".join(lines)


def serial_pair_record(port_a, port_b, seconds=10, baud_a=115200, baud_b=115200):
    """同时监听两个串口，输出时间线交错的录制日志（RS485 主从/两设备对话调试）。

    每行前缀 [A]/[B] 标明来自哪个口；输出原始字节 HEX，避免波特率猜错时完全看不懂。
    """
    if not HAS_PYSERIAL:
        return _deps_msg()
    if seconds > 120:
        seconds = 120
    # 同时占住两个端口 ⟹ 两个口都要过隔离检查（任一口被挡就整体不执行）
    for _p in (port_a, port_b):
        blocked = guard.check("pair_record", _p)
        if blocked:
            return blocked
    try:
        sa = _open_serial(port=port_a, baudrate=int(baud_a), timeout=0.05)
    except serial.SerialException as e:
        return "打开失败：%s（确认两个口都存在且没被占用）" % e
    # 2026-10-13：旧版两个口在同一个 try 里开，sb 开失败时 sa 的句柄
    # 直接泄漏（异常路径不经 finally）——Windows 上句柄不释放，
    # 下一次 pair_record 报「端口被占用」，而端口其实没人用。
    try:
        sb = _open_serial(port=port_b, baudrate=int(baud_b), timeout=0.05)
    except serial.SerialException as e:
        try:
            sa.close()
        except Exception:
            pass
        return "打开失败：%s（确认两个口都存在且没被占用）" % e

    events = []     # (t, tag, bytes)
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            for tag, s in (("A", sa), ("B", sb)):
                data = s.read(512)
                if data:
                    events.append((time.time() - t0, tag, data))
    finally:
        try:
            sa.close()
        except Exception:
            pass
        try:
            sb.close()
        except Exception:
            pass

    lines = ["双口录制 %.1fs（A=%s@%d，B=%s@%d），共 %d 段数据："
             % (seconds, port_a, baud_a, port_b, baud_b, len(events))]
    for t, tag, data in events[:400]:
        text = data.decode("utf-8", errors="replace").replace("\r", "").replace("\n", "⏎")
        hexs = data.hex(" ").upper()[:80]
        lines.append("  [%s %6.2fs] %s ｜HEX: %s" % (tag, t, text[:60], hexs))
    if len(events) > 400:
        lines.append("（仅显示前 400 段，共 %d 段）" % len(events))
    if not events:
        lines.append("两个口都一片寂静：设备没在说话。确认波特率、复位设备、"
                     "或先用 baudrate_autodetect 扫档。")
    return "\n".join(lines)


def serial_read(handle, seconds=2.0, expect=None, idle_ms=None):
    """读串口数据（非阻塞轮询，三条件提前退出）。

    expect: 正则字符串，**按【整行】匹配**（re.fullmatch），累计缓冲里任意一整行
            被正则覆盖即提前返回（"发指令等 OK" 场景核心用法）。
            刻意不做子串搜索：设备日志里偶然出现的 "OK" 不该被当成 AT 应答。
            要「包含即命中」写成 expect=".*关键字.*"。
    idle_ms: 静默超时——连续 idle_ms 毫秒无新数据就返回（板子说完就走的场景）
    seconds: 总时长上限（默认 2s，最大 60s）
    """
    if not HAS_PYSERIAL:
        return _deps_msg()
    s = get_session(handle)
    if s is None:
        return _NO_HANDLE_MSG % handle
    ser = s["port"]
    total = max(0.1, min(float(seconds), 60.0))
    deadline = time.time() + total
    idle_deadline = None
    if idle_ms is not None:
        idle_deadline = time.time() + max(10, min(int(idle_ms), 30000)) / 1000.0
    buf = bytearray()
    matched = False
    # 2026-10-13：读循环旧版不捕串口异常——设备被拔/USB 重置时 ser.in_waiting
    # 直接抛 SerialException 给 MCP 客户端（用户看到一坨栈，看不到排查方向）。
    # 现在掉线=一种可诊断的结果：已收到的字节照常返回，并明说后半程丢了。
    try:
        while time.time() < deadline:
            n = ser.in_waiting
            if n:
                buf.extend(ser.read(n))
                if idle_deadline is not None:
                    idle_deadline = time.time() + max(10, min(int(idle_ms), 30000)) / 1000.0
                if expect is not None:
                    text_now = bytes(buf).decode("utf-8", errors="replace")
                    try:
                        matched, _line = _at_line_match(text_now, re.compile(expect))
                    except re.error as e:
                        return "expect 正则无效：%s（示例 \"OK\" 或 \"OK|ERROR\"）" % e
                    if matched:
                        break
            else:
                time.sleep(0.02)
                if idle_deadline is not None and time.time() >= idle_deadline:
                    break
    except Exception as e:
        raw = bytes(buf)
        _sess_append(s, raw.decode("utf-8", errors="replace"))
        return ("handle=%d 读取中串口掉线（%s: %s）。\n"
                "已收到 %d 字节（下方照常给出）。\n"
                "排查：1) USB 线/ hub 供电是否稳；2) 设备是否刚被复位或重枚举"
                "（设备管理器看端口号是否变）；3) 拔插后重新 serial_open。"
                % (handle, type(e).__name__, e, len(raw)))
    raw = bytes(buf)
    text = raw.decode("utf-8", errors="replace")
    _sess_append(s, text)
    if not raw:
        return ("handle=%d 在 %.1f 秒内没有收到任何字节。\n排查：1) 单片机是否在持续打印（很多程序只打印一次，试试 serial_send 发个回车或复位板子）；"
                "2) 波特率是否匹配；3) TX/RX 是否接反。" % (handle, total))
    head = "handle=%d 收到 %d 字节（%.1f 秒）" % (handle, len(raw), total)
    if expect is not None:
        head += "，expect=%r %s（整行匹配）" % (expect, "✓ 已命中（提前返回）" if matched else "✗ 超时未命中")
        # 最气人的一种「假失败」：字符串明明在输出里，却报没命中——因为它是某行的
        # 一部分而不是整行。不点破的话，用户会以为设备没回，去改固件/接线白折腾。
        if not matched and expect in text:
            head += ("\n  ⓘ %r 确实出现在输出里，但只是某行的一部分、没独占一行。"
                     "本工具按【整行】匹配是刻意的（防止设备日志里凑巧出现的 OK 被误判成应答）。"
                     "要改成「包含即命中」，写成 expect=\".*%s.*\"。"
                     % (expect, str(expect).replace("\\", "\\\\")))
    hex_view = " ".join("%02X" % b for b in raw[:256])
    return ("%s\n--- 文本 ---\n%s\n--- HEX（前256字节）---\n%s" % (head, text, hex_view))


def serial_send(handle, data, hex_mode=False, newline=True, expect=None, wait_s=0.5):
    if not HAS_PYSERIAL:
        return _deps_msg()
    s = get_session(handle)
    if s is None:
        return _NO_HANDLE_MSG % handle
    # 写出去的字节能真正改变设备状态 ⟹ 过隔离闸门；端口从会话取，好让端口名单也生效
    blocked = guard.check("serial_write", s.get("portname"))
    if blocked:
        return blocked
    try:
        if hex_mode:
            payload = bytes.fromhex(data.replace(" ", "").replace("\n", ""))
        else:
            payload = data.encode("utf-8")
            if newline:
                payload += b"\r\n"
        s["port"].write(payload)
    except ValueError as e:
        return "发送失败：HEX 格式错误（示例 \"01 03 00 00 00 02 C4 0B\"）。%s" % e
    except Exception as e:
        return "发送失败：%s" % e
    total = max(0.1, min(float(wait_s), 60.0))
    buf = bytearray()
    matched = False
    deadline = time.time() + total
    # 2026-10-13：等响应的读循环同样不捕异常——写成功但设备随后掉线时
    # （写瞬间 USB 重枚举很常见），in_waiting 抛栈给客户端。已发字节数照常报告。
    try:
        while time.time() < deadline:
            n = s["port"].in_waiting
            if n:
                buf.extend(s["port"].read(n))
                if expect is not None:
                    text_now = bytes(buf).decode("utf-8", errors="replace")
                    try:
                        matched, _line = _at_line_match(text_now, re.compile(expect))
                    except re.error as e:
                        return "expect 正则无效：%s（示例 \"OK\" 或 \"OK|ERROR\"）" % e
                    if matched:
                        break
            else:
                time.sleep(0.02)
    except Exception as e:
        _sess_append(s, bytes(buf).decode("utf-8", errors="replace"))
        return ("已发送 %d 字节，但等响应时串口掉线（%s: %s）。\n"
                "设备可能刚被复位或重枚举——重新 serial_open 后再试。"
                % (len(payload), type(e).__name__, e))
    resp = bytes(buf).decode("utf-8", errors="replace")
    _sess_append(s, resp)
    head = "已发送 %d 字节%s" % (len(payload), "（含\\r\\n）" if (not hex_mode and newline) else "")
    if expect is not None:
        head += "，expect=%r %s（整行匹配）" % (expect, "✓ 已命中" if matched else "✗ %.1fs 内未命中" % total)
    auto_crlf = (not hex_mode and newline)
    # ★ 下面这段来自真机实测，不是推测（NUCLEO-H563ZI + SENSOR_PWR_CTRL 固件）：
    #   固件用 HAL_UART_Receive(轮询, 每次收 1 字节) 收命令，工具默认给数据补
    #   \r\n。实测：只发 1 字节 '1' 反复正常；发 "1\r\n" 时**第 1 次照样能收到
    #   "PWR ON"**（所以看响应根本发现不了问题），但之后**永久无响应**，连单字节
    #   也不理，必须复位才恢复。
    #   机制：\r 与 \n 在固件回显那 8 字节期间先后到达 ⟹ UART 溢出(ORE)；
    #   HAL 的轮询接收遇 ORE 提前 return 且没复位 huart->RxState，下次调用直接
    #   返回 HAL_BUSY ⟹ 接收端僵死。
    #   ★ 关键点：判据必须是「expect 没有命中」，不能等「完全无响应」才提示 ——
    #     等收不到任何东西时设备早就死了，而事发现场那条成功响应会把提示吞掉
    #     （实测就是这样：收到 "?" 后走了"有响应"分支，提示根本没机会出现）。
    orel = ""
    if expect is not None and not matched and auto_crlf:
        orel = ("\n★ 另一常见原因：本次在数据后自动加了 \\r\\n（共发出 %d 字节）。"
                "若设备是逐字节接收的单字符协议（典型：STM32 用 HAL_UART_Receive "
                "轮询一次只收 1 字节），这两个多余字节可能在设备回显期间先后到达而"
                "触发 UART 溢出(ORE)；HAL 的轮询接收遇 ORE 会提前返回且没复位内部状态，"
                "下一次调用直接返回 HAL_BUSY —— 接收永久僵死，之后彻底不响应（要复位才恢复）。"
                "先试 newline=False 只发有效字节；需要精确字节就用 hex_mode。" % len(payload))
    elif expect is None and not resp.strip() and auto_crlf:
        # 不传 expect 是最常见用法（如 serial_send(port, "AT")）——这正是上面那条
        # 强提示的盲区：此时 CRLF 把逐字节设备打死，用户只看到「没反应」，零线索。
        # 给一档**弱**提示：列可能原因、不武断归因（没响应也可能是接线/波特率/供电）。
        orel = ("\n★ 没有任何响应。可能原因之一：本次在数据后自动加了 \\r\\n"
                "（共发出 %d 字节）——若设备是逐字节接收的单字符协议，多余字节可能"
                "触发它那边的 UART 溢出(ORE) 而接收僵死（特征是之后彻底不响应、要复位才恢复），"
                "可先试 newline=False 只发有效字节。\n"
                "  其它常见原因：TX/RX 未交叉、波特率不一致、设备未上电/未就绪、"
                "要发的其实是精确二进制（用 hex_mode）。" % len(payload))
    if resp.strip():
        return "%s。响应：\n%s%s" % (head, resp, orel)
    tail = "（无响应——若发的是 AT 指令，结合 AT_NO_RESP 模式排查接线/波特率/供电）"
    return "%s。响应：\n%s%s" % (head, tail, orel)


def serial_session_log(handle):
    s = get_session(handle)
    if s is None:
        return _NO_HANDLE_MSG % handle
    if not s["log"]:
        return "handle=%d 会话日志为空（还没读到过数据）。" % handle
    return s["log"][-4000:]


# ---------------------------------------------------------------
# 会话持久化（"记得住"支柱：每次串口会话自动落盘，可跨轮次回看）
# ---------------------------------------------------------------

def sessions_list(limit=20):
    """列出已落盘的串口会话日志（logs/ 目录，新→旧）。

    会话在 serial_open 时自动创建、读写时自动追加——AI 隔了几轮对话后
    仍能通过 sessions_list + sessions_get 找回之前的串口现场。
    """
    logs_dir = _logs_dir()
    if not os.path.isdir(logs_dir):
        return "还没有会话日志：先 serial_open 打开串口，日志会自动落盘。"
    files = glob.glob(os.path.join(logs_dir, "*.log"))
    if not files:
        return "logs/ 目录为空。serial_open 后会话日志会自动落盘到这里。"
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    limit = max(1, min(int(limit), 100))
    lines = ["共 %d 个会话日志（新→旧）：" % len(files)]
    for f in files[:limit]:
        name = os.path.basename(f)
        port_info = ""
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    if not ln.startswith("#"):
                        break
                    if ln.lower().startswith("# 端口"):
                        port_info = ln.lstrip("#").strip()
                        break
        except Exception:
            pass
        size_kb = os.path.getsize(f) / 1024.0
        mtime = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(f)))
        lines.append("- %s（%s，%.1f KB，%s）" % (name, port_info or "元信息缺失", size_kb, mtime))
    if len(files) > limit:
        lines.append("（仅显示前 %d 个）" % limit)
    lines.append("用 sessions_get(\"文件名\") 回看内容（可整段喂 analyze_log）。")
    return "\n".join(lines)


def sessions_get(fname, tail_lines=200):
    """读取已落盘的会话日志内容（文件名来自 sessions_list）。

    默认返回尾部 200 行——日志开头常是复位前的旧数据，尾巴才是现场。
    tail_lines 最大 5000。
    """
    name = os.path.basename(str(fname or "").strip())
    if not name:
        return "需要文件名，如 sessions_get(\"20260918_174923_COM3_h1.log\")。先 sessions_list 列出。"
    logs_dir = _logs_dir()
    path = os.path.join(logs_dir, name)
    if (os.path.dirname(os.path.abspath(path)) != os.path.abspath(logs_dir)
            or not os.path.isfile(path)):
        return "没有这个会话日志：%r。先 sessions_list 列出可用的。" % name
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        return "读取失败：%s" % e
    body = [l.rstrip("\r\n") for l in all_lines if not l.startswith("#")]
    tail = max(1, min(int(tail_lines or 200), 5000))
    shown = body[-tail:]
    out = ["%s：共 %d 行正文，显示尾部 %d 行" % (name, len(body), len(shown))]
    out.extend(shown)
    if body:
        out.append("（整段日志可直接喂 analyze_log 做整体分析）")
    return "\n".join(out)


# 会话日志的默认保留策略（都不给参数时用这组）
_LOGS_KEEP_DAYS = 30
_LOGS_KEEP_COUNT = 200


def logs_prune(keep_days=None, keep_count=None, confirm=False, dry_run=True):
    """会话日志的**保留策略 + 清理入口**（能力缺口 N-12）。

    为什么需要（实测）：正常用一阵子后 ``~/.mcu-debug-mcp/logs`` 会攒到几十上百个
    ``.log``。单个不大，但**只增不减**——既没有保留策略，也没有地方清。
    实测环境（2026-09-22）已经是 47 个文件。

    保留规则（两个维度；同时给就是「两个都要满足才留」）：
        keep_count=N   保留最新的 N 个，更旧的删
        keep_days=D    保留最近 D 天内改动过的，更旧的删
    都不给时用默认策略（keep_days=30, keep_count=200）。

    ★ 默认 ``dry_run=True``：只列清单、算回收空间，**真的什么都不删**。
    ★ 真删是不可逆操作：需要 write 档 + ``confirm=True``，并由
      ``guard.OPS["logs_prune"]`` 统一把关（删了找不回来，日志常是偶发故障的唯一现场）。
    """
    if keep_days is None and keep_count is None:
        keep_days, keep_count = _LOGS_KEEP_DAYS, _LOGS_KEEP_COUNT
    try:
        kd = None if keep_days is None else max(0, int(keep_days))
        kc = None if keep_count is None else max(0, int(keep_count))
    except (TypeError, ValueError):
        return "参数必须是整数：keep_days / keep_count（收到 %r / %r）" % (keep_days, keep_count)

    logs = _logs_dir()
    if not os.path.isdir(logs):
        return "还没有会话日志目录（%s）——serial_open 打开一次串口后才会创建。" % logs
    files = glob.glob(os.path.join(logs, "*.log"))
    if not files:
        return "会话日志目录为空（%s），无需清理。" % logs

    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)   # 新 → 旧
    now = time.time()

    def _size(paths):
        tot = 0
        for p in paths:
            try:
                tot += os.path.getsize(p)
            except OSError:
                pass
        return tot

    keep, drop = [], []
    for i, f in enumerate(files):
        alive = True
        if kc is not None and i >= kc:
            alive = False                                       # 超出「最新 N 个」
        if kd is not None and (now - os.path.getmtime(f)) > kd * 86400.0:
            alive = False                                       # 超出「最近 D 天」
        (keep if alive else drop).append(f)

    rules = []
    if kc is not None:
        rules.append("保留最新 %d 个" % kc)
    if kd is not None:
        rules.append("保留最近 %d 天内改动的" % kd)

    lines = ["会话日志清理", "-" * 44,
             "  目录     : %s" % logs,
             "  现有     : %d 个，共 %.1f KB" % (len(files), _size(files) / 1024.0),
             "  保留规则 : %s" % ("、".join(rules) or "无（等价于全清）"),
             "  将保留   : %d 个" % len(keep),
             "  待清理   : %d 个，约 %.1f KB" % (len(drop), _size(drop) / 1024.0)]

    # 敏感信息提示：这是「日志治理」的第三条要求，不能只做删旧文件。
    lines.append("  ⓘ 会话日志**原样记录**串口收发内容——AT 指令里的 WiFi 密码、")
    lines.append("    MQTT/HTTP token、密钥都可能落在里面。分享日志前请自行检查。")

    if drop:
        lines.append("")
        lines.append("  待清理文件（新→旧里的尾段，最多列 20 个）：")
        for f in drop[:20]:
            lines.append("    · %s" % os.path.basename(f))
        if len(drop) > 20:
            lines.append("    …… 还有 %d 个" % (len(drop) - 20))

    if dry_run:
        lines.append("")
        lines.append("  ★ 这是 dry-run，**什么都没删**。确实要清理再传 dry_run=False + confirm=True。")
        return "\n".join(lines)

    # 真删：过隔离闸门（不可逆 → 需 write 档 + confirm）
    blocked = guard.check("logs_prune", confirm=confirm)
    if blocked:
        return blocked
    if not drop:
        lines.append("")
        lines.append("  没有需要清理的文件（当前规则下全部保留）。")
        return "\n".join(lines)

    removed, freed, failed = 0, 0, []
    for f in drop:
        try:
            sz = os.path.getsize(f)
            os.remove(f)
            removed += 1
            freed += sz
        except OSError as e:
            failed.append("%s（%s）" % (os.path.basename(f), e))
    lines.append("")
    lines.append("  已删除   : %d 个，回收约 %.1f KB" % (removed, freed / 1024.0))
    if failed:
        lines.append("  ⚠ 删除失败 %d 个：%s" % (len(failed), "；".join(failed[:5])))
    return "\n".join(lines)


def report_session(handle=None, fname=None, out_path=None, tail_lines=40,
                   overwrite=False, confirm=False):
    """把一次串口会话导出成 Markdown 交付报告（能力缺口 G13）。

    为什么需要：调完之后总要把「现象 → 证据 → 结论 → 下一步」交出去
    （毕设报告、工单、给同事看），而手动整理最容易丢的恰恰是**原始证据**。
    这里直接把会话日志 + 故障分析拼成一份可交付的 Markdown，原始日志附在末尾——
    结论是人话，证据可回查。

    数据来源（二选一）：
        handle   当前打开的会话句柄（未关闭也能导，用内存里的日志）
        fname    已落盘的会话日志文件名（sessions_list 给的）
    都不给时：若恰好只有一份会话日志就用它，否则报错并说明怎么给。

    落点：``out_path`` 指定；不给则 ``<数据根>/reports/<时间戳>_session_report.md``。
    ★ 目标已存在时**不覆盖**，除非显式 ``overwrite=True``——报告是交付物，
      不该悄悄盖掉别人的文件。
    """
    log_text, src_desc = None, ""
    if handle is not None:
        s = get_session(handle)
        if s is None:
            return _NO_HANDLE_MSG % handle
        log_text = s.get("log") or ""
        src_desc = "活动会话 handle=%d（%s）" % (handle, s.get("portname", "?"))
    elif fname:
        logs = _logs_dir()
        name = os.path.basename(str(fname).strip())
        path = os.path.join(logs, name)
        if (not name or os.path.dirname(os.path.abspath(path)) != os.path.abspath(logs)
                or not os.path.isfile(path)):
            return "没有这个会话日志：%r。先 sessions_list 列出可用的。" % fname
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            log_text = "".join(l for l in f if not l.startswith("#"))
        src_desc = "已落盘日志 %s" % name
    else:
        logs = _logs_dir()
        files = glob.glob(os.path.join(logs, "*.log")) if os.path.isdir(logs) else []
        if len(files) == 1:
            with open(files[0], "r", encoding="utf-8", errors="replace") as f:
                log_text = "".join(l for l in f if not l.startswith("#"))
            src_desc = "已落盘日志 %s（唯一一份，自动选用）" % os.path.basename(files[0])
        else:
            return ("需要指定数据来源：handle=<会话句柄> 或 fname=<日志文件名>。\n"
                    "当前落盘日志 %d 个——先 sessions_list 看；日志内容喂 analyze_log 也行，"
                    "但要一份**带原始证据的交付报告**就用本工具。" % len(files))

    if log_text is None:
        log_text = ""
    body = [l.rstrip("\r\n") for l in log_text.splitlines()]
    try:
        tail = max(1, min(int(tail_lines or 40), 2000))
    except (TypeError, ValueError):
        tail = 40

    analysis = analyze_log(log_text)
    lines = [
        "# 串口会话诊断报告", "",
        "- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "- 工具版本：mcu-debug-mcp %s" % _pkg_version(),
        "- 数据来源：%s" % src_desc,
        "- 日志规模：%d 行 / %d 字节" % (len(body), len(log_text.encode("utf-8"))),
        "",
        "## 一、故障分析", "",
        analysis, "",
        "## 二、原始日志（尾部 %d 行）" % min(tail, len(body) or 1), "",
        "```text",
    ]
    lines.extend(body[-tail:])
    lines += [
        "```", "",
        "---", "",
        "> 本报告由 mcu-debug-mcp 自动生成：结论来自内置故障模式库（每条给人话诊断 + 排查步骤），",
        "> 原始日志原样附后以便回查——修 bug 最怕的就是只有结论、没有证据。",
    ]
    report = "\n".join(lines)

    if out_path:
        target = os.path.abspath(os.path.expanduser(str(out_path)))
        if os.path.exists(target) and not overwrite:
            return ("目标已存在：%s\n要覆盖请传 overwrite=True"
                    "（报告属交付物，不默认盖掉别人的文件）。" % target)
        if os.path.exists(target) and overwrite and not confirm:
            # 2026-10-13：覆盖已存在文件=毁掉原文件，先要一道显式确认。
            # 旧版 overwrite=True 直接盖，且本操作完全没接 guard 闸门。
            return ("目标已存在：%s\noverwrite=True 会**覆盖**它（没有回收站）。\n"
                    "确认那确实是要盖掉的旧报告，带 confirm=True 再调。" % target)
    else:
        target = os.path.join(paths.writable_root(), "reports",
                              "%s_session_report.md" % time.strftime("%Y%m%d_%H%M%S"))
    # 写本地文件同样过隔离闸门（2026-10-13 审查 P1：本操作此前完全没接，
    # readonly 档下也能写/覆盖主机任意路径）
    blocked = guard.check("session_report")
    if blocked:
        return blocked
    try:
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(report)
    except OSError as e:
        return "写报告失败：%s\n（可用 out_path 指定一个可写位置）" % e

    preview = "\n".join(report.splitlines()[:12])
    return ("报告已生成：%s\n（%d 字节，含 %d 行原始日志尾部）\n\n前 12 行预览：\n%s"
            % (target, len(report.encode("utf-8")), min(tail, len(body)), preview))


def pinmap_get(mcu):
    """输出指定 MCU 的 pinmap JSON 原文（数据手册级复用映射，含 caveats）。"""
    data = _load_pinmap(mcu)
    if data is None:
        return "没有收录 MCU：%r。已收录：%s" % (mcu, ", ".join(pinmap_list()) or "（空）")
    return json.dumps(data, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------
# RTT 通道（pyocd 桥接，实验性）
# ---------------------------------------------------------------

def rtt_read(seconds=5.0, cbuffer=None, probe_serial=None):
    """通过 pyocd 的 RTT 通道读目标板调试日志（⚠ 实验性功能）。

    RTT（SEGGER Real-Time Transfer）：固件在 RAM 放一个 RTT 控制块，
    调试器通过 SWD 轮询读出——比 UART 快得多、不占用串口外设。
    前提：固件集成了 SEGGER RTT 库 + pyocd >= 0.34 + 支持的调试器。
    cbuffer：控制块编号（固件开了多个通道时指定，默认读全部）。
    probe_serial：多调试器时用 probe_list 查到的序列号指定。
    """
    if not HAS_PYOCD:
        return ("本环境未安装 pyocd，RTT 不可用。安装：pip install pyocd\n"
                "先用 probe_list 确认调试器在位；RTT 属实验性功能，失败请退回串口。")
    try:
        from pyocd.core.session import Session
        from pyocd.rtt.rtt import RTT
    except ImportError:
        return ("pyocd 版本过旧（缺 RTT API，需 >= 0.34）。升级：pip install -U pyocd\n"
                "或退回串口方案：serial_list + serial_open。")
    total = max(0.5, min(float(seconds), 60.0))
    session = None
    try:
        session = Session(probe_serial) if probe_serial else Session()
        session.open()
        rtt = RTT(session.target)
        rtt.start()
        buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < total:
            try:
                chunks = rtt.get_data()
                if isinstance(chunks, dict):
                    keys = sorted(chunks) if cbuffer is None else [int(cbuffer)]
                    for k in keys:
                        v = chunks.get(k)
                        if v:
                            buf.extend(v)
                elif chunks:
                    buf.extend(chunks)
            except Exception:
                pass
            time.sleep(0.05)
        text = bytes(buf).decode("utf-8", errors="replace")
        if not text:
            return ("%.1f 秒内 RTT 没有读到数据。排查：\n"
                    "1) 固件是否初始化了 SEGGER RTT 输出（控制块要放 RAM，名字防编译器剔除）；\n"
                    "2) probe_list 确认调试器在位；\n"
                    "3) 芯片是否进了低功耗/睡眠（WFI 时 RTT 挂起）；\n"
                    "4) 芯片 flash 里是不是旧固件（不含 RTT）——重新烧录。"
                    % total)
        hex_view = " ".join("%02X" % b for b in buf[:128])
        return ("RTT 收到 %d 字节（%.1f 秒）\n--- 文本 ---\n%s\n--- HEX（前128字节）---\n%s"
                % (len(buf), total, text, hex_view))
    except Exception as e:
        return ("RTT 读取失败：%s\nRTT 属实验性功能：确认调试器（probe_list）、pyocd 版本（pip show pyocd）、"
                "固件含 RTT 控制块。不行就退回串口：serial_list + serial_open。" % e)
    finally:
        try:
            if session:
                session.close()
        except Exception:
            pass

# ---------------------------------------------------------------
# 分析层（零依赖）
# ---------------------------------------------------------------

_LARGE_LOG_BYTES = 4 * 1024 * 1024


def _large_log_note(nbytes):
    """日志过大时的提示（单独成函数：可单测，不必真造 4 MB 输入）。

    45 条规则是全量扫描，实测约 1.2 MB/s、耗时与体积成正比。没有这句提示时，
    贴一份十几 MB 的日志会让调用方以为「工具卡住了 / 没跑完」，甚至等不到返回就超时。
    这里不改分析行为，只把规模与更快路径说清楚。
    """
    if nbytes <= _LARGE_LOG_BYTES:
        return ""
    return ("\n（注：本次日志 %.1f MB，逐条规则全量扫描，耗时与体积成正比，"
            "请耐心等待返回。若只关心最近一次故障，先取日志尾部（tail）再分析会快得多。）"
            % (nbytes / 1048576.0))


def analyze_log(log_text):
    r = analyzer.analyze_log(log_text)
    out = [r["summary"]]
    for f in r["findings"]:
        out.append("\n[%s] %s（严重度：%s）" % (f["id"], f["name"], f["severity"]))
        out.append("证据：%s" % "；".join(str(e) for e in f["evidence"][:3]))
        out.append("诊断：%s" % f["diagnosis"])
        out.append("下一步：\n" + "\n".join("  %d. %s" % (i + 1, s) for i, s in enumerate(f["next_steps"])))
    note = _large_log_note(len(log_text or ""))
    if note:
        out.append(note)
    return "\n".join(out)


def _crc16_modbus(buf):
    crc = 0xFFFF
    for b in buf:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def modbus_crc(hex_frame, check=False):
    clean = hex_frame.replace(" ", "").replace("\n", "")
    if len(clean) < 4 or len(clean) % 2 != 0:
        return "HEX 格式不对：需要偶数个十六进制字符，如 \"010300000002\""
    try:
        data = bytes.fromhex(clean)
    except ValueError:
        return "HEX 解析失败：包含非十六进制字符。"
    if check and len(data) < 3:
        return "校验模式需要完整帧（数据+CRC，至少 3 字节）。"

    if check:
        body, recv = data[:-2], data[-2:]
        calc = _crc16_modbus(body)
        ok = (recv[0] == (calc & 0xFF)) and (recv[1] == ((calc >> 8) & 0xFF))
        return ("帧 %s：%s（帧内 CRC=%02X %02X，计算 CRC=%02X %02X，低字节在前）"
                % (clean, "CRC 校验通过" if ok else "CRC 不匹配！数据或 CRC 抄错",
                   recv[0], recv[1], calc & 0xFF, (calc >> 8) & 0xFF))
    calc = _crc16_modbus(data)
    return ("数据 %s 的 CRC16-Modbus = %02X %02X（低字节在前；完整帧 = %s %02X %02X）"
            % (clean, calc & 0xFF, (calc >> 8) & 0xFF, clean, calc & 0xFF, (calc >> 8) & 0xFF))


# Modbus 异常码表：优先读 knowledge/error_codes/modbus_exception.json（外置可贡献），
# 缺文件时用内置 fallback（Modbus Application Protocol Specification V1.1b3 第 7 章）。
# ★ 文案必须与 knowledge/error_codes/modbus_exception.json **逐字一致**
#   （fallback 是"知识库文件缺失时保底"，若两处释义不同，就会出现
#    "文件在时一套说法、文件丢了另一套说法"的静默脱节）。
#   一致性由 tests/audit_constants.py 机器校验，改这里不改知识库会直接红。
_MODBUS_EXC_FALLBACK = {
    "1": ("非法功能（Illegal Function）", "从机不支持该功能码——查从机型号支持的指令集"),
    "2": ("非法数据地址（Illegal Data Address）", "寄存器地址超范围——对照从机寄存器表，注意 40001 与 0x0000 的偏移约定"),
    "3": ("非法数据值（Illegal Data Value）", "写入值超范围/寄存器数量为 0——查寄存器表的有效值域"),
    "4": ("从站设备故障（Slave Device Failure）", "从机内部执行出错——看从机状态/重试/复位"),
    "5": ("确认（Acknowledge）：请求已受理，处理中", "长耗时操作（如写 EEPROM），稍后轮询"),
    "6": ("从站设备忙（Slave Device Busy）", "从机在处理前一请求——加间隔重发"),
}

_MODBUS_EXC = analyzer._load_code_table(
    "modbus_exception.json", "Modbus 异常码 %s", _MODBUS_EXC_FALLBACK)

# 标准功能码全表：只列 01-06/15-16 时，标准码 7（读异常状态）等会被说成
# "非标准或请求帧"——那本身也是一种假答案，所以把串行链路常见功能码补齐。
_MODBUS_FC_NAMES = {
    1: "读线圈", 2: "读离散输入", 3: "读保持寄存器", 4: "读输入寄存器",
    5: "写单线圈", 6: "写单个寄存器", 7: "读异常状态", 8: "诊断",
    11: "取通讯事件计数器", 12: "取通讯事件日志",
    15: "写多线圈", 16: "写多个寄存器", 17: "报告从站 ID",
    20: "读文件记录", 21: "写文件记录", 22: "屏蔽写寄存器",
    23: "读/写多寄存器", 24: "读 FIFO 队列", 43: "读设备标识",
}


# ---------------- 错误码查询表（knowledge/error_codes/*.json 的统一入口）----------------

def _errcode_tables():
    """层叠扫描 error_codes/*.json → {表名: 路径}。

    走 paths.layer_files：内置层 + 外置补充层**合并**，同名以外层为准——
    想加一张自己的错误码表，往 ~/.mcu-debug-mcp/knowledge/error_codes/ 丢即可，
    不用改源码，也不会顶掉内置那几张表。
    """
    return {name[:-5]: path
            for name, path in paths.layer_files("knowledge", "error_codes")}


def _resolve_errcode_table(table):
    """把用户给的表名解析成真实表名 → (key, tables, err)。

    命中：key 是真表名、err 为 None；未命中：key 为 None、err 是可直接
    返回给用户的说明；table 为空：key/err 都是 None（由调用方决定怎么说）。

    ★ 这是**唯一**的表名解析口径——error_code_lookup（问答式）与
      errcode_table_get（整表浏览，供 mcu://errcode/{table} 资源用）共用。
      同一件事写两套是本项目栽过的坑（serial_list 的 VID:PID 两张表脱节）。
    """
    tables = _errcode_tables()
    key = (table or "").strip().lower()
    if not key:
        return None, tables, None
    if key in tables:
        return key, tables, None
    cand = [t for t in tables if t.startswith(key) or key in t]
    if len(cand) == 1:
        return cand[0], tables, None
    return None, tables, ("没有名为 %r 的错误码表。可用：%s"
                          % (table, "、".join(sorted(tables))))


# ---------------------------------------------------------------
# 通用帧解析（G08）：NMEA 与自定义二进制
#
# 为什么单开一块：Modbus 有 modbus_parse，NMEA 有固定字段语义，而**自定义二进制帧**
# 才是嵌入式里最常见的一类（各家一套「帧头 + 长度 + 校验」）。没有通用入口时，
# 用户只能肉眼数字节。这里把「切帧 + 校验」做掉，并且切不出来要**明说**——
# 静默丢数据正是这类工具最容易犯、也最难发现的错。
# ---------------------------------------------------------------

#: NMEA talker ID → 星座/系统名（同一语句由不同系统发出时只差这两位）
_NMEA_TALKERS = {
    "GP": "GPS", "GL": "GLONASS", "GA": "Galileo", "GB": "BeiDou",
    "BD": "BeiDou", "GQ": "QZSS", "GN": "多系统组合",
}


def _parse_hex_bytes(text, what="数据"):
    """把 "AA 55 0D 0A" / "0xAA55" / "AA-55" 这类文本转成 bytes；失败给可读原因。"""
    s = re.sub(r"[\s,:;_\-]", "", str(text or "").lower()).replace("0x", "")
    if not s:
        raise ValueError("没有%s（空）。示例：\"AA 55 04 01 02 03\"。" % what)
    if len(s) % 2:
        raise ValueError("%s的十六进制字符数是奇数（%d）——多半少抄/多抄了一位。"
                         % (what, len(s)))
    if not re.fullmatch(r"[0-9a-f]+", s):
        raise ValueError("%s含非十六进制字符：%r" % (what, str(text)[:40]))
    return bytes.fromhex(s)


def _nmea_deg(dm, hemi):
    """NMEA 的 ddmm.mmmm + 半球字母 → 十进制度数。"""
    try:
        v = float(dm)
    except (TypeError, ValueError):
        return None
    deg = int(v // 100)
    dec = deg + (v - deg * 100) / 60.0
    if hemi in ("S", "W"):
        dec = -dec
    return dec


def _nmea_xor(body):
    """NMEA 校验：``$`` 与 ``*`` 之间所有字符的 XOR。"""
    c = 0
    for ch in body:
        c ^= ord(ch)
    return c


def nmea_parse(text):
    """解析 NMEA 0183 语句（GPS / 北斗模块最常见的输出格式）。

    支持 GGA / RMC / GSA / GSV / VTG / GLL，逐句给**人话解读**：UTC 时间、
    经纬度（原始度分 + 十进制度）、定位质量、卫星数、HDOP、速度、日期。

    ★ 每句都验 XOR 校验和；校验不过会明说「这句不可信」——不会把错帧当对的解。
    ★ 非 NMEA 的行会被单独指出并跳过，不当成数据。
    """
    lines = [l.strip() for l in str(text or "").splitlines()]
    lines = [l for l in lines if l]
    if not lines:
        return ('没有输入。贴一段 GPS 输出，例如：'
                '$GNGGA,012345.00,3112.3456,N,12122.3456,E,1,08,0.9,12.3,M,...*hh')

    out, sentences, bad = [], 0, 0
    for raw in lines:
        if not raw.startswith(("$", "!")):
            out.append("· 跳过（不是 NMEA 语句，应以 $ 或 ! 开头）：%s" % raw[:60])
            continue
        sentences += 1
        body, star, ck = raw.partition("*")
        ck_txt = "（无 *hh 校验段，无法校验）"
        if star:
            want = _nmea_xor(body[1:])
            try:
                got = int((ck or "")[:2], 16)
            except ValueError:
                got = None
            if got == want:
                ck_txt = "✓ 校验通过"
            else:
                bad += 1
                ck_txt = ("✗ 校验不通过（帧内 %s，应为 %02X）——这句数据不可信"
                          % ((ck or "")[:2] or "??", want))
        tag = body[1:]
        parts = tag.split(",")
        head = parts[0]
        talker, stype = (head[:2], head[2:]) if len(head) >= 5 else ("", head)
        f = parts[1:]
        sysname = _NMEA_TALKERS.get(talker, talker or "?")

        def g(i):
            return f[i] if i < len(f) else ""

        desc = []
        if stype == "GGA":
            lat, lon = _nmea_deg(g(1), g(2)), _nmea_deg(g(3), g(4))
            q = {"0": "无效定位", "1": "单点定位(GPS)", "2": "差分定位(DGPS)",
                 "3": "PPS", "4": "RTK 固定解", "5": "RTK 浮点解",
                 "6": "估计值"}.get(g(5), g(5))
            desc.append("时间 %s UTC" % g(0))
            desc.append("纬度 %s %s / 经度 %s %s" % (g(1), g(2), g(3), g(4)))
            if lat is not None and lon is not None:
                desc.append("十进制度 %.6f, %.6f" % (lat, lon))
            desc.append("定位质量 %s" % q)
            desc.append("卫星数 %s / HDOP %s（越小越好）" % (g(6), g(7)))
            desc.append("海拔 %s m" % g(8))
        elif stype == "RMC":
            st = {"A": "有效定位", "V": "无效定位"}.get(g(1), g(1))
            lat, lon = _nmea_deg(g(2), g(3)), _nmea_deg(g(4), g(5))
            desc.append("时间 %s UTC / 日期 %s" % (g(0), g(8)))
            desc.append("状态 %s" % st)
            desc.append("纬度 %s %s / 经度 %s %s" % (g(2), g(3), g(4), g(5)))
            if lat is not None and lon is not None:
                desc.append("十进制度 %.6f, %.6f" % (lat, lon))
            try:
                kn = float(g(6))
                desc.append("地面速度 %.2f 节 = %.2f km/h" % (kn, kn * 1.852))
            except ValueError:
                pass
            desc.append("航向 %s°" % g(7))
        elif stype == "GSA":
            fix = {"1": "未定位", "2": "2D 定位", "3": "3D 定位"}.get(g(1), g(1))
            svs = [x for x in f[2:14] if x]
            desc.append("模式 %s（A=自动/M=手动）" % g(0))
            desc.append("定位类型 %s" % fix)
            desc.append("参与解算卫星 %s（%d 颗）" % ("/".join(svs) or "无", len(svs)))
            desc.append("PDOP %s / HDOP %s / VDOP %s" % (g(14), g(15), g(16)))
        elif stype == "GSV":
            desc.append("第 %s/%s 句，共见 %s 颗星" % (g(1), g(0), g(2)))
            for i in range(0, 16, 4):
                sv = g(3 + i)
                if sv:
                    desc.append("  卫星 %s：仰角 %s° 方位 %s° 信噪比 %s dBHz"
                                % (sv, g(4 + i) or "-", g(5 + i) or "-", g(6 + i) or "-"))
        elif stype == "VTG":
            desc.append("对地航向 %s°（真北）" % g(0))
            desc.append("对地速度 %s 节 / %s km/h" % (g(4), g(6)))
        elif stype == "GLL":
            lat, lon = _nmea_deg(g(0), g(1)), _nmea_deg(g(2), g(3))
            desc.append("纬度 %s %s / 经度 %s %s" % (g(0), g(1), g(2), g(3)))
            if lat is not None and lon is not None:
                desc.append("十进制度 %.6f, %.6f" % (lat, lon))
            desc.append("时间 %s UTC / 状态 %s"
                        % (g(4), "有效" if g(5) == "A" else (g(5) or "-")))
        else:
            desc.append("未内置解读的语句类型（原样保留字段，共 %d 个）" % len(f))
            if f:
                desc.append("  " + ",".join(f[:12]))
        out.append("$%s（%s · %s）%s" % (head, sysname, stype or "?", ck_txt))
        out.extend("    " + d for d in desc)

    head_line = "共 %d 条 NMEA 语句" % sentences
    if bad:
        head_line += "，其中 %d 条校验不通过（★ 这些句子的数据不可信，别拿去算位置）" % bad
    out.insert(0, head_line)
    return "\n".join(out)


def frame_parse(data_hex, header="", length_offset=None, length_size=1,
                endian="big", checksum="none", length_includes_header=False,
                max_frames=200):
    """通用二进制帧解析：按「帧头 + 长度字段」切帧，可选逐帧校验（G08）。

    参数（都有明确默认，能少填就少填）：

        data_hex               形如 ``"AA 55 04 01 02 03 04"``
        header                 帧头字节（如 ``"AA55"``）；不给则必须给 length_offset
        length_offset          长度字段相对**帧首**的偏移
        length_size            长度字段字节数（默认 1）
        endian                 ``"big"``（默认）或 ``"little"``
        length_includes_header 长度字段的值是否含「头 + 长度字段本身」
                               （默认 False：值 = 载荷长度）
        checksum               ``"none"``（默认）/ ``"sum8"``（帧末 1 字节，前 n-1 字节
                               和取低 8 位）/ ``"crc16_modbus"``（帧末 2 字节低字节在前）
        max_frames             最多切多少帧（默认 200，上限 2000）；给 0 或 None 取默认

    ★ 判据：切不出来（帧头找不到 / 长度字段越界 / 帧被截断）会**逐条报出来**，
      并附上剩余字节——**绝不静默丢**。这类工具最危险的错就是把数据悄悄吞掉。
    """
    try:
        raw = _parse_hex_bytes(data_hex)
    except ValueError as e:
        return str(e)
    try:
        hdr = _parse_hex_bytes(header, "帧头") if header else b""
    except ValueError as e:
        return str(e)
    if not hdr and length_offset is None:
        return ("需要至少一个切帧依据：header=\"AA55\" 或 length_offset=<长度字段偏移>。\n"
                "都不给时无法确定帧边界。当前共 %d 字节：%s%s"
                % (len(raw), raw[:16].hex(" ").upper(),
                   " …" if len(raw) > 16 else ""))
    endian = "little" if str(endian).lower().startswith("l") else "big"
    ck = (checksum or "none").lower()
    if ck not in ("none", "sum8", "crc16_modbus"):
        # 不认识的校验方式不能当没写（写错名字会被静默忽略，然后用户以为校验过了）
        return ("checksum 只支持 \"none\" / \"sum8\" / \"crc16_modbus\"，"
                "收到的是 %r。\n改对再试——写错名字时如果装作没看见，"
                "你会以为「校验通过」，其实根本没校验。" % checksum)
    #: 帧尾校验字节数（长度字段一般不含它，但它确实占帧内字节）
    ck_len = {"none": 0, "sum8": 1, "crc16_modbus": 2}[ck]
    try:
        lo = int(length_offset) if length_offset is not None else None
        lsz = max(1, min(int(length_size), 4))
    except (TypeError, ValueError):
        return "length_offset / length_size 必须是整数。"

    frames, issues = [], []
    pos, guard, n = 0, 0, len(raw)
    while pos < n and guard < max(1, min(int(max_frames or 200), 2000)):
        guard += 1
        if hdr:
            i = raw.find(hdr, pos)
            if i < 0:
                issues.append("偏移 %d 起（剩余 %d 字节）找不到帧头——这 %d 字节未解析。"
                              % (pos, n - pos, n - pos))
                break
            if i > pos:
                issues.append("偏移 %d 起跳过 %d 字节垃圾数据（到下一个帧头）。"
                              % (pos, i - pos))
            pos = i
        body, plen = b"", 0
        if lo is not None:
            lp = pos + lo
            if lp + lsz > n:
                issues.append("偏移 %d 的长度字段越界（数据只剩 %d 字节）——停止。"
                              % (pos, n - pos))
                break
            val = int.from_bytes(raw[lp:lp + lsz], endian)
            # 长度字段通常只数到「数据」为止——末尾校验字节不算在内（Modbus 就是这样）。
            # 所以总帧长 = 长度字段推出的边界 + 校验字节数，否则会把 CRC 切掉。
            core_end = val if length_includes_header else (lo + lsz + val)
            flen = core_end + ck_len
            if flen <= 0 or pos + flen > n:
                issues.append("偏移 %d 按长度字段算出帧长 %d（含 %d 字节校验），"
                              "但剩余只有 %d 字节——帧被截断。"
                              % (pos, flen, ck_len, n - pos))
                frames.append({"offset": pos, "len": n - pos, "truncated": True,
                               "hex": raw[pos:].hex(" ").upper()})
                break
            body = raw[pos:pos + flen]
            # 帧长 = 头 + 长度字段 + 载荷 + 校验字节，故载荷长度要扣掉后两者
            plen = flen - lo - lsz - ck_len
        else:
            nxt = raw.find(hdr, pos + len(hdr))
            flen = (nxt - pos) if nxt > 0 else (n - pos)
            body = raw[pos:pos + flen]
            plen = max(0, flen - len(hdr) - ck_len)

        ck_txt = ""
        if ck == "sum8" and len(body) >= 2:
            calc = sum(body[:-1]) & 0xFF
            ck_txt = "｜sum8 %s（帧内 %02X / 计算 %02X）" % (
                "✓" if calc == body[-1] else "✗ 不匹配", body[-1], calc)
        elif ck == "crc16_modbus" and len(body) >= 3:
            calc = _crc16_modbus(body[:-2])
            got = body[-2] | (body[-1] << 8)
            ck_txt = "｜CRC16 %s（帧内 %04X / 计算 %04X）" % (
                "✓" if calc == got else "✗ 不匹配", got, calc)
        elif ck != "none":
            ck_txt = "｜（帧太短，放不下校验字节）"

        pl = body
        if hdr and pl.startswith(hdr):
            pl = pl[len(hdr):]
        frames.append({"offset": pos, "len": flen, "payload_len": plen,
                       "hex": body.hex(" ").upper(), "payload": pl.hex(" ").upper(),
                       "ck": ck_txt})
        pos += max(1, flen)

    lines = ["二进制帧解析（header=%s，length_offset=%s，checksum=%s）"
             % (hdr.hex(" ").upper() if hdr else "无", lo if lo is not None else "无", ck),
             "  输入共 %d 字节，切出 %d 帧。" % (n, len(frames))]
    for idx, fr in enumerate(frames[:40], 1):
        if fr.get("truncated"):
            lines.append("  #%d @%d  ★ 截断：剩 %d 字节 %s"
                         % (idx, fr["offset"], fr["len"], fr["hex"][:60]))
            continue
        lines.append("  #%d @%d  帧长 %d  载荷 %d%s"
                     % (idx, fr["offset"], fr["len"], fr["payload_len"], fr["ck"]))
        lines.append("      %s" % fr["hex"][:96])
    if len(frames) > 40:
        lines.append("  …… 还有 %d 帧未显示" % (len(frames) - 40))
    if issues:
        lines.append("  ⚠ 未能解析的部分（**逐条列出，不静默丢弃**）：")
        for it in issues:
            lines.append("    · %s" % it)
    if not frames and not issues:
        lines.append("  没有任何帧被切出来——检查帧头字节或长度字段偏移是否写对。")
    return "\n".join(lines)


def error_code_lookup(table="", code=""):
    """查错误码释义表（外置知识库 knowledge/error_codes/*.json）。

    table 留空 → 列出所有可查的表；
    只给 table → 列出该表全部码；
    table + code → 给出该码的释义、排查方向与**出处**。

    查不到时明说「未收录」，绝不猜——本项目的铁律是宁可回答「不知道」，
    也不给一个看着合理的假释义。
    """
    tables = _errcode_tables()
    if not tables:
        return "错误码知识库为空（knowledge/error_codes/ 下没有 *.json）。"

    if not table:
        lines = ["可查的错误码表（knowledge/error_codes/*.json）："]
        for name in sorted(tables):
            try:
                with open(tables[name], encoding="utf-8") as fh:
                    d = json.load(fh)
                n = len(d.get("entries") or {})
                lines.append("  %-16s %2d 条  %s" % (name, n, (d.get("desc") or "")[:44]))
            except Exception as e:
                lines.append("  %-16s 读取失败：%s" % (name, e))
        lines.append('')
        lines.append('用法：error_code_lookup(table="esp_idf", code="0x105")')
        return "\n".join(lines)

    key, tables, err = _resolve_errcode_table(table)
    if err:
        return err
    # 2026-10-13：与 errcode_table_get（1653 行起）对齐口径——空表名/空知识库
    # 都要给可用提示，而不是 tables[None] 抛 KeyError（table="  " 实测崩过）。
    if not tables:
        return "错误码知识库为空（knowledge/error_codes/ 下没有 *.json）。"
    if key is None:
        return ('用法：error_code_lookup(table="esp_idf", code="0x105")\n可用：%s'
                % "、".join(sorted(tables)))

    with open(tables[key], encoding="utf-8") as fh:
        d = json.load(fh)
    entries = d.get("entries") or {}
    title = d.get("desc") or key
    fsrc = d.get("source") or ""

    if not code:
        lines = ["%s（表 %s，共 %d 条）：" % (title, key, len(entries))]
        for k, v in entries.items():
            lines.append("  %-12s %s" % (k, (v.get("meaning") or "")[:58]))
        lines.append("")
        lines.append('用法：error_code_lookup(table="%s", code=<码>)' % key)
        return "\n".join(lines)

    def _norm(x):
        x = str(x).strip().lower()
        return x[2:] if x.startswith("0x") else x

    if code in entries:
        hit = code
    else:
        cands = [k for k in entries if _norm(k) == _norm(code)]
        if len(cands) == 1:
            hit = cands[0]
        elif len(cands) > 1:
            return ("码 %r 在表 %s 里对应多条（%s）——请给出更精确的写法，"
                    "我不替你选。" % (code, key, "、".join(cands)))
        else:
            return ("%s 里没有码 %r（未收录）。该表已收录：%s\n"
                    "⚠ 未收录就是查不到，别据此推断释义。"
                    % (title, code, "、".join(str(k) for k in entries.keys())))

    v = entries[hit] or {}
    out = ["%s = %s" % (hit, v.get("meaning") or "(无释义)")]
    if v.get("hint"):
        out.append("排查：%s" % v["hint"])
    out.append("出处：%s" % (v.get("source") or fsrc or "(未标注)"))
    return "\n".join(out)


def errcode_table_get(table):
    """输出指定错误码表的 JSON 原文（条目级 source 一并可见）。

    与 error_code_lookup 的分工：lookup 是「问一个码」的问答式输出；
    本函数是整表浏览，供 MCP 资源 mcu://errcode/{table} 使用——
    AI 想通读某张表、或想逐条看依据（source）时，读原文最省事。
    表名解析复用 _resolve_errcode_table，与 lookup 同一套口径。
    """
    key, tables, err = _resolve_errcode_table(table)
    if not tables:
        return "错误码知识库为空（knowledge/error_codes/ 下没有 *.json）。"
    if err:
        return err
    if key is None:
        return ('用法：errcode_table_get("esp_idf")\n可用：%s'
                % "、".join(sorted(tables)))
    with open(tables[key], encoding="utf-8") as fh:
        return fh.read()


def modbus_parse(hex_frame):
    """解析 Modbus RTU 帧：正常响应拆出寄存器值，异常响应（功能码|0x80）直译异常码。

    异常码 01-06 直译是本工具的核心价值——手册是英文的，AI 现场翻不出来。
    """
    clean = hex_frame.replace(" ", "").replace("\n", "").lower()
    if len(clean) < 8 or len(clean) % 2 != 0:
        return ("HEX 格式不对：Modbus RTU 帧至少 4 字节（addr+fc+crc16）且偶数个十六进制字符，"
                "如 \"01 83 02 C0 F1\"")
    try:
        data = bytes.fromhex(clean)
    except ValueError:
        return "HEX 解析失败：包含非十六进制字符。"

    addr, fc = data[0], data[1]
    body, recv = data[:-2], data[-2:]
    calc = _crc16_modbus(body)
    crc_ok = (recv[0] == (calc & 0xFF)) and (recv[1] == ((calc >> 8) & 0xFF))
    lines = ["帧 %s（从站地址 %d）" % (clean, addr)]
    lines.append("CRC：%s（帧内 %02X %02X，计算 %02X %02X，低字节在前）"
                 % ("通过" if crc_ok else "不匹配！抄错或干扰", recv[0], recv[1], calc & 0xFF,
                    (calc >> 8) & 0xFF))

    if fc & 0x80:
        # 异常响应帧：addr + (fc|0x80) + exception_code + crc = 5 字节
        real_fc = fc & 0x7F
        lines.append("这是【异常响应】：功能码 %02X = %d|0x80，对应主站请求的 %s" %
                     (fc, real_fc, _MODBUS_FC_NAMES.get(real_fc, "功能 %d" % real_fc)))
        if len(data) != 5:
            lines.append("异常帧长度应为 5 字节（addr+fc+异常码+CRC16），当前 %d 字节——"
                         "CRC 或抓帧可能有误" % len(data))
            return "\n".join(lines)
        code = str(data[2])
        lines.append(_MODBUS_EXC.get(code, "Modbus 异常码 %s = 未知异常码（查从站手册）" % code))
        return "\n".join(lines)

    # 正常响应
    fc_name = _MODBUS_FC_NAMES.get(fc, "功能码 %d（0x%02X，非标准或请求帧）" % (fc, fc))
    lines.append("功能码 %02X：%s" % (fc, fc_name))
    if fc not in _MODBUS_FC_NAMES and not crc_ok:
        lines.append("⚠ 功能码非标准、CRC 也对不上：这帧很可能【不是 Modbus RTU 帧】"
                     "（被别的协议或二进制数据当 Modbus 贴进来了）。下面即使有解读也别当结论。")
    payload = data[2:-2]

    # ★ 帧结构校验：长度与功能码对不上时必须【拒绝解读并说清原因】，不能只回一句
    #   "功能码 XX" 就结束——那是本项目最忌讳的"看似成功的假解析"。
    #   真实数据回放已复现：4 字节的 "01 03 00 00" 曾被当成一次正常的"读保持寄存器"。
    if fc in (3, 4):
        if not payload:
            lines.append("⚠ 未解读：功能码 %d 的【响应帧】至少 6 字节"
                         "（addr+fc+字节数N+数据…+CRC16），当前只有 %d 字节、连字节数字段都没有。"
                         "这更像是【请求帧】或被截断的帧，不是响应——别当解析结果用。" % (fc, len(data)))
            return "\n".join(lines)
        n = payload[0]
        regs = payload[1:]
        if n == 0 or n % 2 != 0 or n != len(regs):
            lines.append("字节数 %d 与实际 %d 不一致——帧可能截断/抄错" % (n, len(regs)))
        else:
            for i in range(0, n, 2):
                lines.append("寄存器 0x%04X = %d (0x%04X)"
                             % (i // 2, (regs[i] << 8) | regs[i + 1], (regs[i] << 8) | regs[i + 1]))
    elif fc in (1, 2):
        if not payload:
            lines.append("⚠ 未解读：功能码 %d 的【响应帧】至少 6 字节"
                         "（addr+fc+字节数N+状态字节…+CRC16），当前只有 %d 字节。"
                         "这更像是【请求帧】或被截断的帧，不是响应。" % (fc, len(data)))
            return "\n".join(lines)
        n = payload[0]
        bits = payload[1:]
        if n != len(bits):
            lines.append("字节数 %d 与实际 %d 不一致——帧可能截断/抄错" % (n, len(bits)))
        else:
            for i in range(0, n):
                vals = ["%d=%d" % (i * 8 + b, (bits[i] >> b) & 1) for b in range(8)]
                lines.append("线圈状态字节 %d：%s" % (i, " ".join(vals)))
    elif fc in (5, 6):
        if len(payload) != 4:
            lines.append("⚠ 未解读：功能码 %d 的响应固定 8 字节（addr+fc+地址2+值2+CRC2），"
                         "当前 %d 字节——帧不完整/抄错，或这其实不是响应帧。" % (fc, len(data)))
            return "\n".join(lines)
        lines.append("回显确认：地址 0x%04X，值/数量 %d" % ((payload[0] << 8) | payload[1],
                                                     (payload[2] << 8) | payload[3]))
    elif fc in (15, 16):
        if len(payload) != 4:
            lines.append("⚠ 未解读：功能码 %d 的响应固定 8 字节（addr+fc+地址2+数量2+CRC2），"
                         "当前 %d 字节——帧不完整/抄错。" % (fc, len(data)))
            return "\n".join(lines)
        lines.append("回显确认：地址 0x%04X，值/数量 %d" % ((payload[0] << 8) | payload[1],
                                                     (payload[2] << 8) | payload[3]))
    else:
        lines.append("（功能码 %d 的响应帧本工具暂不拆解——只给了帧头与 CRC 判定，别当"
                     "解析完成。）" % fc)
    return "\n".join(lines)


# ---------------------------------------------------------------
# 探测层
# ---------------------------------------------------------------

def probe_list():
    """列出接在本机的调试器。

    ★ 为什么必须把「可执行文件」与「Python 模块」分开说：
      实测本机装着 VS Code ARM 扩展自带的 pyocd.exe——磁盘上真有、能直接跑，
      但那个 pyocd 只活在扩展自己目录里，不会进当前解释器的 sys.path。
      于是只看 `import pyocd` 会得出「没装 pyocd」，只看 exe 又会以为能直接调
      DAPAccess——两种口径混为一谈，就会出现「env_status 说发现了 pyocd、
      probe_list 说未安装」这种自相矛盾（本会话实机踩到）。分开报才算诚实。
    """
    exe = _which_tool("pyocd")
    if not HAS_PYOCD:
        if exe:
            return ("找到 pyocd 可执行文件：%s\n"
                    "但当前 Python 解释器里 import pyocd 失败，本进程无法直接用它做枚举。\n"
                    "两种修法：\n"
                    "  1) pip install pyocd —— 装进当前解释器，功能最全（推荐）\n"
                    "  2) 直接用那个可执行文件：\"%s\" list\n"
                    "期间可人工排查：设备管理器里看调试器是否识别；ST-Link 需装官方驱动；"
                    "DAPLink 免驱；swd 排线 SWDIO/SWCLK/GND 三根线是否插对。" % (exe, exe))
        return ("本环境没有可用的 pyocd：既没装 Python 模块，也没找到 pyocd 可执行文件。\n"
                "安装：pip install pyocd\n"
                "期间可人工排查：设备管理器里看调试器是否识别；ST-Link 需装官方驱动；"
                "DAPLink 免驱；swd 排线 SWDIO/SWCLK/GND 三根线是否插对。")
    try:
        probes = DAPAccess.get_connected_devices()
    except Exception as e:
        return "探测失败：%s" % e
    if not probes:
        return "没有发现调试器。检查 USB 连接与驱动（ST-Link 装 ST 官方驱动，DAPLink 免驱）。"
    lines = ["共 %d 个调试器：" % len(probes)]
    for p in probes:
        lines.append("- %s（%s）" % (p.product_name, p.vendor_name))
        try:
            lines.append("  序列号：%s" % p.serial_number)
        except Exception:
            pass
    return "\n".join(lines)

# ---------------------------------------------------------------
# 驱动模板库
# ---------------------------------------------------------------

def _driver_dirs():
    """层叠列举驱动库 → [(名, 路径)]（同名以外层为准）。"""
    return paths.layer_dirs("drivers")


def _driver_dir(name):
    """按名解析驱动目录（大小写不敏感）；没有返回 None。"""
    key = (name or "").strip().lower()
    if not key:
        return None
    for nm, p in _driver_dirs():
        if nm.lower() == key:
            return p
    return None


def _driver_meta(name):
    ddir = _driver_dir(name)
    meta_path = os.path.join(ddir, "meta.json") if ddir else ""
    if meta_path and os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def driver_list():
    dirs = _driver_dirs()
    if not dirs:
        return "驱动库目录不存在。"
    lines = ["共 %d 个预验证驱动：" % len(dirs)]
    for n, _p in dirs:
        meta = _driver_meta(n)
        lines.append("- %s：%s（验证：%s）" % (n, meta.get("desc", ""), meta.get("verified", "见 meta.json")))
    return "\n".join(lines)


def driver_get(name):
    # ★ 空名不能当成「某个驱动目录」：os.path.join(驱动库, "") 拼出来的是驱动库
    #   根目录，而它**确实存在** ⟹ 会走进「目录存在但没内容」分支，输出
    #   "驱动  目录存在，但里面没有任何可读内容"——用户完全看不懂（真实回放已复现）。
    dirs = _driver_dirs()
    if not dirs:
        return "驱动库目录不存在。"
    if not (name or "").strip():
        return "没给驱动名。可用：%s" % ", ".join(n for n, _p in dirs)
    ddir = _driver_dir(name)
    if not ddir:
        return "没有名为 %s 的驱动。可用：%s" % (name, ", ".join(n for n, _p in dirs))
    parts = []
    meta = _driver_meta(name)
    if meta:
        parts.append("【验证记录】%s" % json.dumps(meta, ensure_ascii=False))
    for pattern in ("*.md", "*.h", "*.c"):
        for f in sorted(glob.glob(os.path.join(ddir, pattern))):
            with open(f, "r", encoding="utf-8") as fh:
                parts.append("===== %s =====\n%s" % (os.path.basename(f), fh.read()))
    if not parts:
        # ★ 空响应也算假答案：用户分不清"驱动确实是空的"还是"工具没干活"
        return ("驱动 %s 目录存在，但里面没有任何可读内容（既没有 meta.json，"
                "也没有 *.md/*.h/*.c）——这个驱动还没写全，或被误删了。"
                "用 driver_list 看其他可用驱动。" % name)
    return "\n\n".join(parts)


def driver_wiring(name):
    """查驱动接线知识库（wiring.md）：接什么引脚、坑在哪、怎么解读输出。"""
    dirs = _driver_dirs()
    if not dirs:
        return "驱动库目录不存在。"
    ddir = _driver_dir(name)
    if not ddir:
        return "没有名为 %s 的驱动。可用：%s" % (name, ", ".join(n for n, _p in dirs))
    path = os.path.join(ddir, "wiring.md")
    if not os.path.exists(path):
        return "驱动 %s 暂无 wiring.md。" % name
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------
# 器件选型库（G01）+ 外设 API 用法知识层（G03）—— 全部从驱动库**派生**，零新数据
#
# ★ 为什么不开 knowledge/components/ 目录（2026-10-13）：
#   PLAN §9 决策 5 定死「产能口径必须先定，宁可少而准，不许凭记忆灌」。
#   器件信息在 drivers/<name>/meta.json 里已经是**逐条过过 validate_knowledge
#   的真实数据**（desc / usage / common_pitfalls / verified）；另存一份就是制造
#   第二事实来源——两份必然漂移，最后谁也不敢信。所以这两层只做派生视图：
#   源没了视图自动空（不会出现「视图还在、知识早没了」的静默退化）。
#
# ★ 派生证据从哪来（不是从模型记忆）：
#   bus          ← meta.desc + meta.usage + *_port.h 里的真实外设符号
#   address      ← meta.usage 里的 7 位地址字面量（正则抓 0xNN）
#   timing       ← port 层是否提供 _delay_us（DWT 微秒时序，单总线/GPIO 时序协议依赖）
#   hal_symbols  ← port 层里真实出现的 HAL_*/I2C1/TIM1... 符号
#   init_requires← port 头注释里「先…初始化 / 必须…」的原句（不改写、不润色）
# ---------------------------------------------------------------

# 总线判定：按优先级从上到下，先命中先算；全不命中记 unknown（不猜）
_BUS_RULES = (
    ("i2c", ("i2c",)),
    ("uart", ("uart", "usart", "串口")),
    ("timer_pwm", ("pwm", "hal_tim", "ccr", "定时器")),
    ("gpio_timing", ("gpio", "trig", "echo", "pin_out", "pin_read")),
)

_PORT_SYM_RE = re.compile(
    r"HAL_[A-Za-z_0-9]+"
    r"|\b(?:I2C\d?|SPI\d?|USART\d+|UART\d+|TIM\d+|GPIO[A-Z]?|DWT)\b")
_HEX_ADDR_RE = re.compile(r"0[xX][0-9A-Fa-f]{2}")
# 初始化顺序要求：只摘 port 头注释里的原句，不改写
_INIT_REQ_RE = re.compile(
    r"^[^\n]*?(?:先|必须|需要|确保)[^\n]*?(?:初始化|配置|Init|Start|start)[^\n]*$",
    re.M)


def _port_source(name):
    """读驱动的 *_port.h 原文（适配层是外设用法的唯一真身）。"""
    ddir = _driver_dir(name)
    if not ddir:
        return ""
    texts = []
    for f in sorted(glob.glob(os.path.join(ddir, "*_port.h"))):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                texts.append(fh.read())
        except Exception:
            pass
    return "\n".join(texts)


def _uniq(seq, limit=None):
    """保序去重（派生视图里同一符号常在多处出现）。"""
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
            if limit and len(out) >= limit:
                break
    return out


def _component_bus(meta, port_text):
    hay = " ".join([meta.get("desc", ""), meta.get("usage", ""),
                     port_text]).lower()
    for bus, keys in _BUS_RULES:
        for k in keys:
            if k.lower() in hay:
                return bus
    return "unknown"


def _needs_us_timing(port_text):
    """这个驱动的适配层是否真需要**微秒精度**延时。

    ★ 为什么按 DWT 符号判、不按 `_delay_us` 函数名判（2026-10-13 实测踩过）：
      stepper_28byj48 也有 `stepper_delay_us()`，但它注释里写的是
   「HAL_Delay 毫秒粒度即可，半步延时通常 1-10ms 量级」——函数名只是接口
   统一而已。按函数名判会把它误标成「需微秒时序」，用户照着去配 DWT 属于
   白费功夫。反过来 dht11/ds18b20/hcsr04 的注释里明写「毫秒粒度不能用」
   且示例实现就是 DWT->CYCCNT——判据落在 DWT 上才对得上真机事实。
    """
    return "DWT" in port_text


def _split_clauses(text):
    """按中文分句切文本，但**括号内不切**。

    ★ 为什么需要括号感知（实测踩过）：mpu6050 的 usage 是
    「port 层提供 I2C 读写两函数（7 位地址 0x68，AD0 接地）」——括号里那个逗号
    是 0x68 与 AD0 之间的分隔，按裸逗号切会把它腰斩成「7 位地址 0x68」，
    用户读到的是句残话。
    """
    out, buf, depth = [], [], 0
    for ch in text or "":
        if ch in "（(【[":
            depth += 1
        elif ch in "）)】]":
            depth = max(0, depth - 1)
        if depth == 0 and ch in "，；;":
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [x for x in out if x]


def _init_requirements(meta, port_text):
    """移植时的初始化/接线硬要求——两条真实证据源，合并去重：

    1. 适配层注释里的原句（「工程里先完成 HAL_I2C_MspInit 与 hi2c1 初始化」）；
    2. meta.usage 里点名 port 的分句（「port 里改定时器时钟/PSC/ARR 三处即可」）。
    只摘原文，不改写、不润色。
    """
    reqs = [s.strip(" */*") for s in _INIT_REQ_RE.findall(port_text)]
    for p in _split_clauses(meta.get("usage", "")):
        # 去掉「复制 xxx.h 进工程」这类文件搬运说明，只要移植动作本身
        if "port" in p.lower() and not p.startswith("复制"):
            reqs.append(p)
    return _uniq(reqs)


def component_records():
    """器件选型记录（结构化，G01 的数据层；coverage.json 探针数它的长度）。"""
    out = []
    for name, _p in _driver_dirs():
        meta = _driver_meta(name)
        if not meta:
            continue
        port_text = _port_source(name)
        addrs = _uniq(_HEX_ADDR_RE.findall(meta.get("usage", "")))
        out.append({
            "name": name,
            "desc": meta.get("desc", ""),
            "usage": meta.get("usage", ""),
            "bus": _component_bus(meta, port_text),
            "address": addrs[0] if addrs else "",
            # 微秒时序依赖：单总线 / HC-SR04 这类协议不给微秒级延时就读不出数据
            "us_timing": _needs_us_timing(port_text),
            "pitfalls": meta.get("common_pitfalls", ""),
            "verified": meta.get("verified", ""),
            "source": "drivers/%s/meta.json" % name,
        })
    return out


def peripheral_records():
    """外设 API 用法记录（结构化，G03 的数据层）。

    每条回答「这个驱动到底要哪几个外设、外设初始化顺序上有什么硬要求」——
    答案全部取自 *_port.h 里的真实代码与注释。
    """
    out = []
    for name, _p in _driver_dirs():
        meta = _driver_meta(name)
        if not meta:
            continue
        port_text = _port_source(name)
        if not port_text:
            continue
        reqs = _init_requirements(meta, port_text)
        out.append({
            "name": name,
            "bus": _component_bus(meta, port_text),
            "hal_symbols": _uniq(_PORT_SYM_RE.findall(port_text), limit=12),
            "init_requires": reqs[:3],
            "pitfalls": meta.get("common_pitfalls", ""),
            "source": "drivers/%s/*_port.h" % name,
        })
    return out


def component_list(bus=None):
    """器件清单（结构化记录；bus 可按接口过滤，如 bus='i2c'）。"""
    recs = component_records()
    if bus:
        recs = [r for r in recs if r["bus"] == bus]
    return recs


def component_brief(bus=None):
    """器件清单的文本视图（MCP 工具返回用）。"""
    recs = component_list(bus)
    if not recs:
        if bus:
            return "没有 bus=%s 的器件。可用总线：%s" % (
                bus, ", ".join(sorted({r["bus"] for r in component_records()})))
        return "器件库为空（驱动库目录为空或 meta.json 全缺）。"
    lines = ["共 %d 个器件%s：" % (
        len(recs), "（bus=%s）" % bus if bus else "，全部来自预验证驱动库")]
    for r in recs:
        bits = [r["bus"]]
        if r["address"]:
            bits.append("地址 %s" % r["address"])
        if r["us_timing"]:
            bits.append("需微秒时序")
        lines.append("- %s [%s]：%s" % (r["name"], " / ".join(bits), r["desc"]))
    lines.append("（坑与接线细节用 component_get 逐个查）")
    return "\n".join(lines)


def component_get(name):
    """单个器件的选型详情：接口 / 地址 / 时序要求 / 坑 / 验证记录。"""
    recs = component_records()
    if not recs:
        return "器件库为空（驱动库目录为空或 meta.json 全缺）。"
    key = (name or "").strip().lower()
    if not key:
        return "没给器件名。可用：%s" % ", ".join(r["name"] for r in recs)
    hit = next((r for r in recs if r["name"].lower() == key), None)
    if hit is None:
        return "没有名为 %s 的器件。可用：%s" % (
            name, ", ".join(r["name"] for r in recs))
    lines = ["【%s】%s" % (hit["name"], hit["desc"]),
             "接口：%s%s" % (hit["bus"],
                          "（需 DWT 微秒时序，给错粒度读不出数据）"
                          if hit["us_timing"] else ""),
             "地址：%s" % (hit["address"] or "无（不占地址总线）"),
             "用法：%s" % hit["usage"],
             "坑：%s" % (hit["pitfalls"] or "（meta.json 未记）"),
             "验证：%s" % hit["verified"],
             "来源：%s" % hit["source"]]
    wiring = driver_wiring(hit["name"])
    if not wiring.startswith("驱动") or "暂无 wiring.md" not in wiring:
        lines.append("===== 接线与避坑 =====\n%s" % wiring)
    return "\n".join(lines)


def peripheral_api_list(bus=None):
    """外设用法记录（结构化）。"""
    recs = peripheral_records()
    if bus:
        recs = [r for r in recs if r["bus"] == bus]
    return recs


def peripheral_api_brief(bus=None):
    """外设用法的文本视图：谁要哪个外设、初始化有什么硬要求。"""
    recs = peripheral_api_list(bus)
    if not recs:
        if bus:
            return "没有 bus=%s 的外设用法记录。" % bus
        return "外设 API 用法知识层为空（驱动库下没有 *_port.h 适配层）。"
    lines = ["共 %d 条外设用法（取自各驱动 *_port.h 适配层）：" % len(recs)]
    for r in recs:
        lines.append("- %s [%s]：%s" % (
            r["name"], r["bus"],
            "、".join(r["hal_symbols"][:5]) or "（无 HAL 符号，纯解析库）"))
        for q in r["init_requires"]:
            lines.append("    顺序要求：%s" % q)
    return "\n".join(lines)


def peripheral_api_get(name):
    """单个驱动的外设初始化要求全文（移植前照着核对一遍）。"""
    recs = peripheral_records()
    if not recs:
        return "外设 API 用法知识层为空（驱动库下没有 *_port.h 适配层）。"
    key = (name or "").strip().lower()
    if not key:
        return "没给驱动名。可用：%s" % ", ".join(r["name"] for r in recs)
    hit = next((r for r in recs if r["name"].lower() == key), None)
    if hit is None:
        return "没有名为 %s 的记录。可用：%s" % (
            name, ", ".join(r["name"] for r in recs))
    lines = ["【%s】外设：%s" % (hit["name"], "、".join(hit["hal_symbols"]) or "（无）")]
    if hit["init_requires"]:
        lines.append("初始化顺序要求（适配层注释原句）：")
        lines.extend("  · %s" % q for q in hit["init_requires"])
    else:
        lines.append("初始化顺序要求：适配层注释里没写（移植时按上面符号自行核对）。")
    lines.append("坑：%s" % (hit["pitfalls"] or "（meta.json 未记）"))
    lines.append("来源：%s" % hit["source"])
    return "\n".join(lines)


# ---------------------------------------------------------------
# 烧录桥接（不自研烧录器，桥接社区标准工具；默认 dry-run 保证安全）
# ---------------------------------------------------------------

#: 烧录/调试工具候选名（按优先级）。STM32_Programmer_CLI 是 ST 官方烧录工具，
#: 也是 STM32CubeProgrammer / eide / CubeIDE 实际落地的那个 exe——它在安装后
#: **不进 PATH**，所以光靠 shutil.which 是找不到的（实测本机装了 2 份却报「无」）。
_FLASH_TOOLS = [
    "STM32_Programmer_CLI",   # ST 官方：STM32 全系，-c port=SWD -w x.hex -rst
    "pyocd",                  # 通用 Cortex-M（DAPLink/ST-Link），支持 RTT
    "openocd",                # 通用，需 target cfg
    "esptool", "esptool.py",  # ESP 系
    "st-flash",               # stlink 开源套件
    # --- N-10 扩广：多探针 / 多厂商（同一工具的别名也计入，便于「到底缺哪个」看得清）---
    "JLinkExe",               # SEGGER J-Link 命令行（J-Link 全系）
    "JLink",                  # J-Link 的别名（部分安装/包装脚本用这个名字）
    "nrfjprog",               # Nordic nRF 系（nRF52/53）
    "dfu-util",               # USB DFU（STM32 DFU 模式、各类 bootloader）
    "avrdude",                # AVR / Arduino 系
    "UniFlash",               # TI（MSP430 / SimpleLink / CC 系）
]

#: 常见安装位置（出厂/IDE 插件自带，默认**不在 PATH**）。
#: 支持 ``*`` 通配（版本号目录）与环境变量；``~`` 会被展开。
_TOOL_SEARCH_DIRS = [
    # --- STM32CubeProgrammer：ST 官方安装器 / STM32CubeIDE 捆绑 / CubeMX 升级器下载 ---
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "programmer", "*", "bin"),
    os.path.join("%LOCALAPPDATA%", "Programs", "STMicroelectronics",
                 "STM32Cube", "STM32CubeProgrammer", "bin"),
    os.path.join("%PROGRAMFILES%", "STMicroelectronics", "STM32Cube",
                 "STM32CubeProgrammer", "bin"),
    os.path.join("%PROGRAMFILES(X86)%", "STMicroelectronics", "STM32Cube",
                 "STM32CubeProgrammer", "bin"),
    # --- STM32 VS Code 扩展（%LOCALAPPDATA%\stm32cube\bundles\）自带的全套工具链 ---
    #     实机实证：本机 ARM GCC 14.3.1 / ARM GDB / CMake 4.x / Ninja 全都在这里，
    #     既不在 PATH，也不在 Program Files。漏了这几条 → toolchain_status 会谎报
    #     「未发现 ARM GCC」，用户以为本机没有编译器，其实是探测目录表没覆盖到。
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "gnu-tools-for-stm32", "*", "bin"),
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "gnu-gdb-for-stm32", "*", "bin"),
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "cmake", "*", "bin"),
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "ninja", "*", "bin"),
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "ninja", "*"),
    # ST-Link GDB server / ST-Link server：GDB 挂调试用的，同样只在这套 bundle 里
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "stlink-gdbserver", "*"),
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "stlink-gdbserver"),
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "stlink-server", "*"),
    os.path.join("%LOCALAPPDATA%", "stm32cube", "bundles", "stlink-server"),
    # --- eide（VS Code 插件，嵌入式常用）自带一份 CLI ---
    os.path.join("~", ".eide", "tools", "st_cube_programer", "bin"),
    os.path.join("~", ".eide", "tools", "openocd", "*", "bin"),
    # --- VS Code cortex-debug / cmsis-debugger 扩展自带 pyocd ---
    os.path.join("~", ".vscode", "extensions", "arm.vscode-cmsis-debugger-*", "tools", "pyocd"),
    os.path.join("~", ".vscode", "extensions", "marus25.cortex-debug-*", "tools", "pyocd"),
    os.path.join("~", ".vscode", "extensions", "marus25.cortex-debug-*", "tools", "openocd", "bin"),
    # --- Espressif（ESP-IDF 自带 openocd / esptool）---
    os.path.join("~", ".espressif", "tools", "openocd-esp32", "*", "openocd-esp32", "bin"),
    os.path.join("~", ".espressif", "python_env", "*", "Scripts"),
    # --- PlatformIO ---
    os.path.join("~", ".platformio", "packages", "tool-openocd", "bin"),
    os.path.join("~", ".platformio", "packages", "tool-esptoolpy"),
    os.path.join("~", ".platformio", "packages", "tool-dfuutil", "bin"),
    os.path.join("~", ".platformio", "packages", "toolchain-gccarmnoneeabi", "bin"),
    # --- SEGGER J-Link（N-10）---
    os.path.join("%PROGRAMFILES%", "SEGGER", "JLink*"),
    os.path.join("%PROGRAMFILES(X86)%", "SEGGER", "JLink*"),
    os.path.join("~", ".eide", "tools", "jlink", "*"),
    # --- Nordic nRF Command Line Tools（N-10）---
    os.path.join("%PROGRAMFILES%", "Nordic Semiconductor",
                 "nrf-command-line-tools", "bin"),
    os.path.join("%PROGRAMFILES(X86)%", "Nordic Semiconductor",
                 "nrf-command-line-tools", "bin"),
    # --- Arduino 自带 avrdude（N-10）---
    os.path.join("%LOCALAPPDATA%", "Arduino15", "packages", "*", "tools", "avrdude", "*", "bin"),
    os.path.join("~", ".arduino15", "packages", "*", "tools", "avrdude", "*", "bin"),
    # --- TI UniFlash（N-10）---
    os.path.join("%PROGRAMFILES%", "Texas Instruments", "UniFlash*"),
    os.path.join("%PROGRAMFILES(X86)%", "Texas Instruments", "UniFlash*"),
    # --- embedder ---
    os.path.join("~", ".embedder", "tools", "openocd", "*", "bin"),
    # --- 系统/用户级（Linux/macOS 上 pyocd/esptool 常装这里）---
    "/usr/local/bin", "/opt/homebrew/bin", os.path.join("~", ".local", "bin"),
]

#: 记录上一次探测"搜了哪些地方"，供 env_status / flash_firmware 说明用
_LAST_TOOL_SEARCH = []


def _search_dirs():
    """展开 ``_TOOL_SEARCH_DIRS``：环境变量 → 通配 → 只留真实存在的目录。"""
    out = []
    for pat in _TOOL_SEARCH_DIRS:
        d = os.path.expanduser(os.path.expandvars(pat))
        if "*" in d or "?" in d:
            out.extend(sorted(glob.glob(d)))
        elif os.path.isdir(d):
            out.append(d)
    return out


def _which_tool(name):
    """可执行探测：先 PATH，再搜常见安装位置（IDE/扩展自带的那份）。"""
    p = shutil.which(name) or shutil.which(name + ".exe")
    if p:
        return p
    names = [name]
    if not name.lower().endswith(".exe"):
        names.append(name + ".exe")
    for d in _search_dirs():
        for fn in names:
            cand = os.path.join(d, fn)
            if os.path.isfile(cand):
                return cand
    return None


def flash_tool_detect():
    """探测可用的烧录/调试工具。

    ★「没找到」和「没搜」是两件事：找不到时会把搜过的位置一并带出
    （见 ``flash_tool_hint()``），免得用户以为工具真的不存在——实测本机
    装了 2 份 STM32_Programmer_CLI 却报「无」，就是因为只扫了 PATH。
    """
    global _LAST_TOOL_SEARCH
    found = {}
    for t in _FLASH_TOOLS:
        p = _which_tool(t)
        if p:
            found[t] = p
    _LAST_TOOL_SEARCH = _search_dirs()
    return found


def flash_tool_hint():
    """没找到烧录工具时给的可读说明（含搜过的位置，不假装"就是没有"）。"""
    lines = ["没找到可用的烧录/调试工具。已搜过：PATH 和下列 %d 个常见安装位置——"
             % len(_LAST_TOOL_SEARCH or _search_dirs())]
    for d in (_LAST_TOOL_SEARCH or _search_dirs())[:12]:
        lines.append("    " + d)
    lines.append("  都没找到时的安装方式（装任一即可，装完重跑）：")
    lines.append("    STM32 官方（推荐，本工具支持直接烧 hex/bin）：")
    lines.append("        下载 STM32CubeProgrammer → https://www.st.com/en/development-tools/stm32cubeprog.html")
    lines.append("    pip install pyocd       通用 Cortex-M（DAPLink/ST-Link，支持 RTT 读取）")
    lines.append("    pip install esptool     ESP32/ESP8266")
    lines.append("    装了 STM32CubeIDE / CubeMX / VS Code 的 eide 插件也会自带一份 CLI，")
    lines.append("    本工具会自动去它们的安装目录找，不需要手动加 PATH。")
    return "\n".join(lines)


def flash_firmware(file_path, tool="auto", chip=None, port=None, sn=None,
                   dry_run=True, confirm=False):
    """烧录固件。默认 dry_run=True：只返回将要执行的完整命令，人工/AI 确认后再执行。

    tool: auto / cube_programmer / pyocd / st-flash / esptool。
      auto 按文件名与 chip 线索选，STM32 优先走 STM32_Programmer_CLI
      （实测比 st-flash/pyocd 可靠：不依赖 PATH、不依赖 pyocd 的 CMSIS pack）。
    sn:   接多台同型号调试器时指定探针序列号（如 ST-Link 的 0046001C...）。

    ★ 三条硬约束，都是实机踩出来的：
      1) 命令里一律用 _which_tool 探到的**绝对路径**。用裸名（"pyocd"）时，
         工具明明在磁盘上却没进 PATH，一律报「不在 PATH」——探到了却用不上，
         这是本工具曾经最离谱的一个自相矛盾。
      2) STM32CubeProgrammer 的 `-v` 必须**紧跟** `-w`，单独调会被拒
         ("must be performed just after write command")。
      3) 烧完必须 `-rst` 复位。SWD 以 mode=normal 连接会 halt 内核，
         不复位的话芯片一直停着，串口一个字都不会有——很容易被误判成烧失败。
    """
    if not os.path.exists(file_path):
        return "固件文件不存在：%s" % file_path
    found = flash_tool_detect()
    if not found:
        return flash_tool_hint()

    ext = os.path.splitext(file_path)[1].lower()
    chosen, cmd = None, None
    t = tool.lower()

    def _pick(*names):
        for n in names:
            if n in found:
                return n
        return None

    # 用户显式指定时的别名归一
    alias = {"cube_programmer": "STM32_Programmer_CLI",
             "cube": "STM32_Programmer_CLI",
             "stm32cube": "STM32_Programmer_CLI"}
    t = alias.get(t, t)

    if t == "auto":
        is_esp = bool(chip and "esp" in chip.lower())
        is_stm32 = bool(chip and "stm32" in chip.lower())
        if is_esp:
            chosen = _pick("esptool", "esptool.py")
        elif is_stm32 or ext in (".hex", ".elf"):
            chosen = (_pick("STM32_Programmer_CLI") or _pick("pyocd")
                      or _pick("st-flash"))
        else:
            chosen = (_pick("STM32_Programmer_CLI") or _pick("st-flash")
                      or _pick("pyocd") or _pick("esptool", "esptool.py"))
    elif t in found:
        chosen = t
    elif t == "esptool" and "esptool.py" in found:
        chosen = "esptool.py"
    else:
        return ("指定了 tool=%s 但没找到可执行文件。当前可用：%s"
                % (tool, ", ".join(sorted(found))))

    exe = found.get(chosen, chosen)          # 绝对路径（约束 1）

    if chosen in ("esptool", "esptool.py"):
        cmd = [exe, "--chip", chip or "esp32", "--port", port or "COM5",
               "--baud", "921600", "write_flash", "0x10000", file_path]
        note = "ESP32 app 默认偏移 0x10000；ESP8266 用 --chip esp8266 且偏移改 0x0"
    elif chosen == "st-flash":
        cmd = [exe, "write", file_path, "0x8000000"]
        note = "STM32 内部 flash 起始 0x08000000；需要 ST-Link 已插好且 SWD 四线接对"
    elif chosen == "pyocd":
        cmd = [exe, "flash", "--target", chip or "stm32f103c8", file_path]
        if port:
            cmd += ["--probe", port]
        note = "pyocd target 写芯片全名；首次使用某芯片需 pyocd pack install"
    elif chosen == "STM32_Programmer_CLI":
        cmd = [exe, "-c", "port=SWD"]
        if sn:
            cmd += ["sn=" + str(sn)]
        cmd += ["mode=normal", "-w", file_path]
        if ext != ".hex" and ext != ".elf":
            cmd += ["0x08000000"]            # .bin 需要显式地址
        cmd += ["-v", "-rst"]                # 约束 2 + 3
        note = ("STM32 官方 CLI：只擦「被写入覆盖到的那几个扇区」，不会全片擦——"
                "所以默认不给 -e（实测 -w 自带擦除）。-v 紧跟 -w 是 CLI 硬要求；"
                "末尾 -rst 复位，否则 SWD 连接把内核 halt 住，烧完板子不会跑。")
    else:
        return "未实现的工具路径：%s" % chosen

    cmd_str = " ".join('"%s"' % c if " " in c else c for c in cmd)
    if dry_run:
        return ("[DRY-RUN] 将执行：\n  %s\n说明：%s\n确认无误后带 dry_run=False + confirm=True 重新调用真正烧录。"
                % (cmd_str, note))
    # 真正写 flash 不可逆 ⟹ 隔离闸门 + 二次确认（不靠调用方自觉）
    blocked = guard.check("flash_firmware", port, confirm=confirm)
    if blocked:
        return blocked
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           encoding="utf-8", errors="replace")
        out = ((r.stdout or "") + (r.stderr or "")).strip()[-3000:]
        return ("退出码 %d\n命令：%s\n输出（截尾）：\n%s\n提示：烧录后复位板子，用 serial_read 看启动日志。"
                % (r.returncode, cmd_str, out if out else "（无输出）"))
    except FileNotFoundError:
        return "执行失败：%s 不在 PATH。" % chosen
    except subprocess.TimeoutExpired:
        return "烧录超时（180s）：查调试器连接与芯片供电后重试。"

# ---------------------------------------------------------------
# 探针直连：芯片身份 / 烧录后校验 / 复位原因
# ---------------------------------------------------------------
#
# 三个工具都建立在 STM32_Programmer_CLI 的「连接 banner + 读寄存器/内存」之上。
# 下面两条是**实测**出来的硬事实，整个设计由它们决定（不是照文档想当然）：
#
#   1) `mode=normal` 的连接【本身就会复位目标】。实测（每次都先写 RMVF 清零再单做被测动作）：
#        hotplug 连接（仅连接）      → 不置任何标志（读数仍是 0）
#        normal  连接（仅连接）      → PINRSTF | SFTRSTF
#        hotplug + -rst              → PINRSTF | SFTRSTF
#        hotplug + -hardRst          → PINRSTF
#        normal  + -rst / -hardRst   → PINRSTF | SFTRSTF
#      而且 normal 档会把内核 halt 住（`-score` 报 "Core is halted"）。
#      ⟹ 读复位原因绝不能在 normal 档做，否则读到的第一个标志就是自己刚造成的复位。
#      只读一律走 `mode=hotplug`（实测 `-score` 报 "Core is running"，不打断程序，
#      而且一个标志都不置）。
#
#      ★ 内核状态其实有**三**种，`-score` 各报什么（实测，同一块 NUCLEO-H563ZI）：
#                          仅连接              连接 + -rst        连接 + -hardRst
#          hotplug      Core is running      Core is running    Core is kept under reset
#          normal       Core is halted       Core is running    Core is kept under reset
#        两个后果：
#        a) `-hardRst` 是**唯一会把内核摁在复位里不放**的动作 —— 用完必须再补一条不带
#           -hardRst 的连接（或 -rst）才能让程序跑起来，否则看起来像"烧完不跑"。
#        b) 认状态别只匹配 "running"/"halted" 两个词，第三种 "Core is kept under reset"
#           照样是 `Core is ` 开头的真实状态，漏了它就会把"被摁住"误报成"读不到状态"。
#
#   2) 反过来，system memory 区（0x08FFF800 的 UID / 0x08FFF80C 的 FlashSize）
#      在 hotplug 下读不到（"failed to read the requested memory content"），
#      必须 normal 档。所以 chip_info 默认只报 hotplug 能拿到的部分；
#      要 UID 与真实容量得显式选 mode="full"，并接受「会复位一次板子」。
#
#   3) 写 RCC_RSR 的 RMVF(bit23) 确实能清掉全部复位标志（实测：写完不复位，读回 0x00000000）；
#      位定义与官方 CMSIS 头一致。但**清零不要和复位并成一条命令**：上面那张表里
#      normal 连接与 -rst 都会自己置标志，合并之后读回必然还看到 PINRSTF|SFTRSTF，
#      于是把「命令自己的痕迹」误判成「RMVF 没生效」——第一版就报错了这句。
#      （另：我一度把 0x04000000 口算成 bit30，实验里固件自己解得 PINRSTF，才发现是算错了。）

_SP_MODE_READ = "hotplug"      # 不 halt、不复位目标
_SP_MODE_FULL = "normal"       # halt 内核，且**连接本身会复位目标**

#: 连接 banner 里值得抓的字段（键名就是 CLI 原样输出的键名）
_SP_BANNER_KEYS = (
    "ST-LINK SN", "ST-LINK FW", "Board", "Voltage", "SWD freq",
    "Connect mode", "Reset mode", "Device ID", "Revision ID", "Device name",
    "Flash size", "Device type", "Device CPU", "BL Version", "SFSP Version",
)

#: 复位标志位表。位号抄自官方 CMSIS 头 ``stm32h563xx.h`` 的 ``RCC_RSR_*_Pos``，
#: 并已用受控固件实测验证（软件复位 → SFTRSTF；硬件复位 → PINRSTF）。
_RSR_BITS = [
    (26, "PINRSTF", "复位引脚被拉低：按了复位键 / 调试器复位（normal 档连接也算）"),
    (27, "BORRSTF", "掉电或欠压复位（BOR）：上电瞬间、电源跌落、供电不足"),
    (28, "SFTRSTF", "软件复位：程序里调了 NVIC_SystemReset，或调试器 -rst"),
    (29, "IWDGRSTF", "独立看门狗 IWDG 复位：程序卡死没喂狗"),
    (30, "WWDGRSTF", "窗口看门狗 WWDG 复位"),
    (31, "LPWRRSTF", "低功耗退出复位：从 Standby / Shutdown 唤醒"),
]
_RSR_RMVF = 1 << 23

#: 芯片族寄存器地址表。**只收已核过出处的**，宁可少而准：
#:   STM32H5：RCC_BASE_NS = AHB3PERIPH_BASE_NS(0x40000000+0x04020000) + 0x0C00 = 0x44020C00，
#:   RCC_TypeDef 里 RSR 的 Address offset = 0xF4 ⟹ 0x44020CF4；
#:   DBGMCU_BASE = 0x44024000（IDCODE 在偏移 0x00，DEV_ID=bits[11:0]、REV_ID=bits[31:16]）；
#:   UID_BASE = 0x08FFF800、FLASHSIZE_BASE = 0x08FFF80C（16 位，单位 KB）。
#:   出处均为官方 CMSIS 头 stm32h563xx.h；且已在 NUCLEO-H563ZI 上实测通过
#:   （写 RMVF 后标志确实清零、DBGMCU 读出的 DEV_ID 与 CLI 报的 Device ID 一致）。
_REG_ADDR = {
    "stm32h5": {
        "family": "STM32H5",
        "rcc_cr": 0x44020C00,
        "rsr": 0x44020CF4,
        "dbgmcu_idcode": 0x44024000,
        "uid": 0x08FFF800,
        "flashsize": 0x08FFF80C,
        "source": "官方 CMSIS 头 stm32h563xx.h（RCC_BASE_NS/RCC_RSR 偏移/DBGMCU_BASE/UID_BASE/FLASHSIZE_BASE）",
    },
}

#: DBGMCU DEV_ID → 型号族。**只收实测/可核对的条目**；
#: 没收录的 DEV_ID 就只报原始值并说明去哪儿查，不硬猜。
_DEV_ID_TABLE = {
    0x484: {
        "name": "STM32H56x / STM32H573（含 STM32H563ZI）",
        "family": "stm32h5",
        "source": "本机实测：NUCLEO-H563ZI 上 DBGMCU_IDCODE 读出 DEV_ID=0x484，"
                  "与 STM32CubeProgrammer 自己报的 'Device ID 0x484 / Device name STM32H56x/573' 一致",
    },
}


def _sp_lines(text):
    """把 CLI 输出拆成有信息量的行：丢空行、进度条（■ 与替换字符）与百分比行。"""
    out = []
    for raw in (text or "").replace("\r", "\n").split("\n"):
        ln = raw.strip()
        if not ln:
            continue
        if "\ufffd" in ln or "\u25a0" in ln:
            continue
        if ln.endswith("%"):
            continue
        out.append(ln)
    return out


def _pad(s, width):
    """按**显示宽度**左对齐补空格（CJK 算 2 列）。

    ★ 为什么不能用 %-12s：Python 的宽度按**字符数**算，而中文在终端占两列。
      「型号族」是 3 个字符、但占 6 列，用 %-12s 补到 12 字符 = 6+9 空格，
      和旁边 "Device name " 的 12 列对不上 —— 输出会歪。歪了不影响正确性，
      但一排排歪着会让人以为工具没认真做。
    """
    s = str(s)
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    return s + " " * max(0, int(width) - w)


def _sp_run(args, timeout=90):
    """跑一次 STM32_Programmer_CLI。返回 (rc, 清洗后的文本)。工具不在时 rc=127。"""
    exe = _which_tool("STM32_Programmer_CLI")
    if not exe:
        return 127, ""
    try:
        r = subprocess.run([exe] + [str(a) for a in args],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as e:
        return 126, str(e)
    return r.returncode, "\n".join(_sp_lines((r.stdout or "") + "\n" + (r.stderr or "")))


def _sp_conn(mode, sn=None):
    args = ["-c", "port=SWD"]
    if sn:
        args.append("sn=" + str(sn))
    args.append("mode=" + mode)
    return args


def _sp_banner(text):
    """从 CLI 输出里抓连接 banner（键: 值）。"""
    got = {}
    for ln in _sp_lines(text):
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        k, v = k.strip(), v.strip()
        if k in _SP_BANNER_KEYS:
            got[k] = v
    return got


def _sp_vals_from(text):
    """从 CLI 输出里抠出 ``0x<addr> : <hex> <hex> ...`` 行的所有 32 位字。

    ★ 输出格式实测是多字**挤在一行**、空格分隔：
      ``0x44024000 : 10076484 00000006 00001800 00000000``
      所以不能只抓一个 token —— 早期的单值正则会静默把第 2..N 个字丢掉，
      而且「只读到一个字」看起来完全正常，最难发现。

    ★ 读失败时（hotplug 读 system memory 就是这种）CLI 给 ``rc=1`` 且打印
      ``Error: failed to read the requested memory content``；这里见 Error 行即返回空。
    """
    vals = []
    for ln in _sp_lines(text or ""):
        if ln.startswith("Error:"):
            # 2026-10-13：旧版见 Error 行即 return []——哪怕前面已经收到有效
            # 值行。CLI 的多步输出里 Error 常出现在成功读出的值**之后**
            # （后续某一步失败），此时丢掉全部已读值等于自废硬证据
            # （reset_reason 链就这么失去过寄存器实值）。
            # 现在：停止继续收，但保留已收到的值；Error 原文仍在 text 里，
            # 调用方要判失败看原文即可（2385 行起两个调用方都持有原文）。
            break
        m = re.match(r"^0x[0-9A-Fa-f]{6,8}\s*:\s*(.+)$", ln)
        if not m:
            continue
        for tok in m.group(1).split():
            if re.fullmatch(r"[0-9A-Fa-f]{1,8}", tok):
                vals.append(int(tok, 16) & 0xFFFFFFFF)
    return vals


def _sp_fetch(addr, size_bytes=4, sn=None, mode=_SP_MODE_READ, extra=None):
    """读设备内存，返回 ``(字列表, 清洗后的原始输出)``。

    ``extra`` 里的命令会接在同一次连接后面执行 —— 例如 ``extra=["-score"]``
    就能一次连接同时拿到寄存器值和内核状态（省下一次 ~0.4s 的连接开销）。
    """
    try:
        n = int(size_bytes)
    except (TypeError, ValueError):
        return [], ""
    if n <= 0:
        return [], ""
    if n % 4:
        n = ((n + 3) // 4) * 4          # CLI 也按 4 字节对齐，这里对齐以保持可预测
    args = _sp_conn(mode, sn) + ["-r32", "0x%08X" % int(addr), str(n)]
    if extra:
        args += [str(a) for a in extra]
    rc, out = _sp_run(args, timeout=90)
    if rc != 0:
        return [], out
    return _sp_vals_from(out), out


def _sp_read_words(addr, size_bytes=4, sn=None, mode=_SP_MODE_READ, extra=None):
    """只取值的便捷包装（读不到返回 []）。语义同 ``_sp_fetch``。

    ★ ``-r32 <addr> <size>`` 的第二个参数是**字节数**、不是字数（CLI 帮助里写的是
      "Size of data"，实测传 8 就报 ``Size : 8 Bytes`` 并给两个字）。这里统一按
      字节数收，调用方别再把「要读几个字」当参数传进来。
    """
    return _sp_fetch(addr, size_bytes, sn, mode, extra)[0]


def _sp_core_state(text):
    """从 CLI 输出里取内核状态行。

    实测 ST-Link 会报**三种**（别只认前两种，见本段上方那张表）：
        "Core is running"            —— 程序在跑（hotplug 连接 / 任何 -rst 之后）
        "Core is halted"             —— 内核被停住（normal 档连接本身就会）
        "Core is kept under reset"   —— 内核被摁在复位里不放（-hardRst 独有）

    判据用前缀 ``Core is`` 而不是枚举三个整句：CLI 加词不必改这里，而"读不到状态"
    必须表现为空串（调用方据此走"没读到"分支），不能往上报一个编出来的状态。
    """
    for ln in _sp_lines(text or ""):
        if ln.startswith("Core is"):
            return ln
    return ""


def _sp_download(addr, size, path, sn=None, mode=_SP_MODE_READ):
    """把设备内存读到本地文件。成功返回 True。"""
    rc, out = _sp_run(_sp_conn(mode, sn) + ["-u", "0x%08X" % addr, str(size), path],
                      timeout=180)
    if rc != 0:
        return False
    if "Data read successfully" in out:
        return True
    return os.path.isfile(path) and os.path.getsize(path) == size


def _parse_ihex(text):
    """解析 Intel HEX，返回 ``[(起始地址, bytes), ...]``（按出现顺序，相邻自动合并）。

    ★ 严格解析：每条记录的字节和必须为 0（长度+地址+类型+数据+校验 ≡ 0xFF）。
      校验不过就**报错**而不是尽力解析 —— 烧录校验里放过一条坏记录，
      等于放过一整个坏扇区，那比不校验更危险。
    """
    segs = []
    base = 0
    cur_addr = None
    cur = bytearray()

    def flush():
        if cur_addr is not None and cur:
            segs.append((cur_addr, bytes(cur)))

    for ln_no, raw in enumerate((text or "").splitlines(), 1):
        s = raw.strip()
        if not s:
            continue
        if not s.startswith(":"):
            raise ValueError("第 %d 行不是 Intel HEX 记录（应以 : 开头）：%r"
                             % (ln_no, raw[:40]))
        try:
            b = bytes.fromhex(s[1:])
        except ValueError:
            raise ValueError("第 %d 行不是合法十六进制：%r" % (ln_no, raw[:40]))
        if len(b) < 5:
            raise ValueError("第 %d 行太短（至少要 5 字节）：%r" % (ln_no, raw[:40]))
        n, typ = b[0], b[3]
        data = b[4:4 + n]
        if len(b) != n + 5 or len(data) != n:
            raise ValueError("第 %d 行「长度字段」与实际字节数不符" % ln_no)
        if (sum(b) & 0xFF) != 0:
            raise ValueError("第 %d 行字节和校验失败（这一行数据不可信）" % ln_no)
        addr = (b[1] << 8) | b[2]
        if typ == 0x00:
            if not data:
                continue
            a = base + addr
            if cur_addr is not None and a == cur_addr + len(cur):
                cur += data
            else:
                flush()
                cur_addr, cur = a, bytearray(data)
        elif typ == 0x01:
            break
        elif typ == 0x02:
            if len(data) < 2:
                raise ValueError("第 %d 行的段地址记录长度不对" % ln_no)
            base = ((data[0] << 8) | data[1]) << 4
        elif typ == 0x04:
            if len(data) < 2:
                raise ValueError("第 %d 行的线性地址记录长度不对" % ln_no)
            base = ((data[0] << 8) | data[1]) << 16
        elif typ in (0x03, 0x05):
            pass                      # 起始地址记录，与内容地址无关，忽略
        else:
            raise ValueError("第 %d 行出现不认识的记录类型 0x%02X" % (ln_no, typ))
    flush()
    return segs


def _firmware_segments(path, base_addr=None):
    """把固件文件规整成 ``([(地址, bytes), ...], 说明)``，供烧录后逐字节比对。

    - ``.bin``：整份一条段，起始地址取 base_addr（默认 STM32 flash 起始 0x08000000）
    - ``.hex``：用内置 Intel HEX 解析器取出真实地址段
    - ``.elf``：复用 ``_parse_elf``，只取落在 flash 里的 PROGBITS 段
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".bin":
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            raise ValueError("文件是空的：%s" % path)
        addr = 0x08000000 if base_addr is None else int(str(base_addr), 0)
        return [(addr, data)], ""
    if ext in (".hex", ".ihex"):
        with open(path, encoding="utf-8", errors="replace") as fh:
            segs = _parse_ihex(fh.read())
        if not segs:
            raise ValueError("这个 hex 里没有任何数据记录：%s" % path)
        return segs, ""
    if ext == ".elf":
        parsed = _parse_elf(path)
        if not parsed:
            raise ValueError("这个 elf 解析不了（不是合法 ELF？）：%s" % path)
        with open(path, "rb") as fh:
            blob = fh.read()
        segs, skipped = [], []
        for name, seg in sorted(parsed["segments"].items(),
                                key=lambda kv: kv[1]["addr"]):
            # 只校验落在 flash 里的 PROGBITS 段（SHT_PROGBITS = 1）。
            # .data 的 vaddr 在 RAM，它在 flash 里的那份初值走的是 LMA，这里看不到
            # —— 这点必须在输出里说清楚，不能让人以为整个 elf 都校验过了。
            if seg.get("type") != 1:
                skipped.append(name)
                continue
            if not (0x08000000 <= seg["addr"] < 0x10000000):
                skipped.append(name)
                continue
            if "off" not in seg:
                skipped.append(name)
                continue
            raw = blob[seg["off"]:seg["off"] + seg["size"]]
            if len(raw) != seg["size"]:
                # 段表说的大小超出了文件实际长度 —— 这种 elf 本身是坏的，
                # 退回「比一小段」比静默短读安全（短读会误报不一致）。
                raise ValueError(
                    "elf 段 %s 说大小 %d 字节，但文件里只有 %d 字节可读，"
                    "文件不完整或段表损坏：%s"
                    % (name, seg["size"], len(raw), path))
            segs.append((seg["addr"], raw))
        if not segs:
            raise ValueError("这个 elf 里没有落在 flash 里的可校验段：%s" % path)
        note = ("ELF 只校验落在 flash 的 PROGBITS 段；跳过了 %s（RAM 段 / NOBITS / 符号表）。"
                ".data 的初值在 flash 里走 LMA，本工具看不到——要严格校验整片，"
                "请用 .bin 或 .hex。" % ("、".join(sorted(skipped)) or "无"))
        return segs, note
    raise ValueError("只支持 .bin / .hex / .elf，收到：%s" % ext)


def chip_info(mode="read", sn=None):
    """读目标芯片的身份与存储信息（型号 / Device ID / 修订 / Flash 容量 / UID / 读保护）。

    mode:
      "read"（默认）走 hotplug —— **不复位、不 halt**，板子照跑。能拿到
        Device ID / Device name / Revision ID / Device CPU / Board / 电压 / ST-LINK 信息，
        以及 DBGMCU_IDCODE 里更精确的 DEV_ID 与 REV_ID。
      "full" 走 normal —— 能多拿到 真实 Flash 容量 / 96 位 UID / 选项字节（读保护、TZEN、
        启动地址、写保护组）。代价是**连接本身会复位一次目标并 halt 内核**，
        别在别人正调试或跑关键流程时用。

    为什么要分两档：这两件事是实测撞出来的矛盾 —— hotplug 读不到 system memory
    （UID/FlashSize 读会报 "failed to read the requested memory content"），
    而 normal 又会复位目标。想「零打扰又要全量信息」在这颗芯片上做不到，
    所以把选择权明明白白交给调用方，而不是悄悄选一个。
    """
    if not _which_tool("STM32_Programmer_CLI"):
        return ("没找到 STM32_Programmer_CLI，无法直连芯片读身份信息。\n"
                + flash_tool_hint())
    if mode not in ("read", "full"):
        return "mode 只支持 'read'（hotplug，不打扰）或 'full'（normal，会复位目标），收到：%r" % (mode,)
    if mode == "full":
        # full 档会复位目标并 halt 内核 —— 那是真实副作用，走隔离层闸门
        blocked = guard.check("chip_info_full")
        if blocked:
            return blocked + ("\n\n  ⓘ 只想知道型号 / Device ID / 修订 / 内核类型的话，"
                              "用默认的 mode=\"read\" 就够了（不打扰目标）。")

    pmode = _SP_MODE_READ if mode == "read" else _SP_MODE_FULL
    rc, out = _sp_run(_sp_conn(pmode, sn), timeout=60)
    banner = _sp_banner(out)
    if not banner.get("Device ID") and not banner.get("Device name"):
        return ("连不上目标（SWD 连接没有返回芯片信息）。原始输出（截尾）：\n%s\n"
                "排查：1) 调试器是否插好、SWD 四线是否接对；2) 目标是否供电；\n"
                "      3) 有没有别的调试会话占着探针（VS Code / CubeIDE 里停掉调试）。"
                % out[-1200:])

    dev = banner.get("Device ID", "")
    try:
        dev_id = int(dev, 16) if dev.lower().startswith("0x") else int(dev, 16)
    except ValueError:
        dev_id = None

    known = _DEV_ID_TABLE.get(dev_id) if dev_id is not None else None
    family = (known or {}).get("family")
    if family is None and "STM32H5" in banner.get("Device name", ""):
        family = "stm32h5"

    lines = ["芯片信息（%s）"
             % ("hotplug 档：不复位、不停内核，板子照跑" if mode == "read"
                else "normal 档：已复位目标并 halt 内核"),
             "-" * 48]
    for k in ("Board", "Device name", "Device CPU", "Device type",
              "Device ID", "Revision ID", "Flash size", "Voltage", "SWD freq",
              "ST-LINK SN", "ST-LINK FW", "BL Version", "SFSP Version"):
        if banner.get(k):
            lines.append("  %s: %s" % (_pad(k, 13), banner[k]))

    if known:
        lines.append("  %s: %s" % (_pad("型号族", 13), known["name"]))
    elif dev_id is not None:
        lines.append("  %s: 未收录（DEV_ID=0x%03X）。可查 ST 的 DBGMCU 器件识别表，"
                     "或把这条补进 core._DEV_ID_TABLE。" % (_pad("型号族", 13), dev_id))

    # DBGMCU_IDCODE：hotplug 就能读，给出比 CLI 更精确的 DEV_ID / REV_ID
    if family and family in _REG_ADDR:
        regs = _REG_ADDR[family]
        vals = _sp_read_words(regs["dbgmcu_idcode"], 4, sn, _SP_MODE_READ)
        if vals:
            v = vals[0]
            lines.append("  %s: 0x%08X  → DEV_ID=0x%03X  REV_ID=0x%04X"
                         % (_pad("DBGMCU", 13), v, v & 0xFFF, (v >> 16) & 0xFFFF))
            if dev_id is not None and (v & 0xFFF) != dev_id:
                lines.append("    ⚠ DBGMCU 读到的 DEV_ID 与连接 banner 不一致 —— "
                             "把两个值都记下来，别只信其中一个。")

    if mode == "read":
        lines.append("")
        lines.append("  ⓘ 未读 UID / 选项字节（读保护等）：这两类信息在 system memory 区，")
        lines.append("    hotplug 档读不到。要读请用 mode=\"full\"（会复位一次板子并 halt 内核）。")
        return "\n".join(lines)

    # ---- full 档：UID / 真实 Flash 容量 / 选项字节 ----
    lines.append("")
    if family and family in _REG_ADDR:
        regs = _REG_ADDR[family]
        import tempfile as _tf
        tmp = _tf.mkdtemp(prefix="mcu-dbg-uid-")
        try:
            uid_path = os.path.join(tmp, "uid.bin")
            if _sp_download(regs["uid"], 12, uid_path, sn, _SP_MODE_FULL):
                with open(uid_path, "rb") as fh:
                    uid = fh.read()
                lines.append("  %-12s: %s（96 位，出厂唯一）"
                             % ("UID", uid.hex().upper()))
            else:
                lines.append("  %-12s: 读取失败" % "UID")
            fs_path = os.path.join(tmp, "fs.bin")
            if _sp_download(regs["flashsize"], 4, fs_path, sn, _SP_MODE_FULL):
                with open(fs_path, "rb") as fh:
                    raw = fh.read()
                if len(raw) < 2:
                    # 2026-10-13：_sp_download 只认「写出了文件」，不校验长度——
                    # 探针半途掉线会留下空/截断文件，raw[1] 直接 IndexError。
                    lines.append("  %-12s: 回读长度不足（%d 字节），读不出容量"
                                 % ("Flash 实测", len(raw)))
                else:
                    kb = raw[0] | (raw[1] << 8)
                    if kb in (0x0000, 0xFFFF):
                        lines.append("  %-12s: 寄存器未编程（0x%04X），按官方头文件规则取默认 2MB"
                                     % ("Flash 实测", kb))
                    else:
                        lines.append("  %-12s: %d KB（寄存器 FLASHSIZE 低 16 位 × 1KB）"
                                     % ("Flash 实测", kb))
            else:
                lines.append("  %-12s: 读取失败" % "Flash 实测")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    rc, ob = _sp_run(_sp_conn(_SP_MODE_FULL, sn) + ["-ob", "displ"], timeout=120)
    if rc == 0 and "PRODUCT_STATE" in ob:
        lines.append("")
        lines.append("  选项字节（读保护 / 安全配置）")
        for ln in _sp_lines(ob):
            if ln.startswith("OPTION BYTES BANK"):
                continue
            m = re.match(r"^(PRODUCT_STATE|TZEN|BOOT_UBE|NSBOOTADD|SECBOOTADD|"
                         r"WRPSGn[12]|HDP[12]_(?:STRT|END)|BOR_LEV|SWAP_BANK)\s*:\s*(.+)$", ln)
            if m:
                lines.append("    %-14s %s" % (m.group(1), m.group(2).strip()))
        lines.append("    说明：读保护等级就体现在 PRODUCT_STATE 上 —— Open = 未保护；")
        lines.append("          若显示 Locked/Closed 说明已上读保护，此时 normal 档连接会失败或受限。")
    else:
        lines.append("")
        lines.append("  选项字节：读取失败（rc=%s）。原始输出（截尾）：\n%s"
                     % (rc, ob[-600:]))
    return "\n".join(lines)


def reset_reason(sn=None, clear_flags=False, confirm=False, then_reset=True):
    """读复位原因与内核运行状态（走 hotplug —— **不复位、不 halt，板子照跑**）。

    为什么必须用 hotplug（这条是实测踩出来的，不是照文档）：
      STM32_Programmer_CLI 以 ``mode=normal`` 连接时**连接动作本身就会复位目标**
      （实测：把 RCC_RSR 清成 0 后只做一次 normal 连接，RSR 立刻变成
      PINRSTF|SFTRSTF），而且会把内核 halt 住。也就是说，用 normal 档读复位原因，
      读到的第一个标志就是你这次读操作自己造成的 —— 结论必然是错的。
      对照实测：hotplug 连接**一个标志都不置**（清成 0 后连一次，读数仍是 0）。

    ★ 关于 RSR 的正确理解（很多调试笔记会讲反）：这些标志是**累积**的，
      只有写 RMVF(bit23) 或整片掉电才会清。所以「多个位同时为 1」的含义是
      「自上次清零以来发生过这几类复位」，**不等于**「最后一次是它们几个一起发生」。
      要定位「这一次」到底是哪种复位：先 clear_flags=True 清零 → 复现 → 再读。

    ★ 哪些动作会**自己**置标志（全部实测，用来区分「真复位」和「读操作自带的噪声」）：
        hotplug 连接（仅连接）      → 不置任何标志
        normal 连接（仅连接）       → PINRSTF | SFTRSTF
        hotplug + -rst              → PINRSTF | SFTRSTF
        hotplug + -hardRst          → PINRSTF
        normal  + -rst / -hardRst   → PINRSTF | SFTRSTF
      所以「清零后读回还看到 PINRSTF/SFTRSTF」几乎总是命令自己造成的，
      不是 RMVF 没生效 —— 第一版就是把这个误报成「✗ RMVF 没生效？」。

    clear_flags=True 会写 RMVF 清掉全部标志 —— 那等于销毁现场，所以它是不可逆操作：
    需要 write 档 + confirm=True。
    then_reset=True（默认）会在清零并**读回验证**之后再做一次复位，让内核继续跑
      （normal 档会把内核 halt 住，不放开的话板子就停在那儿了）。
      代价是这次复位会重新置起 PINRSTF/SFTRSTF，输出里会写明那是命令自己的痕迹。
    """
    if not _which_tool("STM32_Programmer_CLI"):
        return ("没找到 STM32_Programmer_CLI，无法读复位原因。\n" + flash_tool_hint())

    rc, out = _sp_run(_sp_conn(_SP_MODE_READ, sn), timeout=60)
    banner = _sp_banner(out)
    if not banner.get("Device ID") and not banner.get("Device name"):
        return ("连不上目标（SWD 连接没有返回芯片信息）。原始输出（截尾）：\n%s" % out[-1200:])

    name = banner.get("Device name", "")
    dev = banner.get("Device ID", "").lower()
    family = None
    if "STM32H5" in name or dev in ("0x484", "484"):
        family = "stm32h5"
    if family is None or family not in _REG_ADDR:
        return ("芯片族 %r（Device ID %s / %s）的复位标志寄存器地址还没收录，"
                "不敢凭印象填地址。\n"
                "已知地址表见 core._REG_ADDR（目前只有 STM32H5，已实测核对）。"
                % (name or "未知", banner.get("Device ID", "?"), dev or "?"))

    regs = _REG_ADDR[family]
    # 一次连接同时取 RSR 与 -score：hotplug 每次连接约 0.4s，能合就合
    vals, raw = _sp_fetch(regs["rsr"], 4, sn, _SP_MODE_READ, extra=["-score"])
    core_state = _sp_core_state(raw)

    lines = ["复位原因 / 运行状态（%s，hotplug 档：未复位、未停内核）" % regs["family"],
             "-" * 48]
    if core_state:
        lines.append("  %s: %s（hotplug 连接不清内核，所以这是它此刻真实的状态）"
                     % (_pad("内核状态", 11), core_state))
        # 实测第三态：-hardRst 会把内核摁在复位里不放。这时板子"有电但一行都不执行"，
        # 表现和"烧完不跑 / 程序卡死"一模一样 —— 不点破就会被当成固件问题查半天。
        if "kept under reset" in core_state:
            lines.append("  ⚠ 内核被**摁在复位里**没放出来（-hardRst 独有的效果）。"
                         "板子有电但一行代码都没执行，")
            lines.append("    别当固件问题查。放出来：随便做一次不带 -hardRst 的连接，"
                         "或 STM32_Programmer_CLI -c port=SWD mode=hotplug -rst。")
    else:
        lines.append("  %s: 读不到（-score 无输出）" % _pad("内核状态", 11))

    if not vals:
        lines.append("  %s: 读取失败（读 0x%08X 没返回数据）" % (_pad("复位标志", 11), regs["rsr"]))
        return "\n".join(lines)

    v = vals[0]
    lines.append("  %s: 0x%08X" % (_pad("RCC_RSR", 11), v))
    lines.append("  已置位的标志：")
    hit = [(nm, why) for bit, nm, why in _RSR_BITS if v & (1 << bit)]
    if not hit:
        lines.append("    （全为 0 —— 说明标志被清过，或这次上电后还没发生过复位）")
    for nm, why in hit:
        lines.append("    · %s %s" % (_pad(nm, 10), why))

    if len(hit) > 1:
        lines.append("  ⚠ 有 %d 个标志同时为 1。这些标志是【累积】的（只有写 RMVF 或掉电才清），"
                     "所以它表示「自上次清零以来发生过这几类复位」，"
                     "不代表最后一次是它们一起发生。" % len(hit))
        lines.append("    要定位「这一次」：先 reset_reason(clear_flags=True, confirm=True) 清零 → 复现 → 再读。")

    # 最常见的情况单独给一句可执行建议
    if any(nm == "IWDGRSTF" for nm, _ in hit):
        lines.append("  ⓘ 出现了看门狗复位：查喂狗点是否被长循环/阻塞调用（HAL_Delay 里的死等、"
                     "while 等标志位、printf 阻塞）拖过去了；把喂狗放到主循环里，别放在中断里。")
    if any(nm == "BORRSTF" for nm, _ in hit):
        lines.append("  ⓘ 出现了欠压复位：查供电能力（尤其带电机/无线发射时的瞬时电流）、"
                     "去耦电容，以及上电斜率。")
    if any(nm == "PINRSTF" for nm, _ in hit) and len(hit) == 1:
        lines.append("  ⓘ 只有 PINRSTF：多半是按键复位或调试器复位，不是程序问题。")

    if not clear_flags:
        lines.append("")
        lines.append("  ⓘ 只想定位「这一次」的话：清标志 → 复现 → 再读。清零用")
        lines.append("    reset_reason(clear_flags=True, confirm=True)（不可逆：会销毁当前现场）。")
        return "\n".join(lines)

    # ---- 清标志（不可逆）----
    blocked = guard.check("reset_flags_clear", None, confirm=confirm)
    if blocked:
        return "\n".join(lines) + "\n\n" + blocked
    lines.append("")
    lines.append("  —— 清零（写 RCC_RSR.RMVF = 0x%08X）——" % _RSR_RMVF)
    # ★ 清零与复位必须分两步、不能合成 "-w32 RMVF -nv -rst"：
    #   normal 档连接本身就会置 PINRSTF|SFTRSTF，-rst 也会再置一次，
    #   合并成一条命令后读回必然还看到那两个位 —— 于是把「命令自己的痕迹」
    #   误报成「RMVF 没生效」。分开之后：写+读回在同一次会话里（normal 连接造成的
    #   标志会被这次写入一并清掉），读回 0 就是 RMVF 生效的硬证据。
    rc2, out2 = _sp_run(_sp_conn(_SP_MODE_FULL, sn)
                        + ["-w32", "0x%08X" % regs["rsr"], "0x%08X" % _RSR_RMVF,
                           "-nv", "-r32", "0x%08X" % regs["rsr"], "4"], timeout=90)
    after = _sp_vals_from(out2)
    if after and after[0] == 0:
        lines.append("  清后读回   : 0x00000000 ✓ RMVF 已生效，全部标志清空")
    elif after:
        lines.append("  ✗ 清后读回 : 0x%08X —— RMVF **没写进去**（不是「标志清不掉」）。"
                     % after[0])
        lines.append("    排查：1) 调试口是否被取消授权 / 目标是否还在 SWD 连接上；"
                     "2) 写保护或生命周期状态是否禁止改 RSR；3) 换 normal 档重试。")
    else:
        lines.append("  ✗ 清后读回 : 读不到（rc=%s），清零是否成功无法确认。" % rc2)

    if not then_reset:
        lines.append("  注意       : then_reset=False，没做复位 —— 内核此刻停在 halt"
                     "（normal 档的副作用）。要放开继续跑，再调一次"
                     " reset_reason(clear_flags=True, confirm=True)。")
        return "\n".join(lines)

    # 读回验证过之后再复位，让内核继续跑
    rc3, _o3 = _sp_run(_sp_conn(_SP_MODE_READ, sn) + ["-rst"], timeout=90)
    final = _sp_read_words(regs["rsr"], 4, sn, _SP_MODE_READ)
    lines.append("  %s: -c port=SWD mode=hotplug -rst（复位让内核继续跑）"
                 % _pad("复位", 11))
    if final:
        names = [nm for bit, nm, _w in _RSR_BITS if final[0] & (1 << bit)]
        lines.append("  %s: 0x%08X  %s"
                     % (_pad("复位后读回", 11), final[0], "、".join(names) or "全 0"))
        noise = [nm for nm in names if nm not in ("PINRSTF", "SFTRSTF")]
        if noise:
            lines.append("  ⚠ 出现了不该由复位产生的标志：%s —— 说明在清零与复位之间"
                         "还有别的复位源在动作（看门狗/欠压），这反而是一条真线索。" % "、".join(noise))
        elif names:
            lines.append("  说明       : 上面这两个位是**刚才这条 -rst 自己**造成的"
                         "（实测：hotplug -rst → PINRSTF|SFTRSTF）。")
            lines.append("               它们既不代表清零失败，也不代表原来的复位原因 —— "
                         "现在标志是干净的，可以去复现问题了。")
        else:
            lines.append("  说明       : 复位后标志仍为全 0。")
    else:
        lines.append("  ✗ 复位后读回 : 读不到（rc=%s）" % rc3)
    return "\n".join(lines)


def flash_verify(file_path, address=None, sn=None, chunk=65536):
    """烧录后校验：把固件文件与芯片上实际内容**逐字节比对**（只读，走 hotplug）。

    为什么不用 `-v`：STM32_Programmer_CLI 的 `-v` 只能紧跟 `-w`（单独调会被拒），
    那就变成「重新烧一遍再校验」，不是「校验已经烧进去的东西」。
    这里改用 `-u` 把 flash 上的内容读回来自己比 —— 结果是可复现的字节级证据，
    顺带能报出**第一个不一致的偏移**，比一句 verified/unverified 有用得多。

    ★ 也不要拿 `-checksum` 去和 `-fchecksum` 互相验：两者口径不同，实测同一份固件
      给出不同值（内存侧 0x000073A9 vs 文件侧 0x000071E2）。字节比对才是唯一真凭据。

    chunk 是单次回读的字节数（默认 64KB）。实测：单次 4KB 约 0.42s、256KB 约 0.96s —
      开销几乎全在每次建立连接（约 0.4s），所以 chunk 越大越快，别调小。

    支持 .bin（需 address，默认 0x08000000）/ .hex（内置 Intel HEX 解析）/ .elf。
    单一 elf 只校验落在 flash 里的 PROGBITS 段，输出里会写明跳过了哪些段。

    返回文本，含每段结论、首个不一致偏移与前后字节上下文。
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return ("要给出固件文件路径（.bin / .hex / .elf），收到：%r\n"
                "例：flash_verify(\"build/app.bin\")、flash_verify(\"build/app.hex\")" % (file_path,))
    if not os.path.exists(file_path):
        return "固件文件不存在：%s" % file_path
    if not _which_tool("STM32_Programmer_CLI"):
        return ("没找到 STM32_Programmer_CLI，无法回读校验。\n" + flash_tool_hint())
    try:
        ck = int(chunk)
    except (TypeError, ValueError):
        return "chunk 必须是整数，收到：%r" % (chunk,)
    if ck <= 0:
        return "chunk 必须为正整数，收到：%r" % (chunk,)

    try:
        segs, note = _firmware_segments(file_path, address)
    except (ValueError, OSError) as e:
        return "解析固件失败：%s" % e

    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="mcu-dbg-verify-")
    total = sum(len(d) for _a, d in segs)
    bad_total = 0
    lines = ["烧录后校验（hotplug 只读，不复位板子）", "-" * 48,
             "  文件       : %s（%d 段，共 %d 字节）" % (os.path.basename(file_path),
                                                        len(segs), total)]
    if note:
        lines.append("  说明       : %s" % note)
    try:
        for idx, (addr, data) in enumerate(segs):
            seg_bad = 0
            seg_ok = True          # False = 这一段没能完成比对（回读失败），不是"一致"
            first = None            # (段内偏移, 期望字节, 实际字节, 期望上下文, 实际上下文)
            off = 0
            while off < len(data):
                n = min(ck, len(data) - off)
                ap = os.path.join(tmp, "r%d_%d.bin" % (idx, off))
                a = addr + off
                if not _sp_download(a, n, ap, sn, _SP_MODE_READ):
                    lines.append("  段%d @0x%08X +%d：回读失败（读不到内存）" % (idx, addr, off))
                    seg_ok = False
                    bad_total += n
                    break
                try:
                    with open(ap, "rb") as fh:
                        got = fh.read()
                except OSError as e:
                    lines.append("  段%d @0x%08X +%d：临时文件读不到（%s）" % (idx, addr, off, e))
                    seg_ok = False
                    bad_total += n
                    break
                if len(got) != n:
                    lines.append("  段%d @0x%08X +%d：回读长度不符（要 %d 得 %d）"
                                 % (idx, addr, off, n, len(got)))
                    seg_ok = False
                    bad_total += n
                    break
                for i in range(n):
                    if got[i] != data[off + i]:
                        seg_bad += 1
                        if first is None:
                            lo, hi = max(0, i - 8), min(n, i + 8)
                            first = (off + i, data[off + i], got[i],
                                     data[off + lo:off + hi], got[lo:hi])
                off += n
            bad_total += seg_bad
            if not seg_ok:
                # 2026-10-13：此前这里只判 seg_bad == 0，回读失败也打印
                # "✓ 完全一致" —— 校验工具给假通过（探针掉线也报固件已正确烧入）。
                lines.append("  段%d @0x%08X  %6d 字节  ？ 未能校验（回读失败，"
                             "不能认定一致）" % (idx, addr, len(data)))
            elif seg_bad == 0:
                lines.append("  段%d @0x%08X  %6d 字节  ✓ 完全一致" % (idx, addr, len(data)))
            else:
                fo, exp, act, ctx_e, ctx_a = first
                lines.append("  段%d @0x%08X  %6d 字节  ✗ 有 %d 字节不一致，"
                             "首个在偏移 +%d（绝对地址 0x%08X）：期望 0x%02X，实际 0x%02X"
                             % (idx, addr, len(data), seg_bad, fo, addr + fo, exp, act))
                lines.append("        期望片段（首个不一致前后各 8 字节）：%s"
                             % ctx_e.hex(" ").upper())
                lines.append("        实际片段：%s" % ctx_a.hex(" ").upper())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    lines.append("-" * 48)
    if bad_total == 0:
        lines.append("结论：✓ 芯片上的内容与文件逐字节一致（%d 字节全部通过）。" % total)
        lines.append("  注意：这只证明「文件写进去了」，不证明「固件能跑」——")
        lines.append("  跑没跑起来要看串口有没有输出（serial_read / stream_capture）。")
    else:
        lines.append("结论：✗ 有 %d 字节未通过校验（不一致或回读失败）。" % bad_total)
        lines.append("  其中含回读失败的字节不计为「证明一致」——探针掉线/SWD 断开/"
                     "读保护打开时先修链路，再谈烧录结果。")
        lines.append("  排查顺序：1) 重烧一次看是否复现；2) 看 flash 写保护 / 读保护设置"
                     "（chip_info(mode=\"full\") 看选项字节）；")
        lines.append("            3) 确认 address 参数与烧录时给的起始地址一致。")
    return "\n".join(lines)


# ---------------------------------------------------------------
# 时序分析 / 长时录测（G07 / G12）
# ---------------------------------------------------------------

#: 采集记录里每行的时间戳前缀，形如 ``[+0.0123]``
_CAP_STAMP_RE = re.compile(r"^\[\+?(\d+(?:\.\d+)?)\]\s?(.*)$")
#: 常见日志时间戳：HH:MM:SS(.mmm) 或 HH:MM:SS,mmm
_CAP_HMS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,6})")
#: 行首独立数字（当成时间戳或序号）
_CAP_LEADNUM_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\b")


def _timing_samples(text, pattern=None, seq_pattern=None):
    """从文本里抽出 ``[(时间戳(秒), 序号或None, 原文), ...]``。

    时间戳来源按优先级：自定义正则 > ``[+0.012]`` 采集前缀 > ``HH:MM:SS.mmm``。
    抽不到时间戳时返回空列表 —— 不猜、不用行号假冒时间。

    序号来源：``seq_pattern`` 的第 1 个捕获组；没给的话退化成「行首那个整数」。

    ★ 为什么要有 seq_pattern：默认的「行首整数」只能认 ``42 TICK ...`` 这种写法，
      而嵌入式日志里更常见的是 ``TICK 42`` / ``seq=42`` —— 序号不在行首。
      不开口子的话，这类日志会静默地「查不出丢帧」，看起来像「一切正常」，
      比报错还危险。所以把口子显式开出来，并在输出里说明用的是哪条规则。
    """
    out = []
    pat = re.compile(pattern) if pattern else None
    spat = re.compile(seq_pattern) if seq_pattern else None
    last_hms = None
    day = 0.0
    for raw in (text or "").splitlines():
        ln = raw.rstrip()
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        ts = None
        seq = None
        if pat is not None:
            m = pat.search(ln)
            if m:
                try:
                    ts = float(m.group(1))
                except (ValueError, IndexError):
                    ts = None
        if ts is None:
            m = _CAP_STAMP_RE.match(ln.strip())
            if m:
                ts = float(m.group(1))
                ln = m.group(2)
        if ts is None:
            m = _CAP_HMS_RE.search(ln)
            if m:
                h, mi, s, frac = (int(m.group(1)), int(m.group(2)),
                                  int(m.group(3)), m.group(4))
                ts = h * 3600 + mi * 60 + s + float("0." + frac)
                if last_hms is not None and ts < last_hms - 60:
                    day += 86400.0          # 跨零点
                last_hms = ts
                ts += day
        if ts is None:
            continue
        if spat is not None:
            ms = spat.search(ln)
            if ms:
                try:
                    seq = int(ms.group(1))
                except (ValueError, IndexError):
                    seq = None
        else:
            m = _CAP_LEADNUM_RE.match(ln)
            if m and re.fullmatch(r"-?\d+", m.group(1)):
                seq = int(m.group(1))
        out.append((ts, seq, ln.strip()))
    return out


def timing_analysis(text=None, path=None, pattern=None, seq_pattern=None):
    """时序 / 数据率分析：找丢帧（序号跳号）、卡顿（间隔离群）、节拍漂移。

    输入二选一：``text``（现成日志）或 ``path``（日志/采集文件）。
    配合 ``stream_capture`` 用最顺：采一段 → 直接把文件喂进来。

    ``pattern``     自定义时间戳正则（第 1 个捕获组是秒），优先于内置两种格式。
    ``seq_pattern`` 自定义序号正则（第 1 个捕获组是序号）。不给则只认「行首整数」；
                    ``TICK 42`` / ``seq=42`` 这类写法必须显式给，
                    例如 ``seq_pattern=r"TICK\\s+(\\d+)"``。

    ★ 能得出什么、不能得出什么（先说清楚，免得拿它当示波器）：
      - 可以：相对比较。间隔中位数、抖动、哪几次明显变慢、序号有没有跳号、节拍是否整体漂移。
      - 不可以：绝对精度的断言。时间戳来自 PC 侧（操作系统调度 + USB 缓冲），
        本身就带毫秒级抖动，所以别拿它去判「周期是不是精确 1.000 ms」。
    """
    if not text and not path:
        return "要给输入：text=<日志内容> 或 path=<文件路径>。"
    # 正则先在这里校验：re.compile 抛的是 re.error，直接冒出去就是「工具抛异常」，
    # 而本项目的口径是工具只回可读文本。这类入参错误必须转成人话。
    for _label, _rx in (("pattern", pattern), ("seq_pattern", seq_pattern)):
        if _rx:
            try:
                re.compile(_rx)
            except re.error as e:
                return ("%s 不是合法的正则：%s\n  收到：%r\n"
                        "  自定义正则只需给**第 1 个捕获组**填你要的值，例如：\n"
                        "    pattern=r\"T\\+(\\d+\\.\\d+)\"（时间戳，单位秒）\n"
                        "    seq_pattern=r\"TICK\\s+(\\d+)\"（序号）"
                        % (_label, e, _rx))
    src = "传入文本"
    if not text and path:
        if not isinstance(path, str) or not path.strip():
            return "path 要是文件路径字符串，收到：%r" % (path,)
        if not os.path.exists(path):
            return "文件不存在：%s" % path
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            return "读文件失败：%s" % e
        src = path

    samples = _timing_samples(text, pattern, seq_pattern)
    if len(samples) < 2:
        return ("只抽到 %d 条带时间戳的记录，做不了时序分析。\n"
                "支持的时间戳形式：\n"
                "  1) 采集记录前缀 [+0.0123]（stream_capture 的产物就是这个格式）\n"
                "  2) HH:MM:SS.mmm 或 HH:MM:SS,mmm\n"
                "  3) 自定义：timing_analysis(..., pattern=\"正则，第 1 个捕获组是秒\")\n"
                "如果日志里根本没有时间戳，那就没有时序信息可用 —— "
                "本工具不会用行号假冒时间。" % len(samples))

    ts = [s[0] for s in samples]
    ivs = [round((ts[i + 1] - ts[i]) * 1000.0, 4) for i in range(len(ts) - 1)]
    t0, t1 = ts[0], ts[-1]

    # ★ 时间戳分辨率就是一个刻度（采集端按 [+%.4f] 记，即 0.1ms）。
    #   整个窗口如果连一个刻度都不到，说明这批行是**同一瞬间**到的
    #   —— 常见于「把一次性回灌的日志当采集记录」。
    #   此时「数据率」「间隔」「抖动」全是除零边缘的假数：实测会打印出
    #   「6000000000.00 行/秒」这种垃圾。与其编一个数，不如直说窗口不够。
    if (t1 - t0) < 1e-4:
        return ("时序分析\n" + "-" * 48 + "\n"
                "  来源       : %s\n"
                "  带时间戳行 : %d 行\n"
                "  ⚠ 但是这些行的时间戳**全部落在同一个刻度内**（窗口 %.6f 秒，"
                "少于 0.1ms 的分辨率）。\n"
                "    这批数据要么是一次性回灌的日志（行与行之间本来就没时间差），"
                "要么采得太快没铺开。\n"
                "    ⟹ 数据率 / 间隔 / 抖动 / 漂移**一律不给** —— 在这种输入上算出来的是垃圾数，"
                "不是「差不多」的估计。\n"
                "    想要能分析的记录：用 stream_capture 采一段真实的持续输出（几秒即可）。"
                % (src, len(samples), t1 - t0))

    span = max(t1 - t0, 1e-9)
    srt = sorted(ivs)

    def pct(p):
        if not srt:
            return 0.0
        k = (len(srt) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(srt) - 1)
        return srt[lo] + (srt[hi] - srt[lo]) * (k - lo)

    med = pct(0.5)
    mean = sum(ivs) / len(ivs)
    p95 = pct(0.95)
    p05 = pct(0.05)

    lines = ["时序分析", "-" * 48,
             "  来源       : %s" % src,
             "  带时间戳行 : %d 行，窗口 %.3f 秒" % (len(samples), span),
             "  数据率     : %.2f 行/秒（平均间隔 %.3f ms）"
             % (len(samples) / span, mean),
             "  间隔中位数 : %.3f ms" % med,
             "  间隔 P05/P95: %.3f / %.3f ms" % (p05, p95),
             "  抖动(P95−P50): %.3f ms；最大间隔 %.3f ms" % (p95 - med, max(ivs))]

    # ---- 1) 序号跳号 = 丢帧 ----
    seqs = [s[1] for s in samples if s[1] is not None]
    lines.append("")
    if len(seqs) >= 2:
        gaps = []
        for i in range(len(seqs) - 1):
            d = seqs[i + 1] - seqs[i]
            if d > 1:
                gaps.append((seqs[i], seqs[i + 1], d - 1))
        missing = sum(g[2] for g in gaps)
        if gaps:
            lines.append("  丢帧       : 检出 %d 处序号跳号，累计缺 %d 个"
                         % (len(gaps), missing))
            for a, b, n in gaps[:8]:
                lines.append("      · %d → %d（缺 %d 个）" % (a, b, n))
            if len(gaps) > 8:
                lines.append("      …… 还有 %d 处" % (len(gaps) - 8))
            lines.append("    含义：接收侧少了数据。查这三处 —— 发送侧丢（缓冲溢出/被高优先级任务打断）、")
            lines.append("          链路丢（波特率过高/线太长/没共地）、接收侧丢（读太慢/缓冲太小）。")
        else:
            lines.append("  丢帧       : 序号连续，没检出跳号（说明这段没丢数据）")
    else:
        lines.append("  丢帧       : 抽不到序号，跳过这项检查")
        lines.append("            默认只认【行首那个整数】（如 \"42 TICK ...\"）。")
        lines.append("            序号不在行首的写法要显式给，例如 "
                     "seq_pattern=r\"TICK\\s+(\\d+)\" 或 r\"seq=(\\d+)\"。")

    # ---- 2) 间隔离群 = 卡顿 / 节拍变慢 ----
    thr = max(med * 1.8, med + 20.0)
    slow = [(i, v) for i, v in enumerate(ivs) if v > thr]
    lines.append("")
    if slow:
        lines.append("  卡顿       : %d 处间隔超过 %.1f ms（中位数的 1.8 倍），最长 %.1f ms"
                     % (len(slow), thr, max(v for _i, v in slow)))
        for i, v in slow[:8]:
            lines.append("      · 第 %d→%d 行：%.1f ms（约基线的 %.1f 倍）"
                         % (i + 1, i + 2, v, v / med if med else 0))
        if len(slow) > 8:
            lines.append("      …… 还有 %d 处" % (len(slow) - 8))
        lines.append("    含义：节拍被拉长。常见原因是整片 flash 擦写（阻塞）、"
                     "printf 阻塞、GC/内存分配、看门狗喂狗点被长任务跨过。")
    else:
        lines.append("  卡顿       : 没有超阈值的离群间隔（节拍平稳）")

    # ---- 3) 漂移：间隔随行号是否单调变化 ----
    n = len(ivs)
    if n >= 10:
        xs = list(range(n))
        mx = sum(xs) / n
        my = mean
        den = sum((x - mx) ** 2 for x in xs)
        slope = (sum((xs[i] - mx) * (ivs[i] - my) for i in range(n)) / den) if den else 0.0
        # ★ 判「有没有系统性漂移」要比**同一量纲**的东西：
        #   斜率是 ms/行，把它直接和「中位间隔的 20%」（ms/次）比是错的 —— 单位不同，
        #   样本一多就一定触发（我第一版就是这样，把一段其实只漂了 22ms、占中位间隔
        #   18% 的数据报成了「系统性变慢」）。改成先按斜率折算到**整段累计变化**
        #   （ms），再和中位间隔、和实际离散度比：
        #     累计变化 > 一个中位间隔     且   累计变化 > P95−P05
        #   两条同时成立才算系统性漂移 —— 后者用来挡「被几个离群点带偏的拟合」。
        total_drift = slope * n
        spread = p95 - p05
        share = (abs(total_drift) / med * 100.0) if med else 0.0
        lines.append("")
        lines.append("  漂移       : 拟合斜率 %.5f ms/行；按此折算整段累计变化 %+.1f ms"
                     "（中位间隔的 %.0f%%）" % (slope, total_drift, share))
        if abs(total_drift) > med and abs(total_drift) > spread:
            lines.append("    ⚠ 累计变化同时超过「一个中位间隔」和「P95−P05 的实际离散度」，")
            lines.append("      像是系统性的变慢/变快，而不是随机抖动：查时钟源（HSI 温漂）、"
                         "中断被逐渐拖长、缓冲越用越满。")
        else:
            lines.append("    线性项太小 ⟹ 节拍没有系统性漂移，剩下的差异属于随机抖动"
                         "（或被个别离群间隔带偏的拟合）。")
    else:
        lines.append("")
        lines.append("  漂移       : 样本太少（<10 个间隔），不做拟合")

    lines.append("")
    lines.append("  ⓘ 时间戳来自 PC 侧（OS 调度 + USB 缓冲），自带毫秒级抖动。")
    lines.append("    上面的数适合做【相对比较】（哪几次变慢、有没有跳号、是否整体漂移），")
    lines.append("    不适合当示波器判「周期是不是精确 1.000 ms」。")
    return "\n".join(lines)


def stream_capture(port, seconds=10.0, baudrate=115200, max_lines=0,
                   out_path=None, idle_stop_s=0.0):
    """长时录测：把串口持续采集一段，逐行打上到达时间戳落盘（G12）。

    产出的文件**直接可以喂给 timing_analysis(path=...)** —— 两件工具是同一条链路。

    参数：
      seconds      最长采集多少秒（默认 10）
      max_lines    收够多少行就提前结束（0 = 不限）
      idle_stop_s  连续多少秒没有新数据就提前结束（0 = 不用这个条件）
      out_path     落盘路径；默认落在会话日志目录（paths.logs_dir()）

    为什么时间戳要在**采集端**打：事后拿到的日志如果没时间戳，时序信息就永久丢了。
    采集时按行记到达时刻，成本几乎为零，却让后面所有分析都成立。
    """
    if not HAS_PYSERIAL:
        return _deps_msg()
    if not isinstance(port, str) or not port.strip():
        return ("要给出串口名，例：stream_capture(\"COM21\", seconds=10)。收到：%r\n"
                "不知道口名时先用 serial_list 枚举。" % (port,))
    if out_path is not None and not isinstance(out_path, str):
        return "out_path 要是文件路径字符串（不给就落到会话日志目录），收到：%r" % (out_path,)
    try:
        secs = float(seconds)
        baud = int(baudrate)
        mlines = int(max_lines or 0)
        idle = float(idle_stop_s or 0.0)
    except (TypeError, ValueError):
        return "参数类型不对：seconds/baudrate/max_lines/idle_stop_s 都要是数字。"

    # 2026-10-13：旧版复用 serial_open（read 档）过闸——长时独占的口径
    # 比占 3 秒的 pair_record 还松，矛盾。改用独立登记的 stream_capture（write 档）。
    blocked = guard.check("stream_capture", port)
    if blocked:
        return blocked
    try:
        sp = _open_serial(port=port, baudrate=baud, timeout=0.05)
    except Exception as e:                                   # noqa: BLE001
        return ("打开 %s 失败：%s\n排查：1) 是否被别的软件占用；2) 端口号是否正确。"
                % (port, e))

    logs_dir = _logs_dir()
    try:
        os.makedirs(logs_dir, exist_ok=True)
    except OSError:
        logs_dir = None
    if not out_path:
        safe = re.sub(r"[^A-Za-z0-9]+", "_", port) or "port"
        out_path = os.path.join(logs_dir or os.getcwd(),
                                "capture_%s_%s.log" % (time.strftime("%Y%m%d_%H%M%S"), safe))
    # 不覆盖已有文件：采集记录就是「现场」，覆盖等于毁证据。不弹窗询问，直接换个名。
    if os.path.exists(out_path):
        base, ext = os.path.splitext(out_path)
        k = 2
        while os.path.exists("%s_%d%s" % (base, k, ext)):
            k += 1
        out_path = "%s_%d%s" % (base, k, ext)

    t0 = time.time()
    buf = b""
    rows = []
    lines_n = 0
    last_data = t0
    stop_reason = "到时间"
    try:
        while True:
            now = time.time()
            if now - t0 >= secs:
                stop_reason = "到时间（%.1fs）" % secs
                break
            chunk = sp.read(4096)
            if chunk:
                buf += chunk
                last_data = now
                while b"\n" in buf:
                    ln, buf = buf.split(b"\n", 1)
                    rows.append((time.time() - t0, ln.rstrip(b"\r").decode("utf-8", "replace")))
                    lines_n += 1
                if mlines and lines_n >= mlines:
                    stop_reason = "收够 %d 行" % mlines
                    break
            elif idle and (now - last_data) >= idle:
                stop_reason = "连续 %.1fs 没数据" % idle
                break
            else:
                time.sleep(0.005)
    finally:
        try:
            sp.close()
        except Exception:
            pass
    if buf:
        rows.append((time.time() - t0, buf.rstrip(b"\r").decode("utf-8", "replace")))

    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("# mcu-debug-mcp 采集记录\n")
            fh.write("# 端口: %s @ %d\n" % (port, baud))
            fh.write("# 开始: %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            fh.write("# 结束原因: %s\n" % stop_reason)
            fh.write("# 格式: [+<相对秒>] <原始行>\n")
            for off, ln in rows:
                fh.write("[+%.4f] %s\n" % (off, ln))
        saved = out_path
    except OSError as e:
        saved = None
        note = "落盘失败（%s），结果只在下面这行摘要里。" % e

    last_off = rows[-1][0] if rows else 0.0
    out = ["串口采集完成", "-" * 48,
           "  端口       : %s @ %d" % (port, baud),
           "  行数       : %d 行" % len(rows)]
    if last_off >= 1e-4:
        out.append("  窗口       : %.3f 秒（%.2f 行/秒）" % (last_off, len(rows) / last_off))
    elif rows:
        # 全部落在同一个时间戳刻度里：算数据率会得到天文数字，不如不给
        out.append("  窗口       : <0.1ms（不足时间戳分辨率）—— 数据率不给，"
                   "这批行是同一瞬间到的")
    out.append("  结束原因   : %s" % stop_reason)
    if saved:
        out.append("  已落盘     : %s" % saved)
        out.append("")
        out.append("  下一步     : timing_analysis(path=\"%s\") 可以直接分析丢帧/卡顿/漂移。" % saved)
    else:
        out.append("  " + note)
    if len(rows) == 0:
        out.append("")
        out.append("  ⓘ 一行都没收到。依次查：1) 设备是否在输出（有没有真的在跑）；"
                   "2) 波特率是否匹配（USB CDC 口波特率不影响数据，别在这儿反复试）；"
                   "3) TX/RX 是否接反、有没有共地。")
    return "\n".join(out)


# ---------------------------------------------------------------
# 总线工具（G06）：I2C / SPI / 1-Wire / RS485 —— 全部经「串口桥」访问
#
# 为什么必须有「桥」：PC 上没有任何一根 I2C/SPI 线。要真的去捅总线，必须有一块
# 把「串口命令」翻译成「总线时序」的 MCU。本机能走的几条路都实测过：
#   · ST-Link 官方 bridge —— 要 STLINK-V3SET + B-STLINK-BRIDGE 扩展板；板载 V3E
#     实测直接报 BRG_NO_STLINK，走不通（那是 300+ 元的一套，不值）。
#   · pyftdi（FT232H/FT2232H）—— 必须能换成 libusb/WinUSB 驱动；本机 FT2232H
#     走官方 VCP，且师兄明确「只能读，不能动」，这条路被关掉。
#   ⟹ 剩下唯一全程可用、零外购的路：拿一块**闲置 MCU** 跑桥固件
#     （源码见 tools/bridge_fw/；ESP32-S3 / STM32 / Arduino 都能跑）。
#
# ★ 桥协议 MCUDBG/1（纯 ASCII 行协议，固件侧几十行就够）：
#     发： <CMD> [args...]\r\n
#     收： +OK <CMD> ...字段...      ← 成功；字段形如 key=value
#          -ERR <CODE> <说明>        ← 失败；CODE 见 _BRIDGE_ERR_HINT
#          #LOG ...                  ← 异步日志，解析时一律忽略
#   命令表（固件侧实现同样这几条；见 tools/bridge_fw/README.md）：
#     PING                                    → BRIDGE=mcu-debug proto=MCUDBG/1 caps=...
#     I2C SCAN  <bus> [lo] [hi]               → bus=0 lo=8 hi=119 n=2 addrs=0x70,0x71
#     I2C READ  <bus> <addr> <reg> <n>        → bus=0 addr=0x68 reg=0x75 n=1 data=0x68
#     I2C WRITE <bus> <addr> <reg> <b0[,b1]>  → n=1
#     SPI SCAN  <bus>                         → bus=0 n=1 slots=0:0xef4018
#     SPI READ  <bus> <cs> <cmd> <n>          → bus=0 cs=0 cmd=0x9f n=3 data=0xef,0x40,0x18
#     SPI WRITE <bus> <cs> <cmd> <b0[,b1]>    → n=1
#     OW SCAN   <bus>                         → bus=0 n=1 roms=28ff64...,...
#     RS485 SEND <bus> <de_ms> <hex>          → bus=0 n=8
#
# ★ PING 是**强制握手**：回话里必须含 BRIDGE=mcu-debug，否则一律拒绝当桥用。
#   没有这道门，任何一根 UART 都可能被当桥，然后被灌一堆垃圾命令。
#
# ★ 为什么**绝不**做「全口自动探测找桥」：本机实测出来的教训 —— 我往 COM46 发了
#   一行 `help`，那块正在跑的 BNO080 手套固件**真的解析并回了话**（"I2C idx=1
#   (reboot to apply)"）。PC 上一个想当然的探测字节，会打进别人正在跑的固件里，
#   可能改它配置、甚至触发重启。所以端口必须由调用方显式给出（或从环境变量
#   MCU_DEBUG_BRIDGE_PORT 读），工具**不去猜**。
#
# ★ 诚实边界（写在最前面，别让用户以为这工具能测出比它真能测的更多）：
#   · 扫描扫到的「有应答」= 该地址上有人 ACK，**不等于**器件的功能是好的；
#     「没应答」也可能是器件在复位中 / 地址被别的器件占了总线。
#   · I2C 扫描会遗漏「重复地址冲突」：两颗器件若都焊成 0x68，扫出来只看到一个。
#   · SPI 没有地址概念，本工具的 spi_scan 是「逐条 CS 读 JEDEC ID」，只对**遵守
#     0x9F 命令的 SPI NOR/NAND flash** 有效；对其它 SPI 从设备（传感器/ADC）需要用
#     spi_read_reg 给具体命令字，扫不出来属于正常，不是故障。
#   · rs485_send 只能确认「桥把字节发出去了」，**对端有没有收到它证明不了** ——
#     要证明得对端回话或用示波器看差分波形。别把它当通信链路已通的证据。
# ---------------------------------------------------------------

_BRIDGE_ENV = "MCU_DEBUG_BRIDGE_PORT"
_BRIDGE_BAUD = 115200
_BRIDGE_TAG = "mcu-debug"
_BRIDGE_PROTO = "MCUDBG/1"
_BRIDGE_CMD_TIMEOUT = 6.0
_BRIDGE_MAX_BYTES = 256          # 单次读写字节数上限（桥侧也有同样的限，别让它俩不一致）
_I2C_SCAN_LO, _I2C_SCAN_HI = 0x08, 0x77

#: 桥的 -ERR 码 → 给用户的排查方向。桥侧只报码，人话在这边给。
_BRIDGE_ERR_HINT = {
    "NAK": ("从设备没有应答。依次查：1) 7 位地址对不对（别把 8 位的读/写地址填进来，"
            "0x68 的读地址是 0xD1）；2) 器件是否上电（VCC/GND）；"
            "3) SDA/SCL 是否接反；4) 有没有上拉电阻（4.7k 典型，缺了就一直 NAK）；"
            "5) 地址脚（AD0/SA0/SDO）的接法。"),
    "TIMEOUT": ("总线超时。常见原因是 SDA 或 SCL 被某个器件**拉死在低电平**（总线卡死）"
                "—— 典型场景是主控在从设备发字节的中途复位。处置：断电重新上电，"
                "或在 SCL 上补 9 个时钟把从设备的状态机顶出来。"),
    "NOBUS": "桥固件里没有配置这条总线（bus 编号对不上，或固件没打开该控制器）。",
    "ARG": "桥认为参数不合法（地址超出 7 位范围 / 字节数超限 / 数值格式错）。",
    "BUSY": "桥正忙（上一笔传输没收尾）。稍后重试；反复出现说明桥侧有卡死路径。",
    "PROTO": "桥回话解析失败（协议版本不匹配，或线路噪声把回话打花了）。",
    "NOCS": "要访问的 CS 槽位在桥固件里没配置。",
    "NOOW": "桥固件没实现 1-Wire（该功能编译时未打开）。",
}

#: I2C 保留地址（7 位视角）。依据 NXP UM10204《I2C-bus specification and user manual》
#: Table 2「Reserved addresses」：0000 xxx 与 1111 xxx 段不做普通从设备寻址。
_I2C_RESERVED = (
    ((0x00, 0x07), "I2C 规范保留段（0000 xxx：广播调用 / CBUS / 不同总线格式 / Hs 主机码）"),
    ((0x78, 0x7F), "I2C 规范保留段（1111 xxx：10 位从地址前缀 与 保留）"),
)

#: I2C 7 位地址 → (可能的器件, source)。**最小集**，逐条来自器件数据手册。
#: ★ 只做「提示」不做「判定」：同一地址常被多个器件共用（0x68 上到底是 MPU-6050
#:   还是 DS1307，**光看地址分不出来**）。要定种必须读该器件的 WHO_AM_I / ID 寄存器。
#: 完整器件库（接口/地址/量程/坑）属 G01，届时会把它扩成可扩展的知识层文件并加校验。
_I2C_KNOWN = {
    0x1E: ("HMC5883L 磁力计 / LSM303 加速度计（0x1E 侧）",
           "Honeywell HMC5883L 数据手册：7 位地址固定 0x1E"),
    0x29: ("VL53L0X 激光测距 / TSL2561 光强（0x29 侧）",
           "ST VL53L0X 数据手册：7 位地址固定 0x29"),
    0x40: ("INA219 电流计 / Si7021 / HTU21D 温湿度（0x40 侧）",
           "TI INA219 数据手册 SBOS448：A1A0=00 → 0x40"),
    0x44: ("SHT31/SHT35 温湿度（ADDR 接 GND）",
           "Sensirion SHT3x 数据手册：ADDR 低 → 0x44、高 → 0x45"),
    0x48: ("ADS1115 ADC / LM75 / TMP102（0x48 侧）",
           "TI ADS1115 数据手册 SBAS444：ADDR 引脚决定 0x48–0x4B"),
    0x4A: ("★ BNO080 / BNO085 / FSP201 类 SH-2 传感器（SA0=低）",
           "CEVA SH-2 官方 HAL（bno08x_i2c_hal.c）ADDR_SH2_0=0x4A；"
           "SA0 拉低为 0x4A、拉高为 0x4B（同一颗芯片 DFU 模式是 0x28/0x29）"),
    0x4B: ("★ BNO080 / BNO085（SA0=高，SparkFun 分线板默认拉高）",
           "同上；SparkFun BNO080 板默认 SA0 上拉 → 0x4B"),
    0x50: ("AT24Cxx / 24LCxx EEPROM（A2A1A0=000）",
           "Microchip AT24C01A/02/… 数据手册：7 位地址 0x50 + A2A1A0"),
    0x68: ("MPU-6050/MPU-6500/ICM-426xx（AD0=低） / DS1307 / DS3231 RTC",
           "InvenSense MPU-6050 Register Map Rev 4.2：AD0=0 → 0x68、AD0=1 → 0x69；"
           "Maxim DS1307 与 DS3231 数据手册：7 位地址固定 0x68"),
    0x69: ("MPU-6050 系列（AD0=高） / ICM-42688（AD0=高）",
           "InvenSense MPU-6050 Register Map Rev 4.2（AD0=1）"),
    0x6A: ("LSM6DS3/LSM6DSO/LSM6DSL（SA0=低） / ICM-42688（AD0=低）",
           "ST LSM6DSO 数据手册：SA0=0 → 0x6A、SA0=1 → 0x6B"),
    0x6B: ("LSM6DS3/LSM6DSO/LSM6DSL（SA0=高）",
           "ST LSM6DSO 数据手册：SA0=1 → 0x6B"),
    0x70: ("★ TCA9548A / PCA9548A 8 路 I2C 多路复用器（A2A1A0=000）",
           "TI TCA9548A 数据手册 SCPS207：7 位地址 = 0x70 | A2A1A0"),
    0x71: ("★ TCA9548A（A2A1A0=001）", "TI TCA9548A 数据手册 SCPS207"),
    0x72: ("★ TCA9548A（A2A1A0=010）", "TI TCA9548A 数据手册 SCPS207"),
    0x73: ("★ TCA9548A（A2A1A0=011）", "TI TCA9548A 数据手册 SCPS207"),
    0x74: ("TCA9548A（A2A1A0=100）", "TI TCA9548A 数据手册 SCPS207"),
    0x75: ("TCA9548A（A2A1A0=101）", "TI TCA9548A 数据手册 SCPS207"),
    0x76: ("BMP280/BME280 气压（SDO=GND） / MS5611（CSB=低）",
           "Bosch BMP280 数据手册：SDO 接 GND → 0x76、接 VDDIO → 0x77"),
    0x77: ("BMP280/BME280（SDO=VDDIO） / MS5611（CSB=高）",
           "Bosch BMP280 数据手册：SDO 接 VDDIO → 0x77"),
    0x3C: ("SSD1306 / SH1106 OLED（SA0=低）",
           "Solomon SSD1306 数据手册：SA0=0 → 0x3C、SA0=1 → 0x3D"),
    0x3D: ("SSD1306 / SH1106 OLED（SA0=高）", "Solomon SSD1306 数据手册"),
}

#: SPI flash 的 JEDEC 厂商码（0x9F 命令的第 1 字节）。
#: 依据：SFUD 的 flash 厂商表（sfud/inc/sfud_flash_def.h）+ 各厂商 RDID 说明。
#: ★ ISSI 存疑：SFUD 记 0xD5，另有资料记 0x9D，两说并存 —— 以芯片丝印为准，
#:   这里两个都不填，避免给出一个可能错的「权威答案」。
_JEDEC_VENDORS = {
    0x01: "Cypress / Spansion", 0x04: "Fujitsu", 0x1C: "EON",
    0x1F: "Atmel", 0x20: "Micron / Numonyx / ST", 0x37: "AMIC",
    0x52: "NOR-MEM", 0x62: "Sanyo", 0x85: "Puya",
    0x89: "Intel", 0x8C: "ESMT", 0xA1: "Fudan",
    0xAD: "Hyundai", 0xBF: "SST / Microchip", 0xC2: "Macronix",
    0xC8: "GigaDevice", 0xEF: "Winbond",
}

#: 1-Wire 家族码 → 器件。依据：Maxim 各器件数据手册里的家族码（ROM 首字节）。
_OW_FAMILY = {
    0x01: "DS2401 / DS1990A 序列号", 0x10: "DS18S20 温度",
    0x22: "DS1822 温度", 0x28: "DS18B20 温度",
    0x3B: "DS1825 温度", 0x42: "DS28EA00 温度（支持链式）",
}


def _bridge_port(port):
    """定端口：显式参数 > 环境变量。**绝不自动探测**（理由见本块头注释）。"""
    p = str(port or "").strip() or os.environ.get(_BRIDGE_ENV, "").strip()
    if p:
        return p, None
    return None, (
        "没有指定桥口 —— 总线工具需要一块跑着桥固件的 MCU，\n"
        "它负责把串口命令翻译成 I2C / SPI / 1-Wire / RS485 的实际时序。\n"
        "  怎么给端口 : 直接传 port=\"COM7\"，或先设环境变量 %s=COM7\n"
        "  桥固件在哪 : tools/bridge_fw/（ESP32-S3 / STM32 / Arduino 都能跑）\n"
        "  ⓘ 本工具**不会**自动扫描所有串口去找桥：实测过，往一个不认识的串口发探测\n"
        "    字节，可能被打进别人正在跑的固件里（本机 COM46 上的手套固件真的解析了\n"
        "    我发过去的 `help`）。所以端口必须由你指名，工具不去猜。" % _BRIDGE_ENV)


def _bridge_lines(text):
    """把桥回话切成**已完结**的有效行：丢空行、`#` 异步日志，以及未终止的半行。

    ★ 2026-10-13：旧版把 split("\\n") 的尾段（设备还在发的半行）也当有效行，
      _bridge_cmd 会拿它当完整应答——"+OK SPI SCAN bus=0 n=2" 的换行还在
      线上时，"slots=..." 字段仍在路上，提前返回就静默丢字段，而调用方
      看到的是「解析成功」。现在只认被 \\n 终结的行（桥是请求-响应协议，
      半行一律等齐）。
    """
    out = []
    parts = (text or "").replace("\r", "").split("\n")
    for ln in parts[:-1]:                       # 丢掉未终结的尾段
        t = ln.strip()
        if t and not t.startswith("#"):
            out.append(t)
    return out


def _bridge_cmd(ser, cmd, timeout=_BRIDGE_CMD_TIMEOUT):
    """发一条命令并等它的应答。返回 (kind, fields, raw)。

    kind: "ok" | "err" | "desync" | "none"（超时 / 串口异常）。
    只认 `+OK` / `-ERR` 开头的行；`#` 开头的异步日志直接丢弃。

    ★ 为什么把「失步」单独拎成一个 kind：早先把回显对不上也返回 "err"，
      于是「协议失步」会被下游渲染成「桥拒绝了这条命令」—— 把两种完全不同的
      故障说成同一种，用户会去查桥的命令解析，而真正的问题是回话错位。
    """
    try:
        ser.reset_input_buffer()
    except Exception:                                     # noqa: BLE001
        pass
    try:
        ser.write((cmd + "\r\n").encode("ascii", "replace"))
        ser.flush()
    except Exception as exc:                              # noqa: BLE001
        return "none", "", "写桥口失败：%s" % exc

    head = cmd.split()[0].upper() if cmd.split() else ""
    buf, t0 = "", time.time()
    while time.time() - t0 < timeout:
        try:
            chunk = ser.read(256)
        except Exception as exc:                          # noqa: BLE001
            return "none", "", "读桥口失败：%s" % exc
        if chunk:
            buf += chunk.decode("ascii", "replace")
        for ln in _bridge_lines(buf):
            u = ln.upper()
            if u.startswith("+OK"):
                rest = ln[3:].strip()
                # 回显校验：回话里必须出现刚发出去的那条命令的首词。
                # 没有它，一旦失步（半行 / 上次的残留应答），后面的解析会全错位，
                # 而且错得「看起来很正常」—— 这种错最难查。
                if head and head not in rest.upper():
                    return "desync", "", ("桥回话与命令对不上（协议失步）：发了 %r，收到 %r"
                                          % (cmd, ln))
                return "ok", rest, buf
            if u.startswith("-ERR"):
                return "err", ln[4:].strip(), buf
    return "none", "", buf


def _bridge_fields(fields):
    """把 `bus=0 n=2 addrs=0x70,0x71` 切成字典。无 = 的裸词放进 ``_flags``。"""
    d, flags = {}, []
    for tok in (fields or "").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            d[k.strip().upper()] = v.strip()
        else:
            flags.append(tok)
    d["_flags"] = flags
    return d


def _bridge_connect(port, baudrate=None, timeout=None):
    """开桥口 + 强制 PING 握手。返回 (ser, fields, err)；任一步失败 ser 为 None。"""
    if not HAS_PYSERIAL:
        return None, None, _deps_msg()
    # 隔离闸门：开串口会翻 DTR/RTS、会抢占该口（与 serial_open 完全同性质的风险）
    blocked = guard.check("serial_open", port)
    if blocked:
        return None, None, blocked
    try:
        ser = _open_serial(port=port, baudrate=int(baudrate or _BRIDGE_BAUD),
                           bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                           stopbits=serial.STOPBITS_ONE, timeout=0.2)
    except Exception as exc:                              # noqa: BLE001
        return None, None, ("打开桥口 %s 失败：%s\n"
                            "排查：1) 端口号对不对（serial_list 看一眼）；"
                            "2) 是否被别的软件占用。" % (port, exc))
    to = float(timeout or _BRIDGE_CMD_TIMEOUT)
    kind, fields, raw = _bridge_cmd(ser, "PING", timeout=to)
    ok = (kind == "ok" and ("BRIDGE=" + _BRIDGE_TAG).upper() in fields.upper())
    if not ok:
        try:
            ser.close()
        except Exception:                                 # noqa: BLE001
            pass
        if kind == "none":
            return None, None, (
                "%s 上没有桥在应答（PING 超时，%.1fs 内没收到任何回话）。\n"
                "排查：1) 这块板烧的是桥固件吗（tools/bridge_fw/README.md）；\n"
                "2) 波特率对不对（桥默认 %d）；3) 是不是把设备口当成桥口了。\n"
                "原始回话（可能为空）：%r" % (port, to, _BRIDGE_BAUD, raw[:200]))
        if kind == "err":
            return None, None, ("%s 拒绝了 PING：%s\n"
                                "这说明口上确实有东西在说 MCUDBG 协议，但它认为这条命令有问题。"
                                % (port, fields or raw[:200]))
        if kind == "desync":
            return None, None, (
                "%s 上有 MCUDBG 风格的应答，但**回话对不上命令**（协议失步）。\n"
                "原始回话：%r\n"
                "多半是：1) 两边版本不一致（固件与工具的命令表不同）；"
                "2) 波特率不符导致回话被打花；3) 有别的程序在同时读这个口。" % (port, raw[:200]))
        return None, None, (
            "%s 上有东西在应答，但**不是**本工具的桥（PING 回话里没有 BRIDGE=%s）。\n"
            "收到的回话：%r\n"
            "★ 这里刻意不做「猜」：回话对不上就停手。因为往别人正在跑的固件里灌命令\n"
            "  是有真实后果的（可能改配置、触发重启）—— 宁可报错，也不赌。" 
            % (port, _BRIDGE_TAG, (fields or raw)[:200]))
    return ser, _bridge_fields(fields), None


def _bridge_close(ser):
    if ser is None:
        return
    try:
        ser.close()
    except Exception:                                     # noqa: BLE001
        pass


def _bridge_reply(port, cmd, ok_fn, timeout=None):
    """一条命令的标准流程：连 → 发 → 断 → 把应答交给 ok_fn 渲染成文本。

    ok_fn(fields, caps) 只在 ``+OK`` 时被调用；它负责把字段渲染成人看的报告。
    ★ 三件事一律集中在这里，不许各工具自己写一遍：
      1) 连接失败 / 非桥口 → 原样返回那句可读说明（不吞、不改写）；
      2) `-ERR` → 翻译成 _BRIDGE_ERR_HINT 里的人话，并附原始码；
      3) 无论成败都关串口（不留占用的口）。
    """
    ser, caps, err = _bridge_connect(port, timeout=timeout)
    if err:
        return err
    try:
        kind, fields, raw = _bridge_cmd(ser, cmd, timeout=float(timeout or _BRIDGE_CMD_TIMEOUT))
    finally:
        _bridge_close(ser)
    if kind == "ok":
        return ok_fn(_bridge_fields(fields), caps)
    if kind == "err":
        code = (fields.split()[0].upper() if fields else "?")
        hint = _BRIDGE_ERR_HINT.get(code)
        out = ["桥拒绝了这条命令：-ERR %s" % (fields or "(空)")]
        if hint:
            out.append("  ⓘ %s" % hint)
        else:
            out.append("  ⓘ 桥没有登记这个错误码，原始说明见上。")
        out.append("  （命令：%s）" % cmd)
        return "\n".join(out)
    if kind == "desync":
        return ("桥回了 MCUDBG 风格的应答，但**回话与命令对不上**（协议失步）。\n"
                "  命令：%s\n  原始回话：%r\n"
                "  多半是：1) 固件与工具的命令表版本不一致；2) 波特率不符把回话打花了；"
                "3) 有别的程序在同时读这个口。" % (cmd, raw[:200]))
    return ("桥没有回应（超时）。命令：%s\n"
            "  已收到 %d 字节原始数据：%r\n"
            "  排查：1) 桥固件是否卡在某次总线传输里（拉死在 SDA 上）；"
            "2) 波特率是否一致。" % (cmd, len(raw), raw[:200]))


def _addr_ok(addr):
    """7 位地址合法性（0x08–0x77）。返回 (int, err)。"""
    try:
        a = int(str(addr), 0)
    except (TypeError, ValueError):
        return None, "地址 %r 不是合法整数（可用 0x68 / 104）" % (addr,)
    if not 0 <= a <= 0x7F:
        return None, ("地址 0x%X 超出 7 位范围（0x00–0x7F）。\n"
                      "  ⓘ 别填 8 位形式的读/写地址：0x68 的写地址是 0xD0、读地址是 0xD1，\n"
                      "    本工具要的是**7 位** 0x68。" % a)
    for (lo, hi), why in _I2C_RESERVED:
        if lo <= a <= hi:
            return None, ("地址 0x%02X 落在 I2C 保留段，普通从设备不会在这里应答。\n  %s\n"
                          "  ⓘ 要扫全部可用地址请用 i2c_scan()（默认扫 0x08–0x77）。" % (a, why))
    return a, None


def _coerce_bytes(value, name="data", maxn=_BRIDGE_MAX_BYTES):
    """把 int / 列表 / "0x01,02" / bytes 统一成 [int, ...]。返回 (list, err)。

    ★ 字符串一律按**十六进制**解释（总线语境下这是约定，且这样 "48656c6c6f"
      才能被理解成 5 个字节）。分词规则按「一个字节最多两位十六进制」推：
        · 带 0x 前缀的词 → 就是一个字节的值（0x1FF 直接报错，因为两字节塞不进）
        · 不带前缀、长度 ≤2 → 一个字节
        · 不带前缀、长度为偶数且全是十六进制 → **按两位一组拆成多个字节**
      这条规则是实测撞出来的：早先没有第三条，于是 rs485_send("48656c6c6f")
      会被当成一个数 0x48656C6C6F（超出字节范围）而报错；若换成宽松解析，
      更糟的是它会「成功地」往线上发出一串完全不是用户本意的字节。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        seq = [value]
    elif isinstance(value, (bytes, bytearray)):
        seq = list(value)
    elif isinstance(value, (list, tuple)):
        seq = list(value)
    elif isinstance(value, str):
        toks = value.strip().replace(",", " ").replace(";", " ").replace("-", " ").split()
        if not toks:
            return None, "%s 为空，没有要写的东西" % name
        seq = []
        for tok in toks:
            t = tok.strip()
            if t[:2].lower() == "0x":
                try:
                    seq.append(int(t, 16))
                except ValueError:
                    return None, "%s 里的 %r 不是合法的十六进制字节" % (name, tok)
                continue
            if not all(c in "0123456789abcdefABCDEF" for c in t):
                return None, ("%s 里的 %r 不是十六进制。\n"
                              "  ⓘ 字符串形式按**十六进制**解释："
                              "要发 5 个字节写 \"48656c6c6f\" 或 \"0x48,0x65,...\"；"
                              "要写十进制数组用 [72,101,108,108,111]。" % (name, tok))
            if len(t) <= 2:
                seq.append(int(t, 16))
                continue
            if len(t) % 2:
                return None, ("%s 里的 %r 长度是奇数，拆不成整字节"
                              "（一个字节最多两位十六进制）" % (name, tok))
            seq.extend(int(t[i:i + 2], 16) for i in range(0, len(t), 2))
    elif value is None:
        return None, "%s 为空，没有要写的东西" % name
    else:
        return None, "%s 类型不支持：%r（给 int / 列表 / \"0x01,02\" / bytes）" % (name, value)
    if not seq:
        return None, "%s 为空，没有要写的东西" % name
    if len(seq) > maxn:
        return None, "%s 有 %d 个字节，超过单次上限 %d（拆成多次调用）" % (name, len(seq), maxn)
    bad = [v for v in seq if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 0xFF]
    if bad:
        return None, "%s 里有非字节值：%r（每个字节必须 0–255）" % (name, bad[:4])
    return seq, None


def _hex_list(vals):
    return ",".join("0x%02x" % v for v in vals)


def _bytes_from_field(fields, key="DATA"):
    """把回话里 data=0x68,0x40 解析成 [int,...]。返回 (list, err)。"""
    raw = (fields.get(key) or "").strip()
    if not raw:
        return None, "桥的 OK 回话里没有 %s 字段（协议不符）" % key.lower()
    try:
        vals = [int(t, 16) for t in raw.replace(",", " ").split()]
    except ValueError:
        return None, "桥回的 %s 字段解析失败：%r" % (key.lower(), raw)
    return vals, None


def _bus_header(title, bus, caps):
    """各总线工具共用的抬头：一行说清「走的哪块桥、哪条总线」。"""
    dev = (caps or {}).get("DEV", "") or (caps or {}).get("BOARD", "")
    return ["%s（bus=%s%s）" % (title, bus, "　桥：" + dev if dev else "")]


def i2c_scan(bus=0, port=None, baudrate=None, low=None, high=None, timeout=None):
    """扫描 I2C 总线上所有应答的 7 位地址，并给器件提示。

    这是「板子上到底挂了什么」的第一手答案，也是接线问题的第一道判据。
    low/high 可缩小范围（默认 0x08–0x77，即 I2C 规范里可自由分配的段）。

    ★ 输出里「扫到 X 个」和「一个都没扫到」都是**有效结论**，不是工具失败：
      一个都没扫到通常意味着 1) 器件没上电；2) SDA/SCL 接反或没接；
      3) 缺上拉电阻；4) 你在扫一条根本不通的总线。
    """
    p, err = _bridge_port(port)
    if err:
        return err
    try:
        b = int(bus)
    except (TypeError, ValueError):
        return "bus %r 不是整数（桥上的总线编号，从 0 开始）" % (bus,)
    try:
        lo = _I2C_SCAN_LO if low is None else int(str(low), 0)
        hi = _I2C_SCAN_HI if high is None else int(str(high), 0)
    except (TypeError, ValueError):
        return "low/high %r/%r 不是整数" % (low, high)
    if not (0 <= lo <= hi <= 0x7F):
        return "扫描范围不合法：low=0x%X high=0x%X（要 0 ≤ low ≤ high ≤ 0x7F）" % (lo, hi)
    # 整段都落在保留段里 = 几乎肯定是填错了，直接拒绝并解释。
    # ★ 不因为「扫一下也没什么害」就放过：扫保留段拿到的「应答」多为异常现象
    #   （广播地址被广播出去、10 位寻址前缀被当成器件……），照单收下会误导判断。
    for (rlo, rhi), why in _I2C_RESERVED:
        if rlo <= lo and hi <= rhi:
            return ("扫描范围 0x%02X–0x%02X 整段落在 I2C 保留段里，普通从设备不会在这里应答。\n"
                    "  %s\n"
                    "  ⓘ 要扫全部可用地址，直接 i2c_scan(bus=%d)（默认 0x08–0x77）。"
                    % (lo, hi, why, b))
    # 扫描本身就是「向总线上发地址」——它有真实电学行为，走闸门
    blocked = guard.check("bus_probe", p)
    if blocked:
        return blocked

    cmd = "I2C SCAN %d %d %d" % (b, lo, hi)

    def _render(fields, caps):
        out = _bus_header("I2C 扫描", b, caps)
        out.append("  扫描范围   : 0x%02X–0x%02X（共 %d 个地址）" % (lo, hi, hi - lo + 1))
        for (rlo, rhi), why in _I2C_RESERVED:
            if not (hi < rlo or lo > rhi):
                out.append("  ⓘ 本次范围含 I2C 保留段地址（0x%02X–0x%02X）：%s"
                           % (rlo, rhi, why))
                out.append("    那些地址上的「应答」多为异常现象（广播被真的发出去、10 位寻址")
                out.append("    前缀被当成器件…），别照单当成一颗正常器件。")
        raw = (fields.get("ADDRS") or "").strip()
        if fields.get("N") is None:
            out.append("  ⚠ 桥的回话里没有 n= 字段，协议不符，结果不可信：%r" % fields)
            return "\n".join(out)
        try:
            addrs = [int(t, 0) for t in raw.replace(",", " ").split()] if raw else []
        except ValueError:
            out.append("  ⚠ addrs 字段解析失败：%r" % raw)
            return "\n".join(out)
        out.append("  应答器件   : %d 个" % len(addrs))
        if not addrs:
            out.append("")
            out.append("  一个都没有。按这个次序查（从最常见开始）：")
            out.append("    1) 器件上电了吗（量 VCC；很多模块要 3.3V 而不是 5V）")
            out.append("    2) SDA/SCL 有没有接反、有没有接上（这是最常见的）")
            out.append("    3) 上拉电阻在不在（4.7k 到 VCC；缺了会一个都扫不到）")
            out.append("    4) 是不是走了 I2C 多路复用器（如 TCA9548A）：")
            out.append("       多路复用器后面的器件**默认是断开的**，必须先给它写通道选择字，")
            out.append("       所以直接扫主总线只能看到复用器自己（0x70–0x77），看不到后面那 8 路。")
            out.append("    5) 共地了吗")
            return "\n".join(out)
        out.append("")
        out.append("  %s %s  %s" % (_pad("地址(7位)", 10), _pad("读/写地址", 12), "可能的器件（提示，非判定）"))
        out.append("  " + "-" * 68)
        for a in addrs:
            hit = _I2C_KNOWN.get(a)
            name = hit[0] if hit else "（未收录在这张最小表里）"
            out.append("  %s %s  %s"
                       % (_pad("0x%02X" % a, 10), _pad("0x%02X/0x%02X" % (a << 1, (a << 1) | 1), 12),
                          name))
        out.append("")
        out.append("  ⓘ 地址只是「有人应答」。同一地址常被多颗器件共用（0x68 上可能是 MPU-6050")
        out.append("    也可能是 DS1307），**光看地址定不了种** —— 要读它的 WHO_AM_I / ID 寄存器：")
        out.append("    i2c_read_reg(addr=0x68, reg=0x75)  # MPU-6050 的 WHO_AM_I，正常应回 0x68")
        src = [(_I2C_KNOWN[a][1]) for a in addrs if a in _I2C_KNOWN]
        if src:
            out.append("")
            out.append("  依据（逐条来自器件数据手册）：")
            for s in src:
                out.append("    · %s" % s)
        else:
            out.append("")
            out.append("  ⓘ 这次扫到的地址都不在收录表里，所以给不出器件提示 —— 这是数据缺口，")
            out.append("    不是结论。（器件库 G01 会把这张表扩成可扩展的知识层文件。）")
        return "\n".join(out)

    return _bridge_reply(p, cmd, _render, timeout=timeout)


def i2c_read_reg(addr, reg, count=1, bus=0, port=None, baudrate=None, timeout=None):
    """读 I2C 从设备的寄存器（最常见的「这颗 IMU 是不是活的」动作）。"""
    p, err = _bridge_port(port)
    if err:
        return err
    a, err = _addr_ok(addr)
    if err:
        return err
    try:
        r = int(str(reg), 0)
        n = int(count)
        b = int(bus)
    except (TypeError, ValueError):
        return "reg/count/bus 必须是整数，收到 %r/%r/%r" % (reg, count, bus)
    if not 0 <= r <= 0xFF:
        return "寄存器地址 0x%X 超出 8 位（0x00–0xFF）" % r
    if not 1 <= n <= _BRIDGE_MAX_BYTES:
        return "count 要在 1–%d 之间，收到 %r" % (_BRIDGE_MAX_BYTES, count)
    blocked = guard.check("bus_probe", p)
    if blocked:
        return blocked

    cmd = "I2C READ %d 0x%02X 0x%02X %d" % (b, a, r, n)

    def _render(fields, caps):
        vals, verr = _bytes_from_field(fields)
        out = _bus_header("I2C 读寄存器", b, caps)
        out.append("  %s: 0x%02X（读写地址 0x%02X/0x%02X）" % (_pad("从设备", 11), a, a << 1, (a << 1) | 1))
        out.append("  %s: 0x%02X" % (_pad("寄存器", 11), r))
        if verr:
            out.append("  ⚠ %s" % verr)
            return "\n".join(out)
        out.append("  %s: %d 字节" % (_pad("读到", 11), len(vals)))
        out.append("  %s: %s" % (_pad("原始值", 11), _hex_list(vals)))
        if len(vals) == 1:
            out.append("  %s: %d（0b%s）" % (_pad("十进制", 11), vals[0], format(vals[0], "08b")))
        else:
            out.append("  %s: %s" % (_pad("十进制", 11), " ".join(str(v) for v in vals)))
        if set(vals) == {0x00}:
            out.append("")
            out.append("  ⓘ 读回全 0。**先别急着说器件坏**：全 0 有几种完全不同的原因 ——")
            out.append("    器件确实回了 ACK 但寄存器是 0；上电复位后还没初始化；")
            out.append("    或者读时序里从设备没能把线拉起来（弱上拉 + 高容性负载）。")
            out.append("    判别手段：读一个「肯定非零」的寄存器（如器件的 WHO_AM_I / ID）。")
        elif set(vals) == {0xFF}:
            out.append("")
            out.append("  ⓘ 读回全 0xFF —— 这**通常是器件根本没接/没上电的信号**（总线被上拉")
            out.append("    电阻拉高，从设备没去驱动它），而不是「寄存器的值是 0xFF」。")
        return "\n".join(out)

    return _bridge_reply(p, cmd, _render, timeout=timeout)


def i2c_write_reg(addr, reg, data, bus=0, port=None, baudrate=None, verify=False,
                  timeout=None):
    """写 I2C 从设备的寄存器。

    verify=True 会紧接着回读一次比对。★ 默认关：有些寄存器是**只写**的
    （命令寄存器、软复位寄存器），回读必然拿到别的东西 —— 那种「不一致」不是缺陷。
    默认开着会造成大量假 FAIL，所以由调用方明确要求才做。
    """
    p, err = _bridge_port(port)
    if err:
        return err
    a, err = _addr_ok(addr)
    if err:
        return err
    try:
        r = int(str(reg), 0)
        b = int(bus)
    except (TypeError, ValueError):
        return "reg/bus 必须是整数，收到 %r/%r" % (reg, bus)
    if not 0 <= r <= 0xFF:
        return "寄存器地址 0x%X 超出 8 位（0x00–0xFF）" % r
    vals, err = _coerce_bytes(data)
    if err:
        return err
    blocked = guard.check("bus_write", p)
    if blocked:
        return blocked

    cmd = "I2C WRITE %d 0x%02X 0x%02X %s" % (b, a, r, _hex_list(vals))

    def _render(fields, caps):
        out = _bus_header("I2C 写寄存器", b, caps)
        out.append("  %s: 0x%02X（读写地址 0x%02X/0x%02X）" % (_pad("从设备", 11), a, a << 1, (a << 1) | 1))
        out.append("  %s: 0x%02X  ←  %s" % (_pad("寄存器", 11), r, _hex_list(vals)))
        out.append("  %s: 桥已确认发出（n=%s）" % (_pad("结果", 11), fields.get("N", "?")))
        return "\n".join(out)

    res = _bridge_reply(p, cmd, _render, timeout=timeout)
    if not verify or "桥已确认发出" not in res:
        return res
    back = i2c_read_reg(a, r, count=len(vals), bus=b, port=p, timeout=timeout)
    if "原始值" not in back:
        return res + "\n\n  —— 回读校验（verify=True）——\n    " + back.replace("\n", "\n    ")
    try:
        got = [int(t, 16) for t in back.split("原始值")[1].split(":", 1)[1]
               .split("\n")[0].strip().replace(",", " ").split()]
    except Exception:                                     # noqa: BLE001
        return res + "\n\n  —— 回读校验（verify=True）——\n    读到：" + back.splitlines()[0]
    tail = ("\n\n  —— 回读校验（verify=True）——\n"
            "    写入: %s\n    读回: %s\n    %s"
            % (_hex_list(vals), _hex_list(got),
               "✓ 一致" if got == vals else
               "✗ 不一致 —— 先确认这个寄存器是不是**只写**的（命令/软复位寄存器读回来不一样"
               "属正常）；否则多半是器件把它映射到别处，或写入时序有问题。"))
    return res + tail


def spi_scan(bus=0, port=None, baudrate=None, timeout=None):
    """逐条 CS 读 JEDEC ID（0x9F），找出板上挂的 SPI flash。

    ★ SPI **没有地址概念**，所以「扫描」在这里的含义只能是：对固件里配置好的每条 CS
      依次发 0x9F。因此本工具只对**遵守 0x9F 的 SPI NOR/NAND flash** 有效；
      对 SPI 传感器 / ADC / 显示驱动（它们各有自己的命令字），扫不出来是**正常**的，
      不是故障 —— 那种器件请用 spi_read_reg 给具体命令字。
    """
    p, err = _bridge_port(port)
    if err:
        return err
    try:
        b = int(bus)
    except (TypeError, ValueError):
        return "bus %r 不是整数" % (bus,)
    blocked = guard.check("bus_probe", p)
    if blocked:
        return blocked

    cmd = "SPI SCAN %d" % b

    def _render(fields, caps):
        out = _bus_header("SPI 扫描（逐 CS 读 JEDEC ID 0x9F）", b, caps)
        out.append("  ⓘ SPI **没有地址概念**，所以「扫描」只能是逐条 CS 发 0x9F：只有遵守")
        out.append("    0x9F 的 SPI NOR/NAND flash 会被列出来。SPI 传感器 / ADC / 显示驱动")
        out.append("    各有自己的命令字 —— 扫不到它们属于正常，不是故障（用 spi_read_reg 指命令）。")
        slots = (fields.get("SLOTS") or "").strip()
        if not slots:
            out.append("  ⚠ 桥的回话里没有 slots= 字段（协议不符）：%r" % fields)
            return "\n".join(out)
        out.append("")
        found = 0
        for item in slots.replace(",", " ").split():
            if ":" not in item:
                out.append("  ⚠ 槽位格式异常：%r" % item)
                continue
            cs, jid = item.split(":", 1)
            try:
                # ★ 掩到 24 位：JEDEC ID 就是 3 字节。真桥可能回 6 位十六进制
                #   （0xef4018），也可能回补零的 8 位（0x00ef4018）或全 1 的空槽
                #   （0xffffffff）。不掩的话，同一个「空槽」会因为补位宽度不同，
                #   一会儿被判成空、一会儿被判成一颗不存在的器件。
                v = int(jid, 16) & 0xFFFFFF
            except ValueError:
                out.append("  %s: JEDEC ID 解析失败 %r" % (_pad("CS" + cs, 8), jid))
                continue
            if v in (0x000000, 0xFFFFFF):
                out.append("  %-8s 0x%06X  —— 无器件（%s）"
                           % ("CS" + cs, v,
                              "读回全 0" if v == 0 else "全 1：MISO 被上拉/悬空，没人在驱动它"))
                continue
            found += 1
            mf, typ, cap = (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF
            vendor = _JEDEC_VENDORS.get(mf, "未知厂商码")
            size = (64 * 1024 * 1024) if cap == 0x20 else (1 << cap if 0 < cap < 32 else None)
            szs = ("%d MB" % (size // (1024 * 1024))) if size else "容量码 0x%02X（非常规，按手册核）" % cap
            out.append("  %-8s 0x%06X  厂商 %s（0x%02X） 类型 0x%02X  %s"
                       % ("CS" + cs, v, vendor, mf, typ, szs))
        out.append("")
        if found:
            out.append("  结论：有 %d 条 CS 上挂着 JEDEC flash。" % found)
            out.append("  ⓘ 容量是按 JEDEC 容量码算的（256B 页 NOR 的通用做法）。个别型号")
            out.append("    容量码不走这个规则，要精确值请读该型号手册的 RDID 表。")
        else:
            out.append("  结论：没有任何一条 CS 上读出有效 JEDEC ID。")
            out.append("  这**不代表 SPI 坏了** —— 三种常见情形：")
            out.append("    1) 板上挂的是 SPI 传感器/ADC（例如 ICM-426xx、ADS 系列），")
            out.append("       它们不认 0x9F，要用自己的命令字（spi_read_reg 指命令）；")
            out.append("    2) CS 脚号配错了（桥固件里的槽位表要对着你的接线改）；")
            out.append("    3) MISO 悬空（读回全 1）或没接。")
        return "\n".join(out)

    return _bridge_reply(p, cmd, _render, timeout=timeout)


def spi_read_reg(cmd_byte, count=1, cs=0, bus=0, port=None, baudrate=None, timeout=None):
    """发一个 SPI 命令字并读回 N 字节（对读了命令字的从设备都通用）。

    例：读 JEDEC ID → spi_read_reg(cmd_byte=0x9F, count=3)；
        读 ICM-42688 的 WHO_AM_I → 先由桥侧决定是否带地址字节（本工具不做猜测）。
    """
    p, err = _bridge_port(port)
    if err:
        return err
    try:
        c = int(str(cmd_byte), 0)
        n = int(count)
        cs_i = int(cs)
        b = int(bus)
    except (TypeError, ValueError):
        return "cmd_byte/count/cs/bus 必须是整数"
    if not 0 <= c <= 0xFF:
        return "命令字 0x%X 超出一个字节（0x00–0xFF）" % c
    if not 1 <= n <= _BRIDGE_MAX_BYTES:
        return "count 要在 1–%d 之间" % _BRIDGE_MAX_BYTES
    blocked = guard.check("bus_probe", p)
    if blocked:
        return blocked

    cmd = "SPI READ %d %d 0x%02X %d" % (b, cs_i, c, n)

    def _render(fields, caps):
        vals, verr = _bytes_from_field(fields)
        out = _bus_header("SPI 读", b, caps)
        out.append("  CS         : %d     命令字: 0x%02X" % (cs_i, c))
        if verr:
            out.append("  ⚠ %s" % verr)
            return "\n".join(out)
        out.append("  %s: %s" % (_pad("读到", 11), _hex_list(vals)))
        out.append("  %s: %s" % (_pad("十进制", 11), " ".join(str(v) for v in vals)))
        if set(vals) == {0x00} or set(vals) == {0xFF}:
            out.append("")
            out.append("  ⓘ 全 0 / 全 1 通常意味着「这条件命令在从设备上不成立」或「MISO 没人驱动」，")
            out.append("    而不是「寄存器的值就是 0 / 0xFF」。先核对命令字，再查 MISO 接线。")
        return "\n".join(out)

    return _bridge_reply(p, cmd, _render, timeout=timeout)


def onewire_scan(bus=0, port=None, baudrate=None, timeout=None):
    """1-Wire 搜索 ROM（Search ROM），列出总线上每颗器件的 64 位 ROM 码。"""
    p, err = _bridge_port(port)
    if err:
        return err
    try:
        b = int(bus)
    except (TypeError, ValueError):
        return "bus %r 不是整数" % (bus,)
    blocked = guard.check("bus_probe", p)
    if blocked:
        return blocked

    cmd = "OW SCAN %d" % b

    def _render(fields, caps):
        out = _bus_header("1-Wire 搜索 ROM", b, caps)
        raw = (fields.get("ROMS") or "").strip()
        if not raw:
            out.append("  没有找到任何 1-Wire 器件。")
            out.append("  ⓘ 常见原因：1) 数据线缺 4.7k 上拉（1-Wire 靠上拉给寄生电容充电，")
            out.append("    缺了必然搜不到）；2) 器件在寄生供电模式但总线没有强上拉；")
            out.append("    3) 线太长/容性太大（降速或加短接）；4) 桥固件里 1-Wire 用的引脚不对。")
            return "\n".join(out)
        roms = raw.replace(",", " ").split()
        out.append("  找到 %d 颗：" % len(roms))
        for rom in roms:
            try:
                fam = int(rom[:2], 16)
            except (ValueError, IndexError):
                fam = None
            name = _OW_FAMILY.get(fam, "未收录的家族码")
            out.append("    %s   家族码 0x%02X → %s"
                       % (rom.upper(), fam if fam is not None else 0, name
                          if fam is not None else "（ROM 码解析失败）"))
        out.append("")
        out.append("  ⓘ ROM 首字节是家族码，后 6 字节是唯一序列号，末字节是 CRC8。")
        out.append("    依据：Maxim 各器件数据手册的「家族码」章节。")
        fams = []
        for rom in roms:
            try:
                fams.append(int(rom[:2], 16))
            except (ValueError, TypeError):
                pass
        if fams and all(f == 0x28 for f in fams):
            out.append("  ⓘ 全是 0x28（DS18B20）：读温度的典型顺序是 ——")
            out.append("    跳过 ROM(0xCC) → 启动转换(0x44) → 等 750ms（12 位分辨率）→")
            out.append("    总线复位 → 跳过 ROM(0xCC) → 读暂存器(0xBE) → 取前 2 字节小端。")
        return "\n".join(out)

    return _bridge_reply(p, cmd, _render, timeout=timeout)


def rs485_send(data, bus=0, de_ms=2, port=None, baudrate=None, newline=False, timeout=None):
    """经桥的 RS485 收发器发送字节（桥负责 DE 方向脚的时序）。

    ★ 能力边界（别把它当成「链路已通」的证据）：它只能确认**桥把字节推出去了**，
      对端有没有收到、波形对不对，它证明不了。要证明得让对端回话（再用串口读），
      或用示波器看 A/B 差分波形。这里如实写在输出里，不代用户下结论。
    """
    p, err = _bridge_port(port)
    if err:
        return err
    vals, err = _coerce_bytes(data, name="data")
    if err:
        return err
    try:
        b = int(bus)
        de = float(de_ms)
    except (TypeError, ValueError):
        return "bus/de_ms 必须是数字，收到 %r/%r" % (bus, de_ms)
    if not 0 <= de <= 1000:
        return "de_ms 要在 0–1000 之间（方向脚在发完后保持多久才放开），收到 %r" % (de_ms,)
    if newline:
        vals = vals + [0x0D, 0x0A]
    blocked = guard.check("bus_write", p)
    if blocked:
        return blocked

    cmd = "RS485 SEND %d %.1f %s" % (b, de, "".join("%02x" % v for v in vals))

    def _render(fields, caps):
        out = _bus_header("RS485 发送", b, caps)
        out.append("  %s: %d 字节  %s" % (_pad("发出", 11), len(vals), _hex_list(vals)))
        out.append("  %s: %s" % (_pad("可打印形式", 11),
                                repr(bytes(vals)[:64].decode("ascii", "replace"))))
        out.append("  %s: DE 拉高后延迟 %.1f ms 才放开（给收发器切换留的时间）"
                   % (_pad("方向控制", 11), de))
        out.append("  %s: 桥已确认发出（n=%s）" % (_pad("结果", 11), fields.get("N", "?")))
        out.append("")
        out.append("  ⚠ 只证明「发得出去」，**不证明对端收到了**。要验证整条链路：")
        out.append("    1) 让对端回话，再用串口工具读回来；")
        out.append("    2) 或示波器看 A/B 差分波形（有波形≠数据对，还要看位宽）。")
        return "\n".join(out)

    return _bridge_reply(p, cmd, _render, timeout=timeout)


def bridge_probe(port=None, baudrate=None, timeout=None):
    """问桥「你是谁、支持哪些总线」——不碰任何总线，纯握手。

    这是排查「总线工具不工作」的第一步：先确认桥活着、认协议，
    再去怀疑接线。省得在接线和固件之间来回猜。
    """
    p, err = _bridge_port(port)
    if err:
        return err

    def _render(fields, caps):
        out = _bus_header("桥自检", "-", caps)
        out.append("  端口       : %s" % p)
        proto = (caps or {}).get("PROTO", "?")
        out.append("  协议       : %s %s"
                   % (proto, "✓ 与工具一致" if proto == _BRIDGE_PROTO
                      else "✗ 工具认的是 %s —— 固件与工具版本不匹配" % _BRIDGE_PROTO))
        caps_v = (caps or {}).get("CAPS", "")
        if caps_v:
            out.append("  桥支持     : %s" % caps_v)
            miss = [k for k in ("I2C", "SPI", "1W", "RS485") if k not in caps_v.upper()]
            if miss:
                out.append("  ⓘ 未声明支持：%s —— 用到这些的工具会收到 -ERR，不是接线问题。"
                           % "、".join(miss))
        fw = (caps or {}).get("FW", "")
        if fw:
            out.append("  固件       : %s" % fw)
        out.append("")
        out.append("  桥活着、认协议。下一步再怀疑接线和器件。")
        return "\n".join(out)

    return _bridge_reply(p, "PING", _render, timeout=timeout)


# ---------------------------------------------------------------
# 寄存器位域解码（零依赖；SVD 支持解析 CMSIS-SVD XML）
# ---------------------------------------------------------------

def decode_register(value, fields, width=32):
    """把寄存器原始值按位域拆开解释。

    fields: [{"name":"HSION","bit":0}, {"name":"PLLSRC","bits":[16,17]}]（bits 为 [lsb,msb] 或 [pos,width] 均支持：见下）
    简化约定：{"name":..., "bit":n} 单位域；{"name":..., "lsb":n, "width":w} 位段域。
    """
    try:
        v = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return "value 必须是整数（支持 0x 前缀），收到：%r" % (value,)
    # ★ width 必须先自查：`1 << width` 在 width<0 时会抛 ValueError
    #   ("negative shift count")，直接冒出 MCP 层——客户端收到一句 Python
    #   内部错误，而不是任何排查提示（真实数据回放已复现 width=-1）。
    #   寄存器位宽只可能是 1~64 的整数。
    try:
        width = int(width)
    except (TypeError, ValueError):
        return "width 必须是整数（8/16/32/64），收到：%r" % (width,)
    if width < 1 or width > 64:
        return ("width=%s 不合法：寄存器位宽只能是 1~64 的整数（常见 8/16/32/64）。"
                "别用 0 或负数——那会让移位计算直接出错。" % width)
    if v < 0 or v >= (1 << width):
        return "value 超出 %d 位范围：%s" % (width, hex(v))
    if not fields:
        return "没有给 fields（位域定义）。示例：[{'name':'HSION','bit':0}]"
    if not isinstance(fields, list):
        return ("fields 需要是列表，如 [{'name':'HSION','bit':0}]，收到：%r" % (type(fields).__name__,))
    lines = ["寄存器值 %s（二进制 %s）：" % (hex(v), format(v, "b").zfill(width))]
    skipped = []
    for f in fields:
        if not isinstance(f, dict):
            skipped.append(repr(f))
            continue
        name = f.get("name", "?")
        # 2026-10-13：字段级解析旧版裸 int() + 无值域校验——{"bit":"x"} 抛
        # ValueError、负 width 抛 "negative shift count"，全都直接冒出工具层。
        # 现在：解析失败或越界统一走「跳过 + 汇总行」，与既有 skipped 机制合流。
        try:
            if "bit" in f:
                lsb, w = int(f["bit"]), 1
            elif "lsb" in f and "width" in f:
                lsb, w = int(f["lsb"]), int(f["width"])
            elif "bits" in f and len(f["bits"]) == 2:
                a, b = sorted(int(x) for x in f["bits"])
                # 二义性：[lsb,msb] 与 [pos,width] 都常见——msb>=lsb 且差值小当区间，否则当 pos,width
                if b - a <= 16:
                    lsb, w = a, b - a + 1
                else:
                    lsb, w = a, b
            else:
                lines.append("  %s：字段定义缺 bit 或 lsb+width，跳过" % name)
                skipped.append(name)
                continue
        except (TypeError, ValueError):
            lines.append("  %s：字段定义里的数字解析不了（%r），跳过" % (name, f))
            skipped.append(name)
            continue
        if lsb < 0 or w <= 0 or lsb + w > 64:
            lines.append("  %s：位域起点 %d 宽度 %d 超出 0~63 合法范围，跳过"
                         % (name, lsb, w))
            skipped.append(name)
            continue
        mask = (1 << w) - 1
        val = (v >> lsb) & mask
        rng = "[%d]" % lsb if w == 1 else "[%d:%d]" % (lsb + w - 1, lsb)
        lines.append("  %-12s %-9s = %s（0b%s）" % (name, rng, val, format(val, "b").zfill(w)))
    if skipped:
        # ★不能只在上面 append 一行"跳过"就算完 —— 全部字段都跳过时，调用方
        #   拿到的是一张只有抬头、没有任何字段的表，看起来却"解码成功了"。
        #   这是"看似合理的假数字"：AI 会照着空表往下写代码。
        #   实测形态：传 {"name":"A","bits":"0-7"}（字符串而非二元组）⟹ 三字段
        #   全跳过 ⟹ 输出"寄存器值 0x12345678："后面什么都没有，且无任何报错。
        lines.append("")
        lines.append("!! %d/%d 个字段未能解析，已跳过：%s"
                     % (len(skipped), len(fields), ", ".join(str(s) for s in skipped)))
        lines.append("   支持的写法只有这三种：")
        lines.append("     {\"name\":\"A\", \"bit\":0}                    单比特")
        lines.append("     {\"name\":\"B\", \"lsb\":16, \"width\":2}        位段（推荐）")
        lines.append("     {\"name\":\"C\", \"bits\":[16,17]}              位段（区间写法）")
        lines.append("   ★注意 bits 必须是【两个整数】的列表，不能写成 \"0-7\" 这样的字符串。")
        if len(skipped) == len(fields):
            lines.append("   ★本次全部字段都没解析出来 ⟹ 上面这张表【没有任何有效信息】，"
                         "不要拿它当解码结果。")
    return "\n".join(lines)


def decode_register_svd(svd_path, peripheral, register, value):
    """用 CMSIS-SVD 文件解码寄存器位域（ST/乐鑫等厂商均提供 .svd）。

    示例：decode_register_svd("STM32F103.svd", "RCC", "CR", 0x00040002)
    """
    if not os.path.exists(svd_path):
        return "SVD 文件不存在：%s\n（SVD 在各厂商固件包/CubeMX 安装目录，或从 keystone 仓库下载）" % svd_path
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(svd_path)
    except ET.ParseError as e:
        return "SVD 解析失败：%s" % e
    root = tree.getroot()

    def _txt(el, tag):
        x = el.find(tag)
        return x.text.strip() if x is not None and x.text else None

    target_per, target_reg = None, None
    for per in root.iter("peripheral"):
        if (_txt(per, "name") or "").lower() != peripheral.lower():
            continue
        target_per = per
        for reg in per.iter("register"):
            if (_txt(reg, "name") or "").split("[")[0].lower() == register.lower():
                target_reg = reg
                break
        break
    if target_per is None:
        return "SVD 里没找到外设 %s。检查名称（如 RCC/USART1/TIM2）。" % peripheral
    if target_reg is None:
        return "外设 %s 里没找到寄存器 %s。" % (peripheral, register)

    width = int(_txt(target_reg, "size") or 32)
    try:
        v = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return "value 必须是整数，收到：%r" % (value,)
    lines = ["%s->%s（%d 位）= %s" % (peripheral.upper(), register.upper(), width, hex(v))]
    # ★ 值域校验：超出该寄存器位宽的值【必须点出来】。旧实现会按位移出一个
    #   看着合理、实则错位的位域解读（真实数据回放已复现：32 位寄存器喂
    #   0x100000000 被静默照常解码）。负数按位宽无符号截断（-1 → 0xFFFFFFFF）。
    if v < 0 or v >= (1 << width):
        lines.append("⚠ 值 %s 超出该寄存器位宽（%d 位，有效范围 0x0 ~ 0x%X）——"
                     "多半是抄错值、或这不是这个寄存器的值。下面按 %d 位无符号截断解读，仅供参考。"
                     % (hex(v), width, (1 << width) - 1, width))
        v &= (1 << width) - 1
    fields = target_reg.find("fields")
    if fields is None:
        return "\n".join(lines) + "\n（该寄存器在 SVD 中未定义位域）"
    for f in fields.iter("field"):
        fname = _txt(f, "name") or "?"
        lsb, w = None, None
        br = _txt(f, "bitRange")            # 形如 "[16:17]"
        if br and br.startswith("[") and ":" in br:
            inner = br.strip("[]")
            msb, lsb_ = inner.split(":")
            lsb, w = int(lsb_), int(msb) - int(lsb_) + 1
        else:
            lo = _txt(f, "lsb") or _txt(f, "bitOffset")
            wo = _txt(f, "bitWidth")
            if lo is not None:
                lsb = int(lo)
                w = int(wo) if wo else 1
        if lsb is None:
            continue
        if not w or w <= 0:
            # ★ 位宽 <=0 说明 SVD 的 bitRange 写反了（msb<lsb）。旧实现会算出
            #   mask=0、恒输出 0，看着像个正常位域——正是"看似合理的假答案"。
            lines.append("  %-16s %-9s = ?    SVD 位域定义非法（bitRange=%s，msb<lsb），"
                         "跳过不解读" % (fname, br or "?", br or ""))
            continue
        val = (v >> lsb) & ((1 << w) - 1)
        desc = _txt(f, "description") or ""
        # 枚举值提示
        enums = []
        ec = f.find("enumeratedValues")
        if ec is not None:
            for e in ec.iter("enumeratedValue"):
                en = _txt(e, "name")
                ev = _txt(e, "value")
                if en and ev is not None:
                    try:
                        if int(ev, 0) == val:
                            enums.append(en)
                    except ValueError:
                        pass
        rng = "[%d]" % lsb if w == 1 else "[%d:%d]" % (lsb + w - 1, lsb)
        hint = (" → " + "/".join(enums)) if enums else ("  " + desc[:60] if desc else "")
        lines.append("  %-16s %-9s = %s%s" % (fname, rng, val, hint))
    return "\n".join(lines)


# ---------------------------------------------------------------
# 引脚冲突检测（pinmap 知识库：knowledge/pinmap/*.json）
# ---------------------------------------------------------------

_PINMAP_CACHE = {}
_BOARDS_CACHE = {}


def _pinmap_files():
    """层叠列举引脚库 → {表名: 路径}（外置层可覆盖内置同名表）。"""
    return {name[:-5]: path
            for name, path in paths.layer_files("knowledge", "pinmap")}


def _board_files():
    """层叠列举板级库 → {表名: 路径}。"""
    return {name[:-5]: path
            for name, path in paths.layer_files("knowledge", "boards")}


def pinmap_list():
    """列出知识库里已收录的 MCU（内置 + 外置层合并）。"""
    return sorted(_pinmap_files())


# ---------------------------------------------------------------
# 生态 / 框架识别（N-09）
#
# 用途：拿到一个不认识的工作目录或一段报错时，先判断「这是哪个生态的工程」——
# Zephyr / RT-Thread 用 Kconfig+SCons，Keil/IAR 用各自的工程文件与错误码前缀，
# Rust 的 defmt 输出根本不是文本。识别错了，后面每一步排查都会拐到沟里。
# ---------------------------------------------------------------

_ECOSYSTEM_CACHE = {}


def _ecosystem_entries():
    """读生态库 → {key: entry}。缺失/坏掉返回空 dict（上层给可读提示）。"""
    if "entries" in _ECOSYSTEM_CACHE:
        return _ECOSYSTEM_CACHE["entries"]
    ent = {}
    p = paths.find("knowledge", "ecosystem.json")
    if p:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data.get("entries"), dict):
                ent = data["entries"]
        except Exception:
            ent = {}
    _ECOSYSTEM_CACHE["entries"] = ent
    return ent


def ecosystem_list():
    """列出已收录的嵌入式生态/框架（识别特征各给一条）。"""
    entries = _ecosystem_entries()
    if not entries:
        return ("生态知识库为空或缺失（knowledge/ecosystem.json）。\n"
                "若是 pip 安装，多半是数据目录没随包分发——看 env_status 的「知识库」段。")
    lines = ["已收录 %d 种生态/框架：" % len(entries)]
    for key, e in entries.items():
        lines.append("- %s（%s）" % (key, e.get("name", "")))
        marks = e.get("project_markers") or []
        if marks:
            lines.append("  识别特征：%s" % marks[0])
    lines.append("")
    lines.append('看详情：ecosystem_get("<上面的 key>"，如 zephyr / keil / rust_defmt)')
    return "\n".join(lines)


def ecosystem_get(name):
    """输出指定生态的识别特征：工程产物 / 构建配置 / 日志特征 / 常见坑。"""
    key = (name or "").strip().lower()
    entries = _ecosystem_entries()
    if not entries:
        return "生态知识库为空或缺失（knowledge/ecosystem.json）。"
    if key not in entries:
        # 允许按显示名模糊匹配（如 "keil mdk" → keil）
        hit = None
        for k, e in entries.items():
            if key and (key in k or key in str(e.get("name", "")).lower()):
                hit = k
                break
        if hit is None:
            return ("没有收录生态：%r。\n已收录：%s" % (name, "、".join(entries.keys())))
        key = hit
    e = entries[key]
    lines = ["%s（key=%s）" % (e.get("name", key), key)]
    for title, field in (("工程识别特征", "project_markers"),
                         ("构建产物", "build_outputs"),
                         ("日志特征", "log_signatures")):
        vals = e.get(field) or []
        if vals:
            lines.append("%s：" % title)
            for v in vals:
                lines.append("  · %s" % v)
    if e.get("gotchas"):
        lines.append("常见坑：%s" % e["gotchas"])
    lines.append("来源：%s" % e.get("source", ""))
    return "\n".join(lines)


# ---------------------------------------------------------------
# 工具链 / 工程结构探测（G02）
#
# 与上面生态识别同源：project_detect 先「认出这是什么工程」，
# toolchain_status 再答「本机拿什么去编它」。
# ---------------------------------------------------------------

#: 工程标志文件 → 生态 key（与 knowledge/ecosystem.json 的 key 对应）。
#: 以 "*" 开头的是后缀匹配（Keil 的 .uvprojx 名字随工程变）。
_PROJECT_MARKERS = (
    ("platformio.ini", "platformio"),
    ("*.uvprojx", "keil"),
    ("*.uvproj", "keil"),
    ("*.ewp", "iar"),
    ("prj.conf", "zephyr"),
    ("west.yml", "zephyr"),
    ("sconstruct", "rtthread"),
    ("cargo.toml", "rust_defmt"),
)

#: 工具链探测清单：(显示名, 候选可执行名, 版本参数)
_TOOLCHAIN_PROBES = (
    ("ARM GCC", ("arm-none-eabi-gcc",), ("--version",)),
    ("ARM GDB", ("arm-none-eabi-gdb", "gdb-multiarch"), ("--version",)),
    ("armclang（Keil AC6）", ("armclang",), ("--version",)),
    ("armcc（Keil AC5）", ("armcc",), ("--version",)),
    ("RISC-V GCC", ("riscv64-unknown-elf-gcc", "riscv32-unknown-elf-gcc"), ("--version",)),
    ("Xtensa GCC（ESP32）", ("xtensa-esp32-elf-gcc", "xtensa-esp32s3-elf-gcc"), ("--version",)),
    ("CMake", ("cmake",), ("--version",)),
    ("Ninja", ("ninja",), ("--version",)),
    ("Make", ("make",), ("--version",)),
    ("West（Zephyr）", ("west",), ("--version",)),
    ("SCons（RT-Thread）", ("scons",), ("--version",)),
    ("PlatformIO", ("pio", "platformio"), ("--version",)),
    ("Cargo（Rust）", ("cargo",), ("--version",)),
    ("probe-rs（Rust 烧录）", ("probe-rs",), ("--version",)),
    ("OpenOCD", ("openocd",), ("--version",)),
)


def _tool_version(path, args):
    """取一个工具的首行版本信息（失败不抛，返回可读说明）。"""
    try:
        r = subprocess.run([path] + list(args), capture_output=True, text=True,
                           timeout=6, encoding="utf-8", errors="replace")
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        for line in out.splitlines():
            if line.strip():
                return line.strip()[:120]
        return "（无版本输出）"
    except Exception as e:                            # noqa: BLE001
        return "（版本探测失败：%s）" % e


def toolchain_status():
    """探测本机嵌入式工具链：编译器 / 调试器 / 构建系统 / 烧录器，各给版本。

    ★ 只报**实测到**的：探不到就明确列为「未发现」，并说清装它有什么用——
      不猜「应该有」。缺一个编译器与缺一个烧录器，对排查的意义完全不同。
    """
    lines = ["本机嵌入式工具链探测", "-" * 50]
    found = []
    missing = []
    for disp, names, vargs in _TOOLCHAIN_PROBES:
        hit = None
        for n in names:
            p = _which_tool(n)
            if p:
                hit = (n, p)
                break
        if hit:
            lines.append("  ✓ %-20s %s" % (disp, _tool_version(hit[1], vargs)))
            found.append(disp)
        else:
            missing.append(disp)
    lines.append("")
    lines.append("已发现 %d 项，未发现 %d 项。" % (len(found), len(missing)))
    if missing:
        lines.append("未发现：%s" % "、".join(missing))
        lines.append("  ⓘ 未发现只代表**本进程的 PATH 与常见安装目录**里没有；")
        lines.append("    IDE 自带工具（Keil/IAR/CubeIDE）不一定暴露到命令行，属正常。")
    lines.append("说明：烧录器探测请用 flash_tool_detect（含 ST-Link / J-Link / esptool 等）。")
    return "\n".join(lines)


def project_detect(path="."):
    """识别一个工程目录属于哪个生态，并给出该生态的构建方式与常见坑。

    怎么判：扫目录里的**标志文件**（platformio.ini / *.uvprojx / prj.conf /
    SConstruct / *.ewp / Cargo.toml），命中即认为是该生态；多个都命中会全列出
    （复合工程确实存在，不硬选一个）。详情来自 knowledge/ecosystem.json。
    """
    root = os.path.abspath(os.path.expanduser(str(path or ".")))
    if not os.path.isdir(root):
        return "不是目录或不存在：%s" % root
    try:
        names = os.listdir(root)
    except OSError as e:
        return "读不到目录 %s：%s" % (root, e)

    low = dict((n.lower(), n) for n in names)
    hits, seen = [], set()
    for pat, key in _PROJECT_MARKERS:
        matched = []
        if pat.startswith("*"):
            ext = pat[1:]
            matched = [n for n in names if n.lower().endswith(ext)]
        elif pat in low:
            matched = [low[pat]]
        if matched and key not in seen:
            seen.add(key)
            hits.append((key, matched[0]))

    lines = ["工程结构识别：%s" % root]
    if not hits:
        lines.append("  没有命中已知生态的标志文件。")
        lines.append("  已识别的标志：%s"
                     % "、".join(p for p, _k in _PROJECT_MARKERS))
        lines.append("  若这是一个裸 CMake/Makefile 工程，本工具不当成某个生态——")
        lines.append("  直接看 CMakeLists.txt / Makefile 更准，不要硬套框架。")
        return "\n".join(lines)

    lines.append("  判定：%s" % "、".join(k for k, _f in hits))
    lines.append("  依据：%s" % "、".join("%s（%s）" % (f, k) for k, f in hits))
    lines.append("")
    for key, fname in hits:
        detail = ecosystem_get(key)
        lines.append("── %s（命中 %s）" % (key, fname))
        for ln in detail.splitlines():
            lines.append("   " + ln)
        lines.append("")
    lines.append("提示：工程目录只探一层（不递归），避免把子模块/依赖目录也算成工程。")
    return "\n".join(lines)


def board_list():
    """列出知识库里已收录的开发板（内置 + 外置层合并）。"""
    return sorted(_board_files())


def _clear_data_caches():
    """清知识缓存（数据根变化后必须调用；测试/切换 MCU_DEBUG_MCP_HOME 时用）。"""
    _PINMAP_CACHE.clear()
    _BOARDS_CACHE.clear()
    _FAULT_CACHE.clear()
    paths.reset_cache()


def board_get(name):
    """输出指定开发板的板级知识：板载外设、板级坑清单、供电说明。"""
    data = _load_board(name)
    if data is None:
        avail = board_list()
        msg = "没有收录开发板：%r。" % name
        msg += "\n已收录：" + (", ".join(avail) if avail else "（boards 目录为空）")
        msg += "\n想加新板：knowledge/boards/<板名>.json，板载外设/坑清单/供电说明，来源标原理图。"
        return msg
    lines = ["%s（MCU: %s）" % (data.get("board", name), data.get("mcu", "?"))]
    lines.append("板载外设：")
    for key, v in (data.get("onboard") or {}).items():
        pins = v.get("pins") or ([v["pin"]] if v.get("pin") else [])
        pins_s = ("，占用 %s" % "/".join(pins)) if pins else ""
        lines.append("  %s%s：%s" % (key, pins_s, v.get("note", "")))
    lines.append("板级坑清单（按翻车率排序）：")
    for i, issue in enumerate(data.get("board_issues", []), 1):
        lines.append("  %d. %s" % (i, issue))
    if data.get("power_notes"):
        lines.append("供电：%s" % data["power_notes"])
    lines.append("来源：%s" % data.get("source", ""))
    return "\n".join(lines)


def _load_board(name):
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in _BOARDS_CACHE:
        return _BOARDS_CACHE[key]
    data = None
    for f, _path in _board_files().items():
        try:
            with open(_path, "r", encoding="utf-8") as fh:
                cand = json.load(fh)
        except Exception:
            continue
        aliases = [cand.get("board", "").lower()] + [a.lower() for a in cand.get("alias", [])]
        if key in aliases or key == f.lower():
            data = cand
            break
    _BOARDS_CACHE[key] = data
    return data


def _load_pinmap(mcu):
    key = (mcu or "").strip().lower()
    if not key:
        return None
    if key in _PINMAP_CACHE:
        return _PINMAP_CACHE[key]
    data = None
    for name, _path in _pinmap_files().items():
        try:
            with open(_path, "r", encoding="utf-8") as f:
                cand = json.load(f)
        except Exception:
            continue
        aliases = [cand.get("mcu", "").lower()] + [a.lower() for a in cand.get("alias", [])]
        if key in aliases or key == name.lower():
            data = cand
            break
    _PINMAP_CACHE[key] = data
    return data


def pin_check(mcu, pins, board=None):
    """检查一组「引脚 → 功能」分配是否与该 MCU 的复用映射冲突。

    pins 形如 {"PA9": "USART1_TX", "PB6": "I2C1_SCL", "PA0": "GPIO"}。
    可选 board（开发板名，如 "bluepill"）：板载外设占用的引脚再被挪用会警告。
    能抓三类错：①引脚不存在 ②该脚没有此功能（AI 幻觉接线重灾区）
    ③同一功能被分配到两个脚；另对 SWD 脚挪用 / remap 档不匹配给出警告。
    """
    data = _load_pinmap(mcu)
    if data is None:
        avail = pinmap_list()
        msg = "没有收录 MCU：%r。" % mcu
        msg += "\n已收录：" + (", ".join(avail) if avail else "（pinmap 目录为空）")
        msg += "\n想加新 MCU：在 knowledge/pinmap/<型号>.json 按现有格式录数据（来源标数据手册）。"
        return msg

    if not isinstance(pins, dict) or not pins:
        return "pins 需要是 {引脚: 功能} 字典，如 {\"PA9\": \"USART1_TX\", \"PB6\": \"I2C1_SCL\"}"

    # ★ 空/缺功能名不能静默当成 "GPIO"：真实数据回放已复现——{"PA9": ""} 会输出
    #   "PA9 → GPIO" 并给出「无硬冲突」结论，用户少打一个字就拿到一个假通过。
    #   None 视为「没写」、空串/纯空白视为「写错了」，两者都明确报出来，不替用户猜。
    pins_u = {str(p).upper().strip(): ("" if f is None else str(f).upper().strip())
              for p, f in pins.items()}
    bad_func = sorted(p for p, f in pins_u.items() if not f)
    for p in bad_func:
        pins_u.pop(p, None)
    all_pins = set(data.get("pins", {}))
    swd = data.get("special", {}).get("debug", {})
    swd_pins = set()
    for v in swd.values():
        swd_pins.update(x.upper() for x in v)

    # 板载外设占用表：pin -> "板载外设名"
    board_data = _load_board(board) if board else None
    board_pins = {}
    if board_data:
        for key, v in (board_data.get("onboard") or {}).items():
            # 兼容两种写法：pins 列表 / pin 单值（贡献者常犯，读取端宽容，核验脚本把关）
            for p in (v.get("pins") or ([v["pin"]] if v.get("pin") else [])):
                board_pins[str(p).upper().strip()] = key

    rows = []           # (级别, 引脚, 功能, 说明)

    # 功能名为空的行单独报错——不能让它进下面的正常判定（否则空名会被当某种
    # 「未知功能」走进 else 分支，给出看似正式的「该脚没有此功能」）。
    for _p in bad_func:
        rows.append(("ERR", _p, "(空)",
                     "功能名是空的——必须写清要分配什么功能（如 GPIO / USART1_TX / "
                     "I2C1_SCL）。空着我没法判断，不替你猜。"))

    if (data.get("engine") or "fixed_mux") == "io_mux":
        # ESP32 类：无固定复用表，查的是硬约束（保留/仅输入/strapping）
        sp = data.get("special", {})
        reserved = set()
        for _g, lst in (sp.get("reserved") or {}).items():
            for p in lst or []:
                reserved.add(str(p).upper().strip())
        input_only = {str(p).upper().strip() for p in sp.get("input_only") or []}
        strapping = {str(p).upper().strip(): why for p, why in (sp.get("strapping") or {}).items()}
        out_hints = ("OUT", "TX", "MOSI", "SCK", "CLK", "CS", "SS", "PWM", "DAC")
        known = set(data.get("pins", {}) or {})
        for pin in sorted(pins_u, key=lambda p: (len(p), p)):
            func = pins_u[pin]
            bkey = board_pins.get(pin)
            if known and pin not in known and pin not in reserved \
                    and pin not in input_only and pin not in strapping:
                # ★引脚存在性前置校验：io_mux 引擎没有固定复用表，但不代表引脚可以随便写。
                # 不校验的话，GPIO999 / GPI08 / 手滑打错的引脚名会落进下面的"任意路由"兜底，
                # 拿到一个 ✅ 通过 + "可以按此接线" 的结论——典型的"看似合理的假通过"。
                _near = [p for p in known if p.startswith(pin[:4])][:6]
                tip = ("；相近的已知引脚：%s" % ", ".join(sorted(_near))) if _near else ""
                rows.append(("ERR", pin, func,
                             "该 MCU 没有此引脚（核对型号/封装，别把引脚名打错）%s" % tip))
            elif pin in reserved:
                rows.append(("ERR", pin, func, "内部 flash 专用脚，配置成任何功能都会掉线重启（必砖级）"))
            elif pin in input_only:
                if func in ("GPIO", "GPIO_IN", "GPIO_INPUT") or "RX" in func or "MISO" in func \
                        or func.endswith("IN") or "READ" in func:
                    rows.append(("WARN", pin, func, "仅输入脚：可以读，但无输出驱动也无内部上下拉（外接按键/上拉电阻）"))
                else:
                    rows.append(("ERR", pin, func, "仅输入脚（GPIO34-39），没有输出驱动，做不了输出"))
            elif pin in strapping:
                rows.append(("WARN", pin, func, "strapping 脚（%s）：上电瞬间的电平决定启动模式，"
                             "外设要保证上电默认电平，不然变砖头不启动" % strapping[pin]))
            elif bkey:
                rows.append(("WARN", pin, func, "板载外设 %s 在用此脚（查板级知识 board_get）" % bkey))
            else:
                extra = "任意路由（GPIO Matrix）：I2C/UART/PWM 挑哪脚都行；SPI 高速信号建议用惯用脚"
                if bkey:
                    extra += "；板载外设 %s 也在此脚" % bkey
                rows.append(("OK", pin, func, extra))
    else:
        seen_func = {}      # 功能名 -> 引脚
        for pin in sorted(pins_u, key=lambda p: (len(p), p)):
            func = pins_u[pin]
            info = data.get("pins", {}).get(pin)
            if info is None:
                rows.append(("ERR", pin, func, "该 MCU 没有此引脚（核对型号/封装）"))
                continue
            funcs = [x.upper() for x in info.get("functions", [])]
            note = info.get("note", "")
            bkey = board_pins.get(pin)
            if func in ("GPIO", "GPIO_IN", "GPIO_OUT"):
                if pin in swd_pins:
                    rows.append(("WARN", pin, func, "调试脚（SWD）挪用后无法烧录调试，除非 Connect under reset 抢救"))
                elif bkey:
                    rows.append(("WARN", pin, func, "板载外设 %s 在用此脚，当普通 IO 会互抢（查板级知识 board_get）" % bkey))
                elif note:
                    rows.append(("WARN", pin, func, note))
                else:
                    rows.append(("OK", pin, func, ""))
                continue
            if func in funcs:
                if func in seen_func:
                    rows.append(("CONFLICT", pin, func, "已被 %s 占用：同一功能不能两路同时开" % seen_func[func]))
                else:
                    seen_func[func] = pin
                    extra = ("；" + note) if note else ""
                    if info.get("ft") is False and "5v" not in extra.lower():
                        extra += "；非 5V 容忍脚"
                    if bkey:
                        extra += "；注意：板载外设 %s 也接在此脚，挪用后板载功能失效" % bkey
                    rows.append(("OK", pin, func, extra))
            elif func + "_REMAP" in funcs:
                _dup = ("；⚠ 同一个 %s 已经分配给 %s 了，默认档与 remap 档只能选一个用"
                        % (func, seen_func[func])) if func in seen_func else ""
                rows.append(("WARN", pin, func, "该脚只有 %s 的 remap 档：先开 AFIO 时钟并调 remap（%s）%s"
                             % (func, func + "_REMAP", _dup)))
            elif func.endswith("_REMAP") and func[:-6] in funcs:
                rows.append(("WARN", pin, func, "该脚是此功能的默认档（%s），无需 remap" % func[:-6]))
            else:
                rows.append(("ERR", pin, func, "该脚没有此功能。可用：%s" % (", ".join(info.get("functions", [])) or "仅普通 GPIO")))

    lines = ["%s（%s）引脚检查：%d 项" % (data.get("mcu", mcu), data.get("package", ""), len(rows))]
    icon = {"OK": "✓", "WARN": "△", "ERR": "✗", "CONFLICT": "✗"}
    for lvl, pin, func, msg in rows:
        lines.append("  %s %-5s → %-16s %s" % (icon[lvl], pin, func, msg))
    bad = sum(1 for r in rows if r[0] in ("ERR", "CONFLICT"))
    warns = sum(1 for r in rows if r[0] == "WARN")
    if bad:
        lines.append("结论：%d 项错误/冲突，必须改。" % bad)
    elif warns:
        lines.append("结论：无硬冲突，%d 项警告建议过目。" % warns)
    else:
        lines.append("结论：全部通过，可以按此接线/配引脚。")
    for tip in data.get("common_conflicts", []):
        lines.append("提示：%s" % tip)
    return "\n".join(lines)


# ---------------------------------------------------------------
# 排故链（空格 9）：fault 解码 / .map 解析 / 符号反查 / 栈回溯
#   knowledge/fault_bits/*.json 存位域表，解析逻辑复用 decode_register 的思路，
#   但 fault 有【三类专属陷阱】，必须在输出里强制处理，不能只写文档：
#     1) valid 位（BFARVALID/MMARVALID）为 0 时，地址寄存器是垃圾值——必须拒绝解读
#     2) IMPRECISERR 时 BFAR 即使 valid 也不可信（总线写延迟）——必须显式标注
#     3) FORCED 置 1 说明真因在 CFSR，只看 HFSR 会漏根因——必须引导去看 CFSR
# ---------------------------------------------------------------

_FAULT_CACHE = {}


def _fault_files():
    """层叠列举故障位域表 → {表名: 路径}。"""
    return {name[:-5]: path
            for name, path in paths.layer_files("knowledge", "fault_bits")}


def fault_bits_catalog():
    """返回已收录的故障位域表：[{"table", "arch", "desc"}]，按文件名排序。

    ★ 为什么不是只给个文件名列表：文件名（cortex_m）与 decode_fault(arch=)
      认的架构名（cortex-m）**不是同一个字符串**。只列文件名，用户/AI 照抄
      文件名去填 arch 就会得到「没有收录」——一个纯由命名不一致制造的坑。
      catalog 把「文件名 + arch 该填什么 + 这张表是什么」一起给出来。
    读不动的文件照样列出来（让人知道文件在、只是坏了），不静默跳过。
    """
    out = []
    for name, _path in sorted(_fault_files().items()):
        arch, desc = "", ""
        try:
            with open(_path, encoding="utf-8") as f:
                d = json.load(f)
            arch = d.get("arch") or ""
            desc = d.get("desc") or ""
        except Exception:
            pass
        out.append({"table": name, "arch": arch, "desc": desc})
    return out


def fault_bits_list():
    """列出已收录故障位域表的文件名（decode_fault 报错文案在用）。"""
    return [c["table"] for c in fault_bits_catalog()]


def _fault_bits_brief():
    """「cortex_m（arch=cortex-m）」这种人能直接照着填的一行摘要。"""
    cat = fault_bits_catalog()
    if not cat:
        return "（目录为空）"
    return "、".join("%s（arch=%s）" % (c["table"], c["arch"] or "未标")
                     for c in cat)


def fault_bits_get(arch):
    """输出指定架构故障位域表的 JSON 原文（条目级 source 一并可见）。

    供 MCP 资源 mcu://faultbits/{arch} 使用。
    两种写法都认：文件名 cortex_m、架构名 cortex-m——本函数存在的意义就是
    别让人为了「文件名还是 arch 名」纠结（见 fault_bits_catalog 的注释）。
    """
    cat = fault_bits_catalog()
    if not cat:
        return "故障位域表目录为空（knowledge/fault_bits/ 下没有 *.json）。"
    key = (arch or "").strip().lower()
    if not key:
        return ('用法：fault_bits_get("cortex-m")\n已收录：%s' % _fault_bits_brief())
    hit = [c for c in cat if key in (c["table"].lower(), (c["arch"] or "").lower())]
    if not hit:
        return ("没有收录架构 %r 的故障位域表。\n已收录：%s\n"
                "新架构：knowledge/fault_bits/<arch>.json（位域须逐条标 source）。"
                % (arch, _fault_bits_brief()))
    with open(_fault_files()[hit[0]["table"]], encoding="utf-8") as f:
        return f.read()


def _load_fault_bits(arch):
    key = (arch or "cortex-m").strip().lower()
    if key in _FAULT_CACHE:
        return _FAULT_CACHE[key]
    data = None
    for name, _path in _fault_files().items():
        try:
            with open(_path, "r", encoding="utf-8") as f:
                cand = json.load(f)
        except Exception:
            continue
        a = (cand.get("arch") or "").lower()
        if key == a or key == name.lower():
            data = cand
            break
    _FAULT_CACHE[key] = data
    return data


def _parse_int(v):
    """解析 int/hex 字符串。失败返回 None。"""
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v.strip(), 0)
        except (TypeError, ValueError):
            return None
    return None


def _looks_like_sentinel(addr):
    """判断一个 32 位地址是否「长得像哨兵值/未初始化值」而不是真实地址。

    ★ 注意这是提示、不是判定：命中不代表地址一定是假的，只代表「这个值本身可疑，
    反查前先自问一句这地址合不合理」。宁可多提醒一句，也不能把 DEADBEEF 当正常地址放行。
    """
    if addr is None:
        return None
    if addr == 0:
        return "地址为 0（NULL 解引用，或寄存器根本没读到值）"
    if addr == 0xFFFFFFFF:
        return "地址是 0xFFFFFFFF（Flash 擦除态 / 读失败返回全 1，不是真实地址）"
    if addr < 0x100:
        return ("地址小于 0x100（几乎不可能是合法外设/RAM/Flash 地址，"
                "更像小整数被当指针用）")
    # 常见「人为写死的哨兵值」：4 位循环、0xDEADBEEF、0xCAFEBABE、0xBAADF00D 等
    b = [(addr >> (8 * i)) & 0xFF for i in range(4)]
    if len(set(b)) == 1:
        return "地址 4 字节全同（如 0x55555555/0xAAAAAAAA，典型的调试填充值）"
    hexed = "%08X" % addr
    if hexed in ("DEADBEEF", "CAFEBABE", "BAADF00D", "DEADC0DE", "FEE1DEAD",
                 "0BADF00D", "DEAD10CC", "0D15EA5E", "ABADBABE"):
        return "地址是经典调试哨兵值 0x%s（多半是内存被填充或变量未初始化）" % hexed
    return None


def decode_fault(cfsr=None, hfsr=None, bfar=None, mmfar=None, arch="cortex-m"):
    """解码 Cortex-M 故障状态寄存器，给出人话诊断 + 排查方向。

    输入是各个寄存器读到的原始值（int 或 0x 字符串），任意组合，只传拿到的。
    ★ 三类陷阱在输出里强制处理（见文件头注释），不会给出"看起来合理但实际错"的地址。
    """
    data = _load_fault_bits(arch)
    # ★ M0/M0+ 是「已知架构但没有 CFSR」，不能混成「没收录」——
    #   否则用户会以为补个 json 就能用，实际是判定路径根本不同。
    if data is None and arch in ("cortex-m0", "cortex-m0+", "cortex-m0plus"):
        return ("%s 不支持本工具的解码路径。\n"
                "\n"
                "原因：Cortex-M0/M0+ 没有 CFSR / HFSR / BFAR / MMFAR 这四个寄存器"
                "（它们是 ARMv7-M 才引入的）。\n"
                "M0 上所有故障——MemManage / BusFault / UsageFault——统统进 HardFault，"
                "没有分项状态位可用。\n"
                "\n"
                "M0 上排查 HardFault 只能用另一条路：\n"
                "  1. 在 HardFault_Handler 里把压栈帧取出来（MSP/PSP 指向的那 8 个字："
                "R0,R1,R2,R3,R12,LR,PC,xPSR）\n"
                "  2. 重点看 PC——它指向出事的那条指令\n"
                "  3. 把 PC 交给 symbol_lookup 反查函数名，再对着反汇编看这条指令在访问什么\n"
                "  4. 用 stack_unwind 看调用链（本工具支持任意 Cortex-M 的压栈帧，M0 也能用）\n"
                "\n"
                "换句话说：M0 上 decode_fault 用不了，但 parse_map / symbol_lookup / "
                "stack_unwind 三个都不受架构限制，照样能用。" % arch)
    if data is None:
        return ("没有收录架构 %r 的故障位域表。\n已收录：%s\n"
                "（括号里的 arch= 才是本工具认的写法；只给文件名（cortex_m）"
                "匹配不上，会走到这条分支。）\n"
                "新架构：knowledge/fault_bits/<arch>.json，位域须逐条标 source。"
                % (arch, _fault_bits_brief()))

    regs = data.get("registers") or {}
    vals = {}
    for name, raw in (("CFSR", cfsr), ("HFSR", hfsr), ("BFAR", bfar), ("MMFAR", mmfar)):
        if raw is None or raw == "":
            continue
        n = _parse_int(raw)
        if n is None:
            return "%s 值无法解析为整数：%r（支持十进制或 0x 前缀）" % (name, raw)
        if n < 0 or n >= (1 << 32):
            return "%s 值超出 32 位范围：%s" % (name, hex(n))
        vals[name] = n

    if not vals:
        return ("至少要给一个寄存器值（cfsr / hfsr / bfar / mmfar）。\n"
                "典型用法：先从 fault handler 里把 CFSR/HFSR 读出来传进来，"
                "若 CFSR 的 BFARVALID/MMARVALID 为 1，再传 BFAR/MMFAR。")

    lines = ["%s 故障寄存器解码" % (data.get("desc", "Cortex-M")).split("（")[0]]
    lines.append("")

    # ---- 收集命中的位，供后续交叉判断 ----
    hits = {}          # 寄存器 -> [(位域名, info)]

    def _scan(reg_name):
        val = vals.get(reg_name)
        if val is None:
            return []
        out = []
        for bit_name, info in (regs.get(reg_name) or {}).get("bits", {}).items():
            bit = info.get("bit")
            w = info.get("width", 1)
            if bit is None:
                continue
            mask = ((1 << w) - 1) << bit
            if val & mask:
                out.append((bit_name, info))
        return out

    for rn in ("CFSR", "HFSR"):
        if rn in vals:
            hits[rn] = _scan(rn)

    # ---- CFSR：按 segment 分组输出 ----
    if "CFSR" in vals:
        v = vals["CFSR"]
        lines.append("CFSR = %s（二进制 %s）" % (hex(v), format(v, "b").zfill(32)))
        cfsr_bits = regs.get("CFSR", {}).get("bits", {})
        segs = regs.get("CFSR", {}).get("segments", {})
        cfsr_hits = hits.get("CFSR", [])

        if not cfsr_hits:
            lines.append("  （无任何故障位置位——若确实进了 HardFault，去查 HFSR 的 DEBUGEVT，"
                         "或确认读 CFSR 的时机是否在故障现场）")
        else:
            # 先输出 info 类的 valid 位提示，再按严重程度排
            sev_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
            cfsr_hits.sort(key=lambda x: sev_rank.get(x[1].get("severity"), 9))
            for seg_key in ("MMFSR", "BFSR", "UFSR"):
                seg_hits = [h for h in cfsr_hits if h[1].get("segment") == seg_key]
                if not seg_hits:
                    continue
                seg_name = segs.get(seg_key, {}).get("name", seg_key)
                lines.append("  [%s] %s" % (seg_key, seg_name))
                for bit_name, info in seg_hits:
                    lines.append("    ● %s：%s" % (bit_name, info.get("meaning", "")))
                    if info.get("cause"):
                        lines.append("        成因：%s" % info["cause"])
                    if info.get("hint"):
                        lines.append("        排查：%s" % info["hint"])
        # 未分组命中的兜底（segment 缺失的情况）
        ungrouped = [h for h in cfsr_hits if not h[1].get("segment")]
        for bit_name, info in ungrouped:
            lines.append("    ● %s：%s" % (bit_name, info.get("meaning", "")))
            if info.get("hint"):
                lines.append("        排查：%s" % info["hint"])
        lines.append("")

    # ---- HFSR ----
    if "HFSR" in vals:
        v = vals["HFSR"]
        lines.append("HFSR = %s（二进制 %s）" % (hex(v), format(v, "b").zfill(32)))
        hfsr_hits = hits.get("HFSR", [])
        if not hfsr_hits:
            lines.append("  （无故障位置位）")
        for bit_name, info in hfsr_hits:
            lines.append("  ● %s：%s" % (bit_name, info.get("meaning", "")))
            if info.get("cause"):
                lines.append("      成因：%s" % info["cause"])
            if info.get("hint"):
                lines.append("      排查：%s" % info["hint"])
        lines.append("")

    # ---- ★陷阱 3：FORCED 置 1 必须引导去看 CFSR ----
    forced = any(b == "FORCED" for b, _ in hits.get("HFSR", []))
    has_cfsr = "CFSR" in vals
    cfsr_empty = not hits.get("CFSR")
    if forced:
        lines.append("★【关键】HFSR.FORCED=1：这是被「升级」成 HardFault 的故障，"
                     "真正的原因记录在 CFSR 里。")
        if not has_cfsr:
            lines.append("  你还没传 CFSR——必须补上，否则只看 HFSR 会漏掉根因。")
        elif cfsr_empty:
            lines.append("  但你传的 CFSR=0，没有故障位。可能是：CFSR 已被人为清过 / "
                         "读的时机不对（应在 fault handler 最开头读）/ 实际是 DEBUGEVT。")
        lines.append("")

    # ---- ★陷阱 1 & 2：地址可信性判断 ----
    def _bit_set(reg_name, bit_name):
        v = vals.get(reg_name)
        if v is None:
            return None
        b = (regs.get(reg_name) or {}).get("bits", {}).get(bit_name, {}).get("bit")
        if b is None:
            return None
        return bool(v & (1 << b))

    bfar_valid = _bit_set("CFSR", "BFARVALID")
    mmar_valid = _bit_set("CFSR", "MMARVALID")
    imprecise = _bit_set("CFSR", "IMPRECISERR")
    precise = _bit_set("CFSR", "PRECISERR")

    if "BFAR" in vals or bfar_valid is not None:
        lines.append("BusFault 地址（BFAR）")
        if bfar_valid is None:
            lines.append("  △ 未传 CFSR，无法判断 BFARVALID——地址可信性未知，"
                         "请传 CFSR 后再下结论。")
            if "BFAR" in vals:
                lines.append("    BFAR = %s（可信性未验证，先别当准）" % hex(vals["BFAR"]))
        elif not bfar_valid:
            lines.append("  ✗ BFARVALID=0：★BFAR 的内容【不是本次故障地址】，是残留/垃圾值。")
            lines.append("    → 不要拿它去反查符号，会得到假结论。先去查为什么 valid 没置位。")
            if "BFAR" in vals:
                lines.append("    （本次读到的 BFAR = %s，仅供确认它确实是垃圾值）" % hex(vals["BFAR"]))
            else:
                lines.append("    （你没传 BFAR——就算读了也别拿它反查。）")
        else:
            addr = vals.get("BFAR")
            if addr is None:
                lines.append("  BFARVALID=1，但你没传 BFAR 值——补上地址才能反查。")
            elif imprecise:
                lines.append("  ⚠ BFARVALID=1，但 IMPRECISERR=1：★此地址【不可信】！")
                lines.append("    总线写是异步的，异常发生时的这个地址与真正出错的指令无关。")
                lines.append("    → 不能按这个地址反查代码定位。改用二分法收缩：")
                lines.append("       在可疑区间插入读回校验（读一下刚写的寄存器）把延迟写变成精确错误，")
                lines.append("       或逐步注释可疑代码段（尤其 DMA/外设寄存器批量写）缩小范围。")
                lines.append("    （本次读到的 BFAR = %s，作为「有过总线写」的旁证保留）" % hex(addr))
            elif precise:
                lines.append("  ✓ BFARVALID=1 且 PRECISERR=1：★此地址可信，可直接反查。")
                lines.append("    BFAR = %s" % hex(addr))
                sent = _looks_like_sentinel(addr)
                if sent:
                    lines.append("    ⚠ 但等等：%s" % sent)
                    lines.append("      → 位域说「有效」不代表这个值本身合理。"
                                 "反查前先确认：这个地址在你的芯片地址图上真实存在吗？")
                    lines.append("      → 常见真相：变量未初始化 / 指针被填充值覆盖 / "
                                 "结构体偏移算错读到了别的字段。")
                lines.append("    → 下一步：用 symbol_lookup 反查这个地址属于哪个函数。")
                lines.append("    常见成因：访问了未使能时钟的外设寄存器（如忘了开 RCC 时钟）、"
                             "指针算错、DMA 缓冲地址配置错误。")
            else:
                lines.append("  △ BFARVALID=1（未见到 PRECISERR/IMPRECISERR 位）：")
                lines.append("    BFAR = %s" % hex(addr))
                lines.append("    无法判定精确性——先确认 CFSR 里的故障类型位，再决定是否按此地址反查。")
        lines.append("")

    if "MMFAR" in vals or mmar_valid is not None:
        lines.append("MemManage 地址（MMFAR）")
        if mmar_valid is None:
            lines.append("  △ 未传 CFSR，无法判断 MMARVALID。")
            if "MMFAR" in vals:
                lines.append("    MMFAR = %s（可信性未验证）" % hex(vals["MMFAR"]))
        elif not mmar_valid:
            lines.append("  ✗ MMARVALID=0：★MMFAR 不是本次故障地址，是残留/垃圾值。")
            if "MMFAR" in vals:
                lines.append("    （本次读到的 MMFAR = %s）" % hex(vals["MMFAR"]))
            else:
                lines.append("    （你没传 MMFAR——就算读了也别拿它反查。）")
        else:
            addr = vals.get("MMFAR")
            if addr is None:
                lines.append("  MMARVALID=1，但没传 MMFAR——补上地址才能反查。")
            else:
                lines.append("  ✓ MMARVALID=1：★此地址可信。MMFAR = %s" % hex(addr))
                sent = _looks_like_sentinel(addr)
                if sent:
                    lines.append("    ⚠ 但等等：%s" % sent)
                    lines.append("      → valid=1 只说明硬件记录了这个地址，不代表这地址本身合理。")
                lines.append("    → 用 symbol_lookup 反查；常见于数组越界写、野指针踩到受保护区。")
        lines.append("")

    # ---- 走向建议 ----
    lines.append("下一步：")
    lines.append("  1. 若上面给出了可信地址 → 用 symbol_lookup 反查函数名")
    lines.append("  2. 若进了 fault 但要看调用链 → 把 fault 时压栈的 8 个字传给 stack_unwind")
    lines.append("  3. 若 CFSR 的 STKERR/UNSTKERR/MSTKERR/MUNSTKERR 命中 → 基本是栈问题，"
                 "用 parse_map 查最大栈深够不够")
    lines.append("")
    lines.append("数据来源：%s" % data.get("source", ""))
    if data.get("caveats"):
        lines.append("注意：")
        for c in data["caveats"]:
            lines.append("  · %s" % c)
    return "\n".join(lines)


# ---------------------------------------------------------------
# .map 解析（空格 4）：段占用 / 栈深 / 符号地址表
#   支持三类真实格式：
#     1) GNU ld（arm-none-eabi-gcc / STM32CubeIDE / ESP-IDF）
#     2) Keil/ARMCC（.map 的 "Image Symbol Table" 段）
#     3) IAR（.map 的 "ENTRY LIST" 段）
#   格式差异极大，靠特征串自动识别，识别不出就明确说"认不出来"，不猜。
# ---------------------------------------------------------------

_MAP_CACHE = {}


def _infer_sizes(symbols, segments=None):
    """GNU ld 的 map 里符号行【不带 size】——size 全为 0。

    这会让"占用最大的符号"（查大局部数组/大缓冲的关键视图）永远为空，
    也会让 symbol_lookup 的越界判断失效。修法：按地址排序后，
    用【下一个符号的地址 - 本符号地址】推断本符号大小。

    ★ 边界诚实处理（每一条都对应一个曾经错过的真实坑）：
      - 最后一个符号没有下一个，无法推断——保持 size=0，不猜
      - 同一地址的多个符号（别名）size 保持 0，避免把别名算成独立占用
      - ★跨段不推断：.text 的最后一个符号绝不能拿 .data 的首地址当"下一个"。
        否则 0x20000000 - 0x0800039c = 384MB 这种离谱数字会直接印出来，
        而它其实是「下一个段的起始」，不是符号大小。传入 segments 时按段边界裁剪。
    """
    # 段边界：[(seg_addr, seg_end, seg_name), ...]，用于跨段保护
    spans = []
    if segments:
        for nm, info in segments.items():
            a = info.get("addr")
            sz = info.get("size")
            if a is not None and sz:
                spans.append((a, a + sz, nm))
        spans.sort()

    def _seg_of(addr):
        for a, end, nm in spans:
            if a <= addr < end:
                return nm
        return None

    by_addr = {}
    for s in symbols:
        by_addr.setdefault(s["addr"], []).append(s)
    addrs = sorted(by_addr.keys())
    for i, a in enumerate(addrs):
        group = by_addr[a]
        # 同址多符号：别名，不给大小
        if len(group) > 1:
            continue
        if i + 1 >= len(addrs):
            continue                      # 最后一个符号：不猜
        nxt = addrs[i + 1]
        gap = nxt - a
        if gap <= 0:
            continue
        # ★跨段保护：本符号与下一个符号必须落在同一段内
        if spans:
            sa, sn = _seg_of(a), _seg_of(nxt)
            if sa is not None and sa != sn:
                continue                  # 跨段 → 差值不是符号大小，不推断
            if sa is None:
                continue                  # 落在所有已知段之外，不敢推断
        # ★上限保护：单个符号推断值不能超过所属段大小（防跨段/对齐造成的离群值）
        if spans:
            over = False
            for s_a, s_end, _nm in spans:
                if s_a <= a < s_end and gap > (s_end - s_a):
                    over = True
                    break
            if over:
                continue
        s = group[0]
        if s.get("size", 0) == 0:
            s["_inferred"] = True
            s["size"] = gap
    return symbols


def _is_elf(path):
    """文件头是否为 ELF 魔数（\\x7fELF）。"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


# ELF 常量（只用到这几个）
_ELF_SHT_SYMTAB = 2
_ELF_STT_OBJECT = 1
_ELF_STT_FUNC = 2


def _parse_elf(path):
    """解析 .elf 的段表与符号表，返回与 ``_parse_map_*`` 同构的 dict。

    ★ 为什么值得单独做：``.map`` 不记符号大小，反查地址只能靠「下一个符号 −
      本符号」推断（符号间有对齐全零填充时会偏大）；``.elf`` 的 ``.symtab``
      带**真实** ``st_size``，于是 stack_unwind / symbol_lookup 判断「这个地址
      到底还在不在函数内」才有真凭据，而不是靠估计。

    只依赖标准库 struct；支持 32/64 位、大/小端（Cortex-M 走 32 位小端这条）。
    解析不出来时返回 None —— 不假装解析成功。
    """
    import struct
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return None
    is64 = data[4] == 2
    endian = "<" if data[5] == 1 else ">"
    try:
        if is64:
            (e_shoff,) = struct.unpack_from(endian + "Q", data, 0x28)
            e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(endian + "HHH", data, 0x3A)
        else:
            (e_shoff,) = struct.unpack_from(endian + "I", data, 0x20)
            e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(endian + "HHH", data, 0x2E)
    except struct.error:
        return None
    min_ent = 64 if is64 else 40
    if not e_shoff or not e_shnum or e_shentsize < min_ent:
        return None

    def read_sh(i):
        off = e_shoff + i * e_shentsize
        if off < 0 or off + e_shentsize > len(data):
            return None
        try:
            if is64:
                name, typ = struct.unpack_from(endian + "II", data, off)
                addr, offset, size = struct.unpack_from(endian + "QQQ", data, off + 0x10)
                link = struct.unpack_from(endian + "I", data, off + 0x28)[0]
                entsize = struct.unpack_from(endian + "Q", data, off + 0x38)[0]
            else:
                name, typ, _flags, addr, offset, size = \
                    struct.unpack_from(endian + "IIIIII", data, off)
                link = struct.unpack_from(endian + "I", data, off + 0x18)[0]
                entsize = struct.unpack_from(endian + "I", data, off + 0x24)[0]
        except struct.error:
            return None
        return dict(name=name, type=typ, addr=addr, offset=offset,
                    size=size, link=link, entsize=entsize)

    shs = []
    for i in range(e_shnum):
        sh = read_sh(i)
        if sh is None:
            return None
        shs.append(sh)

    def cstr(sh, idx):
        if not sh or not sh["size"]:
            return ""
        base = sh["offset"] + idx
        if base < 0 or base >= len(data):
            return ""
        end = data.find(b"\x00", base)
        if end < 0:
            end = len(data)
        return data[base:end].decode("utf-8", errors="replace")

    shstr = shs[e_shstrndx] if e_shstrndx < len(shs) else None

    segments, symbols = {}, []
    for sh in shs:
        sname = cstr(shstr, sh["name"])
        # 段占用：只要已分配地址且有大小、名字像段名的
        # ★ off 是段在文件里的字节偏移，flash 校验（flash_verify）要靠它去 elf 里
        #   取「本该是什么」。老调用方只读 name/addr/size，多一个键不影响它们。
        if sh["addr"] and sh["size"] and sname.startswith("."):
            segments[sname] = {"name": sname, "addr": sh["addr"], "size": sh["size"],
                               "off": sh["offset"], "type": sh["type"]}
        # 符号表
        if sh["type"] == _ELF_SHT_SYMTAB:
            strtab = shs[sh["link"]] if 0 <= sh["link"] < len(shs) else None
            ent = sh["entsize"] or (24 if is64 else 16)
            if not ent:
                continue
            for k in range(sh["size"] // ent):
                off = sh["offset"] + k * ent
                if off + ent > len(data):
                    break
                try:
                    if is64:
                        nm, info, _other, shndx = struct.unpack_from(endian + "IBBH", data, off)
                        value, ssize = struct.unpack_from(endian + "QQ", data, off + 8)
                    else:
                        nm, value, ssize, info, _other, shndx = \
                            struct.unpack_from(endian + "IIIBBH", data, off)
                except struct.error:
                    break
                stype = info & 0xF
                # 跳过未定义（外部引用）符号；只保留函数与数据对象
                if shndx == 0 or stype not in (_ELF_STT_OBJECT, _ELF_STT_FUNC):
                    continue
                sym_name = cstr(strtab, nm)
                if not sym_name:
                    continue
                if stype == _ELF_STT_FUNC:
                    # ★ ARM Thumb：FUNC 符号的 st_value 最低位可能是 1（Thumb 状态位）。
                    #   不清掉会让**整张函数符号表偏移 1 字节**——实测 frame_dummy 被
                    #   算成 0x800275（arm-none-eabi-nm 给的是 0x800274），于是
                    #   0x800290 被误判成落在 frame_dummy 区间里，反查给出错函数名。
                    value &= ~1
                symbols.append({"name": sym_name, "addr": int(value), "size": int(ssize)})

    # segments 与 _parse_map_* 保持同构：name -> {"name","addr","size"}
    return {"symbols": symbols, "segments": segments, "mem_regions": [], "format": "elf"}


def _sniff_map_format(text):
    """按特征串判断 .map 来自哪个工具链。认不出返回 None。"""
    head = text[:20000]
    if "Linker script and memory map" in head or "Memory Configuration" in head:
        return "gnu"
    if "Image Symbol Table" in head or "Image component sizes" in head:
        return "keil"
    if "ENTRY LIST" in head or "*** MODULE SUMMARY" in head:
        return "iar"
    # GNU ld 的备用特征
    if "\nMemory Configuration" in head or ".text " in head and "0x" in head:
        return "gnu"
    return None


def _parse_map_gnu(text):
    """解析 GNU ld .map：段占用 + 符号表。

    GNU map 的符号行形如：
        0x08004a2c                mpu6050_read
        0x08004a2c                .text.mpu6050_read
        0x08004a2c    0x1c        mpu6050_read
    """
    seg_map = {}     # 段名 -> {"size": int, "addr": int}
    symbols = []     # [{"name","addr","size"}]
    mem_regions = [] # 内存区域定义

    # --- 符号表：找 "Global Symbols" / "Allocating common symbols" / 段内符号行 ---
    # 取「地址 + 可选大小 + 名字」的行
    sym_re = re.compile(
        r"^\s*(0x[0-9a-fA-F]+)\s+(?:(0x[0-9a-fA-F]+)\s+)?([A-Za-z_$][\w.$]*)\s*$")

    for line in text.split("\n"):
        # 内存区域：ORIGIN = 0x..., LENGTH = ...
        if "ORIGIN" in line and "LENGTH" in line:
            mem_regions.append(line.strip())
        m = sym_re.match(line)
        if m:
            addr = int(m.group(1), 16)
            size = int(m.group(2), 16) if m.group(2) else 0
            name = m.group(3)
            # 过滤掉明显是段名/节名的（以 . 开头的跳过——那是 section 而非 symbol）
            if name.startswith("."):
                continue
            # 过滤掉 fill/长名字
            if name in ("fill", "BYTE", "LONG", "PROVIDE", "ALIGN"):
                continue
            symbols.append({"name": name, "addr": addr, "size": size})

    # --- 段占用：找形如 " .text  0x08000000  0x1234" 的汇总行 ---
    # GNU map 末尾有 "section size" 表，形如： .text  0x1234  0x8000000
    seg_re = re.compile(r"^\s*(\.[\w.]+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s*$")
    for line in text.split("\n"):
        m = seg_re.match(line)
        if m:
            sname = m.group(1)
            a = int(m.group(2), 16)
            s = int(m.group(3), 16)
            # 同名段取第一个（多次出现时保留首次的地址、累加大小交给下面汇总）
            if sname not in seg_map:
                seg_map[sname] = {"addr": a, "size": s}
            else:
                # 有些 layout 会把同名段分散，size 取最大更接近真实占用
                if s > seg_map[sname]["size"]:
                    seg_map[sname]["size"] = s

    # GNU ld 的符号行不带 size ⟹ 按相邻地址差推断（否则"大符号"视图永远是空的）
    # ★ 必须传 seg_map：跨段差值不是符号大小（见 _infer_sizes 注释）
    symbols = _infer_sizes(symbols, seg_map)

    return {"format": "gnu", "segments": seg_map, "symbols": symbols, "mem_regions": mem_regions}


def _parse_map_keil(text):
    """解析 Keil/ARMCC .map 的 Image Symbol Table。

    形如：
        mpu6050_read                           0x08004a2c   Thumb Code    28  mpu6050.o(.text)
    """
    symbols = []
    seg_map = {}
    in_sym = False
    for line in text.split("\n"):
        if "Image Symbol Table" in line:
            in_sym = True
            continue
        if in_sym and line.startswith("Memory Map of the image"):
            in_sym = False
        if in_sym:
            m = re.match(
                r"^\s*(\S+)\s+(0x[0-9a-fA-F]{8})\s+\S+\s+(?:Code|Data|Zero|Section)?\s*"
                r"(\d+)?\s*(\S+)?\s*$", line)
            if m:
                name = m.group(1)
                addr = int(m.group(2), 16)
                size = int(m.group(3)) if m.group(3) else 0
                if name.startswith("."):
                    continue
                symbols.append({"name": name, "addr": addr, "size": size})

    # 段汇总：
    #   Total RO  Size (Code + RO Data)   12345 ( 12.06kB)
    for line in text.split("\n"):
        m = re.match(r"\s*Total\s+(RO|RW|ROM)\s+Size\s*\([^)]*\)\s+(\d+)", line)
        if m:
            seg_map["Total_%s" % m.group(1)] = {"addr": 0, "size": int(m.group(2))}
        m2 = re.match(r"\s*Total\s+(RO|RW|ZI)\s+Size\s+(\d+)", line)
        if m2:
            seg_map["Total_%s" % m2.group(1)] = {"addr": 0, "size": int(m2.group(2))}
    return {"format": "keil", "segments": seg_map, "symbols": symbols, "mem_regions": []}


def _parse_map_iar(text):
    """解析 IAR .map 的 ENTRY LIST。

    形如：
        mpu6050_read            0x08004a2c   0x1c  Code  Gb  mpu6050.o [1]
    """
    symbols = []
    seg_map = {}
    in_entry = False
    for line in text.split("\n"):
        if "ENTRY LIST" in line:
            in_entry = True
            continue
        if in_entry and line.strip().startswith("***"):
            in_entry = False
        if in_entry:
            m = re.match(r"^\s*(\S+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\S+", line)
            if m:
                name = m.group(1)
                if name.startswith("."):
                    continue
                symbols.append({"name": name,
                                "addr": int(m.group(2), 16),
                                "size": int(m.group(3), 16)})
    # IAR 段汇总：  "  1234 bytes of readonly code memory"
    for line in text.split("\n"):
        m = re.match(r"\s*(\d+)\s+bytes\s+of\s+(readonly|readwrite)\s+(\S+)", line)
        if m:
            seg_map["%s_%s" % (m.group(2), m.group(3))] = {"addr": 0, "size": int(m.group(1))}
    return {"format": "iar", "segments": seg_map, "symbols": symbols, "mem_regions": []}


def _dup_check(symbols):
    """同一地址多符号是正常的（别名、段符号）；但同名不同址要提示。"""
    by_name = {}
    for s in symbols:
        by_name.setdefault(s["name"], set()).add(s["addr"])
    dupes = {k: v for k, v in by_name.items() if len(v) > 1}
    return dupes


_RUNTIME_MARKERS = (
    # 桌面/主机运行时符号——出现这些基本可断定不是去 MCU 上跑的镜像。
    # ★ 只收**明确属于 Windows / MSVC / MinGW** 的名字。
    #   反例（实测踩过）：`__do_global*` 看着像主机，其实 arm-none-eabi-gcc 的
    #   crtstuff 同样生成 __do_global_dtors_aux（证据：SENSOR_PWR_CTRL.elf 的
    #   符号表里就有，地址 0x08000250）。把它收进来，会让一份**正常的 STM32
    #   固件**被判成「疑似主机程序」——误报比漏报更伤，用户会去查错方向。
    "__mingw", "_pei386", "__lib64", "_head_lib64", "__dyn_tls", "_matherr",
    "__imp_", "_FindPESection", "GetLastError", "__acrt", "__xi_z",
    "libmsvcrt", "libkernel32", "Ldrp", "RtlInitUnicodeString",
)
#: 只列 **PE 专有**的段名（ELF 里不存在）。
#: `.debug_frame` 在 ARM ELF 里是常规段；`.pdata`/`.xdata` Keil 也会生成 ——
#: 这两个都不能当主机判据，否则正常固件会被误判。
_HOST_ONLY_SEGMENTS = (".idata", ".rsrc")


def _map_target_check(segs, syms):
    """判断这份 map 像不像「真的跑在 MCU 上的镜像」。

    ★ 只在证据充分时才报警——宁可不说，也不能把正常的 MCU map 误判成主机程序。
    返回 (level, reasons)；level ∈ {"host", "suspect", None}
    """
    reasons = []
    names = [s["name"] for s in syms]
    host_hits = [n for n in names if any(m in n for m in _RUNTIME_MARKERS)]
    seg_set = set(segs.keys())
    host_segs = [s for s in _HOST_ONLY_SEGMENTS if s in seg_set]

    if len(host_hits) >= 3 or (host_hits and len(host_segs) >= 3):
        if host_hits:
            reasons.append("出现主机运行时符号（如 %s）" % ", ".join(host_hits[:4]))
        if host_segs:
            reasons.append("含仅主机 PE 才有的段（%s）" % ", ".join(host_segs))
        return "host", reasons

    if host_hits or len(host_segs) >= 3:
        if host_hits:
            reasons.append("疑似主机运行时符号（%s）" % ", ".join(host_hits[:3]))
        if host_segs:
            reasons.append("含主机 PE 段（%s）" % ", ".join(host_segs))
        return "suspect", reasons

    return None, []


def parse_map(path, top_n=15):
    """解析 .map / .elf，输出段占用 / 栈深 / 符号表统计。

    top_n: 列出占用最大的前 N 个符号。
    ★ 支持 .elf：.symtab 带真实 st_size，比 .map 的「下一个符号 − 本符号」推断准。
    """
    if not path or not os.path.isfile(path):
        return "文件不存在：%r\n请给 .map 或 .elf 的绝对路径。" % (path,)

    if _is_elf(path):
        # ★ 二进制 ELF 直接走段表/符号表解析。
        #   以前这里用文本模式读，于是 355KB 的 .elf 被当成「认不出的 map」，
        #   还把二进制内容当「前几行」打给用户看 —— 而文档一直写着支持 .elf。
        parsed = _parse_elf(path)
        if parsed is None:
            return ("这是一个 ELF 文件，但段表/符号表解析失败——可能是被 strip 过的、"
                    "或不是标准 ELF。\n用 arm-none-eabi-readelf -S 确认，"
                    "或改用链接时生成的 .map 文件。")
        fmt = "elf"
    else:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            return "读取失败：%s" % e

        if not text.strip():
            return "文件是空的：%r" % path

        fmt = _sniff_map_format(text)
        if fmt is None:
            head = "\n".join(text.split("\n")[:8])
            return ("认不出这个 .map 的格式（不是 GNU ld / Keil / IAR 的已知样式）——"
                    "没有猜，避免给出错的段占用。\n前几行供你确认：\n%s" % head)

        parsed = {"gnu": _parse_map_gnu, "keil": _parse_map_keil, "iar": _parse_map_iar}[fmt](text)
    segs = parsed["segments"]
    syms = parsed["symbols"]

    if not segs and not syms:
        return ("识别为 %s 格式，但没解析出任何段或符号——可能是被裁剪过的 map。\n"
                "前几行：\n%s" % (fmt.upper(), "\n".join(text.split("\n")[:8])))

    lines = ["%s（格式：%s）" % (os.path.basename(path), parsed["format"].upper())]
    lines.append("")

    if segs:
        lines.append("段占用：")
        # 常见段按固定顺序排
        order = [".text", ".rodata", ".data", ".bss", ".heap", ".stack", ".init", ".fini"]
        def key(s):
            i = order.index(s) if s in order else 99
            return (i, s)
        for name in sorted(segs, key=key):
            info = segs[name]
            lines.append("  %-12s %8d 字节（%s）" % (name, info["size"], _human_size(info["size"])))
        total = sum(int(v["size"]) for v in segs.values())
        lines.append("  %s" % ("-" * 40))
        lines.append("  合计 %d 字节（%s）" % (total, _human_size(total)))
    else:
        lines.append("段占用：（未解析出——该 map 可能是符号表裁剪版）")
    lines.append("")

    if syms:
        lines.append("符号表：%d 个" % len(syms))

        # ★ 目标平台自检：先确认这不是一份主机程序（x86 PE/ELF）的 map
        level, reasons = _map_target_check(segs, syms)
        if level == "host":
            lines.append("")
            lines.append("★【重要提醒】这份 map 看起来不是 MCU 固件的，而是主机（PC）程序：")
            for r in reasons:
                lines.append("    · %s" % r)
            lines.append("  → 符号地址/段布局都与目标 MCU 无关，下面的「最大符号」多半是"
                         "运行时库函数，对排查固件问题没有意义。")
            lines.append("  → 请确认你传的是交叉编译器（arm-none-eabi-gcc / Keil / IAR）"
                         "给目标板链接时生成的 .map。")
        elif level == "suspect":
            lines.append("  △ 提示：这份 map 含一些主机程序特征（%s），"
                         "若本意是查 MCU 固件，请确认 map 来源。" % "；".join(reasons))

        # 最大符号（近似栈用量参考）
        with_size = [s for s in syms if s["size"] > 0]
        if with_size:
            inferred_any = any(s.get("_inferred") for s in with_size)
            # ★ 排序：应用符号优先于运行时/库符号。
            #   排查「大局部数组撑爆栈」时，用户要看自己的 buf，不是 libc 的符号。
            def _rank(s):
                n = s["name"]
                is_rt = any(m in n for m in _RUNTIME_MARKERS)
                is_lib = n.startswith("__") or n.startswith("_imp")
                return (1 if is_rt else (0.5 if is_lib else 0), -s["size"])
            ordered = sorted(with_size, key=_rank)[:top_n]
            lines.append("  占用最大的 %d 个符号（查大局部数组/大缓冲；应用符号优先）：" % len(ordered))
            for s in ordered:
                mark = "（推断）" if s.get("_inferred") else ""
                tag = ""
                if any(m in s["name"] for m in _RUNTIME_MARKERS) or s["name"].startswith("__"):
                    tag = "  ← 运行时/库符号，通常与你的 bug 无关"
                lines.append("    %8d 字节%s  %s%s" % (s["size"], mark, s["name"], tag))
            if inferred_any:
                lines.append("    注：标「推断」的大小 = 下一个符号地址 − 本符号地址。"
                             "GNU ld 的 map 不记符号大小，这是可达的最佳近似；"
                             "若符号间有对齐全零填充，推断值会【偏大】。")

        # 栈深参考：_Min_Stack_Size / Stack_Size 符号
        stack_hits = [s for s in syms
                      if any(k in s["name"].lower()
                             for k in ("min_stack", "stack_size", "_stack", "heap_size"))]
        if stack_hits:
            lines.append("  栈/堆相关符号：")
            for s in stack_hits[:10]:
                lines.append("    %s = %s" % (s["name"], _human_size(s["size"]) if s["size"] else hex(s["addr"])))

        dupes = _dup_check(syms)
        if dupes:
            lines.append("  △ 同名多地址符号 %d 个（同名不同址，反查会有二义性）：" % len(dupes))
            for k in list(dupes)[:5]:
                lines.append("    %s → %s" % (k, ", ".join(hex(a) for a in sorted(dupes[k]))))

        # 地址范围，供反查边界判断
        addrs = [s["addr"] for s in syms if s["addr"]]
        if addrs:
            lines.append("  地址范围：%s ~ %s" % (hex(min(addrs)), hex(max(addrs))))
    else:
        lines.append("符号表：（未解析出符号——没有符号表就无法反查地址，"
                     "检查是不是把 map 裁掉了）")

    if parsed.get("mem_regions"):
        lines.append("")
        lines.append("内存区域：")
        for r in parsed["mem_regions"][:8]:
            lines.append("  %s" % r)

    lines.append("")
    lines.append("下一步：用 symbol_lookup 反查 fault 地址属于哪个函数（传同一个 map 路径）。")
    return "\n".join(lines)


def _human_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1024 * 1024:
        return "%.2f MB" % (n / 1024.0 / 1024.0)
    if n >= 1024:
        return "%.2f KB" % (n / 1024.0)
    return "%d B" % n


def _load_map_full(path):
    """加载并缓存 map 的完整解析结果（symbols + segments），供反查/回溯复用。"""
    if not path or not os.path.isfile(path):
        return None
    key = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(key)
    except OSError:
        return None
    cached = _MAP_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    if _is_elf(key):
        # .elf：符号带真实 st_size（.map 只能推断），反查/回溯的边界判断才准
        parsed = _parse_elf(key)
        _MAP_CACHE[key] = (mtime, parsed)
        return parsed
    try:
        with open(key, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return None
    fmt = _sniff_map_format(text)
    if fmt is None:
        return None
    parsed = {"gnu": _parse_map_gnu, "keil": _parse_map_keil, "iar": _parse_map_iar}[fmt](text)
    _MAP_CACHE[key] = (mtime, parsed)
    return parsed


def _load_map_symbols(path):
    """加载并缓存 map 的符号表，供 symbol_lookup / stack_unwind 复用。"""
    parsed = _load_map_full(path)
    if parsed is None:
        return None
    return parsed["symbols"]


def _build_bounds(path, uniq, addrs_sorted):
    """为每个地址基点算「符号地址上界」，供反查时判断地址是否越界。

    ★ 这是防「0xdeadbeef → app_state+0xbeadb817 ✓」这类假答案的核心：
      size=0 不代表任意大偏移都算"在这个符号里"。
    规则：有 size 用 base+size；无 size 用下一个符号地址；段尾符号用所在段末。
    返回 {base_addr: upper_addr_or_None}。
    """
    bounds = {}
    for i, a in enumerate(addrs_sorted):
        cands = uniq[a]
        sz = max((s.get("size") or 0) for s in cands)
        if sz:
            bounds[a] = a + sz
            continue
        nxt = addrs_sorted[i + 1] if i + 1 < len(addrs_sorted) else None
        bounds[a] = nxt      # None = 段尾且无下一符号，靠段末兜底
    # 段末兜底：段尾符号没有下一符号时，用所属段的结束地址
    seg_ends = []
    parsed = _load_map_full(path)
    if parsed:
        for _nm, info in (parsed.get("segments") or {}).items():
            if info.get("addr") is not None and info.get("size"):
                seg_ends.append(info["addr"] + info["size"])
        seg_ends.sort()
    for a, b in list(bounds.items()):
        if b is None:
            for e in seg_ends:
                if e > a:
                    bounds[a] = e
                    break
    return bounds


def symbol_lookup(path, addresses):
    """把地址反查成「函数名+偏移」。

    path: .map 文件路径；addresses: 地址列表（int 或 "0x..." 字符串）
    ★ 找不到符号时明确说"未找到"，不猜函数名——猜出来的名字比没名字更害人。
    """
    syms = _load_map_symbols(path)
    if syms is None:
        return ("无法读取符号表：%r\n"
                "（文件不存在 / 格式认不出 / 没有符号）。\n"
                "请给 .map 或 .elf 的路径，且该文件含完整符号表。" % (path,))

    if not addresses:
        return "没给地址。用法：addresses=['0x08004a2c'] 或 [0x8004a2c, '0x8005120']"

    if not syms:
        return "该 map 里解析到的符号数为 0——无法反查。检查 map 是否被裁剪。"

    # 解析地址
    want = []
    for a in addresses:
        n = _parse_int(a)
        if n is None:
            want.append((a, None))
        else:
            want.append((a, n))

    # 只保留有大小或可作边界判断的符号（去重同址）
    uniq = {}
    for s in syms:
        if s["addr"] and not s["name"].startswith("."):
            uniq.setdefault(s["addr"], []).append(s)
    addrs_sorted = sorted(uniq.keys())

    lines = ["符号反查（%d 个地址，符号表 %d 个条目）" % (len(want), len(syms))]
    lines.append("")

    import bisect
    # 每个符号的「隐式上界」——★与 stack_unwind 共用 _build_bounds，判定口径一致
    upper = _build_bounds(path, uniq, addrs_sorted)

    def _bound(base):
        """返回 base 处符号的地址上界；None 表示无法确定。"""
        return upper.get(base)

    # ★ 无 size 且无上界时，这个绝对阈值用于拦截明显荒谬的偏移。
    #   MCU 上一个函数不可能大到 1MB；超出即「不在这个符号里」。
    ABS_LIMIT = 0x100000

    for raw, n in want:
        if n is None:
            lines.append("  %-14s ✗ 无法解析为整数地址" % str(raw))
            continue
        if not addrs_sorted:
            # 2026-10-13：符号表地址全为 0（未链接 map / 被 strip）时
            # addrs_sorted 为空，下面 addrs_sorted[0] 会 IndexError。
            lines.append("  %-14s ✗ 符号表为空或地址全为 0"
                         "（map 未参与链接或已被 strip），无法定位" % str(raw))
            continue
        # 找 <= 该地址的最近符号
        i = bisect.bisect_right(addrs_sorted, n) - 1
        if i < 0:
            lines.append("  %-14s ✗ 未找到符号（该地址低于符号表最小地址 %s，"
                         "可能是向量表/启动区）" % (hex(n), hex(addrs_sorted[0])))
            continue
        base = addrs_sorted[i]
        cands = uniq[base]
        # 优先带 size 的符号
        cs = sorted(cands, key=lambda s: (0 if s["size"] else 1))
        s = cs[0]
        off = n - base
        bnd = _bound(base)

        if s["size"] and off >= s["size"]:
            # 越出该符号范围——报出但标注"可能在下一符号之前"
            nxt = addrs_sorted[i + 1] if i + 1 < len(addrs_sorted) else None
            lines.append("  %-14s △ %s+%s" % (hex(n), s["name"], hex(off)))
            lines.append("  %s    但偏移 %s 已超出该符号大小 %s"
                         % (" " * 14, hex(off), hex(s["size"])))
            if nxt:
                lines.append("  %s    （下一个符号 %s 在 %s）"
                             % (" " * 14, uniq[nxt][0]["name"], hex(nxt)))
            else:
                lines.append("  %s    （其后没有已知符号——可能是数据区或未收录的函数）" % (" " * 14))
        elif bnd is not None and n >= bnd:
            # ★地址落在该符号上界之外 → 不属于这个符号
            lines.append("  %-14s ✗ 不属于 %s（地址已越出该符号范围 %s ~ %s）"
                         % (hex(n), s["name"], hex(base), hex(bnd)))
            lines.append("  %s    → 这个地址落在符号表的空隙里：可能是未导出函数、"
                         "内联代码、或地址本身是错的（野指针/垃圾值）。"
                         % (" " * 14))
            lines.append("  %s    → 别硬套函数名，会得到假结论。" % (" " * 14))
        elif bnd is None and off > ABS_LIMIT:
            # ★无上界 + 偏移荒谬 → 拒绝给名，不猜
            lines.append("  %-14s ✗ 未找到可信符号（最近的前置符号 %s 在 %s，"
                         "但偏移 %s 大得不合理）"
                         % (hex(n), s["name"], hex(base), hex(off)))
            lines.append("  %s    → 该地址很可能根本不在任何函数里（野指针/未初始化值）。"
                         % (" " * 14))
        else:
            suffix = ("（偏移 %s）" % hex(off)) if off else "（正好是函数入口）"
            mark = "✓"
            note = ""
            if not s["size"]:
                mark = "△"
                note = "  ← 该符号无大小记录，偏移仅为估计"
            lines.append("  %-14s %s %s%s%s" % (hex(n), mark, s["name"], suffix, note))
            if len(cs) > 1:
                others = [x["name"] for x in cs[1:4]]
                lines.append("  %s同址还有：%s" % (" " * 14, ", ".join(others)))

    lines.append("")
    lines.append("说明：符号名来自 map 的符号表。带 size 的符号可判断是否越界；"
                 "无 size 的符号只给了基点，偏移可能不准。")
    return "\n".join(lines)


def stack_unwind(path, frame=None, exc_return=None, sp=None):
    """栈回溯：把 fault 时压栈的 8 个字还原成调用链。

    frame: 8 个字，按硬件压栈顺序 [R0,R1,R2,R3,R12,LR,PC,xPSR]，int 或 0x 字符串。
    exc_return: LR 的值（可选，用于判断栈帧类型与栈位置）
    sp: 栈指针当前值（可选，用于提示是哪套栈）
    """
    syms = _load_map_symbols(path)
    if syms is None:
        return ("无法读取符号表：%r\n先确认 .map 路径和格式。" % (path,))

    # 解析 exc_return，判断扩展帧与栈位置
    er = _parse_int(exc_return) if exc_return is not None else None
    notes = []
    if er is not None:
        if er & 0x10:
            notes.append("EXC_RETURN bit4=1 → 标准栈帧（8 字，无 FPU 惰性压栈）")
        else:
            notes.append("EXC_RETURN bit4=0 → ★扩展栈帧（含 FPU 的 S0-S15 + FPSCR，额外 18 字）。"
                         "注意栈里的字序与标准帧不同，本工具按标准帧解析，"
                         "PC 的位置需要按扩展帧修正。")
        # ★ bit2 / bit3 的含义（2026-09-22 真机实测纠错，之前写反了）：
        #     bit2 = SPSEL —— 0 = MSP，1 = PSP。**决定去哪取栈帧**。
        #     bit3 = Mode  —— 0 = Handler（嵌套异常里出的错），1 = Thread。
        #   这两位一度对调：工具曾输出「bit3=1 → ★异常前用 PSP」。而真机实测
        #   （STM32H563ZI / Cortex-M33，非法地址读 → HardFault）拿到
        #   EXC_RETURN = 0xFFFFFFB8，压栈帧实际在 MSP = 0x2009FFC8；此时 PSP = 0。
        #   照工具提示去 PSP 取，会拿到一整帧的 0 —— 寄存器看起来"合理"却全假。
        #   线下测试抓不到，因为 exc_return 是编的；只有真机对照才暴露。
        if er & 0x4:
            notes.append("EXC_RETURN bit2=1 → ★异常前用 PSP（RTOS 任务栈）。"
                         "栈帧在任务的栈里，不在 MSP。取栈内容时别拿错栈指针。")
        else:
            notes.append("EXC_RETURN bit2=0 → 异常前用 MSP（主栈）")
        if er & 0x8:
            notes.append("EXC_RETURN bit3=1 → 异常前在 Thread 模式（普通代码里出的错）")
        else:
            notes.append("EXC_RETURN bit3=0 → ★异常前在 Handler 模式"
                         "（错出在嵌套中断里，不是主流程）")

    if not frame:
        msg = ["栈回溯：没给栈帧内容。",
               "用法：把 fault 时压栈的 8 个字按顺序传进来——",
               "  frame=[R0, R1, R2, R3, R12, LR, PC, xPSR]",
               "这些值在 fault handler 里可以从栈指针指向的内存读出（8 个字）。"]
        if sp is not None:
            msg.append("你给的 SP = %s" % (hex(_parse_int(sp) or 0),))
        if notes:
            msg.append("")
            msg.append("来自 exc_return 的信息：")
            for n in notes:
                msg.append("  · %s" % n)
        return "\n".join(msg)

    if not isinstance(frame, (list, tuple)):
        return "frame 需要是列表，形如 [R0,R1,R2,R3,R12,LR,PC,xPSR]，收到 %s" % type(frame).__name__
    if len(frame) < 8:
        return ("frame 需要 8 个字（R0,R1,R2,R3,R12,LR,PC,xPSR），只收到 %d 个。\n"
                "少了 PC 就没法定位出错点。完整帧：%r" % (len(frame), frame))

    vals = []
    names = ["R0", "R1", "R2", "R3", "R12", "LR", "PC", "xPSR"]
    for nm, raw in zip(names, frame):
        n = _parse_int(raw)
        if n is None:
            return "%s 无法解析为整数：%r" % (nm, raw)
        vals.append(n)
    f = dict(zip(names, vals))
    pc = f["PC"]
    lr = f["LR"]

    # 为反查准备符号表
    uniq = {}
    for s in syms:
        if s["addr"] and not s["name"].startswith("."):
            uniq.setdefault(s["addr"], []).append(s)
    addrs_sorted = sorted(uniq.keys())

    # ★ 每个符号的地址上界（有 size 用 size；无 size 用下一符号地址；段尾用段末）。
    #   与 symbol_lookup 共用同一套判定，避免"一边拦假答案、另一边照样放行"。
    _bnds = _build_bounds(path, uniq, addrs_sorted)
    _ABS_LIMIT = 0x100000

    def _resolve(addr):
        import bisect
        i = bisect.bisect_right(addrs_sorted, addr) - 1
        if i < 0:
            return None, None, "none"
        base = addrs_sorted[i]
        cs = sorted(uniq[base], key=lambda s: (0 if s["size"] else 1))
        s = cs[0]
        off = addr - base
        bnd = _bnds.get(base)
        if s["size"] and off >= s["size"]:
            return s["name"], off, "overrun"
        if bnd is not None and addr >= bnd:
            return None, off, "gap"          # ★落在符号表的空隙里 → 拒绝给名
        if bnd is None and off > _ABS_LIMIT:
            return None, off, "gap"          # ★无上界且偏移荒谬 → 拒绝给名
        return s["name"], off, ("exact" if off == 0 else "offset")

    lines = ["栈回溯（fault 现场 8 字栈帧）"]
    lines.append("")
    lines.append("寄存器：")
    for nm in names:
        lines.append("  %-5s = %s" % (nm, hex(f[nm])))
    if notes:
        lines.append("")
        lines.append("exc_return 分析：")
        for n in notes:
            lines.append("  · %s" % n)

    lines.append("")
    lines.append("★出错点（PC 反查）：")
    name, off, kind = _resolve(pc)
    if kind in ("none", "gap"):
        lines.append("  PC = %s → ✗ 未找到可信符号。" % hex(pc))
        if kind == "gap":
            lines.append("  它落在符号表的空隙里（比最近的前置符号越界很远），"
                         "或本身是垃圾值。")
        lines.append("  这说明 PC 落在【没有任何符号的地方】——典型的「跑飞」：")
        lines.append("    程序跳到了非代码区（数据区/非法地址/已擦除的 flash），"
                     "或 PC 本身被写坏。")
        lines.append("  下一步查：函数指针是否为空/被写坏、跳转表是否越界、")
        lines.append("          栈是否被踩坏导致返回地址错误。")
    elif kind == "overrun":
        lines.append("  PC = %s → %s+%s（△ 偏移已超出该符号大小）"
                     % (hex(pc), name, hex(off)))
        lines.append("  地址在符号区间之外——可能是内联代码，或地址本身就不可靠。")
    else:
        lines.append("  PC = %s → %s+%s" % (hex(pc), name, hex(off)))
        if kind == "exact":
            lines.append("  （正好在函数入口——可能是刚进入该函数就出错）")
        lines.append("  出错函数：%s" % name)

    lines.append("")
    lines.append("★调用链（LR 反查，反映「被谁调用」）：")
    name2, off2, kind2 = _resolve(lr)
    if kind2 in ("none", "gap"):
        lines.append("  LR = %s → ✗ 未找到可信符号"
                     "（返回地址也是野的，栈很可能已被踩坏）" % hex(lr))
    elif kind2 == "overrun":
        lines.append("  LR = %s → %s+%s（△ 偏移超出符号范围，可信度低）"
                     % (hex(lr), name2, hex(off2)))
    else:
        lines.append("  LR = %s → %s+%s" % (hex(lr), name2, hex(off2)))
        lines.append("  即：%s 调用了出错点所在的函数。" % name2)
        lines.append("  注意：LR 只反映【一层】返回地址，硬件栈帧里没有完整调用链。"
                     "要看多层调用链，需沿栈逐帧展开（需知道帧大小，不同编译器不同，"
                     "本工具不做自动展开，避免给出错的链）。")

    lines.append("")
    lines.append("xPSR = %s" % hex(f["xPSR"]))
    ipsr = f["xPSR"] & 0x1FF
    lines.append("  IPSR（当前异常号）= %d" % ipsr)
    if ipsr == 3:
        lines.append("  → 处于 HardFault 处理中（异常号 3）")
    elif ipsr == 4:
        lines.append("  → 处于 MemManage 处理中（异常号 4）")
    elif ipsr == 5:
        lines.append("  → 处于 BusFault 处理中（异常号 5）")
    elif ipsr == 6:
        lines.append("  → 处于 UsageFault 处理中（异常号 6）")
    elif ipsr == 0:
        lines.append("  → 线程模式（不是异常处理中）")
    else:
        lines.append("  → 异常号 %d" % ipsr)
    t_bit = (f["xPSR"] >> 24) & 1
    lines.append("  T 位（Thumb 状态）= %d%s" % (t_bit, "" if t_bit else " ★为 0 说明状态异常"))

    lines.append("")
    lines.append("下一步：")
    lines.append("  1. 若 PC 落在合法函数 → 结合 decode_fault 的故障类型判断根因")
    lines.append("  2. 若 PC 未找到符号 → 重点查栈溢出（用 parse_map 看大符号）与函数指针")
    lines.append("  3. 若 CFSR 有 STKERR/UNSTKERR → 栈问题已确认，查栈大小配置")
    return "\n".join(lines)


# ---------------------------------------------------------------
# 环境自检（N-04）：一台陌生机器上「这里到底能干什么」，一次问清
# ---------------------------------------------------------------

def _pkg_version():
    """本包版本：安装元数据 > 包 ``__init__.__version__``。

    2026-10-13 C 档重构后版本单一来源是 ``mcu_debug_mcp/__init__.py``，
    pyproject 用 dynamic attr 从那读（源码树里 pyproject 不再写死版本，
    也不再与模块同目录 —— 旧回退「读同目录 pyproject.toml」两个前提都塌了）。
    """
    try:
        import importlib.metadata as md
        return md.version("mcu-debug-mcp")
    except Exception:
        pass
    try:
        from mcu_debug_mcp import __version__ as v
        return v
    except Exception:
        return "未知"


def _dist_version(pkg):
    """已安装分发的版本号；取不到返回空串（不编一个假版本）。"""
    try:
        import importlib.metadata as md
        return md.version(pkg)
    except Exception:
        return ""


def env_status():
    """环境自检：依赖 / 串口 / 烧录器 / 知识库根 / 版本，一次问清。

    为什么需要它：拿到一台陌生机器时，第一件事是「这里到底能干什么」。
    没有这个工具，只能一个个试错，而且每种失败原因长得都不一样。

    ★ 只报**实测**，不报「应该没问题」：能探的探，探不了的就明说为什么——
      例如 Windows 没有 Unix 权限模型，就不假装能替人判断串口能不能打开。
    """
    lines = ["mcu-debug-mcp 环境自检", "-" * 48]
    lines.append("版本         : %s" % _pkg_version())
    lines.append("Python       : %s" % os.sys.version.split()[0])
    lines.append("解释器       : %s" % os.sys.executable)
    lines.append("代码目录     : %s" % paths.pkg_dir())
    lines.append("")

    lines.append("隔离 / 安全")
    lines.append(guard.describe())
    lines.append("")

    lines.append("依赖")
    if HAS_PYSERIAL:
        lines.append("  pyserial   : ✓ %s —— 串口读写 / 会话日志 / 波特率探测可用"
                     % (_dist_version("pyserial") or "已加载"))
    else:
        lines.append("  pyserial   : ✗ 未安装 —— 串口类工具**全部不可用**（pip install pyserial）")
    if HAS_PYOCD:
        lines.append("  pyocd      : ✓ %s —— 探针 / RTT 可用" % (_dist_version("pyocd") or "已加载"))
    else:
        lines.append("  pyocd      : ✗ 未安装 —— 探针 / RTT 不可用（pip install pyocd）")
    lines.append("")

    lines.append("串口")
    if not HAS_PYSERIAL:
        lines.append("  —— pyserial 未安装，无法枚举串口")
    else:
        ports = list(serial.tools.list_ports.comports())
        lines.append("  已发现     : %d 个" % len(ports))
        for p in ports[:8]:
            vid = ("%04X:%04X" % (p.vid, p.pid)) if (p.vid is not None and p.pid is not None) \
                else "VID:PID 未知"
            lines.append("    · %-12s %s（%s）" % (p.device, p.description or "未知设备", vid))
        if len(ports) > 8:
            lines.append("    …… 共 %d 个" % len(ports))
        if os.name != "nt":
            blocked = [p.device for p in ports
                       if not os.access(p.device, os.R_OK | os.W_OK)]
            if blocked:
                lines.append("  读写权限   : ★ 下列口不可读写：%s" % "、".join(blocked))
                lines.append("               多半是没进 dialout 组：sudo usermod -aG dialout $USER（重登生效）")
            else:
                lines.append("  读写权限   : 全部可读写")
        else:
            lines.append("  读写权限   : Windows 无 Unix 权限模型；端口被别的软件占用时，"
                         "serial_open 会明确报「被占用」并给排查步骤")
    lines.append("")

    lines.append("烧录器 / 探针")
    found_tools = flash_tool_detect()
    if found_tools:
        lines.append("  已发现     : %d 个" % len(found_tools))
        for tname, tpath in sorted(found_tools.items()):
            lines.append("    · %-12s %s" % (tname, tpath))
    else:
        lines.append("  已发现     : 无 —— 烧录类工具会给安装指引")
        lines.append("                pip install esptool / pip install pyocd / 装 stlink")
    lines.append("")

    lines.append("知识库 / 驱动库")
    lines.append(paths.describe())
    pin = pinmap_list()
    brd = board_list()
    drv = [n for n, _p in paths.layer_dirs("drivers")]
    tabs = _errcode_tables()
    n_codes = 0
    for _name, _p in tabs.items():
        try:
            with open(_p, encoding="utf-8") as fh:
                n_codes += len(json.load(fh).get("entries") or {})
        except Exception:
            pass
    lines.append("  引脚库     : %d 款  %s" % (len(pin), "、".join(pin) or "（空）"))
    lines.append("  开发板     : %d 款  %s" % (len(brd), "、".join(brd) or "（空）"))
    lines.append("  驱动库     : %d 款  %s" % (len(drv), "、".join(drv) or "（空）"))
    lines.append("  错误码表   : %d 张，共 %d 条  %s"
                 % (len(tabs), n_codes, "、".join(sorted(tabs)) or "（空）"))
    lines.append("  故障位域表 : %d 张  %s"
                 % (len(fault_bits_list()), "、".join(fault_bits_list()) or "（空）"))
    lines.append("  故障模式   : %d 条（内置 %d + 外置 %d）"
                 % (analyzer.rule_count(),
                    len(getattr(analyzer, "RULES", [])),
                    len(getattr(analyzer, "_EXTRA_RULES", []))))
    if not (pin and brd and drv):
        lines.append("  ⚠ 有知识库是空的。若是 pip 安装，多半是数据目录没随包分发——")
        lines.append("    检查装出来的包里有没有 knowledge/ 与 drivers/（见 PLAN.md §2 / N-01）。")
    lines.append("")

    lines.append("会话日志")
    logs = _logs_dir()
    n_log, size = 0, 0
    if os.path.isdir(logs):
        for f in glob.glob(os.path.join(logs, "*.log")):
            n_log += 1
            try:
                size += os.path.getsize(f)
            except OSError:
                pass
    lines.append("  目录       : %s" % logs)
    lines.append("  已有       : %d 个，共 %.1f KB" % (n_log, size / 1024.0))
    lines.append("  保留策略   : 默认保留最近 %d 天 / 最新 %d 个（logs_prune 可调）"
                 % (_LOGS_KEEP_DAYS, _LOGS_KEEP_COUNT))
    if n_log > _LOGS_KEEP_COUNT or size > 50 * 1024 * 1024:
        lines.append("  ⓘ 已超出默认保留量：logs_prune（默认 dry-run）会列出可清理项，")
        lines.append("    真删需 dry_run=False + confirm=True（不可逆操作）。")
    else:
        lines.append("  ⓘ 清理入口：logs_prune（默认 dry-run，只列不删）")
    lines.append("  ⓘ 日志原样记录收发内容，可能含 WiFi 密码 / token 等凭据——分享前请检查。")
    return "\n".join(lines)
