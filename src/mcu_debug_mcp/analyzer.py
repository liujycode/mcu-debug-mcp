# -*- coding: utf-8 -*-
"""
mcu-debug-mcp · 串口日志异常模式库（analyzer.py）

定位：把学生"看不出门道"的串口日志，翻译成"是什么问题 + 下一步干什么"。
设计约束：
- 纯标准库，零第三方依赖 —— 本模块是整个 MCP 的核心资产，必须可以独立测试
- 每条规则 = 特征识别 + 人话诊断 + 可执行的下一步，不允许只说"可能有问题"
- 输入：任意串口日志文本（含乱码也能给判断）
- 输出：findings 列表（id/严重度/证据/诊断/下一步），可直接喂给 LLM 组织回复

规则验证状态：见 tests/test_analyzer.py（每条规则正例命中 + 正常日志不误报）
"""

import json
import os
import re

# 数据根定位：与 core.py 共用唯一口径（$MCU_DEBUG_MCP_HOME > ~/.mcu-debug-mcp > 包目录）。
# 历史坑：这里曾用 dirname(__file__)/knowledge —— pip 装出来的包里没有该目录，
# 于是外置规则与错误码表**静默**失效（静默回退到内置 fallback，不报错）。
# 2026-10-13 C 档重构：包内绝对导入原生可用，平铺布局时代的
# 「把自己目录插进 sys.path」hack 退役。
from mcu_debug_mcp import paths  # noqa: E402

# ---------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------

def _printable_ratio(text):
    """可打印 ASCII+常见中文 占比。乱码日志的可打印占比会显著偏低。"""
    if not text:
        return 1.0
    ok = 0
    for ch in text:
        o = ord(ch)
        if 0x20 <= o < 0x7F or ch in "\r\n\t" or 0x4E00 <= o <= 0x9FFF:
            ok += 1
    return ok / len(text)


def _nonascii_hex_ratio(text):
    """非打印字符里"疑似波特率错位"特征：高频出现替换符/控制字符。"""
    if not text:
        return 0.0
    bad = sum(1 for ch in text if ord(ch) < 0x20 and ch not in "\r\n\t")
    repl = text.count("\ufffd")
    return (bad + repl) / len(text)


def _count_boot_banners(text):
    """识别启动 banner 重复出现的次数（复位循环的特征）。

    覆盖常见启动输出：STM32 HAL Hello / ESP8266&ESP32 ROM boot / RT-Thread / 裸机自定义 banner。
    """
    banners = [
        r"(?i)rst\s*:\s*0x",                # ESP ROM 复位行
        r"(?i)boot\s*:\s*0x",               # ESP ROM boot 行
        r"(?i)load\s*:\s*0x",               # ESP ROM load 行
        r"(?i)entry\s+0x",                  # ESP entry
        r"(?i)tail\s*:\s*0x",               # ESP tail
        r"(?i)\|\|\|\|\|\s*%",              # 一些 bootloader 花屏进度
        r"(?i)Welcome\s+to\s+RT-Thread",    # RT-Thread
        r"(?im)^\s*System\s+[Ii]nit",       # 裸机 System Init（要求行首，避免正文提及）
        r"(?i)start\s*#",                   # 常见计数启动
    ]
    counts = []
    for pat in banners:
        ms = re.findall(pat, text)
        if ms:
            counts.append(len(ms))
    if not counts:
        return 0
    # 任一 banner 模式出现次数的最大值
    return max(counts)


# HardFault 寄存器解读表（CFSR/HFSR 常见位）—— 诊断依据：ARMv7-M Architecture Reference Manual
_HARDFAULT_BITS = [
    (r"(?i)IMPRECISERR", "不精确总线错误：非法地址写入已进队列，出错指令位置已难定位，重点查野指针写/数组越界写/栈溢出"),
    (r"(?i)PRECISERR",   "精确总线错误：出错指令可直接定位，查该地址对应外设是否使能时钟、指针是否有效"),
    (r"(?i)UNDEFINSTR",  "未定义指令：函数指针跑飞 / 中断向量表错位 / 编译器库不匹配 / flash 没擦干净就跳转"),
    (r"(?i)INVSTATE",    "无效状态（EPSR.T=0）：函数指针低位没置 1（-thumb 位），或跳到了数据区当代码执行"),
    (r"(?i)INVPC",       "非法 PC 载荷：异常返回值被破坏，常见于 RTOS 上下文切换代码写错"),
    (r"(?i)DIVBYZERO",   "除零：SDIV/UDIV 除数为 0（默认未开除零陷阱时结果为0，开了就 fault）"),
    (r"(?i)UNALIGNED",   "非对齐访问：对 uint32_t* 用了未 4 字节对齐的地址（常见于强制转换报文 buffer）"),
    (r"(?i)NOCP",        "NOCP 协处理器错误：用了 FPU 指令但 FPU 未使能（启动文件 SCB->CPACR 未开 CP10/CP11）"),
    (r"(?i)FORCED",      "强制上访：本来的 fault handler 没实现被上访到 HardFault（真正原因在它前面的标志位）"),
]

# 常见 USB 转串口芯片 VID:PID → 芯片名 + 驱动提示
USB_SERIAL_VIDPID = {
    "1A86:7523": ("CH340/CH341", "装 WCH 官方 CH341SER 驱动"),
    "1A86:5523": ("CH340/CH341 变体", "装 WCH 官方 CH341SER 驱动"),
    "10C4:EA60": ("CP2102/CP210x", "装 Silicon Labs CP210x VCP 驱动"),
    "0403:6001": ("FT232R", "装 FTDI VCP 驱动"),
    "0403:6010": ("FT2232H（双通道 USB-UART）", "装 FTDI VCP 驱动；常用于 J-Link/OpenOCD 调试桥"),
    "0403:6014": ("FT232H", "装 FTDI VCP 驱动"),
    # --- ST-Link（ST 官方板载调试器）---
    # 0483:374E 实测来源：NUCLEO-H563ZI(MB1404) 板载 STLINK-V3EC，Windows 枚举
    #   为 "STMicroelectronics STLink Virtual COM Port (COM21)"，可正常打开收发。
    "0483:374E": ("ST-Link V3 虚拟串口（STLINK-V3EC/V3E）",
                  "ST-Link 驱动自带的 VCP，免驱；设备管理器无黄叹号就不用装任何东西"),
    "0483:5740": ("ST-Link 虚拟串口", "ST-Link 固件自带 VCP，装 ST-Link 驱动包"),
    "1A86:7522": ("CH9102", "装 WCH 官方驱动"),
    "1A86:55D4": ("CH9102 变体", "装 WCH 官方驱动"),
    "303A:1001": ("ESP32-Sx USB-Serial", "ESP32-S3/C3 原生 USB 串口，免驱"),
    "303A:0002": ("ESP32 USB-JTAG/Serial", "ESP32-C3/S3 的 USB JTAG 复合设备，免驱"),
    "2E8A:000A": ("Raspberry Pi Pico (CDC)", "Pico 原生 USB 串口，免驱"),
    "2E8A:0005": ("RP2040 USB CDC", "RP2040 原生 USB 串口，免驱"),
    "1366:0105": ("SEGGER J-Link CDC UART", "J-Link 自带 VCP 串口，装 SEGGER 驱动即用"),
    "2341:005E": ("Arduino (SAMD CDC)", "Arduino 原生 USB CDC，免驱"),
    "2341:804D": ("Arduino (CDC 复合设备)", "Arduino 原生 USB CDC，免驱"),
}


# ---------------------------------------------------------------
# 规则表：每条 = (id, 名称, 严重度, 检测函数, 诊断, 下一步)
# 检测函数输入整段日志文本，输出 evidence（命中的片段列表），空列表=未命中
# ---------------------------------------------------------------

def _det_mojaibake(text):
    """乱码：可打印占比低 + 控制字符/替换符高 → 波特率不匹配。"""
    if len(text) < 8:
        return []
    pr = _printable_ratio(text)
    nr = _nonascii_hex_ratio(text)
    # 日志主体基本不可读，且垃圾特征明显
    if pr < 0.55 and nr > 0.15:
        return ["可打印字符占比 %.0f%%，控制/替换字符占比 %.0f%%" % (pr * 100, nr * 100)]
    return []


def _det_at_error(text):
    """AT 连续 ERROR：指令格式/模式/固件不支持。

    必须确认处于 AT 场景（文本里出现过 AT 命令）——否则内核/工具链/curl 输出里
    孤立的 ERROR 行会被误判成"WiFi 模块拒绝执行指令"（真实日志回放已复现的假答案）。
    """
    hits = re.findall(r"(?im)^\s*(?:AT[\w+=?\"',. -]*)?\bERROR\b\s*$", text)
    if not hits:
        return []
    if not re.search(r"(?i)(?:^|[\s:>])(AT[\w+=?]{1,})", text):
        return []          # 无 AT 命令痕迹 → 不是 AT 场景，不下 AT 结论
    return hits[:5]


def _det_at_no_resp(text):
    """AT 发出但全无响应：只认带 send/tx/> 前缀的发送痕迹（避免把正文里的 at 当命令），且其后无 OK/ERROR。"""
    lonely = []
    for m in re.finditer(r"(?i)(?:send|tx|>)\s*[:>]?\s*(AT[\w+=?]{0,20})", text):
        tail = text[m.end():m.end() + 200]
        # AT 协议的响应是独立成行的 OK/ERROR，行内出现 ok 字样不算响应
        if not re.search(r"(?im)^\s*(?:\r?\n)?\s*(?:AT.*)?\b(OK|ERROR)\b\s*$", tail):
            lonely.append(m.group(1))
    if lonely:
        return lonely[:5]
    return []


def _det_boot_loop(text):
    n = _count_boot_banners(text)
    if n >= 2:
        return ["检测到 %s 次启动 banner" % n]
    # 板子自己打印的复位原因也算强证据
    extra = re.findall(r"(?i)(?:WDT|watchdog|bowdog).{0,20}(?:reset|rst|timeout)", text)
    if extra:
        return extra[:3]
    return []


def _det_hardfault(text):
    ev = []
    if re.search(r"(?i)hard\s*fault", text):
        ev.append("出现 HardFault 字样")
    return ev


def _det_hardfault_detail(text):
    """HardFault 寄存器解读（独立于字样检测，输出位级解读）。"""
    out = []
    for pat, meaning in _HARDFAULT_BITS:
        if re.search(pat, text):
            out.append("%s → %s" % (pat.replace("(?i)", ""), meaning))
    return out


# 传感器语义词：SENSOR_ZERO 必须命中其一，否则计数类日志会凑够 3 行被误判
_SENSOR_WORD = (r"(?:temp|temperature|humi|humid|light|lux|dist|distance|press|pressure|"
                r"accel|gyro|mag|alt|altitude|co2|pm2|tvoc|volt|voltage|current|adc|"
                r"温度|湿度|光照|距离|压力|加速度|角速度|海拔|电压|电流|浓度)")


def _det_sensor_zero(text):
    """数值全 0：I2C 地址错/未接线/上电时序/未初始化。

    行内必须带传感器语义词——否则 RTOS 任务表/网络统计里的
    "send queue: 0 / recv queue: 0 / dropped: 0" 会凑够 3 行，
    被误判成"传感器读数全是 0"（真实日志回放已复现的假答案）。
    """
    hits = re.findall(
        r"(?im)^[^\n\d-]*" + _SENSOR_WORD +
        r"[^\n\d-]*[:=]\s*0(?:\.0+)?\s*(?:℃|°C|%|lux|cm|m|g|Pa|hPa)?\s*$", text)
    if len(hits) >= 3:
        return hits[:5]
    return []


def _det_sensor_ff(text):
    """全 0xFF / 65535 / -1：数据线悬空/上拉缺失/MISO 断开/读到了未初始化 flash。

    ★ 2026-10-13 加语义守卫（同 SENSOR_ZERO 的范式）：旧版裸数字匹配，
      实测 `BSSID strength 255 255 255`（WiFi 信号强度日志）会凑够 3 个
      255 被误判「数据线悬空」——让用户去查杜邦线，而真问题是信号弱。
      现在两条路才认：
        ① 行尾即单个 255/65535/-1/0xFF，且键名是读数类（传感器词或
           value/data/raw/reg 这类通用读数键）、不是强度/计数类
           （strength/rssi/snr/count/queue/level/dropped…）——「key: 值」
           的传感器日志形态；WiFi 强度行内多值且带 strength，被排除
        ② 连续多字节 0xFF 的 dump 形态（寄存器/数据 dump 本身就是
           "读到的原始字节"，无需语义词；但 `255 255 255` 这类十进制
           强度值不算 dump 形态）
    """
    wordy = re.findall(
        r"(?im)^(?![^\n]*(?:strength|rssi|snr|signal|count|queue|level|dropped"
        r"|retry|loss|free|used|remain|pending|miss|fail))"
        r"[^\n\d-]*(?:" + _SENSOR_WORD +
        r"|value|val|data|raw|reg|read)[^\n\d-]*"
        r"[:=]\s*(?:255|65535|-1|0[xX][fF]{1,2})\s*$", text)
    dumps = re.findall(
        r"(?im)(?:0[xX][fF]{2}(?:\s*[, ]\s*0[xX][fF]{2}){2,}"
        r"|\b[fF]{2}(?:[ ]+[fF]{2}){2,}\b)", text)
    hits = wordy + dumps
    if len(hits) >= 3:
        return hits[:5]
    return []


def _det_ds18b20_85(text):
    """DS18B20 恒 85.0（其复位寄存器值）：转换命令没发/时序错/寄生电源供电不足。

    只认"整行 85.0"或"带温度语义且带小数/单位"的行——旧的 `85$` 会命中任何以 85 结尾的行
    （如寄存器原始值 "temp raw: 85"，真实日志回放已复现的假答案）。
    """
    hits = re.findall(
        r"(?im)^\s*85\.0+\s*(?:℃|°C)?\s*$"
        r"|^\s*85\s*(?:℃|°C)\s*$"
        r"|^[^\n]*(?:temp|temperature|温度)[^\n]*[:=]\s*85(?:\.0+)?\s*(?:℃|°C)\s*$"
        r"|^[^\n]*(?:temp|temperature|温度)[^\n]*[:=]\s*85\.0+\s*$",
        text)
    if len(hits) >= 3:
        return hits[:5]
    return []


def _det_dht_timeout(text):
    """DHT11/22 超时或校验失败。

    必须带 DHT 型号前缀——裸的 "timeout"/"checksum error" 是 TCP/文件/校验场景的通用词，
    会被误判成 DHT 时序问题（真实日志回放已复现的假答案）。
    """
    hits = re.findall(
        r"(?i)(?:DHT11|DHT22|DHT\d{2})[^\r\n]{0,40}(?:timeout|超时|checksum|校验|fail|err)",
        text)
    if hits:
        return hits[:5]
    return []


def _det_neg_invalid(text):
    """异常负值：常见传感器错误码（-127、-999 等 DS18B20/BMP 系列错误返回）。"""
    hits = re.findall(r"(?im)-\s*(?:127|999|1000)(?:\.0+)?", text)
    if len(hits) >= 2:
        return hits[:5]
    return []


def _det_i2c_nack(text):
    """I2C NACK/ADDR 错误。"""
    pats = [
        r"(?i)(?:i2c|sda|scl)[^\r\n]{0,30}\bNACK\b",
        r"(?i)\bNACK\b[^\r\n]{0,30}(?:addr|i2c|0x[0-9a-f]{2})",
        r"(?i)ADDR\s*ACK",
        r"(?i)i2c\s*(?:err|error|fail)",
        r"(?i)\back\s*fail(?:ure)?\b",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    if out:
        return out[:5]
    return []


def _det_mqtt_tcp(text):
    """ESP8266/ESP32 网络链路错误（毕设联网环节最常见）。+CME ERROR 挪到 AT_CME_ERROR 细分错误码。"""
    pats = [
        r"(?i)WIFI\s*DISCONNECT", r"(?i)CLOSED", r"(?i)connection\s*(?:refused|timeout|failed)",
        r"(?i)DNS\s*(?:fail|error|Fail)", r"(?i)link\s+is\s+not\s+valid",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:5]


# ---------------- 以下为 v2 扩充规则（13 → 30 类） ----------------

# ESP8266/ESP32 +CWJAP 错误码（依据 Espressif AT 指令集手册）
# 码表从 knowledge/error_codes/*.json 加载；内置 fallback 保底（JSON 缺失不炸）
_CWJAP_FALLBACK = {
    "1": "连接超时——路由器无响应", "2": "密码错误", "3": "找不到目标 AP", "4": "连接失败",
}


def _load_code_table(fname, prefix, fallback):
    """从 knowledge/error_codes/<fname>.json 加载码表 → {code: "prefix%code = meaning：hint"}"""
    def _fmt(k, meaning, hint):
        s = "%s = %s" % (prefix % k, meaning)
        if hint:
            s += "：" + hint
        return s
    try:
        path = paths.find("knowledge", "error_codes", fname)
        if not path:
            raise IOError("知识库未找到：knowledge/error_codes/%s" % fname)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = {}
        for k, v in (data.get("entries") or {}).items():
            out[k] = _fmt(k, v.get("meaning", ""), v.get("hint"))
        return out or {k: _fmt(k, m, "") for k, m in fallback.items()}
    except Exception:
        return {k: _fmt(k, m, "") for k, m in fallback.items()}


_CWJAP_CODES = _load_code_table("cwjap.json", "+CWJAP:%s", _CWJAP_FALLBACK)

# +CME ERROR 常见错误码（3GPP TS 27.007 常用子集）
# fallback 提成命名常量：与 _CWJAP_FALLBACK / core._MODBUS_EXC_FALLBACK 结构统一，
# 并可被 tests/audit_constants.py 机器校验「fallback 是否与知识库同步」。
_CME_FALLBACK = {
    "3": "operation not allowed（操作不允许）",
    "10": "SIM not inserted（SIM 未插入）",
    "11": "SIM PIN required（需要 PIN 码解锁）",
    "12": "SIM PUK required（PIN 已锁死，需 PUK 解锁）",
    "13": "SIM failure（SIM 故障）",
    "14": "SIM busy（SIM 正忙）",
    "15": "SIM wrong（SIM 不识别/不匹配）",
    "16": "incorrect password（密码错误，含 PIN/PIN2/PUK 尝试失败）",
}
_CME_CODES = _load_code_table("cme.json", "+CME ERROR:%s", _CME_FALLBACK)

# PubSubClient connect 返回码（毕设 MQTT 最高频库）
_MQTT_RC = _load_code_table("mqtt_rc.json", "MQTT rc=%s", {
    "-1": "连接失败：服务器地址/端口错或网络未就绪",
    "-2": "连接失败：TCP 层没通，先确认 broker IP 可 ping 通",
    "-3": "连接失败：TCP 通了但 CONNECT 包被拒——clientId 重复/认证信息错",
    "-4": "连接超时：服务器无响应，查端口与防火墙",
    "-5": "已断开：连接建立后被踢，多为 clientId 冲突（两台设备同 ID 互踢）",
})


def _det_esp_panic(text):
    """ESP 系崩溃：Guru Meditation / panic'ed / abort() / Backtrace。"""
    pats = [
        r"(?i)Guru\s*Meditation\s*Error", r"(?i)panic'ed", r"(?i)abort\(\)\s*was\s+called",
        r"(?i)^Backtrace:\s*0x",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_esp_brownout(text):
    """ESP 掉电检测触发：供电不足的标志性报错。"""
    return re.findall(r"(?i)Brownout\s+detector\s+was\s+triggered", text)[:3]


def _det_esp_wdt(text):
    """ESP 看门狗：任务看门狗触发 / WDT 复位。"""
    pats = [
        r"(?i)Task\s+watchdog\s+got\s+triggered", r"(?i)TG[01]WDT_[A-Z_]+_RST",
        r"(?i)\brst:0x[0-9A-Fa-f]+\s*\((?:TG\d?WDT|SW_CPU)_RST\)",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_esp_flash_corrupt(text):
    """ESP 固件镜像损坏/烧录偏移错误。"""
    pats = [
        r"(?i)image\s+magic\s+mismatch", r"(?i)flash\s+read\s+err",
        r"(?i)invalid\s+header:\s*0x[0-9A-Fa-f]+", r"(?i)bootloop\s*\(load\s+error\)",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_cwjap_err(text):
    """ESP-AT 连 WiFi 失败错误码。"""
    out = []
    for m in re.finditer(r"(?i)\+CWJAP:\s*([1-4])", text):
        code = _CWJAP_CODES.get(m.group(1))
        if code:
            out.append(code)
    return out[:3]


def _det_cme_error(text):
    """+CME ERROR 错误码/文本形式。"""
    out = []
    for m in re.finditer(r"(?i)\+CME\s+ERROR:\s*(\d+)", text):
        code = _CME_CODES.get(m.group(1))
        out.append(code if code else "+CME ERROR:%s（3GPP 错误码，查 TS 27.007）" % m.group(1))
    out.extend(re.findall(r"(?i)\+CME\s+ERROR:\s*(?!$)[A-Za-z][^\r\n]{0,40}", text))
    return out[:3]


def _det_rtos_assert(text):
    """RTOS/HAL 断言失败：assert failed / configASSERT。"""
    pats = [
        r"(?i)assert\s+failed[^\r\n]{0,60}", r"(?i)configASSERT\s*\(",
        r"(?i)Assertion\s+failed[^\r\n]{0,60}",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_rtos_stack_overflow(text):
    """RTOS 任务栈溢出（FreeRTOS/ESP 报警文本）。"""
    pats = [
        r"(?i)\*\*\*ERROR\*\*\*\s+Stack\s+overflow", r"(?i)stack\s+overflow\s+(?:in|detected)",
        r"(?i)stack\s+canary\s+watchpoint\s+triggered\s*\((\w+)\)",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_compile_undef_ref(text):
    """链接期未定义符号（把编译报错贴进来也能分析）。"""
    pats = [
        r"(?i)undefined\s+reference\s+to\s+[`'\"]?(\w+)",
        r"(?i)undefined\s+symbol\s+(\w+)",
        r"(?i)L6218E:[^\r\n]*Undefined\s+symbol\s+(\w+)",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:5]


def _det_compile_syntax_err(text):
    """编译期错误：gcc 文件:行:列 / Keil 文件(行) / armcc error: #N / MSVC error C1234。"""
    pats = [
        r"(\S+\.(?:c|h|cpp|hpp)):\d+:\d*:\s*(?:fatal\s+)?error:[^\r\n]{0,50}",
        r"(\S+\.(?:c|h|cpp|hpp))\(\d+\):\s*(?:fatal\s+)?error:[^\r\n]{0,50}",
        r"(?i)error\s*:\s*#\d+:[^\r\n]{0,50}",
        r"(?i)error\s+C\d{4}[^\r\n]{0,50}",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:5]


def _det_i2c_scan_empty(text):
    """I2C 扫描完成但一个设备都没扫到。"""
    pats = [
        r"(?i)(?:I2C\s*)?scan[^\r\n]{0,40}(?:0\s+devices?|no\s+devices?|no\s+I2C|not\s+found)",
        r"(?i)No\s+I2C\s+devices?\s+found",
        r"(?i)devices?\s+found:\s*0\b",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_uart_framing(text):
    """UART 帧错误/溢出错误标志（波特率不匹配或中断阻塞的硬件层证据）。"""
    pats = [
        r"(?i)(?:framing|overrun|noise)\s*error",
        r"(?i)(?:UART|USART|serial|rx|receive)[^\r\n]{0,30}\b(?:ORE|overrun)\b",
        r"(?i)\b(?:rx|receive)\s+overrun\b",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_hal_assert(text):
    """STM32 HAL 参数断言（assert_param）触发。"""
    pats = [
        r"(?i)assert_param[^\r\n]{0,60}", r"(?i)HAL_ASSERT[^\r\n]{0,60}",
        r"(?i)Wrong\s+parameters?[^\r\n]{0,40}",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_adc_saturated(text):
    """ADC 读数恒满量程：输入浮空/直连 VCC/增益配置错。要求行内带 ADC 关键字。"""
    hits = re.findall(r"(?i)\bADC[^\r\n]{0,30}\b(?:4095|1023|65535|32767)\b", text)
    if len(hits) >= 2:
        return hits[:5]
    return []


def _det_mqtt_connect_fail(text):
    """MQTT 连接失败（PubSubClient rc 负值码 / 文本报错）。"""
    out = []
    for m in re.finditer(r"(?i)\brc\s*[:=]\s*(-[1-5])\b", text):
        code = _MQTT_RC.get(m.group(1))
        if code:
            out.append(code)
    out.extend(re.findall(r"(?i)mqtt[^\r\n]{0,50}(?:fail|refus|timeout|error|denied)", text))
    return out[:3]


def _det_http_err_status(text):
    """HTTP 4xx/5xx 状态码或 curl 网络错误。"""
    pats = [
        r"HTTP/1\.[01]\s+[45]\d\d[^\r\n]{0,30}",
        r"(?i)curl:\s*\(\d+\)[^\r\n]{0,40}",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


def _det_sd_init_fail(text):
    """SD 卡初始化/挂载失败。"""
    pats = [
        r"(?i)SD[^\r\n]{0,20}(?:init[^\r\n]{0,10}fail|mount\s+fail|not\s+init)",
        r"(?i)cardType:\s*0\b",
        r"(?i)FR_NOT_(?:READY|INITIALIZED|DISK_ERR)",
        r"(?i)f_mount[^\r\n]{0,20}FR_[A-Z_]+",
    ]
    out = []
    for p in pats:
        out.extend(re.findall(p, text))
    return out[:3]


# ---------------- 可外置规则（knowledge/fault_patterns.json） ----------------
# 架构约定：正则模式类规则放 JSON（社区/AI 可贡献，带 source 来源标注），
# 复杂逻辑类规则（比例/计数/阈值）留在本文件内置。运行时两者合并。

_EXTRA_RULES = []   # [(id, name, severity, det_fn), ...]
_EXTRA_DIAG = {}    # id -> (diagnosis, next_steps)


def _load_pattern_rules():
    path = paths.find("knowledge", "fault_patterns.json")
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    for item in data.get("rules", []):
        rid = item.get("id")
        pats = item.get("patterns") or []
        if not rid or not pats:
            continue

        def _make_det(patterns, min_count=1):
            def det(text):
                out = []
                for p in patterns:
                    try:
                        found = re.findall(p, text)
                    except re.error:
                        continue
                    if min_count > 1 and len(found) < min_count:
                        continue        # 计数门槛不达标（如断连 ≥3 次才算风暴）
                    out.extend(found)
                return out[:3]
            return det

        _EXTRA_RULES.append((rid, item.get("name", rid), item.get("severity", "medium"),
                             _make_det(pats, int(item.get("min_count", 1) or 1))))
        _EXTRA_DIAG[rid] = (item.get("diagnosis", ""), item.get("next_steps", []))


_load_pattern_rules()


def rule_count():
    """当前生效规则总数（内置 + 外置）。README/自检用。"""
    return len(RULES) + len(_EXTRA_RULES)


RULES = [
    ("MOJIBAKE",      "波特率不匹配（整段乱码）",        "high",   _det_mojaibake),
    ("AT_ERROR",      "AT 指令返回 ERROR",               "medium", _det_at_error),
    ("AT_NO_RESP",    "AT 指令无任何响应",               "high",   _det_at_no_resp),
    ("BOOT_LOOP",     "板子在反复重启",                  "high",   _det_boot_loop),
    ("HARDFAULT",     "HardFault（程序跑飞）",           "high",   _det_hardfault),
    ("HF_DETAIL",     "HardFault 寄存器解读",            "high",   _det_hardfault_detail),
    ("SENSOR_ZERO",   "数值全是 0",                      "medium", _det_sensor_zero),
    ("SENSOR_FF",     "数据全是 0xFF/65535/-1",          "high",   _det_sensor_ff),
    ("DS18B20_85",    "DS18B20 恒 85.0°C",               "medium", _det_ds18b20_85),
    ("DHT_TIMEOUT",   "DHT 系列超时/校验失败",           "medium", _det_dht_timeout),
    ("NEG_INVALID",   "出现异常负值（传感器错误码）",    "medium", _det_neg_invalid),
    ("I2C_NACK",      "I2C 应答失败（NACK）",            "medium", _det_i2c_nack),
    ("NET_LINK",      "WiFi/网络链路错误",               "medium", _det_mqtt_tcp),
    # --- v2 扩充（ESP 生态 / RTOS / 编译报错 / 总线硬件层 / IoT 协议） ---
    ("ESP_PANIC",     "ESP 崩溃（Guru Meditation）",     "high",   _det_esp_panic),
    ("ESP_BROWNOUT",  "ESP 掉电检测触发",                "high",   _det_esp_brownout),
    ("ESP_WDT",       "ESP 看门狗触发/复位",             "high",   _det_esp_wdt),
    ("ESP_FLASH_CORRUPT", "ESP 固件镜像损坏",            "high",   _det_esp_flash_corrupt),
    ("CWJAP_ERR",     "ESP-AT 连 WiFi 失败错误码",       "medium", _det_cwjap_err),
    ("AT_CME_ERROR",  "AT +CME ERROR（错误码解读）",     "medium", _det_cme_error),
    ("RTOS_ASSERT",   "RTOS/HAL 断言失败",               "high",   _det_rtos_assert),
    ("RTOS_STACK_OVF", "RTOS 任务栈溢出",                "high",   _det_rtos_stack_overflow),
    ("COMPILE_UNDEF", "链接错误：未定义符号",            "medium", _det_compile_undef_ref),
    ("COMPILE_SYNTAX", "编译错误（文件:行号）",          "medium", _det_compile_syntax_err),
    ("I2C_SCAN_EMPTY", "I2C 扫描零设备",                 "medium", _det_i2c_scan_empty),
    ("UART_FRAMING",  "UART 帧错误/溢出标志",            "medium", _det_uart_framing),
    ("HAL_ASSERT",    "STM32 HAL assert_param 触发",     "high",   _det_hal_assert),
    ("ADC_SATURATED", "ADC 读数恒满量程",                "medium", _det_adc_saturated),
    ("MQTT_FAIL",     "MQTT 连接失败",                   "medium", _det_mqtt_connect_fail),
    ("HTTP_ERR",      "HTTP 4xx/5xx 或 curl 错误",       "low",    _det_http_err_status),
    ("SD_INIT_FAIL",  "SD 卡初始化/挂载失败",            "medium", _det_sd_init_fail),
]

_DIAG = {
    "MOJIBAKE": ("波特率（或电平）不匹配——你的串口工具和单片机的波特率对不上，数据被采样错了。"
                 "【例外】若你现在用的是芯片自带 USB 口（ESP32-S3/C3、RP2040 等 USB CDC），"
                 "波特率不参与数据路径，乱码/二进制不是波特率问题，别按下面第 1 条换档。",
                 ["把串口波特率依次换 115200 / 9600 / 74880 / 38400 / 19200 逐个试"
                  "（STM32 HAL 默认常用 115200，老例程常用 9600）——"
                  "★但 USB CDC 口跳过这一步，该口换波特率无效",
                  "换了所有波特率仍乱 → 查电平：3.3V 板子接了 5V 模块 TX？USB 转 TTL 的 VCC 接法是否正确",
                  "USB CDC 口上仍读到二进制：那是设备本来就输出的协议帧/二进制日志，"
                  "不是乱码，改用 hex_mode 收发或看设备固件是否该打文本日志",
                  "用本 MCP 的 serial_read 换波特率重读一次验证"]),
    "AT_ERROR": ("WiFi 模块收到了指令但拒绝执行：多数是指令格式错，少数是当前模式不允许该指令。",
                 ["检查每条 AT 指令结尾必须是 \\r\\n（回车+换行），不是 \\n",
                  "先发 AT 测试基本通信，再发 AT+CWMODE=1 等配置指令",
                  "查模块固件版本支持的指令集（ESP8266 老固件没有 MQTT 系列指令）",
                  "把串口回显打开（ATE1）方便对照实际收到的是什么"]),
    "AT_NO_RESP": ("模块完全没回话：接线、供电、波特率三者之一有问题（不是指令的问题）。",
                   ["确认 TX/RX 没接反：模块 RX 接 MCU TX，模块 TX 接 MCU RX（新手第一大坑）",
                    "确认模块供电：ESP8266 要 3.3V 且电流 ≥500mA，接 MCU 板载 3.3V 引脚常因电流不足而不工作",
                    "查模块默认波特率（ESP8266 出厂 115200 或 9600），用 serial_read 多个波特率试探"]),
    "BOOT_LOOP": ("板子在反复复位：通常是供电不足或程序在启动早期就崩溃。",
                  ["换供电：USB 供电不足时换独立 5V/2A 电源，共地",
                   "如果是接了外设后才出现 → 断开外设逐个排除（大电容/电机/继电器拉低电源）",
                   "看复位原因：打印 RCC->RSR / 检查是否开了 IWDG 但没喂狗",
                   "把 main 里外设初始化逐段注释，二分定位崩溃点"]),
    "HARDFAULT": ("程序跑飞进了 HardFault 中断：非法内存访问或非法指令。跟着寄存器解读定位。",
                  ["在 HardFault_Handler 里把栈上的 PC/LR/PSR 和 SCB->CFSR/HFSR 打印出来",
                   "常见三大来源：野指针/数组越界写、中断里调用了不允许的函数、函数指针错乱",
                   "用本工具输出的寄存器解读定位具体 fault 类型"]),
    "HF_DETAIL": ("HardFault 寄存器位级解读（按命中的标志位定位原因）。",
                  ["按解读出的具体 fault 类型处理；IMPRECISERR 时在 SCB->ACTLR 置 DISDEFWBUF 可精确定位（STM32F4 适用）"]),
    "SENSOR_ZERO": ("传感器读数全是 0：数据根本没读进来，不是传感器坏了就是没读对。",
                    ["查接线：信号线/SDA/SCL 是否接对引脚，是否共地",
                     "I2C 传感器先跑 I2C 扫描（或用本 MCP 提示的地址确认），确认 7 位地址（常见 0x76/0x68/0x23）",
                     "读之前确认传感器已上电完成初始化（多数模块要 100ms~2s 启动时间，上电立刻读会得到 0）",
                     "ADC 类传感器：确认 ADC 通道/引脚映射没配错，参考电压是否正确"]),
    "SENSOR_FF": ("读到全 0xFF/65535：总线数据线悬空的典型特征——MCU 每个位都读到 1。",
                  ["查数据线是否虚焊/断线（杜邦线母头松动高发）",
                   "I2C/SPI 数据线必须上拉（I2C 4.7kΩ），SPI 的 MISO 悬空也读 FF",
                   "确认读的寄存器/地址是否正确——读错地址常见返回 FF",
                   "DS18B20 读 FF：检查 4.7kΩ 上拉电阻是否接在 DQ 和 VCC 之间"]),
    "DS18B20_85": ("DS18B20 的 85.0°C 是它上电复位的默认温度值——说明你读了温度寄存器，但转换命令没生效。",
                   ["发转换命令 0x44 后必须等待 ≥750ms（12 位分辨率）再发读命令 0xBE",
                    "时序用微秒级延时，us 级 delay 不准会让器件不响应",
                    "寄生供电模式（VCC 悬空）在转换瞬间电流不够会失败——直接给 VCC 供电最稳"]),
    "DHT_TIMEOUT": ("DHT11/22 无响应或校验失败：单总线时序很挑，中断一打断就崩。",
                    ["读时序期间关中断（__disable_irq/__enable_irq 包住整个读时序）",
                     "确认数据线有 4.7k~10k 上拉",
                     "起始信号拉低 ≥18ms 不能少；DHT22 和 DHT11 的数据格式不同，库要对应",
                     "两次读取间隔 ≥1s（DHT11 采样率 1Hz），读太快会失败"]),
    "NEG_INVALID": ("读到 -127/-999 这类固定负值：多数是传感器驱动的错误码，不是真实温度。",
                    ["查所用库的错误码定义（DS18B20 库 -127=无应答；BMP 系列 -999=初始化失败）",
                     "对应查初始化流程和接线"]),
    "I2C_NACK": ("I2C 从机没应答：地址不对/器件没上电/总线被拉死。",
                 ["跑 I2C 地址扫描确认 7 位地址（有的模块手册写 8 位写地址，要右移一位）",
                  "确认 SDA/SCL 上拉电阻存在（多数模块板载，裸传感器没有）",
                  "检查是否把 SDA/SCL 接反；确认 GPIO 复用成了开漏模式"]),
    "NET_LINK": ("WiFi/网络链路断了：联网毕设最常见环节，先保链路再谈业务。",
                 ["WIFI DISCONNECT：查路由器 2.4G（ESP8266 不支持 5G）、密码、信号强度",
                  "DNS fail：改用固定 IP 测试，排除 DNS 配置",
                  "connection refused：服务器没监听该端口/防火墙拦截，先用网络调试助手模拟服务器验证",
                  "云平台连接失败：查三元组/Token 是否抄错，时间戳是否过期"]),
    # --- v2 扩充 ---
    "ESP_PANIC": ("ESP32/ESP8266 崩溃了：Guru Meditation 是 ESP 系的 HardFault。Backtrace 里有出错地址。",
                  ["把完整 Backtrace 用 idf.py monitor 或 addr2line 解析成函数名（纯串口日志看不懂地址）",
                   "常见原因：空指针解引用、中断里做耗时操作、任务栈溢出",
                   "如果 panic 发生在 WiFi 初始化后 → 先查供电（见 ESP_BROWNOUT）"]),
    "ESP_BROWNOUT": ("电源电压跌落触发了 ESP 内部掉电检测：不是软件 bug，是供电撑不住。",
                     ["换粗一点短一点的 USB 线（细长线压降大，头号坑）",
                      "WiFi 发射瞬间电流峰值 300mA+，接电脑 USB 口或板载 3.3V LDO 都可能不够，换独立 3.3V/1A 电源并共地",
                      "外设（继电器/电机/WS2812）和 ESP 共电源时加 470uF~1000uF 电解电容"]),
    "ESP_WDT": ("看门狗超时：某个任务长时间不喂狗（死循环/阻塞太久）。",
                ["耗时循环里加 vTaskDelay(1) 或 yield() 让出 CPU",
                 "中断服务函数（ISR）里严禁 vTaskDelay/printf/长循环",
                 "确认触发的是哪个任务：日志里 Task watchdog got triggered 后面有任务名"]),
    "ESP_FLASH_CORRUPT": ("固件镜像校验失败：烧录不完整、波特率不稳或分区表错位。",
                          ["重烧：esptool 擦除全片（erase_flash）后重新烧录，烧录时降波特率 115200 更稳",
                          "确认烧录偏移：ESP32 app 0x10000、bootloader 0x1000；ESP8266 从 0x0 起",
                          "刚改过分区表/不用 OTA 却改了 layout → 恢复默认分区表"]),
    "CWJAP_ERR": ("连 WiFi 失败，ESP-AT 给出了具体错误码（按码定位）。",
                  ["按错误码提示处理：1=超时查路由器 2.4G、2=核对密码、3=查 SSID 拼写、4=查路由器拒绝原因",
                   "先 AT+CWJAP? 确认当前连接状态，AT+CWLAP 列出可见 AP 确认目标在列表里"]),
    "AT_CME_ERROR": ("模块返回了 CME 错误码：这是标准 3GPP 错误码，按码查原因。",
                     ["按解读的错误码处理：3=状态不对（先查当前模式/是否已注册网络）",
                      "带 SIM 卡模块：10/11/12/13 都是卡的问题——查卡插没插好、PIN 解没解"]),
    "RTOS_ASSERT": ("断言失败（configASSERT/assert）：代码里某处的前提条件被打破了，RTOS 直接停车。",
                    ["日志里断言带文件名和行号——直接跳过去看哪个条件挂了",
                     "FreeRTOS 高频原因：在中断里调用非 FromISR 版本的 API",
                     "队列/信号量创建失败后没判空就使用"]),
    "RTOS_STACK_OVF": ("任务栈溢出：某任务的栈开小了，写穿到了相邻内存。",
                       ["日志里有任务名——把该任务 xTaskCreate 的栈深度加大 2~4 倍（单位是字不是字节！usStackDepth×4 才是字节数）",
                        "printf/局部大数组是吃栈大户，改用堆缓冲或缩小局部变量",
                        "临时调试可开 configCHECK_FOR_STACK_OVERFLOW=2 定位"]),
    "COMPILE_UNDEF": ("链接器找不到函数/变量的定义：头文件声明了（所以编译过了）但没有实现（链接挂了）。",
                      ["查函数名拼写与声明是否一致（C++ 还要 extern \"C\" 包裹）",
                       "对应的 .c 文件没加进工程/没编译（Keil 看源文件组，CMake 看 add_executable 列表）",
                       "库函数未定义：确认勾选了对应外设库（如 stm32f4xx_hal_uart.c）"]),
    "COMPILE_SYNTAX": ("编译器报语法/类型错误：定位到文件和行号直接看。",
                       ["按 文件:行号 跳过去——常见：少了分号、用了未定义类型、中文标点混进代码",
                        "Keil 的 error C 消码可查手册；error # 数字是 armcc/TI 编译器风格",
                        "如果报错在头文件里，多半是宏/包含顺序问题，不是那一行本身"]),
    "I2C_SCAN_EMPTY": ("I2C 总线扫描完成但一个设备都没找到：总线本身没通。",
                       ["查 SDA/SCL 是否接反、是否共地（新手第二大坑）",
                        "确认上拉电阻：裸传感器没有板载上拉，需要外接 4.7kΩ×2",
                        "确认 GPIO 配置成开漏复用模式 + I2C 时钟已使能；3.3V 器件接了 5V 总线也可能锁死",
                        "软件 I2C 的话确认延时参数没写错，先低速 100kHz 试"]),
    "UART_FRAMING": ("UART 硬件层报帧错误/溢出：采样到的数据不成帧。",
                     ["两端波特率/校验位/停止位不一致——逐项核对（含小数波特率误差 >3% 就会错帧）",
                      "时钟频率改过但波特率寄存器值没重算（换外部晶振后常见）",
                      "overrun 反复出现 → 接收中断被长时间阻塞，读完 ORE 标志并缩短中断处理"]),
    "HAL_ASSERT": ("STM32 HAL 库参数断言触发：传给 HAL 函数的参数不合法。",
                   ["日志/调试器里看 assert_param 展开的位置——哪个 HAL 函数收到了坏参数",
                    "高频原因：句柄 NULL、句柄未 HAL_UART_Init 初始化就使用、超时参数为 0",
                    "需要确认 USE_FULL_ASSERT 与否：关掉断言只是不报错，参数错照样硬故障"]),
    "ADC_SATURATED": ("ADC 读数恒为满量程：模拟输入已经顶到（超过）参考电压。",
                      ["查输入引脚是否浮空/误接 VCC；分压电阻是否断路",
                       "确认参考电压：VREF+/VDDA 与代码配置一致；输入信号幅度不能超 VDDA",
                       "确认通道映射（channel 与 GPIO 的对应关系表）与采样时间设置"]),
    "MQTT_FAIL": ("MQTT 连接失败：按返回码分层排查（TCP → MQTT 握手 → 业务）。",
                  ["先保证 WiFi 已连上且 IP 有效（AT+CIFSR 或 dhcp 结果）",
                   "rc<0 见上面错误码解读：-2 TCP 没通、-3/被拒查 clientId 重复与用户名密码",
                   "broker 地址用 IP 直连排除 DNS；端口 1883/8883（TLS）别搞混",
                   "服务器在本机调试时：手机热点与电脑不在同一网段连不通，用局域网 IP"]),
    "HTTP_ERR": ("HTTP 请求返回了错误状态码：网络通了，是服务端/请求内容的问题。",
                 ["4xx：URL 路径错、请求头缺 Content-Type、鉴权 Token 过期",
                  "5xx：服务端崩了，先看服务器日志；云平台查 API 文档的参数要求",
                  "curl 错误：无法解析主机名=DNS，无法连接=IP:端口不通"]),
    "SD_INIT_FAIL": ("SD 卡初始化/挂载失败：卡、SPI/SDIO 时序、文件系统三层之一出问题。",
                     ["查卡座接触与卡是否为 FAT32（exFAT 多数裸机库不支持）",
                      "SPI 模式：MOSI/MISO/SCK/CS 四线对位，CS 必须拉低期间才响应",
                      "卡初始化要 ≥1ms 的上电稳定 + 74 个时钟周期的 dummy 时钟（很多例程漏）",
                      "挂载失败码 FR_ 开头的看 FatFs 错误码表，FR_NOT_READY 多为底层通信没通"]),
}


def analyze_log(text, context=""):
    """分析串口日志。

    返回 dict：
      findings: [{id, name, severity, evidence, diagnosis, next_steps}]
      summary : 一句话总结（给 LLM 组织回复用）
    按 severity 排序，high 在前。
    """
    findings = []
    for rid, name, sev, det in list(RULES) + _EXTRA_RULES:
        try:
            ev = det(text)
        except Exception as e:  # 任何规则自身异常都不阻断整体分析
            ev = []
        if ev:
            pair = _DIAG.get(rid) or _EXTRA_DIAG.get(rid) or ("", [])
            diag, steps = pair
            findings.append({
                "id": rid, "name": name, "severity": sev,
                "evidence": ev, "diagnosis": diag, "next_steps": steps,
            })
    sev_order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: sev_order.get(f["severity"], 9))

    if not findings:
        summary = "未命中已知故障模式。日志内容正常或问题较特殊，建议把完整日志和代码段发出来进一步分析。"
    else:
        tops = "、".join(f["name"] for f in findings[:3])
        summary = "命中 %d 类故障模式，最可能的依次是：%s。请按 first finding 的 next_steps 逐条排查。" % (len(findings), tops)
    return {"findings": findings, "summary": summary}


#: STMicroelectronics 的 USB VID。ST-Link 各代 PID 都不一样（V2 / V2-1 / V3 /
#: V3E / DFU 各是一个），逐个列 PID 必然漏掉新板子。但 0x0483 下枚举出的
#: 「虚拟串口」只可能是 ST-Link 的 VCP（ST 自家 MCU 的原生 USB CDC 走别的
#: PID），所以按 VID 兜底既安全又不会误导。
_STLINK_VID = "0483"


def identify_usb_serial(vid_pid):
    """VID:PID → 芯片名与驱动建议。未收录返回 None。

    ★ 表里没有时按 VID 兜底：0483 开头 ⟹ ST-Link 的 VCP。
      不兜底的后果是实测过的：NUCLEO-H563ZI 的 0483:374E 落入「未收录，
      装对应厂商驱动」，把用户引去装一个根本不需要的串口驱动。
    """
    if not vid_pid:
        return None
    key = str(vid_pid).upper()
    hit = USB_SERIAL_VIDPID.get(key)
    if hit:
        return hit
    if key.startswith(_STLINK_VID + ":"):
        return ("ST-Link 虚拟串口（ST-Link V3 系；该 PID 未逐条收录）",
                "ST-Link 驱动自带的 VCP，免驱；设备管理器无黄叹号就不用装任何东西")
    return None


def suggest_baudrates(text):
    """给乱码日志一个逐个试的波特率清单（按 STM32/ESP 例程出现频率排序）。"""
    return [115200, 9600, 74880, 38400, 19200, 57600, 230400]
