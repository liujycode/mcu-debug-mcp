# -*- coding: utf-8 -*-
"""pytest 全局夹具：**测试期间强制硬件隔离**。

为什么必须由框架统一兜住（而不是每条测试各自记得）：

    开发机上常年挂着**正在使用**的串口设备（UART 桥、调试器 CDC 口……）。
    若某条测试意外走到真实路径上，它会真的打开端口、真的写字节——打断正在跑的
    调试会话；而这类事故在 CI 上永远复现不了（CI 没有设备，测试永远绿）。
    所以隔离是**测试环境的基础设施**，不能靠每条测试的作者自觉。

三层防线（层层兜底，任一层被绕过都还有下一层）：

    ① 档位锁：``MCU_DEBUG_MCP_MODE=readonly`` —— 与生产默认一致；
       写硬件、AT、波特率扫描、双口对录、烧录一律被 guard 拦下。
    ② 端口全禁：``MCU_DEBUG_MCP_PORT_DENY=*`` —— 连"打开端口"这一步都过不去，
       于是任何测试都无法接通真实设备。
    ③ 替身兜底：把 ``serial.Serial`` 换成一构造就报错的替身。
       万一将来 guard 被改出绕过路径，这一层会立刻把问题暴露成测试失败，
       而不是悄悄去碰用户的设备。

逃生门（给将来的**真机夹具**用，默认关闭）：

    ``MCU_DEBUG_MCP_TEST_ALLOW_HW=1`` 时以上三层全部不生效。
    但它必须由人显式设置——默认状态永远是隔离的。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

#: 真正的真机测试必须显式打开才会执行（见上方"逃生门"）
ALLOW_HW = (os.environ.get("MCU_DEBUG_MCP_TEST_ALLOW_HW") or "").strip().lower() in (
    "1", "true", "yes", "on")


def _isolate_process():
    """进程级隔离：在**任何测试开始前**执行（conftest 导入期）。"""
    if ALLOW_HW:
        # 仍然把档位放开写权限，否则真机夹具自己都会被拦；但端口名单交给夹具自己管
        os.environ.setdefault("MCU_DEBUG_MCP_MODE", "write")
        sys.stderr.write(
            "\n[conftest] ★ MCU_DEBUG_MCP_TEST_ALLOW_HW 已开启："
            "测试**不再隔离硬件**，请确认设备状态安全。\n\n")
        return

    # ① + ② 档位与端口名单
    os.environ["MCU_DEBUG_MCP_MODE"] = "readonly"
    os.environ["MCU_DEBUG_MCP_PORT_DENY"] = "*"
    os.environ.pop("MCU_DEBUG_MCP_PORT_ALLOW", None)
    os.environ.pop("MCU_DEBUG_MCP_NO_TOUCH_DTR", None)

    # ③ 替身兜底
    try:
        import serial
    except ImportError:
        return

    class _RealSerialForbidden(object):
        """真实串口的替身。**行为取决于当前隔离档位**：

        * 隔离中（默认，readonly/write）：一构造就 ``AssertionError`` ——
          测试若意外走到真机路径，立刻爆掉，而不是悄悄去碰用户的设备。
        * 显式解除隔离（测试用 ``isolation.relaxed_env()`` 把档位设成 off）：
          抛 ``SerialException``，模拟"这个端口打不开"。
          —— 这样「端口打不开时工具必须给出结论」这类**笼子内部的**降级逻辑
             才测得到，而且照样不碰任何真实硬件。
        """

        def __init__(self, *args, **kwargs):
            mode = (os.environ.get("MCU_DEBUG_MCP_MODE") or "").strip().lower()
            if mode == "off":
                raise serial.SerialException(
                    "(测试替身) 未打开真实端口：%r 不存在或不可用"
                    % (args[0] if args else kwargs.get("port")))
            raise AssertionError(
                "测试禁止打开真实串口（serial.Serial 已被 conftest 禁用）。\n"
                "  这条测试应当走「无效句柄 / 假件 / 离线日志」路径，而不是真机路径。\n"
                "  要测隔离层里面的降级逻辑：用 tests/isolation.py 的 relaxed_env() 包住。\n"
                "  确实需要真机：设 MCU_DEBUG_MCP_TEST_ALLOW_HW=1 后单独跑该测试。"
            )

    serial.Serial = _RealSerialForbidden


_isolate_process()
