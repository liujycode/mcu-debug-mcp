# -*- coding: utf-8 -*-
"""离线 demo：**不接任何硬件、不装 pyserial** 也能跑，直接看这个工具能干什么。

运行：

    python examples/offline_demo.py

本文件只调用 ``core.py``（零框架依赖），不启动 MCP 服务、不打开任何串口——
所以可以放心在任何机器上跑。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core  # noqa: E402


def show(title, text, max_lines=14):
    print("")
    print("=" * 76)
    print(title)
    print("=" * 76)
    lines = str(text).splitlines()
    for ln in lines[:max_lines]:
        print(ln)
    if len(lines) > max_lines:
        print("……（共 %d 行，此处只显示前 %d 行）" % (len(lines), max_lines))


def main():
    print("mcu-debug-mcp · 离线 demo（不碰硬件）")

    # 1) 日志分析：45 条故障模式库。这段日志来自 2026-09-22 真机实测：
    #    STM32 逐字节轮询接收被多余的 CRLF 触发 ORE，接收通道永久僵死。
    show("1) 日志分析：真机实测过的「ORE 把接收端拖死」日志",
         core.analyze_log(
             "boot ok\nready\n"
             "UART Error: ORE detected\n"
             "HAL_UART_Receive failed: HAL_BUSY\n"
             "rx overrun error\n"))

    # 2) 错误码查询：只答知识库收录的码，未收录会明说「查不到」
    show("2) 错误码查询（带出处）", core.error_code_lookup("esp_idf", "0x101"))
    show("3) 错误码查询：故意给一个没收录的码（应明说查不到，不猜）",
         core.error_code_lookup("esp_idf", "0xDEAD"))

    # 4) Modbus RTU 帧解析：这是一条**响应帧**（从站 1 回读 2 个保持寄存器 = 10 / 11）
    show("4) Modbus RTU 帧解析（响应帧：两个保持寄存器 = 10 / 11）",
         core.modbus_parse("01 03 04 00 0A 00 0B 9B F6"))
    print("")
    print("   （CRC 可顺手算：）")
    print("   " + core.modbus_crc("01 03 04 00 0A 00 0B").splitlines()[0])
    print("   —— 帧里 CRC 抄错时会明确报「不匹配」，不会把错帧当对的解析。")

    # 5) 引脚冲突检测：故意把 USART1_TX 分配到它不在的脚上
    show("5) 引脚冲突检测（能抓 AI 幻觉接线）",
         core.pin_check("stm32f103c8t6", {"PA1": "USART1_TX"}))

    # 6) 生态识别：这是哪个框架的工程？
    show("6) 生态识别（Keil 的工程长什么样、会报什么错）", core.ecosystem_get("keil"))

    # 7) 工程结构识别：扫标志文件判断生态
    show("7) 工程结构识别（对本仓库：不是嵌入式生态工程，明说而不是硬套）",
         core.project_detect(HERE))

    # 8) 环境自检：这台机器能干什么
    show("8) 环境自检 env_status（版本/依赖/串口/烧录器/知识库）",
         core.env_status(), max_lines=26)

    print("")
    print("=" * 76)
    print("以上全部离线完成。要读真实串口，用 MCP 客户端调 serial_list / serial_open /")
    print("serial_read / analyze_log——那几步才需要 pyserial 与真实设备（默认只读，")
    print("要发字节得显式放写权限，见 README「安全与隔离」）。")


if __name__ == "__main__":
    main()
