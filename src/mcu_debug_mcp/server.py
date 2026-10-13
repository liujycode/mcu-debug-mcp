# -*- coding: utf-8 -*-
"""
mcu-debug-mcp · MCP 服务器薄壳（server.py）

所有实现都在 core.py（零框架依赖、可独立测试）。本文件只做 FastMCP 注册。

启动：python server.py            （stdio 传输，由 MCP 客户端拉起）
依赖：pip install pyserial mcp    （pyocd 可选：pip install pyocd）
"""

import os
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("缺少 mcp 依赖：pip install mcp", file=sys.stderr)
    sys.exit(1)

# 2026-10-13 C 档重构：src 正规包，绝对导入（平铺时代的 sys.path hack 退役）。
from mcu_debug_mcp import core  # noqa: E402

mcp = FastMCP(
    "mcu-debug-mcp",
    instructions=(
        "嵌入式/毕设硬件调试工具集（v4，领域级）。调试流程建议："
        "1) serial_list 看有哪些串口（含芯片识别与驱动建议）；"
        "2) 波特率不确定时 baudrate_autodetect 扫档（有日志样本可离线评分；"
        "USB CDC 口如 ESP32-S3 原生 USB 会被识别并短路——那种口波特率不影响数据）；"
        "3) serial_open 打开（会话日志自动落盘 logs/，关掉后也能用 sessions_list/sessions_get 回看）；"
        "4) serial_read 读几秒日志（expect 参数可'等 OK 再返回'）；"
        "5) analyze_log 分析（故障模式库共 45 条：串口/固件/传感器/网络/RTOS/编译报错/烧录失败/"
        "CAN/BLE/WiFi风暴/J-Link/DNS/USB 全管线，错误码直译内置，知识库外置可扩展）；"
        "6) 按诊断的 next_steps 引导用户排查；7) serial_send 发 AT 指令验证（expect='OK'）；"
        "8) 批量 AT 用例用 at_sequence 一键回归，RS485/双机对话用 serial_pair_record 对录。"
        "会话日志堆积用 logs_prune 按保留策略清理（默认 dry-run 只列不删）；"
        "收尾要交付报告用 report_session 把会话日志导出成 Markdown 报告。"
        "driver_list/driver_get 输出的是出厂验证过的驱动代码，直接交付用户，不要自己现写同类驱动；"
        "需要适配的只有 *_port.h 里的 MCU 引脚接口。"
        "用户说要做某类采集/测距/姿态时先用 component_list 看有没有现成器件（可按 i2c/uart/"
        "timer_pwm/gpio_timing 过滤），component_get 给地址、时序要求与头号坑；"
        "移植驱动或排查「外设配了却读不到数」用 peripheral_api_list / peripheral_api_get"
        "（要占哪些外设、初始化顺序的硬要求，取自各驱动适配层原注释），不要凭记忆讲 HAL 顺序。"
        "接线问题用 driver_wiring 查知识库（含头号坑清单与答辩加分点），不要凭记忆答。"
        "分配 MCU 引脚前必须用 pin_check 核对（三引擎：F1 固定复用 / F4 AF 表 / ESP32 io_mux 硬约束，"
        "可带 board 参数交叉检查板载外设占用），不要凭记忆分配引脚。"
        "选板子/查板级坑先 board_list + board_get（蓝药丸/黑药丸已收录板载外设与坑清单）。"
        "不确定工程用什么框架/工具链时，先 project_detect 认工程结构、toolchain_status 认编译器与 SDK；"
        "生态的识别特征、构建产物、日志签名与各自的坑用 ecosystem_list + ecosystem_get"
        "（Zephyr/RT-Thread/PlatformIO/Keil/IAR/Rust defmt），不要凭印象套框架。"
        "烧录用 flash_firmware，默认 dry-run 先给命令确认再真烧。"
        "J-Link/DAPLink 在位时可用 rtt_read 走 RTT 通道读日志（实验性，固件需含 SEGGER RTT 控制块）。"
        "寄存器位域问题用 decode_register / decode_register_svd。"
        "Modbus 帧（含异常响应）用 modbus_parse 直译异常码；"
        "自定义二进制协议对不上时用 frame_parse（帧头+长度字段切帧；★校验字节不计入长度字段，"
        "Modbus RTU 就靠这条；切不动/截断/垃圾字节会逐条报出，绝不静默丢）；"
        "GPS/北斗模块的串口输出用 nmea_parse（逐句验 XOR 校验和，不过的会明说这句不可信）。"
        "手上有具体错误码（esp_err_t 0x10x / AT 响应 / MQTT rc 等）要查释义与出处时用 error_code_lookup；"
        "它只答知识库已收录的码，未收录会明说查不到——别拿别的码去猜。"
        "驱动/接线/引脚映射/板级/错误码/故障位域/生态知识也可通过 mcu:// 资源浏览"
        "（mcu://drivers、mcu://components、mcu://peripheralapis、mcu://pinmaps、mcu://boards、"
        "mcu://ecosystems、mcu://errcodes、mcu://faultbits）；"
        "每类都是「清单 + 明细」两段：清单看有哪些，明细读原文（条目级出处一并可见）。"
        "常见故障有固定排查套路，可直接用 prompts：diag_no_response / diag_garbled / "
        "diag_i2c_not_found / diag_flash_fail / diag_crash / diag_data_loss。"
    ),
)


@mcp.tool()
def serial_list() -> str:
    """列出系统可用串口。识别 CH340/CP2102/FT232 等常见 USB 转串口芯片并给驱动建议。
    毕设学生"串口列表里找不到板子"时先调这个。"""
    return core.serial_list()


@mcp.tool()
def serial_open(port: str, baudrate: int = 115200, data_bits: int = 8,
                parity: str = "N", stop_bits: int = 1) -> str:
    """打开串口，返回句柄 handle。port 形如 "COM3" 或 "/dev/ttyUSB0"。
    波特率不确定就先用 115200（STM32 HAL 默认）或 9600（老例程/模块默认）。"""
    return core.serial_open(port, baudrate, data_bits, parity, stop_bits)


@mcp.tool()
def serial_close(handle: int) -> str:
    """关闭串口句柄。调试结束记得关，释放端口。"""
    return core.serial_close(handle)


@mcp.tool()
def serial_read(handle: int, seconds: float = 2.0, expect: str = "", idle_ms: int = 0) -> str:
    """从串口读取数据（非阻塞，三条件提前退出）。
    expect：正则，命中立即返回（如 expect="OK|ERROR" 等响应）；
    idle_ms：静默超时（板子说完就走）；
    seconds：总时长上限。返回文本视图 + HEX 视图 + 是否命中。
    读到的内容同时累积进会话日志。"""
    return core.serial_read(handle, seconds, expect or None, idle_ms or None)


@mcp.tool()
def serial_send(handle: int, data: str, hex_mode: bool = False, newline: bool = True,
                expect: str = "", wait_s: float = 0.5) -> str:
    """向串口发送数据并等响应。newline=True 自动追加 \\r\\n（AT 指令必须！）。
    expect：正则，如 "OK"——发 AT 后等到 OK/ERROR 立即返回，不傻等；
    wait_s：等待响应上限秒数（默认 0.5，AT 慢响应建议 3~5）。
    hex_mode=True 时 data 按 HEX 解析（RS485/Modbus 用）。"""
    return core.serial_send(handle, data, hex_mode, newline, expect or None, wait_s)


@mcp.tool()
def baudrate_autodetect(port: str, sample_s: float = 0.6, log_text: str = "",
                        candidates: list = None) -> str:
    """自动探测波特率：逐档打开串口采样，按"最像正常日志"评分排序并给推荐。
    log_text 提供现成日志样本时走离线评分（不占串口）。设备静默时会提示先复位再扫。

    ★USB CDC 口（ESP32-S3 原生 USB、RP2040 等）上波特率不参与数据路径，本工具会
      直接短路说明，不给排序——在那种口上"各档评分"只是同一批字节被反复读。
      确需强行扫（例如怀疑枚举成 CDC 但实际是 UART 桥）时显式传 candidates，
      如 candidates=[115200, 9600]。
    """
    if candidates is None:
        return core.baudrate_autodetect(port, sample_s=sample_s, log_text=log_text or None)
    return core.baudrate_autodetect(port, candidates=candidates, sample_s=sample_s,
                                    log_text=log_text or None)


@mcp.tool()
def at_sequence(port: str, steps: list, default_baud: int = 115200,
                total_timeout_s: int = 60) -> str:
    """批量执行 AT 指令序列并逐条断言（半自动回归测试）。
    steps 形如 [{"cmd": "AT", "expect": "OK", "timeout_s": 2},
               {"cmd": "AT+CWJAP?", "expect": "WIFI|ERROR"}]（expect 是正则）。
    输出每步 PASS/FAIL/TIMEOUT + 回显尾，中途失败不中断，方便一次看全。"""
    return core.at_sequence(port, steps, default_baud, total_timeout_s)


@mcp.tool()
def serial_pair_record(port_a: str, port_b: str, seconds: int = 10,
                       baud_a: int = 115200, baud_b: int = 115200) -> str:
    """同时监听两个串口，输出时间线交错的录制日志（RS485 主从对话/两设备联调）。
    每行前缀 [A]/[B] 标明来源口，附 HEX 视图防波特率猜错时完全看不懂。最长 120s。"""
    return core.serial_pair_record(port_a, port_b, seconds, baud_a, baud_b)


@mcp.tool()
def serial_session_log(handle: int) -> str:
    """取该句柄自打开以来的全部累积日志（喂给 analyze_log 做整体分析用）。"""
    return core.serial_session_log(handle)


@mcp.tool()
def sessions_list(limit: int = 20) -> str:
    """列出已落盘的串口会话日志（serial_open 时自动创建，新→旧）。
    会话被关闭甚至对话轮次已隔很久之后，仍可用它找回之前的串口现场。"""
    return core.sessions_list(limit)


@mcp.tool()
def sessions_get(fname: str, tail_lines: int = 200) -> str:
    """读取指定会话日志的内容（文件名来自 sessions_list）。
    默认显示尾部 200 行（日志开头常是复位前旧数据）。整段内容可直接喂 analyze_log。"""
    return core.sessions_get(fname, tail_lines)


@mcp.tool()
def logs_prune(keep_days: int = 0, keep_count: int = 0, dry_run: bool = True,
               confirm: bool = False) -> str:
    """会话日志的保留策略与清理入口（日志只增不减时用）。

    ★ 默认 dry_run=True：只列出会删哪些文件、能回收多少空间，**什么都不删**。
    要真删：dry_run=False + confirm=True，且隔离档位需为 write
    （删除不可逆——那些日志往往是几天前那次偶发故障的唯一现场）。

    keep_days / keep_count 传 0（默认）时用内置策略：保留最近 30 天 / 最新 200 个。
    ⓘ 日志会原样记录串口收发内容，可能含 WiFi 密码 / token 等凭据，分享前请检查。
    """
    return core.logs_prune(
        keep_days=(keep_days or None),
        keep_count=(keep_count or None),
        confirm=confirm, dry_run=dry_run)


@mcp.tool()
def report_session(handle: int = 0, fname: str = "", out_path: str = "",
                   tail_lines: int = 40, overwrite: bool = False,
                   confirm: bool = False) -> str:
    """把一次串口会话导出成 **Markdown 交付报告**（故障分析 + 原始日志附后）。

    调完一次问题，把「现象/证据/结论/下一步」直接变成可交付文件——毕设报告、
    工单、给同事看都行。原始日志原样附在末尾，避免「只有结论没有证据」。

    数据来源：给 handle（活动会话）或 fname（已落盘日志，来自 sessions_list）；
    都不给时若恰好只有一份日志会自动用它。
    默认落点 <数据根>/reports/；已存在不覆盖（要覆盖传 overwrite=True + confirm=True）。
    """
    return core.report_session(
        handle=(handle or None), fname=(fname or None),
        out_path=(out_path or None), tail_lines=tail_lines,
        overwrite=overwrite, confirm=confirm)


@mcp.tool()
def analyze_log(log_text: str) -> str:
    """分析日志，识别 30 类常见故障模式（乱码/AT报错/反复重启/HardFault/传感器全0/全FF/
    DS18B20恒85/DHT超时/I2C NACK/网络断链/ESP崩溃/掉电/看门狗/固件损坏/CWJAP错误码/
    CME错误码/RTOS断言/栈溢出/链接错误/编译错误/I2C扫描空/UART帧错/HAL断言/ADC饱和/
    MQTT失败/HTTP错误/SD卡失败等），每条给出人话诊断和排查步骤。
    输入可以是 serial_read 的输出、用户粘贴的串口日志、编译器/链接器报错。"""
    return core.analyze_log(log_text)


@mcp.tool()
def modbus_crc(hex_frame: str, check: bool = False) -> str:
    """计算或校验 Modbus RTU 的 CRC16（RS485 毕设必备）。
    check=False：对数据部分计算 CRC；check=True：校验完整帧（数据+CRC）。"""
    return core.modbus_crc(hex_frame, check)


@mcp.tool()
def modbus_parse(hex_frame: str) -> str:
    """解析 Modbus RTU 帧：正常响应拆出寄存器值/线圈状态，异常响应（功能码|0x80）
    直译异常码 01-06（非法功能/非法地址/非法值/设备故障/忙...）并给排查建议。
    从站"该响应但不响应/回了一帧看不懂"时用它。"""
    return core.modbus_parse(hex_frame)


@mcp.tool()
def error_code_lookup(table: str = "", code: str = "") -> str:
    """查错误码释义表（外置知识库 knowledge/error_codes/*.json，每条带出处）。

    table 留空列出所有可查的表；只给 table 列出该表全部码；
    table+code 给出该码的释义、排查方向与依据（出处）。
    已收录：esp_idf（esp_err_t）/ at_common（AT 通用响应）/ cwjap / cme /
    modbus_exception / mqtt_rc。
    查不到时**明说未收录、不给释义**——不要拿别的码去猜。"""
    return core.error_code_lookup(table, code)


@mcp.tool()
def pin_check(mcu: str, pins: dict, board: str = "") -> str:
    """检查 MCU 引脚分配是否冲突（内置数据手册级复用映射）。
    mcu 如 "stm32f103c8t6"；pins 如 {"PA9": "USART1_TX", "PB6": "I2C1_SCL", "PA0": "GPIO"}。
    board 可选开发板名（如 "bluepill"）：板载外设占用的引脚再挪用会警告。
    能抓：该脚没有此功能（AI 幻觉接线）、同一功能双占、SWD 脚挪用、
    remap 档不匹配、非 5V 容忍脚；ESP32 类查硬约束（flash 保留脚/仅输入/strapping）。
    写代码分配引脚之前必须先过一遍。"""
    return core.pin_check(mcu, pins, board or None)


@mcp.tool()
def pinmap_list() -> str:
    """列出引脚知识库已收录的 MCU 型号。pin_check 前可用它确认型号是否支持。"""
    lst = core.pinmap_list()
    return "已收录：" + (", ".join(lst) if lst else "（空）")


@mcp.tool()
def pinmap_get(mcu: str) -> str:
    """输出指定 MCU 的 pinmap JSON 原文（全引脚复用映射 + caveats + 常见冲突提示）。
    pin_check 报错后想看完整数据、或需要给用户展示映射依据时用。"""
    return core.pinmap_get(mcu)


@mcp.tool()
def board_list() -> str:
    """列出板级知识库已收录的开发板。选板子前先看有没有现成板级坑清单。"""
    lst = core.board_list()
    return "已收录：" + (", ".join(lst) if lst else "（空）")


@mcp.tool()
def board_get(name: str) -> str:
    """输出指定开发板的板级知识：板载外设及占用引脚、板级坑清单（按翻车率排序）、
    供电说明、数据来源。如 "bluepill"（蓝药丸）、"blackpill_f411"（黑药丸）。
    给用户做硬件选型/排板级坑之前先查这个。"""
    return core.board_get(name)


@mcp.tool()
def ecosystem_list() -> str:
    """列出已收录的嵌入式生态/框架（Zephyr / RT-Thread / PlatformIO / Keil / IAR / Rust defmt）。
    拿到不认识的工作目录、或一段陌生的报错时，先判断「这是哪个生态的工程」——
    识别错了，后面每一步排查都会拐到沟里。"""
    return core.ecosystem_list()


@mcp.tool()
def ecosystem_get(name: str) -> str:
    """输出指定生态的识别特征：工程产物 / 构建配置 / 日志特征 / 常见坑。
    name 用 ecosystem_list 给的 key（如 zephyr / keil / rust_defmt）。"""
    return core.ecosystem_get(name)


@mcp.tool()
def project_detect(path: str = ".") -> str:
    """识别一个**工程目录**属于哪个生态（扫标志文件：platformio.ini / *.uvprojx /
    prj.conf / SConstruct / *.ewp / Cargo.toml），并给出该生态的构建方式与常见坑。
    拿到别人的工程压缩包、或不知道从哪下手时先跑这个。只探一层，不递归。"""
    return core.project_detect(path)


@mcp.tool()
def toolchain_status() -> str:
    """探测**本机**嵌入式工具链：编译器（ARM GCC / armclang / RISC-V / Xtensa）、
    调试器、构建系统（CMake/Ninja/west/SCons/PlatformIO/Cargo）各给版本。
    换机器、或「我这能编过、换台电脑就编不过」时先看这个。烧录器用 flash_tool_detect。"""
    return core.toolchain_status()


@mcp.tool()
def probe_list() -> str:
    """探测已连接的调试器（DAPLink/ST-Link/J-Link...）和目标芯片。
    "烧录失败/找不到目标芯片"时先调这个。需要可选依赖 pyocd。"""
    return core.probe_list()


@mcp.tool()
def rtt_read(seconds: float = 5.0, cbuffer: int = -1, probe_serial: str = "") -> str:
    """通过 pyocd 的 RTT 通道读目标板调试日志（⚠ 实验性）。
    固件需集成 SEGGER RTT 控制块；比 UART 快且不占串口外设。
    cbuffer：控制块编号（多通道时指定，默认读全部）；probe_serial：多调试器时指定。
    失败自动给排查指引，建议先 probe_list 确认调试器。"""
    return core.rtt_read(seconds, None if cbuffer < 0 else cbuffer, probe_serial or None)


@mcp.tool()
def flash_tool_detect() -> str:
    """检测本机已安装的烧录工具（esptool/st-flash/pyocd/openocd）。
    烧录前先调这个看有什么可用。"""
    found = core.flash_tool_detect()
    if not found:
        return "没有检测到任何烧录工具。安装：pip install esptool / pyocd，或下载 st-flash。"
    return "可用烧录工具：\n" + "\n".join("- %s → %s" % (k, v) for k, v in sorted(found.items()))


@mcp.tool()
def flash_firmware(file_path: str, tool: str = "auto", chip: str = "",
                   port: str = "", sn: str = "", dry_run: bool = True,
                   confirm: bool = False) -> str:
    """烧录固件到开发板（**不可逆操作，双闸门**）。

    第一道：dry_run 默认 True，只返回将要执行的完整命令供逐字核对，不写 flash。
    第二道：真正执行必须 dry_run=False 且 confirm=True —— 两道都齐才会写。
    另受隔离档位管辖：MCU_DEBUG_MCP_MODE 为 readonly（默认）时直接拒绝。
    STM32+ST-Link（默认首选，实测最稳）：
        flash_firmware("app.bin", chip="stm32h563")
        → STM32_Programmer_CLI -c port=SWD mode=normal -w app.bin 0x08000000 -v -rst
    接多台同型号调试器时用 sn 指定探针序列号。
    ESP 系：flash_firmware("app.bin", chip="esp32", port="COM5")
    通用 Cortex-M：flash_firmware("app.elf", tool="pyocd", chip="stm32f103c8")
    烧完自动 -rst 复位；**不复位的话 SWD 会把内核 halt 住，板子不会跑**。"""
    return core.flash_firmware(file_path, tool, chip or None, port or None,
                               sn or None, dry_run, confirm)


@mcp.tool()
def driver_list() -> str:
    """列出可用的预验证驱动模板（带出厂验证记录）。
    用户要做毕设传感器部分时，先看这里有没有现成的，而不是自己写。"""
    return core.driver_list()


@mcp.tool()
def driver_get(name: str) -> str:
    """输出指定驱动的接线表+完整代码+验证记录。name 来自 driver_list。
    这些代码是预验证过的，直接交付用户；需要适配的只有 *_port.h。"""
    return core.driver_get(name)


@mcp.tool()
def driver_wiring(name: str) -> str:
    """查指定驱动的接线知识库：接线表、头号坑清单（按翻车率排序）、
    输出日志解读、答辩加分点。回答"XX传感器怎么接/为什么不工作"前先查这个。"""
    return core.driver_wiring(name)


@mcp.tool()
def component_list(bus: str = "") -> str:
    """器件选型清单：按接口（i2c / uart / timer_pwm / gpio_timing）列出可用的
    传感器与模块，带地址、微秒时序要求。回答"我要做温湿度采集/测距/姿态，
    用哪个现成器件"时先查这里，别凭记忆报型号。bus 可按接口过滤（如 "i2c"）。"""
    return core.component_brief(bus or None)


@mcp.tool()
def component_get(name: str) -> str:
    """单个器件的选型详情：接口 / 地址 / 移植要求 / 头号坑 / 出厂验证记录 /
    接线避坑全文。name 来自 component_list。"""
    return core.component_get(name)


@mcp.tool()
def peripheral_api_list(bus: str = "") -> str:
    """外设 API 用法知识层：每个驱动要占哪些外设（I2C1/TIM1/UART2…）、
    初始化顺序有什么硬要求。移植驱动或排查"外设配了但读不到数"时先查这里。"""
    return core.peripheral_api_brief(bus or None)


@mcp.tool()
def peripheral_api_get(name: str) -> str:
    """单个驱动的外设初始化要求全文（取自其 *_port.h 适配层原注释）。
    name 来自 peripheral_api_list。"""
    return core.peripheral_api_get(name)


@mcp.tool()
def decode_register(value: int, fields: list, width: int = 32) -> str:
    """把寄存器原始值按位域拆开解释。
    fields 形如 [{"name":"HSION","bit":0}, {"name":"PLLSRC","lsb":16,"width":2}]。
    手头有寄存器值不知道每个位什么意思时用。"""
    return core.decode_register(value, fields, width)


@mcp.tool()
def decode_register_svd(svd_path: str, peripheral: str, register: str, value: int) -> str:
    """用 CMSIS-SVD 文件解码寄存器位域（含枚举值提示与描述）。
    svd_path 指向厂商 SVD 文件（CubeMX/固件包里有），如：
    decode_register_svd("STM32F103.svd", "RCC", "CR", 0x00040002)"""
    return core.decode_register_svd(svd_path, peripheral, register, value)


# ---- 排故链：fault 解码 / .map 解析 / 符号反查 / 栈回溯 ----

@mcp.tool()
def decode_fault(cfsr: int = None, hfsr: int = None, bfar: int = None,
                 mmfar: int = None, arch: str = "cortex-m") -> str:
    """解码 Cortex-M 故障寄存器，给出人话诊断 + 排查方向。

    设备跑飞/进 HardFault 时用。传你从 fault handler 读到的原始值（任意组合）：
      decode_fault(cfsr=0x8200, hfsr=0x40000000, bfar=0x40021000)

    ★ 会自动处理三类陷阱，别跳过它的结论：
      · BFARVALID/MMARVALID=0 时地址是垃圾值——工具会拒绝解读，不给假地址
      · IMPRECISERR 时即使 valid，BFAR 也不可信（总线写延迟）——工具会标注
      · FORCED=1 说明真因在 CFSR——工具会提示去读 CFSR
    拿到可信地址后，用 symbol_lookup 反查是哪个函数。"""
    return core.decode_fault(cfsr, hfsr, bfar, mmfar, arch)


@mcp.tool()
def parse_map(path: str, top_n: int = 15) -> str:
    """解析 .map 文件：段占用（Flash/RAM）、符号表、可疑大符号（查大数组/大缓冲）。

    支持 GNU ld / Keil / IAR 三种格式，自动识别；认不出会明说，不猜。
      parse_map("build/app.map")
    查"RAM 怎么爆了""哪个大数组吃栈"时用。结果里的符号大小若是「推断」值，
    表示该 map 不记 size，大小由相邻符号地址差推出（可能偏大）。"""
    return core.parse_map(path, top_n)


@mcp.tool()
def symbol_lookup(path: str, addresses: list) -> str:
    """把地址反查成「函数名+偏移」。fault 地址定位代码的关键一步。

      symbol_lookup("build/app.elf", ["0x08004a2c"])
    ★优先给 .elf 而不是 .map：.elf 的 .symtab 带真实 st_size，能判断
    「地址还在不在这个函数内」；.map 只能靠「下一个符号 − 本符号」推断。
    ★GNU ld（GCC/STM32CubeIDE/ESP-IDF）默认**不往 .map 里写符号表**，
    同一份固件的 .map 可能只有个位数条目（实测 3 个，而 .elf 有 31 个）。
    此时反查会命中段符号给出**假函数名**——所以本工具遇到这种稀疏 map 会
    明确提示，并把同目录的 .elf 摆出来让你换源。
    找不到符号时明确报"未找到"，不给猜测的函数名——猜出来的名字比没名字更害人。"""
    return core.symbol_lookup(path, addresses)


@mcp.tool()
def stack_unwind(path: str, frame: list = None, exc_return: int = None,
                 sp: int = None) -> str:
    """栈回溯：把 fault 时压栈的 8 个字还原成调用链，定位出错函数。

    frame 按硬件压栈顺序：[R0, R1, R2, R3, R12, LR, PC, xPSR]
      stack_unwind("build/app.elf", frame=[r0,r1,r2,r3,r12,lr,pc,xpsr])
    ★同 symbol_lookup：优先给 .elf。.map 的符号表稀疏时 PC/LR 可能反查出
    假函数名，工具会就此提示。
    exc_return 传 handler 入口时的 LR——它决定栈帧是标准还是 FPU 扩展、
    以及异常前用的是 MSP 还是 PSP（RTOS 任务）。传了会给出更准的判断。
    ★ PC 反查不到符号时，说明是典型「跑飞」，工具会给排查方向。"""
    return core.stack_unwind(path, frame, exc_return, sp)


@mcp.tool()
def env_status() -> str:
    """环境自检：依赖 / 串口 / 烧录器 / 知识库根 / 版本，一次问清。

    拿到陌生机器、或工具突然不好使时先跑这个：它把「这里能干什么、缺什么、
    缺了会导致哪些工具不可用」一次列清，省掉一个个试错。
    ★ 只报实测——探不到的会明说为什么探不到。"""
    return core.env_status()


# ---- 协议报文解析：NMEA(GPS) / 自定义二进制帧 ----

@mcp.tool()
def nmea_parse(text: str) -> str:
    """解析 NMEA 0183 语句（GPS/北斗模块最常见的串口输出），给人话解读。

    支持 GGA/RMC/GSA/GSV/VTG/GLL，逐句输出 UTC 时间、经纬度（度分 + 十进制度）、
    定位质量、卫星数、HDOP、速度、日期。把模块吐出来的整段原始数据直接贴进来：
      nmea_parse("$GNGGA,012345.00,3112.3456,N,12122.3456,E,1,08,0.9,12.3,M,,M,,*47")
    ★ 每句都验 XOR 校验和，不过的会明说「这句数据不可信」，不会把错帧当对的解。
    ★ 非 NMEA 的行会单独指出并跳过，不当成数据。"""
    return core.nmea_parse(text)


@mcp.tool()
def frame_parse(data_hex: str, header: str = "", length_offset: int = None,
                length_size: int = 1, endian: str = "big", checksum: str = "none",
                length_includes_header: bool = False, max_frames: int = 200) -> str:
    """通用二进制帧解析：按「帧头 + 长度字段」切帧，可选逐帧校验。

    单片机自定义协议（上位机抓到的 hex 流）对不上时用。参数都能少填就少填：
      frame_parse("AA 55 04 01 02 03 04 AA 55 02 05 06")
      frame_parse("AA 55 04 01 02 03 04", header="AA55", checksum="sum8")
      frame_parse("01 03 04 00 0A 00 0B 9B F6", checksum="crc16_modbus")
    header 不给则须给 length_offset（长度字段相对帧首的偏移）。
    checksum 可选 "none"/"sum8"/"crc16_modbus"。
    ★ 切不出来（找不到帧头 / 长度越界 / 帧被截断）会**逐条报出并附剩余字节**，
      **绝不静默丢数据**——静默丢帧是这类工具最危险的错。"""
    return core.frame_parse(data_hex, header, length_offset, length_size,
                            endian, checksum, length_includes_header, max_frames)


# ---- 直连芯片：身份 / 复位原因 / 烧录校验 / 时序 / 长时录测 ----

@mcp.tool()
def chip_info(mode: str = "read", sn: str = "") -> str:
    """读目标芯片的身份与存储信息（型号 / Device ID / 修订 / Flash 容量 / UID / 读保护）。

    mode="read"（默认）走 hotplug：**不复位、不停内核**，板子照跑。
      能拿到 型号 / Device ID / Revision / Device CPU / Board / 电压 / 调试器信息，
      以及 DBGMCU_IDCODE 里更精确的 DEV_ID 与 REV_ID（会和连接 banner 交叉核对）。
    mode="full" 走 normal：多拿 真实 Flash 容量 / 96 位 UID / 选项字节（读保护等）。
      代价是**连接本身会复位一次目标并 halt 内核** —— 受隔离层管辖（需要写权限）。
      ★ 如果你正在查复位原因，别先调它：这次复位会把 PINRSTF 打进 RCC_RSR，污染现场。

    实测依据：system memory（UID @0x08FFF800 / FlashSize @0x08FFF80C）在 hotplug 下
    读不到（CLI 报 "failed to read the requested memory content"），必须 normal 档；
    而 normal 档连上就复位。所以拆成两档，把选择权交给你，不悄悄替你做主。"""
    return core.chip_info(mode, sn or None)


@mcp.tool()
def reset_reason(sn: str = "", clear_flags: bool = False, confirm: bool = False,
                 then_reset: bool = True) -> str:
    """读复位原因（RCC_RSR 标志）与内核运行状态，走 hotplug：**不复位、不停内核**。

    回答「板子为什么自己重启了」「是不是看门狗喂狗没喂上」这类问题。
    RCC_RSR 会给出 PINRSTF（复位引脚）/ BORRSTF（欠压）/ SFTRSTF（软件）/
    IWDGRSTF（独立看门狗）/ WWDGRSTF（窗口看门狗）/ LPWRRSTF（低功耗退出）。

    ★ 关键理解：这些标志是【累积】的，只有写 RMVF 或整片掉电才清。
      所以「好几个位同时为 1」= 「自上次清零以来发生过这几类复位」，
      **不等于**「最后一次是它们一起发生」。
      要定位「这一次」：先 clear_flags=True + confirm=True 清零 → 复现 → 再读。
      clear_flags 不可逆（销毁现场），受隔离层管辖且需 confirm=True。
      then_reset 只在 clear_flags=True 时有意义：清零并读回验证后再复位一次，
      让内核继续跑（默认 True）。

    ★ 哪些动作会自己置标志（实测，用来区分「真复位」与「读操作自带的噪声」）：
        hotplug 连接（仅连接）→ 不置任何标志； normal 连接（仅连接）→ PINRSTF|SFTRSTF；
        hotplug + -rst → PINRSTF|SFTRSTF；   hotplug + -hardRst → 仅 PINRSTF。
      所以清零后读回还看到 PINRSTF/SFTRSTF 几乎总是命令自己造成的，不是 RMVF 没生效。

    ★ 为什么必须 hotplug：STM32CubeProgrammer 以 normal 档连接**动作本身就会复位目标**
      并 halt 内核。用 normal 读复位原因，读到的第一个标志就是这次读操作自己造成的
      —— 结论必然错。"""
    return core.reset_reason(sn or None, clear_flags, confirm, then_reset)


@mcp.tool()
def flash_verify(file_path: str, address: str = "", sn: str = "",
                 chunk: int = 65536) -> str:
    """烧录后校验：把固件文件与芯片上实际内容**逐字节比对**（只读，走 hotplug）。

    比 `-v` 有用：`-v` 只能紧跟烧录命令，等于重烧一遍；这个读回来直接比，
    还会报出**第一个不一致的偏移**和前后 8 字节上下文 —— 能直接看出来是「整段没写进去」
    还是「某几个字节错了」。

    file_path 支持 .bin（配 address，默认 0x08000000）/ .hex / .elf。
    烧完提示 "verified" 但设备不工作时，先调它排除「根本没烧进去」。
    ★ 不要拿 CLI 的 -checksum 和 -fchecksum 互相验：两者口径不同，实测同一份固件
      给出不同值。字节比对才是唯一真凭据。"""
    return core.flash_verify(file_path, address or None, sn or None, chunk)


@mcp.tool()
def timing_analysis(text: str = "", path: str = "", pattern: str = "",
                    seq_pattern: str = "") -> str:
    """时序 / 数据率分析：找丢帧（序号跳号）、卡顿（间隔离群）、节拍漂移。

    输入二选一：text（现成日志内容）或 path（日志/采集文件）。
    最顺的用法：先 stream_capture 采一段，再把它的文件路径喂进来。

    时间戳自动识别两种：[+0.0123] 采集前缀、HH:MM:SS.mmm；其他格式用 pattern
    （第 1 个捕获组是秒）。
    seq_pattern 给序号（第 1 个捕获组），例如 TICK 42 这种要写 seq_pattern="TICK\\s+(\\d+)"；
    不给则只认行首那个整数。

    ★ 定位说明：时间戳来自 PC 侧（OS 调度 + USB 缓冲），自带毫秒级抖动。上面的数适合
      【相对比较】——哪几次明显变慢、有没有跳号、是否整体漂移；不适合当示波器判
      「周期是不是精确 1.000 ms」。"""
    return core.timing_analysis(text or None, path or None,
                                pattern or None, seq_pattern or None)


@mcp.tool()
def stream_capture(port: str, seconds: float = 10.0, baudrate: int = 115200,
                   max_lines: int = 0, idle_stop_s: float = 0.0,
                   out_path: str = "") -> str:
    """长时录测：持续采集串口一段时间，逐行打上到达时间戳落盘。

    产出的文件**直接可以喂给 timing_analysis(path=...) ** —— 两件工具是同一条链路。

    seconds 最长采集秒数；max_lines 收够多少行提前结束（0=不限）；
    idle_stop_s 连续多少秒没数据提前结束（0=不用）；out_path 默认落在会话日志目录
    （已存在同名文件会自动改名，不覆盖旧现场）。

    ★ 为什么时间戳必须在采集端打：事后才拿到的日志如果没时间戳，时序信息就永久丢了。
      采集时按到达时刻记一行，成本几乎为零，却让后面所有分析成立。
      排查「偶发丢数据」「隔一会儿卡一下」时用：让它自己采几分钟，再看 timing_analysis。"""
    return core.stream_capture(port, seconds, baudrate, max_lines,
                               out_path or None, idle_stop_s)


# ---- 总线工具（G06）：I2C / SPI / 1-Wire / RS485，全部经「串口桥」 ----
#
# 为什么要一块板当桥、桥协议长什么样、以及**为什么不做全口自动探测**，
# 都在 core.py 的「总线工具」块头里写清楚了；这里只做参数转接。

@mcp.tool()
def bridge_probe(port: str = "", baudrate: int = 115200) -> str:
    """桥自检：问那块跑桥固件的板「你是谁、支持哪些总线」——不碰任何总线，纯握手。

    排查「总线工具不工作」的第一步：先确认桥活着、认协议，再去怀疑接线。
    省得在「固件问题」和「接线问题」之间来回猜。

    port 不填则读环境变量 MCU_DEBUG_BRIDGE_PORT。桥固件源码见 tools/bridge_fw/。"""
    return core.bridge_probe(port or None, baudrate)


@mcp.tool()
def i2c_scan(bus: int = 0, port: str = "", baudrate: int = 115200,
             low: int = 0, high: int = 0) -> str:
    """扫描 I2C 总线上的 7 位地址，标出每个应答地址上**可能**是什么器件。

    这是「板子上到底挂了什么」的第一手答案。low/high 可缩小范围
    （默认 0x08–0x77，即 I2C 规范里可自由分配的段；保留段会被跳过）。

    ★ 三条边界（别把结论放大）：
      · 「有应答」只说明该地址有人 ACK，不代表这颗器件功能正常；
      · 同一地址常被多颗器件共用（0x68 上可能是 MPU-6050 也可能是 DS1307），
        要定种必须读它的 WHO_AM_I / ID 寄存器（i2c_read_reg）；
      · 挂在 TCA9548A 这类 I2C 多路复用器**后面**的器件默认是断开的，
        直接扫主总线只能看到复用器自己（0x70–0x77）。"""
    return core.i2c_scan(bus, port or None, baudrate,
                         low or None, high or None)


@mcp.tool()
def i2c_read_reg(addr: int, reg: int, count: int = 1, bus: int = 0,
                 port: str = "", baudrate: int = 115200) -> str:
    """读 I2C 从设备的寄存器（最常见的「这颗 IMU 是不是活的」动作）。

    addr 是**7 位**地址（MPU-6050 写 0x68，别写 8 位的 0xD0/0xD1，工具会拦下来）；
    reg 是寄存器地址；count 是要读的字节数（1–256）。

    ★ 读回全 0 或全 0xFF 时不要直接判「器件坏了」：工具会分别给出这两种情况
      完全不同的成因清单（全 0xFF 往往是器件没接/没上电，而不是寄存器的值是 0xFF）。"""
    return core.i2c_read_reg(addr, reg, count, bus, port or None, baudrate)


@mcp.tool()
def i2c_write_reg(addr: int, reg: int, data: str, bus: int = 0,
                  port: str = "", baudrate: int = 115200,
                  verify: bool = False) -> str:
    """写 I2C 从设备的寄存器。data 支持 "0x01" / "1,2" / [1,2]。

    verify=True 会在写完后回读比对。★ 默认关：有些寄存器是**只写**的
    （命令寄存器、软复位寄存器），回读必然拿到别的东西，默认开着会造成大量假 FAIL。

    写操作受隔离层写权限门禁：只读档会被拒绝，并给出放开方式。"""
    return core.i2c_write_reg(addr, reg, data, bus, port or None, baudrate,
                              verify)


@mcp.tool()
def spi_scan(bus: int = 0, port: str = "", baudrate: int = 115200) -> str:
    """逐条 CS 读 JEDEC ID（0x9F），找出板上挂的 SPI flash，并解出厂商与容量。

    ★ SPI **没有地址概念**，所以「扫描」只能是「对每条 CS 发 0x9F」。因此本工具
      只对遵守 0x9F 的 SPI NOR/NAND flash 有效；对 SPI 传感器 / ADC / 显示驱动
      （各有自己的命令字）扫不出来是**正常**的，不是故障 —— 那种器件请用
      spi_read_reg 给具体命令字。"""
    return core.spi_scan(bus, port or None, baudrate)


@mcp.tool()
def spi_read_reg(cmd_byte: int, count: int = 1, cs: int = 0, bus: int = 0,
                 port: str = "", baudrate: int = 115200) -> str:
    """发一个 SPI 命令字并读回 N 字节（对读了命令字的从设备通用）。

    例：读 JEDEC ID → spi_read_reg(cmd_byte=0x9F, count=3)。"""
    return core.spi_read_reg(cmd_byte, count, cs, bus, port or None, baudrate)


@mcp.tool()
def onewire_scan(bus: int = 0, port: str = "", baudrate: int = 115200) -> str:
    """1-Wire 搜索 ROM，列出总线上每颗器件的 64 位 ROM 码，并解出家族码对应的器件。

    搜不到时的第一条怀疑对象是**数据线缺 4.7k 上拉**（1-Wire 靠上拉给器件充电，
    缺了必然一颗都搜不到）——输出里会给完整清单。"""
    return core.onewire_scan(bus, port or None, baudrate)


@mcp.tool()
def rs485_send(data: str, bus: int = 0, de_ms: float = 2.0, newline: bool = False,
               port: str = "", baudrate: int = 115200) -> str:
    """经桥的 RS485 收发器发送字节（桥负责 DE 方向脚的时序）。

    ★ 能力边界：它只能确认**桥把字节推出去了**，对端有没有收到它证明不了。
      要验证整条链路得让对端回话（再用 serial_read 读），或示波器看 A/B 差分波形。
      这句限制会直接打印在输出里，不代用户下结论。"""
    return core.rs485_send(data, bus, de_ms, port or None, baudrate, newline)


# ---- MCP resources：知识库只读浏览（AI 可主动引用，不用逐个调工具） ----

@mcp.resource("mcu://drivers")
def resource_driver_list() -> str:
    """预验证驱动库清单（带验证状态）。"""
    return core.driver_list()


@mcp.resource("mcu://driver/{name}")
def resource_driver(name: str) -> str:
    """指定驱动的完整代码 + 验证记录。"""
    return core.driver_get(name)


@mcp.resource("mcu://wiring/{name}")
def resource_wiring(name: str) -> str:
    """指定驱动的接线知识库（接线表+坑清单+日志解读）。"""
    return core.driver_wiring(name)


@mcp.resource("mcu://components")
def resource_component_list() -> str:
    """器件选型清单（按接口分类，全部来自预验证驱动库）。"""
    return core.component_brief()


@mcp.resource("mcu://component/{name}")
def resource_component(name: str) -> str:
    """指定器件的选型详情 + 接线避坑全文。"""
    return core.component_get(name)


@mcp.resource("mcu://peripheralapis")
def resource_peripheral_api_list() -> str:
    """外设 API 用法清单（谁要哪个外设、初始化顺序要求）。"""
    return core.peripheral_api_brief()


@mcp.resource("mcu://pinmaps")
def resource_pinmap_list() -> str:
    """引脚知识库已收录 MCU 清单。"""
    lst = core.pinmap_list()
    return "已收录：" + (", ".join(lst) if lst else "（空）")


@mcp.resource("mcu://pinmap/{mcu}")
def resource_pinmap(mcu: str) -> str:
    """指定 MCU 的 pinmap JSON 原文（数据手册级复用映射）。"""
    return core.pinmap_get(mcu)


@mcp.resource("mcu://boards")
def resource_board_list() -> str:
    """板级知识库已收录开发板清单。"""
    lst = core.board_list()
    return "已收录：" + (", ".join(lst) if lst else "（空）")


@mcp.resource("mcu://board/{name}")
def resource_board(name: str) -> str:
    """指定开发板的板级知识（板载外设+坑清单+供电说明）。"""
    return core.board_get(name)


@mcp.resource("mcu://ecosystems")
def resource_ecosystem_list() -> str:
    """已收录的嵌入式生态/框架清单。"""
    return core.ecosystem_list()


@mcp.resource("mcu://ecosystem/{name}")
def resource_ecosystem(name: str) -> str:
    """指定生态的识别特征（工程标志 / 构建产物 / 日志特征 / 常见坑）。"""
    return core.ecosystem_get(name)


@mcp.resource("mcu://errcodes")
def resource_errcode_list() -> str:
    """错误码知识库已收录的表清单（含每张表的条目数与说明）。"""
    return core.error_code_lookup("")


@mcp.resource("mcu://errcode/{table}")
def resource_errcode(table: str) -> str:
    """指定错误码表的 JSON 原文（每条码的含义/排查方向/出处一并可见）。"""
    return core.errcode_table_get(table)


@mcp.resource("mcu://faultbits")
def resource_faultbits_list() -> str:
    """故障位域库已收录的架构清单。

    ★ 特意把「文件名」和「decode_fault(arch=) 该填的值」一起列出来——
      两者不是同一个字符串（cortex_m vs cortex-m），只列文件名会引人填错。
    """
    cat = core.fault_bits_catalog()
    if not cat:
        return "故障位域表目录为空（knowledge/fault_bits/ 下没有 *.json）。"
    lines = ["已收录故障位域表（%d 张）：" % len(cat)]
    for c in cat:
        lines.append("- %s  arch=%s  %s"
                     % (c["table"], c["arch"] or "未标", c["desc"] or "(无说明)"))
    lines.append("")
    lines.append("读原文：mcu://faultbits/<文件名>（如 mcu://faultbits/cortex_m）")
    lines.append('解码：decode_fault(arch="<上表 arch= 的值>")——填文件名匹配不上。')
    return "\n".join(lines)


@mcp.resource("mcu://faultbits/{arch}")
def resource_faultbits(arch: str) -> str:
    """指定架构的故障位域表 JSON 原文（CFSR/HFSR/BFAR/MMFAR 位域，逐条带出处）。"""
    return core.fault_bits_get(arch)


# --------------------------------------------------------------------------- #
# MCP prompts（诊断流程）—— MCP 第三原语
#
# 为什么值得做：MCP 三原语此前只用了 tools 与 resources，prompts 是 0 个。
# prompts 的独特价值在于**客户端无关**——任何支持 prompts 的 MCP 客户端都能一键
# 进入这些流程，不依赖本项目的专用 Skill。
#
# 每条都写成「按顺序调哪些工具、看到什么分哪条叉」，并且在**能力尚未实现**的地方
# 如实写明走人工排查——prompt 里许诺一个不存在的工具，比没有这个 prompt 更糟。
# ★ 2026-09-22：G05 烧录后验证（flash_verify）、G06 总线工具（i2c_scan 等，经串口桥）、
#   G12 长时录测（stream_capture / timing_analysis）都已落地，下方三条 prompt 里
#   「尚未实现」的旧说法已按实际能力改写。改能力时**必须同步改这里**，
#   否则用户会照着 prompt 去手工排查一件工具已经能做的事。
# --------------------------------------------------------------------------- #

@mcp.prompt()
def diag_no_response(port: str = "<你的端口>", baud: str = "115200") -> str:
    """上电后设备完全没反应：从「口在不在」一路查到「为什么没输出」。"""
    return (
        "设备已上电但没有输出/没有响应，请按下面顺序排查，每一步都有分叉：\n"
        "\n"
        "1. 先摸清本机能力：env_status。看 pyserial 在不在、探到哪些烧录器、知识库根在哪。\n"
        "2. 找端口：serial_list。\n"
        "   · 列表里没有目标口 → 不是软件问题：换根数据线（排除纯充电线）、查驱动\n"
        "     （输出里会给 VID:PID 与「未收录」提示）、确认板子真的通电了。\n"
        "   · 有口 → 记下设备名，进第 3 步。\n"
        "3. 打开：serial_open(port=%s, baudrate=%s)。本工具默认尽量不拉 DTR/RTS，\n"
        "   避免「只想看一眼却把板子复位了」；确实需要复位行为时再单独说明。\n"
        "4. 空读两秒：serial_read(handle, seconds=2)。\n"
        "   · 有输出 → 链路是通的，转 analyze_log 看内容（乱码另走 diag_garbled）。\n"
        "   · 完全无输出 → 波特率不确定就先 baudrate_autodetect（USB CDC 口波特率\n"
        "     不影响数据，工具会直接判定并告诉你）。\n"
        "5. 仍无输出时，硬件侧清单（工具帮不上，但别漏）：供电是否够（大电流外设）；\n"
        "   BOOT0/BOOT1 是否被拉高（进了 bootloader 就不跑你的程序）；复位脚是否被\n"
        "   持续拉低；是否卡在时钟初始化（HSE 未起振，常见「死在上电第一行」）。"
        % (port, baud)
    )


@mcp.prompt()
def diag_garbled(port: str = "<你的端口>") -> str:
    """串口有输出但全是乱码：先分清是波特率、数据格式，还是时钟问题。"""
    return (
        "串口有输出但内容乱码，按下面顺序缩小范围：\n"
        "\n"
        "1. 先判口类型：baudrate_autodetect(%s)。若工具判定这是 USB CDC 口，\n"
        "   波特率**不影响数据**——乱码跟波特率无关，别再换档（换了也不会有变化）。\n"
        "2. 非 CDC 口：用 baudrate_autodetect 扫标准档位（按可打印率与字符集打分）。\n"
        "   · 某一档明显胜出 → 用那一档重开。\n"
        "   · 所有档都乱 → 波特率不是根因，进第 3 步。\n"
        "3. 数据格式：确认数据位/校验位/停止位（8N1 是绝大多数）。serial_open 支持\n"
        "   7/8 位、N/E/O 校验、1/2 停止位——逐个试。\n"
        "4. 时钟侧（最容易漏）：外部晶振未起振，芯片退到内部 RC → 实际波特率偏离标称\n"
        "   → 全乱码。查 HSE 起振（负载电容、晶振型号）；也检查系统时钟配置与波特率\n"
        "   计算用的时钟源是否一致（改了主频没重算 BRR）。\n"
        "5. 电平/接线：TTL 与 RS232 电平混用会得到**稳定乱码**（不是无输出）——\n"
        "   确认没接反、没接错电平。"
        % port
    )


@mcp.prompt()
def diag_i2c_not_found(mcu: str = "<mcu>", device: str = "<器件名>") -> str:
    """I2C 从设备扫不到 / 读写失败：先扫总线拿事实，再按清单排查。"""
    return (
        "I2C 从设备扫不到或读写失败。\n"
        "\n"
        "1. 先拿事实，不要先猜：i2c_scan(bus=0, port=\"<桥口>\")。\n"
        "   · 扫到别的地址、就是没有你要的那颗 → 地址脚（A0/A1/A2）接法不对，或器件没上电。\n"
        "   · 一个都没扫到 → 先查上拉电阻与 SDA/SCL 接线，别急着换器件。\n"
        "   · 只扫到 0x70–0x77 → 总线走了 TCA9548A 多路复用器，通道是**默认断开**的，\n"
        "     得先给它写通道选择字才能看到后面那 8 路上的器件。\n"
        "   ★ 桥固件与端口在 tools/bridge_fw/README.md（先 bridge_probe 确认桥活着）。\n"
        "2. 扫到后定种：读它的 WHO_AM_I / ID 寄存器，例如\n"
        "   i2c_read_reg(addr=0x68, reg=0x75) —— MPU-6050 正常应回 0x68。\n"
        "   **光看地址定不了种**：0x68 上既可能是 MPU-6050 也可能是 DS1307。\n"
        "3. 接线核对：driver_wiring(%s)。本项目驱动库自带头号坑清单（上拉、电平、\n"
        "   地址冲突），别凭记忆答。\n"
        "4. 引脚复用：pin_check(%s, {...})。查 SCL/SDA 是否复用到了错误的引脚、或被\n"
        "   别的外设占了（工具用三引擎核对，含 F1 固定复用与 F4 AF 表）。\n"
        "5. 地址：**7 位地址左移一位才是写地址**——这是最常见的一类 bug。\n"
        "6. 总线卡死：读回全是超时（-ERR TIMEOUT）时，多半是 SDA 被某颗器件拉死在低电平\n"
        "   （典型成因：主控在从设备发字节的中途复位）。处置是断电重上电，\n"
        "   或在 SCL 上补 9 个时钟顶出从设备的状态机。"
        % (device, mcu)
    )


@mcp.prompt()
def diag_flash_fail(file_path: str = "<固件文件>") -> str:
    """烧不进去 / 烧录报错：先看命令，再按概率排根因。"""
    return (
        "烧录失败，按下面顺序来：\n"
        "\n"
        "1. flash_tool_detect —— 本机装了哪些烧录器、在哪（不在 PATH 的也会被找出来）。\n"
        "2. flash_firmware(file_path=%s, dry_run=True) —— **先看它准备执行的确切命令**，\n"
        "   确认器件型号/地址/文件都对，再真烧。这是本项目对不可逆操作的默认策略。\n"
        "3. 常见根因按概率排：\n"
        "   · 读保护 RDP 等级 2 —— 芯片被锁死，SWD 也连不上，只能靠 BOOT 模式或专用工具解。\n"
        "   · 选项字节/写保护误设。\n"
        "   · SWD 引脚被固件复用成普通 GPIO（烧一次能进、之后再也连不上）→ 用\n"
        "     connect-under-reset（按住复位再连）。\n"
        "   · 供电不足（大电流外设同时工作）→ 换独立供电。\n"
        "   · 时钟配置错误导致调试口停摆。\n"
        "4. 连得上但写入失败：查 Flash 擦除粒度/对齐、目标地址是否落在保留区。\n"
        "5. 烧完必须验证：flash_verify(file_path=%s, address=0x08000000)。它会读回设备\n"
        "   内容与文件**逐字节**比对，不一致时报出首个不符的偏移与绝对地址，\n"
        "   而不是只告诉你「不一致」。别只看烧录器那句「烧录成功」就当过了。"
        % file_path
    )


@mcp.prompt()
def diag_crash(map_or_elf: str = "<固件 .map 或 .elf>") -> str:
    """跑飞 / HardFault：用寄存器现场 + 固件产物定位到具体函数。"""
    return (
        "程序跑飞 / HardFault / 复位后行为异常。\n"
        "\n"
        "有寄存器现场时（最强证据，按序做）：\n"
        "1. decode_fault(cfsr=..., hfsr=..., bfar=..., mmfar=...) —— 直接翻译故障位域，\n"
        "   告诉你到底是总线错误 / 内存管理 / 用法错误，以及精确还是非精确总线故障。\n"
        "2. 有 .elf 或 .map 时（★优先 .elf）：\n"
        "   · parse_map(%s) 看 Flash/RAM 占用是否超了；\n"
        "   · symbol_lookup(%s, [\"0x...\"]) 把 PC/LR 地址换成函数名；\n"
        "   · stack_unwind(%s, frame=[...]) 还原调用栈。\n"
        "   ★ ARM Thumb 下函数地址最低位是标志位不是地址，本工具已处理（不会整体偏 1）。\n"
        "   ★ GCC/STM32CubeIDE/ESP-IDF 的 .map 默认【没有符号表】，反查会给假函数名。\n"
        "     所以只要手上有 .elf 就用 .elf；工具遇到稀疏 map 也会提示并指出 .elf 在哪。\n"
        "3. 常见根因：空指针解引用、栈溢出（查栈水位）、数组越界、DMA 与 CPU 竞争、\n"
        "   未对齐访问。\n"
        "\n"
        "暂时拿不到寄存器现场时（工具帮不上的部分）：\n"
        "· 调试器连上后读 CFSR/HFSR/BFAR/MMFAR（Cortex-M 标准故障寄存器）；\n"
        "· 或在 HardFault_Handler 里把 LR/MSP/PSP 存到固定地址，复位后再读出来。"
        % (map_or_elf, map_or_elf, map_or_elf)
    )


@mcp.prompt()
def diag_data_loss(port: str = "<你的端口>") -> str:
    """数据丢帧 / 节拍变慢 / 偶发通信失败：从真实日志倒推。"""
    return (
        "数据丢帧、采样周期变长、通信偶发失败。\n"
        "\n"
        "1. 先拿真实日志再分析：analyze_log(<日志全文>)。45 条模式库会匹配 ORE 溢出、\n"
        "   接收超时、缓冲不足等。本项目在真机上正是靠它定位到「HAL 轮询接收遇 ORE 后\n"
        "   RxState 不复位 → 接收永久僵死」这一类问题。\n"
        "   ★ 若你是用 serial_send 发指令之后设备才开始不响应：优先怀疑自动追加的 \\r\\n\n"
        "     触发了目的板 UART 溢出——工具会在未命中 expect 时给出这条提示，按提示试\n"
        "     newline=False 只发有效字节（真机实测：STM32 逐字节轮询接收的固件就怕这个）。\n"
        "2. 串口侧：波特率是否真够（采样周期 × 每帧字节数）；接收方缓冲是否足够；\n"
        "   是 DMA/环形缓冲还是逐字节轮询（逐字节最容易丢）。\n"
        "3. 中断/任务侧：接收中断优先级是否被更高优先级长时间抢占；RTOS 里任务优先级\n"
        "   与队列深度。\n"
        "4. 时间侧：采样节拍是否依赖 delay 而被别的阻塞操作拉长；看门狗是否在悄悄复位。\n"
        "5. 长时复现：stream_capture(port=%s, seconds=300) 先采几分钟落盘，再用\n"
        "   timing_analysis(path=...) 看丢帧、节拍变慢、漂移。不要只贴片段 ——\n"
        "   丢帧往往就发生在你没贴的那一段。\n"
        "   ★ timing_analysis 只有在**时间戳分辨率足够**时才给结论：若整段日志的时间戳\n"
        "     全落在同一个刻度内，它会明确拒绝给数据率/间隔/抖动（而不是编一个数）。\n"
        "\n"
        "涉及端口：%s（需要现场读时用 serial_open / serial_read）。" % (port, port)
    )


def main():
    """pip/uvx 安装后的命令行入口（mcu-debug-mcp 命令）。stdio 传输。"""
    mcp.run()


if __name__ == "__main__":
    main()
