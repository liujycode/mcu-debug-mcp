# -*- coding: utf-8 -*-
"""mcu-debug-mcp · 运行时隔离层（guard.py）

目的只有一个：**在没有任何明确授权时，绝不碰真实硬件。**

为什么需要它（不是过度设计）：

    一台开发机上往往同时挂着**正在使用**的设备。本项目的若干工具会真的对端口动手：

      · serial_open           打开端口就会翻转 DTR/RTS —— ESP32/STM32 常把这两根线
                              接成自动复位电路，于是"我只想看一眼"变成了"设备重启了"
      · baudrate_autodetect   逐档打开端口，并写 "\\r\\n" 去"捅一下"让设备开口
      · at_sequence           向端口写真实 AT 指令
      · serial_send           向端口写任意字节
      · serial_pair_record    同时占住两个端口
      · flash_firmware        dry_run=False 时**真的执行烧录**，覆盖 flash 内容

    一次误调用就可能打断正在进行的调试会话、把设备打进 bootloader、或覆盖固件。
    所以默认档位是 **readonly**：读得到、看得见，但不许写。

三档（环境变量 ``MCU_DEBUG_MCP_MODE``）：

    readonly（默认）  只允许枚举 / 读取。一切写操作拒绝，并给出怎么放开的说明。
    write             允许写（串口写 / AT / 波特率探测 / 双口对录）；
                      **不可逆**操作（烧录）仍额外要求 ``confirm=True``。
    off               全部放行（CI 与真机夹具用；日常不建议）。

端口名单（与档位相乘的第二条防线）：

    MCU_DEBUG_MCP_PORT_DENY  = "COM6,COM28"   这些口一律不许打开 —— 放你自己在用的设备
    MCU_DEBUG_MCP_PORT_ALLOW = "COM12"        非空时进入白名单模式：只有列出的口能开
    两者都支持 ``*`` 通配。**DENY 优先于 ALLOW**（否认比许可更强）。

「不打扰」开关：

    MCU_DEBUG_MCP_NO_TOUCH_DTR = "1"  打开端口前先把 DTR/RTS 置为不生效，
                                      尽量不触发开发板的自动复位电路。
                                      （调用契约已离线验证；真机复位行为待板卡验证。）

设计原则（与本项目其它部分一致）：

    1. 拒绝时必须说清「为什么拒 + 具体风险 + 怎么放开」——不能只甩一句"权限不足"；
    2. 检查失败一律**保守**：环境变量写成不认识的值 ⟹ 按 readonly 处理，不静默放行；
    3. 只依赖标准库，不 import core / analyzer（是 core 反过来 import 本模块）。
"""

import fnmatch
import os

# ---------------------------------------------------------------
# 档位
# ---------------------------------------------------------------

MODE_ENV = "MCU_DEBUG_MCP_MODE"
DENY_ENV = "MCU_DEBUG_MCP_PORT_DENY"
ALLOW_ENV = "MCU_DEBUG_MCP_PORT_ALLOW"
DTR_ENV = "MCU_DEBUG_MCP_NO_TOUCH_DTR"

MODE_READONLY = "readonly"
MODE_WRITE = "write"
MODE_OFF = "off"

VALID_MODES = (MODE_READONLY, MODE_WRITE, MODE_OFF)
DEFAULT_MODE = MODE_READONLY

#: 环境变量写错时的告警（供 describe / env_status 显示），进程内累积
_WARNINGS = []


def _raw_mode():
    return (os.environ.get(MODE_ENV) or "").strip().lower()


def mode():
    """当前档位。未知取值 ⟹ 按 readonly（保守，不静默放行）。"""
    raw = _raw_mode()
    if raw in VALID_MODES:
        return raw
    return DEFAULT_MODE


def mode_source():
    """档位来源描述（给自检用，别让用户猜当前是"默认"还是"他设过"）。"""
    raw = _raw_mode()
    if not raw:
        return "默认（未设 %s）" % MODE_ENV
    if raw in VALID_MODES:
        return "%s=%s" % (MODE_ENV, raw)
    return "★ %s=%r 不是合法档位（合法：%s）——已按保守档 %s 处理" % (
        MODE_ENV, raw, "/".join(VALID_MODES), MODE_READONLY)


def mode_banner():
    """一行话说明当前能不能写硬件（给工具返回信息用）。"""
    m = mode()
    if m == MODE_OFF:
        return "隔离档位 off：不做任何检查（自行承担风险）"
    if m == MODE_WRITE:
        return "隔离档位 write：可写硬件；烧录这类不可逆操作仍需 confirm=True"
    return "隔离档位 readonly（默认）：只允许读；写操作会被拒绝"


def _port_patterns(env_name):
    raw = os.environ.get(env_name) or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def deny_ports():
    return _port_patterns(DENY_ENV)


def allow_ports():
    return _port_patterns(ALLOW_ENV)


def suppress_dtr():
    """是否在打开端口前把 DTR/RTS 置为不生效（避免开发板自动复位）。"""
    return (os.environ.get(DTR_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _port_match(port, pattern):
    return fnmatch.fnmatch((port or "").upper(), pattern.upper())


#: 纯软件端口：pyserial 自造的**进程内回环**，写进去的字节只回到自己的读缓冲，
#: 不碰设备、不占真实端口、没有任何外部副作用。所以它不适用档位与端口名单——
#: 否则测试用的 loop:// 会被自己的隔离层挡住，只能被迫做「豁免测试」这种坏设计。
#:
#: ★只豁免 loop://，别的 URL 形态一律不豁免：
#:   socket:// 连的是网络串口（远端可能是真设备）
#:   spy:// / alt:// 是对**真实端口**的包装
#:   hwgrep:// 会去枚举真实端口
#:   把它们放进来等于给隔离层开后门。
VIRTUAL_PORT_PREFIXES = ("loop://",)


def is_virtual_port(port):
    """是否是**无外部副作用**的纯软件端口（当前仅 pyserial 的 loop://）。"""
    p = (port or "").strip().lower()
    return any(p.startswith(v) for v in VIRTUAL_PORT_PREFIXES)


def port_verdict(port):
    """端口名单检查：返回 (ok, 文案)。ok=False 时文案说明被哪条规则挡住。"""
    if not port:
        # 没有端口名（例如离线分析类调用）——不在本函数职责内，交给档位判断
        return True, ""
    for pat in deny_ports():
        if _port_match(port, pat):
            return False, (
                "端口 %s 命中你设置的【禁用名单】(%s=%s)。\n"
                "  这是你自己加的硬保护（例如该口上是正在用的设备），本工具不会绕过它。\n"
                "  确实要操作：改 %s，或在工具里换一个端口。"
                % (port, DENY_ENV, ",".join(deny_ports()), DENY_ENV))
    allow = allow_ports()
    if allow and not any(_port_match(port, p) for p in allow):
        return False, (
            "端口 %s 不在【白名单】内（%s=%s），当前是白名单模式：只有列出的口能打开。\n"
            "  确实要操作：把 %s 加进 %s，或清空该变量退出白名单模式。"
            % (port, ALLOW_ENV, ",".join(allow), port, ALLOW_ENV))
    return True, ""


# ---------------------------------------------------------------
# 操作矩阵
# ---------------------------------------------------------------
# needs: "read"  ⟹ readonly 档也允许（只读/只枚举，不动设备状态）
#        "write" ⟹ 需要 write 或 off 档
# irreversible: True ⟹ 即使 write 档，也必须 confirm=True（不可逆操作二次确认）

OPS = {
    "serial_open": {
        "title": "打开串口",
        "needs": "read",
        "irreversible": False,
        "why": ("打开串口会翻转 DTR/RTS 控制线。ESP32/STM32 等开发板常把这两根线接成自动复位电路，"
                "于是「只想看一眼」会变成「设备重启了」；该口若被别的软件占用，还会抢断它的数据。"),
        "how": ("只枚举端口用 serial_list / env_status（不打开，不影响设备）。"
                "真要打开：本操作在只读档也允许，但请先确认设备状态；"
                "想尽量不触发复位，设 %s=1。" % DTR_ENV),
    },
    "serial_write": {
        "title": "向串口写字节",
        "needs": "write",
        "irreversible": False,
        "why": ("写出去的字节会真正发到设备上，可能触发复位、进入 bootloader、"
                "改变运行状态，或让正在跑的固件把命令当成数据。"),
        "how": "确认设备允许接收命令后再放开写权限。",
    },
    "at_sequence": {
        "title": "批量执行 AT 指令",
        "needs": "write",
        "irreversible": False,
        "why": ("会向设备写真实 AT 指令（可能改配置、重启、进入透传），"
                "不是只读回显。某些指令（如 AT+RST / AT+CWMODE）会改变设备状态。"),
        "how": "确认模组当前空闲、且你接受它被改配置后再放开写权限。",
    },
    "baud_probe": {
        "title": "波特率扫描（逐档打开端口并写探测字节）",
        "needs": "write",
        "irreversible": False,
        "why": ("会对同一端口反复打开/关闭共十余次，每次写一个 \"\\r\\n\" 去「捅一下」设备；"
                "这个过程本身就会扰动正在通信的设备（还可能因反复开合触发多次复位）。"),
        "how": ("只要分析现成日志（离线）不碰硬件：传 log_text 参数走离线分支即可，本档位不禁。"
                "要真扫端口再放开写权限。"),
    },
    "pair_record": {
        "title": "双口对录",
        "needs": "write",
        "irreversible": False,
        "why": "会同时占用两个端口并持续读秒级，期间别的软件打不开它们。",
        "how": "确认两个口都空闲后再放开写权限。",
    },
    "flash_firmware": {
        "title": "烧录固件（不可逆）",
        "needs": "write",
        "irreversible": True,
        "why": ("会真实写入目标 flash，覆盖原有固件。写错文件/偏移会砖板，"
                "且多数场景没有回读备份的能力。"),
        "how": ("先用 dry_run=True（默认）拿到完整命令核对，确认无误后"
                "带 dry_run=False + confirm=True 执行。"),
    },
    "logs_prune": {
        "title": "清理会话日志（不可逆）",
        "needs": "write",
        "irreversible": True,
        "why": ("会**删除**本地已落盘的会话日志文件，删掉就找不回来。"
                "那些日志常常是「几天前那次偶发故障」的唯一现场——"
                "典型误操作是「空间紧张，随手清一下」，把还没分析的证据一起清了。"),
        "how": ("先按默认（dry_run=True）跑一次，看清会删哪些、回收多少；"
                "确认无误后再 dry_run=False + confirm=True。"
                "只是空间紧张的话，也可以只用 keep_days/keep_count 把保留窗口调小，"
                "而不是全清。"),
    },
    "reset_flags_clear": {
        "title": "清复位标志（不可逆）",
        "needs": "write",
        "irreversible": True,
        "confirm_hint": ("本操作没有 dry_run 这一步（它就是在改寄存器，没有「只打印命令」的中间态）："
                         "把上面读到的标志先记下来存档，确认不需要它了，"
                         "再 reset_reason(clear_flags=True, confirm=True)。"),
        "why": ("会写 RCC_RSR 的 RMVF 位，把累积的复位标志（看门狗/欠压/引脚/软件复位）**全部清掉**。"
                "那些标志是「上次为什么重启」的唯一证据，清掉就再也读不回来了 —— 除非当场复现。"
                "另外默认会再复位一次让内核继续跑，那次复位会重新置起 PINRSTF/SFTRSTF，"
                "那是命令自己的痕迹，别把它当成原来的复位原因。"),
        "how": ("它是「清 → 复现 → 再读」这套定位流程的第一步，不是随手做的清理："
                "先用不带 clear_flags 的调用把当前标志读下来存档，"
                "再 clear_flags=True + confirm=True 清零。"),
    },
    "chip_info_full": {
        "title": "full 档读芯片信息（会复位目标并 halt 内核）",
        "needs": "write",
        "irreversible": False,
        "why": ("UID / 真实 Flash 容量 / 选项字节在 system memory 区，hotplug 档读不到，"
                "只能走 normal 档；而 STM32CubeProgrammer 以 normal 档连接**动作本身就会复位目标**"
                "并 halt 内核（实测）。后果有两条：一是打断了正在运行/正在被调试的程序；"
                "二是这次复位会把 RCC_RSR 的 PINRSTF 打上 —— 于是你在查的复位原因被自己污染了。"),
        "how": ("只想知道型号/Device ID/修订/内核类型，用默认的 mode=\"read\"（hotplug，不打扰）。"
                "确实要看 UID 或选项字节再放开写权限；"
                "若同时在查复位原因，请先做完 reset_reason 再调它。"),
    },
    "bus_probe": {
        "title": "向 I2C / SPI / 1-Wire 总线上发探测（扫描 / 读寄存器）",
        "needs": "read",
        "irreversible": False,
        "why": ("扫描不是「纯读」：它真的会在 SDA/SCL 上打出地址字节和时钟。"
                "后果有三条：1) 会唤醒总线上处于低功耗/挂起态的器件，改变它的状态；"
                "2) 若总线被某颗器件**拉死在低电平**（典型是主控在从设备发字节的中途复位），"
                "这时任何一次探测都会超时，而且并不会自己恢复；"
                "3) 某些器件（如带地址锁存的、或正在被别的主控访问的双主总线）会因收到"
                "意料之外的地址而切进异常状态。所以它归在「会对外部世界产生真实动作」这一类，"
                "而不是「只读枚举」那一类。"),
        "how": ("只读档也允许（真正只读的场合——板子在手边、就想看看挂了什么——不该被挡住）。"
                "但请知道它会在总线上打时序；如果那块板正被别的程序/别的主控使用，"
                "先确认没有第二主控在抢总线，再扫。"),
    },
    "bus_write": {
        "title": "向从设备写寄存器 / 发 RS485 字节",
        "needs": "write",
        "irreversible": False,
        "why": ("写出去的值会真的进到器件里，可能改配置、改地址、触发测量、启动自检，"
                "甚至把器件切出 I2C 模式（有些器件用某个寄存器位切 SPI/UART）。"
                "写错寄存器地址时，副作用可能落在**另一个**功能上，而且不会报错。"),
        "how": ("先确认器件型号和寄存器手册，再放开写权限。"
                "写完后想确认落没落进去，用 i2c_write_reg(..., verify=True) 回读比对"
                "（注意只写寄存器回读必然不一致，别把那个当成缺陷）。"),
    },
}


def _reject(op_id, port, extra_lines=None):
    """构造拒绝文案：档位 + 为什么 + 风险 + 怎么放开。"""
    op = OPS[op_id]
    lines = ["【隔离拦截】已拒绝：%s" % op["title"],
             "  当前档位   : %s" % mode_banner()]
    if port:
        lines.append("  目标端口   : %s" % port)
    lines.append("  这个操作会做什么 : %s" % op["why"])
    if op["needs"] == "write" and mode() == MODE_READONLY:
        lines.append("  怎么放开   : 设环境变量 %s=%s 后重启 MCP 服务" % (MODE_ENV, MODE_WRITE))
    if op["irreversible"]:
        lines.append("  二次确认   : 本操作不可逆，即使放开了写权限，仍需 confirm=True 才会执行")
    lines.append("  说明       : %s" % op["how"])
    if extra_lines:
        lines.extend(extra_lines)
    lines.append("  —— 默认挡在这里，是为了不误碰你正在用的设备；不是功能没做。")
    return "\n".join(lines)


def check(op_id, port=None, confirm=False):
    """统一闸门。放行返回 None；拦截返回**可读的中文说明**（不是异常）。

    调用方约定：拿到非 None 就直接把它当工具返回值返回给用户 —— 这与本项目
    「工具不该抛 Python 异常给客户端」的口径一致。
    """
    if op_id not in OPS:
        # 未登记的操作：保守拒绝，避免新写的写操作悄悄绕过隔离层
        return ("【隔离拦截】操作 %r 未登记在 guard.OPS 中，出于保守已拒绝。\n"
                "  这是本工具自身的配置问题：请在 guard.py 的 OPS 里登记该操作"
                "（含 needs / irreversible / 风险说明）后再调用。" % op_id)
    if mode() == MODE_OFF:
        return None
    if is_virtual_port(port):
        # 纯软件回环：外部零副作用，不适用档位与名单（见 VIRTUAL_PORT_PREFIXES）
        return None

    ok, msg = port_verdict(port)
    if not ok:
        return "【隔离拦截】已拒绝：%s\n  当前档位   : %s\n  %s" % (
            OPS[op_id]["title"], mode_banner(), msg)

    op = OPS[op_id]
    if op["needs"] == "write" and mode() == MODE_READONLY:
        return _reject(op_id, port)

    if op["irreversible"] and not confirm:
        lines = ["【需要二次确认】%s 是不可逆操作，已阻止执行。" % op["title"],
                 "  当前档位   : %s" % mode_banner()]
        if port:
            lines.append("  目标端口   : %s" % port)
        lines.append("  这个操作会做什么 : %s" % op["why"])
        # 二次确认的「怎么做」按操作区分：不是每个不可逆操作都有 dry_run 那一步。
        # 早期这里对所有操作都写「先用 dry_run=True」，对 reset_flags_clear 是错的 ——
        # 它根本没有 dry_run 参数，照那句做会找不到参数、以为工具坏了。
        lines.append("  怎么做     : %s" % op.get(
            "confirm_hint",
            "先用 dry_run=True（默认）拿到完整命令逐字核对，"
            "确认无误后带 dry_run=False + confirm=True 再调用。"))
        lines.append("  —— 不可逆操作不靠「相信调用方」，靠参数门禁。")
        return "\n".join(lines)
    return None


def describe():
    """给 env_status 用：当前隔离状态一览。"""
    lines = ["  - %s" % mode_source()]
    m = mode()
    if m == MODE_READONLY:
        lines.append("    当前行为：读/枚举允许；写串口、AT、波特率探测、双口对录、烧录一律拒绝")
    elif m == MODE_WRITE:
        lines.append("    当前行为：允许写硬件；烧录等不可逆操作仍需 confirm=True")
    else:
        lines.append("    当前行为：★ 不做任何检查，所有操作直接放行")
    d, a = deny_ports(), allow_ports()
    lines.append("  - %s = %s" % (DENY_ENV, ",".join(d) if d else "（未设置，不禁任何端口）"))
    lines.append("  - %s = %s" % (ALLOW_ENV, ",".join(a) if a else "（未设置，非白名单模式）"))
    lines.append("  - %s = %s" % (DTR_ENV, "开（尽量不触发开发板自动复位）" if suppress_dtr()
                                  else "关（打开端口会按驱动默认翻转 DTR/RTS）"))
    lines.append("  - 虚拟端口豁免：%s（纯软件回环，外部零副作用，不受档位/名单限制）"
                 % "、".join(VIRTUAL_PORT_PREFIXES))
    return "\n".join(lines)


def reset_cache():
    """保留给测试/未来缓存用（当前无缓存，保持与其他模块一致的接口）。"""
    _WARNINGS.clear()
