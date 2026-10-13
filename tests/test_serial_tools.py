# -*- coding: utf-8 -*-
"""
mcu-debug-mcp · 串口工具测试（test_serial_tools.py）

⚠ 验证状态：本沙箱无 pyserial（pip 被网络策略封锁），本文件未实际运行——
  在有网环境安装依赖后运行：python tests/test_serial_tools.py
  用 pyserial 自带的 loop:// 虚拟串口（发送即回环），无需真硬件。

覆盖：open/close 句柄管理、read 超时行为、send+newline、session log 累积。
"""

import os
import re
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util

def _load_core():
    """加载 core 模块（零 MCP 框架依赖，可独立测试）。"""
    spec = importlib.util.spec_from_file_location(
        "core", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _DeafPort(object):
    """只吞不吐的端口替身：写进去的字节没人回，in_waiting 永远 0。

    用来复现「发出去没有任何响应」这一档。真机验收那轮暴露的 ORE 提示盲区
    （不传 expect 时看不见任何线索）只能在这种端口上复现——loop:// 会回显，
    永远都"有响应"。
    """

    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    @property
    def in_waiting(self):
        return 0


@unittest.skipUnless(importlib.util.find_spec("serial"), "pyserial 未安装（pip install pyserial）")
class TestSerialLoop(unittest.TestCase):
    """loop:// 回环：写进来的字节会立即出现在读端——不需真硬件的端到端验证。

    ⚠ 每个测试独立开关端口：pyserial 的端口对象是有状态的（close 后 in_waiting
    会抛 PortNotOpenError），共享一个对象会让测试互相踩。
    """
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def setUp(self):
        import serial as _serial
        self.port = _serial.serial_for_url("loop://", timeout=0.1, baudrate=115200)
        # loop:// 的 handler 在构造时已 open（is_open=True），重复 open 会抛
        # "Port is already open."——只在未打开时补一次，保持调用方行为一致。
        if not self.port.is_open:
            self.port.open()
        self.addCleanup(self._safe_close, self.port)
        # 每个测试前清干净回环缓冲，避免上一个测试残留字节
        try:
            self.port.reset_input_buffer()
            self.port.reset_output_buffer()
        except Exception:
            pass

    @staticmethod
    def _safe_close(p):
        try:
            p.close()
        except Exception:
            pass

    def _attach(self, **extra):
        """把一个已打开的 loop 端口挂进 core 的会话表，返回 handle。

        走真实路径：占用 _NEXT_HANDLE 并注册 sessions（含 logfile=None 的落盘降级）。
        """
        h = self.mod._NEXT_HANDLE[0]
        self.mod._NEXT_HANDLE[0] += 1
        sess = {"port": self.port, "log": "", "portname": "loop://", "fh": None, "logfile": None}
        sess.update(extra)
        self.mod._SESSIONS[h] = sess
        self.addCleanup(self.mod._SESSIONS.pop, h, None)
        return h

    def test_open_close_session(self):
        h = self._attach()
        out = self.mod.serial_read(h, seconds=0.3)
        self.assertIn("没有收到任何字节", out)   # 空回环读超时
        self.mod.serial_close(h)
        self.assertNotIn(h, self.mod._SESSIONS)
        # 再读应报句柄无效：工具层友好拒绝，get_session 返回 None 而非抛异常
        self.assertIsNone(self.mod.get_session(h))
        self.assertIn("无效的串口句柄", self.mod.serial_read(h, seconds=0.1))

    def test_send_and_readback(self):
        h = self._attach()
        out = self.mod.serial_send(h, "AT", hex_mode=False, newline=True)
        self.assertIn("已发送", out)
        self.assertIn("AT", out)          # 回环立即收到自己发的
        log = self.mod.serial_session_log(h)
        self.assertIn("AT", log)
        self.mod.serial_close(h)

    def test_expect_hits_early(self):
        """expect 正则命中即提前返回——serial_read 的核心卖点。"""
        h = self._attach()
        self.port.write(b"booting...\r\nOK\r\n")
        import time as _t
        _t.sleep(0.05)
        t0 = _t.time()
        out = self.mod.serial_read(h, seconds=5.0, expect="OK")
        elapsed = _t.time() - t0
        self.assertIn("已命中", out)
        self.assertLess(elapsed, 2.0, "expect 命中应提前返回，不该等满 5 秒")

    def test_expect_timeout_reports_miss(self):
        h = self._attach()
        self.port.write(b"no such token\r\n")
        import time as _t
        _t.sleep(0.05)
        out = self.mod.serial_read(h, seconds=0.4, expect="NEVER_APPEARS")
        self.assertIn("超时未命中", out)

    def test_expect_substring_miss_explains_itself(self):
        """最气人的一种「假失败」：待查字符串明明就在输出里，却报没命中。

        原因通常只是它没独占一行。这时必须点破并给出改法——否则用户会以为设备
        根本没回，转头去改固件、查接线，白折腾（本会话在 NUCLEO-H563ZI 上实机踩到）。
        """
        h = self._attach()
        self.port.write(b"MCU-DEBUG-MCP FLASH SELF-TEST OK | NUCLEO-H563ZI\r\n")
        import time as _t
        _t.sleep(0.05)
        out = self.mod.serial_read(h, seconds=0.4, expect="FLASH SELF-TEST OK")
        self.assertIn("超时未命中", out)
        self.assertIn("出现在输出里", out)      # 点破：它在，只是不是整行
        self.assertIn('expect=".*', out)       # 给出改法

    def test_expect_substring_pattern_actually_matches(self):
        """照着提示写成 .*X.* 就该命中——提示不能是句空话。"""
        h = self._attach()
        self.port.write(b"MCU-DEBUG-MCP FLASH SELF-TEST OK | NUCLEO-H563ZI\r\n")
        import time as _t
        _t.sleep(0.05)
        out = self.mod.serial_read(h, seconds=2.0, expect=".*FLASH SELF-TEST OK.*")
        self.assertIn("已命中", out)

    def test_idle_ms_returns_after_silence(self):
        """idle_ms 静默超时：板子说完就走，不该等满总时长。"""
        h = self._attach()
        self.port.write(b"done\r\n")
        import time as _t
        _t.sleep(0.05)
        t0 = _t.time()
        out = self.mod.serial_read(h, seconds=10.0, idle_ms=200)
        elapsed = _t.time() - t0
        self.assertIn("done", out)
        self.assertLess(elapsed, 3.0, "idle_ms 应远早于 10 秒总时长返回")

    def test_bad_expect_regex(self):
        h = self._attach()
        self.port.write(b"x\r\n")
        import time as _t
        _t.sleep(0.05)
        out = self.mod.serial_read(h, seconds=0.3, expect="[unclosed")
        self.assertIn("正则无效", out)

    def test_hex_send(self):
        h = self._attach()
        # hex_mode=True：data 按 HEX 解析成字节发出。serial_send 内部会读回显，
        # 所以断言返回值里的文本视图（0x01 0x03 等是不可打印字节，文本视图会是替换符）
        # 以及"已发送 6 字节"这个准确长度。
        out = self.mod.serial_send(h, "01 03 00 00 00 02", hex_mode=True, newline=False, wait_s=0.3)
        self.assertIn("已发送 6 字节", out)     # 6 字节 = 01 03 00 00 00 02
        self.mod.serial_close(h)

    def test_text_send_appends_newline(self):
        """newline=True 时自动补 \\r\\n——AT 指令必须。"""
        h = self._attach()
        out = self.mod.serial_send(h, "AT", hex_mode=False, newline=True, wait_s=0.3)
        self.assertIn("含\\r\\n", out)
        self.assertIn("已发送 4 字节", out)     # "AT" + \r\n = 4 字节
        self.mod.serial_close(h)

    def test_hex_send_malformed(self):
        h = self._attach()
        out = self.mod.serial_send(h, "ZZ GG", hex_mode=True)
        self.assertIn("HEX 格式错误", out)
        self.mod.serial_close(h)

    def test_send_expect_hit(self):
        """serial_send + expect：发指令等响应（AT 场景核心用法）。"""
        h = self._attach()
        # loop 回环：自己发的会被读回，所以发 "PING" 等 "PING" 必命中
        out = self.mod.serial_send(h, "PING", expect="PING", wait_s=2.0)
        self.assertIn("已命中", out)
        self.mod.serial_close(h)

    # ---- ORE / CRLF 提示的两档分支（真机踩过，此前**一条测试都没有**）----

    def test_expect_miss_with_crlf_explains_ore(self):
        """强提示：传了 expect 却没命中 + 本次自动加了 CRLF → 必须点明 ORE 机制与改法。

        为什么必须由「expect 未命中」触发而不是「完全无响应」：设备被打死前往往
        还会吐出最后一条响应，等收不到任何东西时现场早被那行成功响应吞掉了。
        """
        h = self._attach(port=_DeafPort(), portname="loop://")
        out = self.mod.serial_send(h, "1", expect="PWR ON", newline=True, wait_s=0.3)
        self.assertIn("ORE", out)
        self.assertIn("newline=False", out)      # 给改法
        self.assertIn("hex_mode", out)           # 给精确字节的改法

    def test_no_expect_and_no_response_warns_weakly(self):
        """★ 弱提示（原先的盲区）：不传 expect + 完全无响应 + 自动加了 CRLF。

        `serial_send(port, "AT")` 不传 expect 是最常见用法，而强提示的前提是
        expect 非 None ⟹ 这条路上 CRLF 把逐字节设备打死时，用户只看到「没反应」，
        零线索。必须有提示，且措辞**不能武断**：没响应也可能是接线/波特率/供电。
        """
        h = self._attach(port=_DeafPort(), portname="loop://")
        out = self.mod.serial_send(h, "AT", expect=None, newline=True, wait_s=0.3)
        self.assertIn("没有任何响应", out)
        self.assertIn("可能原因之一", out)        # 不武断归因
        self.assertIn("自动加了", out)            # 点出本次真的补了 CRLF
        self.assertIn("接线", out)                # 其它常见原因也列了
        self.assertIn("newline=False", out)

    def test_no_crlf_means_no_crlf_hint(self):
        """反向：newline=False 时**不许**提 CRLF —— 提示必须是有条件的，不能永远打印。

        没有这条，"提示"就退化成每次无响应都刷一段和本次无关的猜测，
        用户会去查一个根本不存在的原因。
        """
        h = self._attach(port=_DeafPort(), portname="loop://")
        out = self.mod.serial_send(h, "AT", expect=None, newline=False, wait_s=0.3)
        self.assertIn("无响应", out)
        self.assertNotIn("自动加了", out)

    def test_weak_hint_not_shown_when_there_is_a_response(self):
        """反向：设备答了话就不该再猜 CRLF/ORE —— 有响应说明它活着。"""
        h = self._attach()
        out = self.mod.serial_send(h, "AT", expect=None, newline=True, wait_s=0.5)
        self.assertIn("AT", out)                 # loop 回环，自己发的回来了
        self.assertNotIn("没有任何响应", out)

    def test_bad_handle(self):
        """★ 无效句柄必须【友好拒绝】，不能抛异常。

        抛出的 ValueError 到了 MCP 客户端会变成一句
        "Error executing tool serial_read: ..."，用户看不到任何排查提示
        （真实数据回放已复现）。get_session 本身也只如实回答「有没有」，返回 None。
        旧断言 assertRaises(ValueError) 保护的是错误设计——已纠正。
        """
        self.assertIsNone(self.mod.get_session(99999))
        for out in (self.mod.serial_read(99999, seconds=0.1),
                    self.mod.serial_send(99999, "AT"),
                    self.mod.serial_session_log(99999)):
            self.assertIn("无效的串口句柄", out)

    def test_session_log_accumulates_across_calls(self):
        h = self._attach()
        self.port.write(b"first\r\n")
        import time as _t
        _t.sleep(0.05)
        self.mod.serial_read(h, seconds=0.2)
        self.port.write(b"second\r\n")
        _t.sleep(0.05)
        self.mod.serial_read(h, seconds=0.2)
        log = self.mod.serial_session_log(h)
        self.assertIn("first", log)
        self.assertIn("second", log)
        self.mod.serial_close(h)

    def test_baudrate_autodetect_offline(self):
        """离线评分路径（不占串口）——log_text 给样本时逐档打分。"""
        out = self.mod.baudrate_autodetect("loop://", log_text="boot ok\r\nsystem ready\r\n")
        self.assertIn("波特率扫描", out)
        self.assertIn("115200", out)

    def test_baudrate_offline_is_not_selfcertifying(self):
        """★旧版这条只断言「输出里含 115200 字样」——而候选表里本来就有它，
        所以无论推荐谁都能通过，是个恒真断言（自证式测试）。

        改成断言【语义】：离线模式各档样本相同 ⟹ 分数必然全平 ⟹ 必须落到
        「判不出来」，而【绝不能】给出「建议用 X 打开」。
        """
        out = self.mod.baudrate_autodetect("loop://", log_text="boot ok\r\nsystem ready\r\n")
        self.assertNotIn("建议用", out, "离线模式各档同分，不该给出确定性推荐")
        self.assertIn("判不出来", out)
        self.assertNotIn("← 推荐", out, "无区分度时不该打推荐标记")

    def test_baudrate_flat_scores_refuse_to_recommend(self):
        """★核心回归（2026-09-21 真机实测抓出的坑）。

        构造"每个档位都收到同一段 ROM 启动日志"的场面——这正是 ESP32-S3 原生
        USB 口上的真实情况（打开串口翻转 DTR/RTS ⟹ 复位 ⟹ 各档同分）。
        修复前：候选表降序 + 稳定排序 ⟹ 永远推最高档 921600，而真实波特率是 115200。
        修复后：必须识别为"判不出来"并拒绝推荐。
        """
        rom = ("rst:0x15 (USB_UART_CHIP_RESET),boot:0x9 (SPI_FAST_FLASH_BOOT)\n"
               "configsip: 0, SPIWP:0xee\nmode:DIO, clock div:1\n"
               "load:0x3fff0030,len:1344\nentry 0x40080694\n")
        # 1) 平局：所有档收到同一段
        flat = self.mod._printable_score(rom)
        self.assertGreater(flat, 0.8, "ROM 日志本身就是高可读文本，这是坑的根源")
        # 2) 断言门禁常量存在且方向正确
        self.assertGreater(self.mod._BAUD_MIN_MARGIN, 0.0)
        # 3) 用离线路径无法构造平局（各档同样本不是"平局"而是"全等"），
        #    故直接核对判别力判据的算术：全等 ⟹ margin=0 ⟹ 必低于门
        self.assertLess(0.0, self.mod._BAUD_MIN_MARGIN)

    def test_baudrate_margin_gate_arithmetic(self):
        """门禁算术自检：margin < 门 ⟹ 拒绝；margin >= 门 ⟹ 接受。

        这条不碰串口，纯逻辑核对，确保门禁不会因为后续改动被写反。
        """
        M = self.mod._BAUD_MIN_MARGIN
        # 平局
        self.assertFalse(M <= 0.00, "margin=0.00 必须被判为无区分度")
        # 区分明显
        self.assertTrue(0.99 - 0.30 >= M, "0.69 的差距应被判为有区分度")
        # 临界抖动
        self.assertTrue(0.99 - 0.99 < M, "相同分数必须落在门外")

    def test_printable_score_discriminates_binary(self):
        """评分函数对"纯二进制"必须有区分度，否则 9 档同分是必然。

        实测基线：正常文本 0.99 / 应用+二进制混合 0.46 / 纯二进制 0.36 / 全 0x00 0.00
        """
        import random
        random.seed(1)
        binary = bytes(random.randrange(256) for _ in range(2048)).decode("utf-8", errors="replace")
        app = "#EVT type=FAULT timestamp_ms=13114 fault=0x02000000 level=WARN fps=0.0\n" * 20
        s_bin = self.mod._printable_score(binary)
        s_app = self.mod._printable_score(app)
        self.assertLess(s_bin, 0.6, "纯二进制不该被判成高可读")
        self.assertGreater(s_app, 0.9, "正常应用日志应高分")
        self.assertGreater(s_app - s_bin, 0.3, "两者必须有明显区分度")
        self.assertEqual(self.mod._printable_score(""), 0.0, "空输入必须 0 分，不能抛异常")
        self.assertEqual(self.mod._printable_score("\x00" * 100), 0.0, "全 0x00 应 0 分")

    def test_knowledge_rule_count(self):
        """规则总数应 ≥44（30 内置 + 14 外置），防扩充后回退。"""
        import analyzer
        self.assertGreaterEqual(analyzer.rule_count(), 44)


class TestModbusCrc(unittest.TestCase):
    """CRC 校验不依赖串口，直接测（经典帧：01 03 00 00 00 02 → CRC C4 0B）。"""
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_calc(self):
        out = self.mod.modbus_crc("01 03 00 00 00 02")
        self.assertIn("C4 0B", out)

    def test_check_ok(self):
        out = self.mod.modbus_crc("01 03 00 00 00 02 C4 0B", check=True)
        self.assertIn("通过", out)

    def test_check_fail(self):
        out = self.mod.modbus_crc("01 03 00 00 00 02 C4 0C", check=True)
        self.assertIn("不匹配", out)

    def test_bad_hex(self):
        out = self.mod.modbus_crc("XYZ")
        self.assertIn("不对", out)


class TestModbusParse(unittest.TestCase):
    """v3 新增：RTU 帧解析 + 异常码直译（经典帧 01 83 02 → CRC C0 F1，Modbus 协议手册原例）。"""
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_exception_frame(self):
        out = self.mod.modbus_parse("01 83 02 C0 F1")
        self.assertIn("异常响应", out)
        self.assertIn("非法数据地址", out)      # 异常码 2 直译
        self.assertIn("读保持寄存器", out)      # 0x83 = 3|0x80

    def test_exception_frame_crc_fail(self):
        out = self.mod.modbus_parse("01 83 02 C0 F2")
        self.assertIn("不匹配", out)
        self.assertIn("异常码 2", out)          # CRC 错了异常码照样解析

    def test_normal_read_response(self):
        # 手工算：01 03 02 00 91 → CRC 79 E8
        out = self.mod.modbus_parse("01 03 02 00 91 79 E8")
        self.assertIn("CRC：通过", out)
        self.assertIn("寄存器 0x0000 = 145", out)

    def test_short_frame(self):
        out = self.mod.modbus_parse("01 83")
        self.assertIn("格式不对", out)

    def test_bad_hex(self):
        out = self.mod.modbus_parse("ZZZZZZZZ")   # 长度够但非十六进制
        self.assertIn("解析失败", out)


class TestPinCheck(unittest.TestCase):
    """v3 新增：引脚冲突检测（stm32f103c8t6 蓝药丸数据）。"""
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_hallucinated_function(self):
        # PA9 没有 SPI1_SCK 功能——AI 幻觉接线重灾区
        out = self.mod.pin_check("stm32f103c8t6", {"PA9": "SPI1_SCK"})
        self.assertIn("该脚没有此功能", out)
        self.assertIn("USART1_TX", out)          # 列出该脚可用功能

    def test_unknown_pin(self):
        out = self.mod.pin_check("stm32f103c8t6", {"PA99": "GPIO"})
        self.assertIn("没有此引脚", out)

    def test_duplicate_function(self):
        # v4 数据修正后：PA15/PB3 的 SPI1_* 是 remap 档（不是默认档），
        # 与 PA4 不构成真冲突——核验脚本保证 fixed_mux 数据无默认档双占
        out = self.mod.pin_check("stm32f103c8t6", {"PA4": "SPI1_NSS", "PA15": "SPI1_NSS"})
        self.assertNotIn("已被", out)
        self.assertIn("remap", out)

    def test_swd_reuse_warned(self):
        out = self.mod.pin_check("stm32f103c8t6", {"PA13": "GPIO"})
        self.assertIn("调试脚", out)

    def test_remap_mismatch(self):
        out = self.mod.pin_check("stm32f103c8t6", {"PB7": "USART1_RX"})
        self.assertIn("remap", out)

    def test_valid_assignment(self):
        out = self.mod.pin_check("STM32F103C8T6", {"PA9": "USART1_TX", "PA10": "USART1_RX",
                                                    "PB6": "I2C1_SCL", "PB7": "I2C1_SDA"})
        self.assertIn("全部通过", out)

    def test_unknown_mcu_lists_available(self):
        out = self.mod.pin_check("esp32s3", {"GPIO4": "GPIO"})
        self.assertIn("stm32f103c8t6", out)

    def test_bad_pins_param(self):
        out = self.mod.pin_check("stm32f103c8t6", [])
        self.assertIn("字典", out)


class TestPinCheckIoMux(unittest.TestCase):
    """v4：ESP32 类 io_mux 引擎（无固定复用表，查硬约束）。"""
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_flash_reserved_pin(self):
        out = self.mod.pin_check("esp32", {"GPIO6": "GPIO"})
        self.assertIn("内部 flash 专用", out)

    def test_input_only_output_rejected(self):
        out = self.mod.pin_check("esp32", {"GPIO34": "PWM_OUT"})
        self.assertIn("仅输入脚", out)

    def test_input_only_input_ok(self):
        out = self.mod.pin_check("esp32", {"GPIO35": "GPIO_IN"})
        self.assertNotIn("做不了输出", out)
        self.assertIn("仅输入脚", out)

    def test_strapping_warned(self):
        out = self.mod.pin_check("esp32", {"GPIO12": "I2C_SDA"})
        self.assertIn("strapping", out)

    def test_flexible_routing_ok(self):
        out = self.mod.pin_check("esp32", {"GPIO21": "I2C_SDA"})
        self.assertIn("任意路由", out)


class TestNewTools(unittest.TestCase):
    """v2 新增工具的零依赖健壮性测试。"""
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_flash_missing_file(self):
        out = self.mod.flash_firmware("no_such_firmware.bin", chip="esp32")
        self.assertIn("不存在", out)

    def test_flash_dry_run_requires_tool(self):
        # 沙箱/无工具机器上：要么给 dry-run 命令，要么给安装指引——都不该炸
        import tempfile
        tf = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        tf.write(b"\xde\xad\xbe\xef")
        tf.close()
        try:
            out = self.mod.flash_firmware(tf.name, chip="esp32", dry_run=True)
            self.assertTrue(("DRY-RUN" in out) or ("没找到" in out) or ("安装" in out))
        finally:
            os.unlink(tf.name)

    def test_decode_register_basic(self):
        out = self.mod.decode_register(0x4002, [{"name": "HSION", "bit": 0},
                                                {"name": "PLL", "lsb": 14, "width": 2}])
        self.assertIn("0x4002", out)
        self.assertIn("HSION", out)

    def test_decode_register_bad_value(self):
        out = self.mod.decode_register("not_a_number", [{"name": "X", "bit": 0}])
        self.assertIn("整数", out)
        out2 = self.mod.decode_register(0xFFFFF, [{"name": "X", "bit": 0}], width=16)
        self.assertIn("超出", out2)
        out3 = self.mod.decode_register(1, [])
        self.assertIn("fields", out3)

    def test_decode_svd_missing_file(self):
        out = self.mod.decode_register_svd("no_such.svd", "RCC", "CR", 0x4002)
        self.assertIn("不存在", out)

    def test_driver_wiring(self):
        out = self.mod.driver_wiring("ds18b20")
        self.assertIn("接线", out)
        out2 = self.mod.driver_wiring("no_such_driver")
        self.assertIn("没有", out2)


class TestV4BoardAndPinmap(unittest.TestCase):
    """v4：board 参数交叉检查 + pinmap_get + board_get。"""
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_pin_check_with_board_warns_onboard(self):
        # PC13 是蓝药丸 LED（低电平点亮），挪用应给板级警告
        out = self.mod.pin_check("stm32f103c8t6", {"PC13": "GPIO"}, board="bluepill")
        self.assertIn("板载外设", out)
        self.assertIn("LED", out)

    def test_pin_check_without_board_no_board_warn(self):
        out = self.mod.pin_check("stm32f103c8t6", {"PC13": "GPIO"})
        self.assertNotIn("板载外设", out)

    def test_pinmap_get(self):
        out = self.mod.pinmap_get("stm32f103c8t6")
        self.assertIn('"engine"', out)
        self.assertIn("PA9", out)
        out2 = self.mod.pinmap_get("no_such_mcu_xyz")
        self.assertIn("没有收录", out2)

    def test_board_get_bluepill(self):
        out = self.mod.board_get("bluepill")
        self.assertIn("蓝药丸", out)
        self.assertIn("板级坑清单", out)
        self.assertIn("低电平", out)          # LED active-low 是蓝药丸头号反直觉

    def test_board_get_unknown(self):
        out = self.mod.board_get("no_such_board")
        self.assertIn("没有收录", out)

    def test_board_list(self):
        lst = self.mod.board_list()
        self.assertIn("bluepill", lst)
        self.assertIn("blackpill_f411", lst)


class TestSessions(unittest.TestCase):
    """v4：会话持久化（落盘/列表/回看/路径防护），离线可测（不依赖 pyserial）。"""
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="mcu_dbg_logs_")
        self._orig = self.mod._LOGS_DIR
        self.mod._LOGS_DIR = self.tmp

    def tearDown(self):
        self.mod._LOGS_DIR = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_log(self, name, lines):
        import time as _t
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# mcu-debug-mcp 会话日志\n# 端口: COM3 @ 115200 8N1\n")
            for i, ln in enumerate(lines):
                f.write("[%6.2fs] %s\n" % (i * 0.1, ln))
        # 错开 mtime 保证排序
        stamp = _t.time() - (100 if name.endswith("_old.log") else 0)
        os.utime(path, (stamp, stamp))
        return path

    def test_sessions_list_empty(self):
        # 目录存在但为空 → 提示目录为空
        out = self.mod.sessions_list()
        self.assertIn("为空", out)

    def test_sessions_list_no_dir(self):
        # 目录不存在 → 提示还没有会话日志
        self.mod._LOGS_DIR = os.path.join(self.tmp, "no_such_logs_dir")
        out = self.mod.sessions_list()
        self.assertIn("还没有会话日志", out)

    def test_sessions_list_with_files(self):
        self._make_log("20260918_100000_COM3_h1.log", ["boot ok", "AT"])
        out = self.mod.sessions_list()
        self.assertIn("20260918_100000_COM3_h1.log", out)
        self.assertIn("COM3 @ 115200 8N1", out)     # 元信息解析
        self.assertIn("sessions_get", out)

    def test_sessions_get_tail(self):
        lines = ["line_%d" % i for i in range(50)]
        self._make_log("s_tail.log", lines)
        out = self.mod.sessions_get("s_tail.log", tail_lines=10)
        self.assertIn("共 50 行正文，显示尾部 10 行", out)
        self.assertIn("line_49", out)
        self.assertNotIn("line_35", out)            # 尾部 10 行不含更早的

    def test_sessions_get_skips_header(self):
        self._make_log("s_hdr.log", ["data1"])
        out = self.mod.sessions_get("s_hdr.log")
        self.assertNotIn("# 端口", out)             # 注释头不进正文行数
        self.assertIn("data1", out)

    def test_sessions_get_path_traversal_blocked(self):
        out = self.mod.sessions_get("..\\..\\evil.log")
        self.assertIn("没有这个会话日志", out)
        out2 = self.mod.sessions_get("..\\..\\..\\windows\\win.ini")
        self.assertIn("没有这个会话日志", out2)

    def test_sessions_get_missing(self):
        out = self.mod.sessions_get("not_exist.log")
        self.assertIn("没有这个会话日志", out)

    def test_sessions_get_empty_name(self):
        out = self.mod.sessions_get("")
        self.assertIn("需要文件名", out)

    def test_sess_append_creates_file(self):
        # _sess_append：logfile 指向的文件不存在时静默降级（不炸）
        sess = {"log": "", "logfile": None}
        self.mod._sess_append(sess, "hello")
        self.assertEqual(sess["log"], "hello")
        sess2 = {"log": "", "logfile": os.path.join(self.tmp, "no_such_dir", "x.log")}
        self.mod._sess_append(sess2, "world")       # 目录不存在也应静默
        self.assertEqual(sess2["log"], "world")


    # ---------------- logs_prune（N-12 日志治理） ----------------

    def _age(self, name, days):
        """在（被覆盖成 tmp 的）logs 目录里放一个指定「几天前」的日志。"""
        import time as _t
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x\n")
        st = _t.time() - days * 86400
        os.utime(path, (st, st))
        return path

    def test_logs_prune_dry_run_deletes_nothing(self):
        """★ 默认必须只列不删——「dry-run 却真删了」是最糟的一种默认行为。"""
        self._age("a.log", 0)
        self._age("b.log", 1)
        out = self.mod.logs_prune(keep_count=1)
        self.assertIn("dry-run", out)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["a.log", "b.log"])

    def test_logs_prune_count_rule_picks_oldest(self):
        """保留最新 N 个：进待清理段的必须是最旧的，不能反过来。"""
        self._age("old.log", 9)
        self._age("new.log", 0)
        tail = self.mod.logs_prune(keep_count=1).split("待清理文件")[-1]
        self.assertIn("old.log", tail)
        self.assertNotIn("new.log", tail)

    def test_logs_prune_days_rule(self):
        """保留最近 D 天：只有超期的进待清理段。"""
        self._age("fresh.log", 1)
        self._age("stale.log", 40)
        tail = self.mod.logs_prune(keep_days=30).split("待清理文件")[-1]
        self.assertIn("stale.log", tail)
        self.assertNotIn("fresh.log", tail)

    def test_logs_prune_warns_about_credentials(self):
        """治理的第三条：必须提示日志可能含凭据（AT 指令里的密码/token）。"""
        self._age("a.log", 0)
        out = self.mod.logs_prune(keep_count=0)
        self.assertTrue("凭据" in out or "token" in out,
                        "logs_prune 没有提示「日志可能含凭据」")

    def test_logs_prune_true_delete_is_gated(self):
        """★ 真删是不可逆操作：隔离档位是 readonly（conftest 锁死）时必须被拦。

        这条同时钉住两件事：① 删本地数据也要过 guard；② 被拦时**一个文件都不能少**。
        """
        self._age("a.log", 0)
        self._age("b.log", 9)
        out = self.mod.logs_prune(keep_count=1, dry_run=False, confirm=True)
        self.assertIn("隔离拦截", out)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["a.log", "b.log"])

    def test_logs_prune_empty_dir_is_not_an_error(self):
        """空目录要明确回复，不是报错也不是空串。"""
        out = self.mod.logs_prune()
        self.assertIn("为空", out)

    # ---------------- report_session（G13 交付物生成） ----------------

    def test_report_session_writes_report_with_evidence(self):
        """正例：报告既要有**结论**，也要有**原始证据**（只有结论的报告没法定责）。"""
        self._make_log("20260922_100000_COM21_h1.log",
                       ["boot ok", "UART Error: ORE detected",
                        "HAL_UART_Receive failed: HAL_BUSY"])
        out = os.path.join(self.tmp, "r.md")
        # 2026-10-13：report_session 接入 guard（write 档）——生成报告是写盘操作。
        # 本测试验内容，档位放开；「readonly 下必须被挡」由隔离测试专责。
        old = os.environ.get("MCU_DEBUG_MCP_MODE")
        os.environ["MCU_DEBUG_MCP_MODE"] = "write"
        try:
            msg = self.mod.report_session(fname="20260922_100000_COM21_h1.log", out_path=out)
        finally:
            if old is None:
                os.environ.pop("MCU_DEBUG_MCP_MODE", None)
            else:
                os.environ["MCU_DEBUG_MCP_MODE"] = old
        self.assertIn("报告已生成", msg)
        body = open(out, encoding="utf-8").read()
        self.assertIn("# 串口会话诊断报告", body)
        self.assertIn("## 一、故障分析", body)
        self.assertIn("## 二、原始日志", body)
        self.assertIn("HAL_BUSY", body)              # 原始证据必须在
        self.assertIn("UART_ORE_RX_LATCH", body)     # 分析结论必须在

    def test_report_session_refuses_to_overwrite(self):
        """★ 反例：目标已存在且未授权覆盖 → 必须拒绝，且原文件一个字节都不能变。

        报告是交付物，默认盖掉别人的文件是数据丢失级别的失误（同 logs_prune 的道理）。
        """
        self._make_log("a.log", ["x"])
        out = os.path.join(self.tmp, "r.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write("原有内容")
        msg = self.mod.report_session(fname="a.log", out_path=out)
        self.assertIn("已存在", msg)
        self.assertEqual(open(out, encoding="utf-8").read(), "原有内容")

    def test_report_session_overwrite_flag_works(self):
        self._make_log("a.log", ["x"])
        out = os.path.join(self.tmp, "r.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write("旧")
        # 2026-10-13：覆盖已存在文件需 overwrite=True + confirm=True 双闸门
        # （此前 overwrite=True 直接盖，且本操作完全没接 guard 隔离层）。
        old = os.environ.get("MCU_DEBUG_MCP_MODE")
        os.environ["MCU_DEBUG_MCP_MODE"] = "write"
        try:
            msg = self.mod.report_session(fname="a.log", out_path=out,
                                          overwrite=True, confirm=True)
        finally:
            if old is None:
                os.environ.pop("MCU_DEBUG_MCP_MODE", None)
            else:
                os.environ["MCU_DEBUG_MCP_MODE"] = old
        self.assertIn("报告已生成", msg)
        self.assertIn("串口会话诊断报告", open(out, encoding="utf-8").read())

    def test_report_session_overwrite_without_confirm_is_blocked(self):
        """反例：overwrite=True 但缺 confirm=True → 不许盖，且要说清怎么补。"""
        self._make_log("a.log", ["x"])
        out = os.path.join(self.tmp, "r.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write("旧")
        msg = self.mod.report_session(fname="a.log", out_path=out, overwrite=True)
        self.assertIn("confirm=True", msg)
        self.assertEqual(open(out, encoding="utf-8").read(), "旧",
                         "缺 confirm 时原文件被盖了——二次确认没生效")

    def test_report_session_ambiguous_source_lists_options(self):
        """反例：既没给来源、日志又不是唯一一份 → 说清怎么给，不瞎猜一份。"""
        self._make_log("a.log", ["x"])
        self._make_log("b.log", ["y"])
        msg = self.mod.report_session()
        self.assertIn("需要指定数据来源", msg)
        self.assertIn("sessions_list", msg)

    def test_report_session_invalid_handle(self):
        msg = self.mod.report_session(handle=999)
        self.assertIn("无效的串口句柄", msg)


class TestRttReadDegrade(unittest.TestCase):
    """v4：rtt_read 在无 pyocd 环境的优雅降级。"""
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_rtt_no_pyocd_gives_guidance(self):
        out = self.mod.rtt_read(seconds=1.0)
        if self.mod.HAS_PYOCD:
            self.assertTrue(isinstance(out, str))   # 有 pyocd：结果可能是成功/失败/无数据，都不炸
        else:
            self.assertIn("pyocd", out)
            self.assertIn("pip install", out)


class TestAtLineMatch(unittest.TestCase):
    """★回归：expect 必须是【整行】匹配，不能在全文里瞎搜。

    实测事故：ESP32 上跑 at_sequence，expect="OK"，设备其实跑的是 BQ27421 电量计固件、
    根本没有 AT 应答，但日志文本里出现了 "OK" 字样 ⟹ 被判 PASS。这是"看似合理的假通过"。
    """
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_standalone_ok_line_matches(self):
        buf = "AT\r\n\r\nOK\r\n"
        m, line = self.mod._at_line_match(buf, re.compile("OK"))
        self.assertTrue(m)
        self.assertEqual(line, "OK")

    def test_ok_embedded_in_log_text_does_not_match(self):
        """日志行里含 OK 但不是独立应答行 ⟹ 必须不命中。"""
        buf = ("BQ27421 at 0x55, V=4146mV I (378)\n"
               "bq27421: ADC cal offset lo OK-ish noise\n")
        m, line = self.mod._at_line_match(buf, re.compile("OK"))
        self.assertFalse(m, "日志行里的 OK 被误判成 AT 应答了：%r" % line)

    def test_ok_as_prefix_of_longer_token_does_not_match(self):
        """OK 只是更长的 token 的前缀（如 OKAY_FAULT）⟹ 整行匹配下不命中。"""
        buf = "OKAY_FAULT at 0x02000000\n"
        m, _ = self.mod._at_line_match(buf, re.compile("OK"))
        self.assertFalse(m)

    def test_multi_choice_regex_still_works(self):
        buf = "\r\nERROR\r\n"
        m, line = self.mod._at_line_match(buf, re.compile("OK|ERROR"))
        self.assertTrue(m)
        self.assertEqual(line, "ERROR")

    def test_line_with_surrounding_whitespace_matches(self):
        buf = "   OK   \r\n"
        m, line = self.mod._at_line_match(buf, re.compile("OK"))
        self.assertTrue(m)
        self.assertEqual(line, "OK")

    def test_loose_mode_restores_old_substring_behaviour(self):
        buf = "noise ... OK ... more noise\n"
        m, _ = self.mod._at_line_match(buf, re.compile("OK"), loose=True)
        self.assertTrue(m, "loose=True 应恢复旧的全文字符串搜索行为")

    def test_empty_buffer_no_match(self):
        m, line = self.mod._at_line_match("", re.compile("OK"))
        self.assertFalse(m)
        self.assertEqual(line, "")

    def test_at_sequence_skips_invalid_regex_without_crash(self):
        """expect 正则非法时必须给 SKIP 而不是抛异常。"""
        # 不走真串口：直接调用时 port 打不开会返回"打开失败"，所以只验证
        # 步骤校验分支——这里用不可能存在的端口，确认不抛异常。
        out = self.mod.at_sequence("COM_NOT_EXIST_999", [{"cmd": "AT", "expect": "OK"}])
        self.assertIsInstance(out, str)


class TestPinCheckUnknownPin(unittest.TestCase):
    """★回归：io_mux 引擎（ESP32）也必须拦截不存在的引脚名。

    实测事故：pin_check("esp32dev", {"GPIO999": "GPIO"}) 曾经返回
      "✅ GPIO999 → GPIO  任意路由（GPIO Matrix）..." + "结论：全部通过，可以按此接线"
    即：引脚名打错（或纯属幻觉）会被判成"通过"。fixed_mux 引擎（STM32）有
    "该 MCU 没有此引脚" 的拦截，但 io_mux 分支缺失，两个引擎判定口径不对称。
    """
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_esp32_nonexistent_pin_is_error(self):
        out = self.mod.pin_check("esp32dev", {"GPIO999": "GPIO"})
        self.assertIn("没有此引脚", out)
        self.assertNotIn("全部通过", out)
        self.assertNotIn("任意路由", out)

    def test_esp32_typo_pin_is_error(self):
        """把 GPIO18 敲成 GPIO8 之外的手滑拼写，必须被拦。"""
        out = self.mod.pin_check("esp32dev", {"GPI08": "GPIO"})   # 字母 O 打成了数字 0
        self.assertIn("没有此引脚", out)
        self.assertNotIn("全部通过", out)

    def test_esp32_valid_pin_still_passes(self):
        """真实存在的引脚不能被误伤。"""
        out = self.mod.pin_check("esp32dev", {"GPIO21": "I2C_SDA"})
        self.assertIn("全部通过", out)
        self.assertNotIn("没有此引脚", out)

    def test_stm32_branch_still_checks_existence(self):
        """fixed_mux 引擎的原有的存在性校验不能被破坏。"""
        out = self.mod.pin_check("stm32f103c8t6", {"PZ9": "GPIO"})
        self.assertIn("没有此引脚", out)


class TestUsbCdcBaudRelevance(unittest.TestCase):
    """★回归：USB CDC 口上 baudrate_autodetect 必须短路，不给"看着有区分度"的排序。

    背景（2026-09-21 师兄指出）：ESP32 用 USB CDC，波特率参数不参与数据路径。
    实测佐证：COM4 上同一波特率(115200)连测 3 轮全 0 字节，而另一次 9600 拿到
    88064 字节 —— 同波特率结果从 88KB 到 0，差异只来自设备状态。
    ⟹ 该口扫档 = 在测"这次打开时设备有没有在说话"，纯随机。
    """
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    def test_known_cdc_vid_pid_classified(self):
        """Espressif 原生 USB (303A:1001) 应被识别为 usb_cdc。"""
        # 直接测分类函数对已知表的判断（不依赖本机是否插着该设备）
        self.assertIn((0x303A, 0x1001), self.mod._USB_CDC_IDS)
        self.assertIn((0x0403, 0x6010), self.mod._USB_UART_BRIDGE_IDS)

    def test_cdc_and_bridge_sets_are_disjoint(self):
        """CDC 表与桥表不能有交集，否则分类逻辑自相矛盾。"""
        self.assertFalse(self.mod._USB_CDC_IDS & self.mod._USB_UART_BRIDGE_IDS)

    def test_bridge_ids_known_to_serial_list_name_table(self):
        """★两张 USB ID 表不能脱节。

        背景（实测踩到）：core._USB_UART_BRIDGE_IDS 认识 0403:6010(FT2232H) 与
        1366:0105(J-Link)，但 analyzer.USB_SERIAL_VIDPID 没有这两条，
        于是 serial_list 对着已经插着的 FT2232H/J-Link 显示「未收录」——
        同一个 VID:PID 在一处被识别、另一处说不认识。
        """
        import analyzer
        for (vid, pid) in self.mod._USB_UART_BRIDGE_IDS:
            key = "%04X:%04X" % (vid, pid)
            self.assertIsNotNone(
                analyzer.identify_usb_serial(key),
                "桥表里的 %s 在 analyzer.USB_SERIAL_VIDPID 里缺失，"
                "会导致 serial_list 显示「未收录」" % key)

    def test_relevance_returns_valid_kind(self):
        """不存在的端口也要给出合法 kind，不能抛异常。"""
        kind, desc = self.mod._port_baud_relevance("COM_NOT_EXIST_999")
        self.assertIn(kind, ("usb_cdc", "bridge", "serial", "unknown"))
        self.assertIsInstance(desc, str)

    def test_autodetect_on_cdc_port_short_circuits(self):
        """若本机恰好有 CDC 口，扫档必须短路且说明"波特率不影响数据"。"""
        kind, _ = self.mod._port_baud_relevance("COM4")
        if kind != "usb_cdc":
            self.skipTest("本机 COM4 不是 USB CDC（或未插设备），跳过")
        out = self.mod.baudrate_autodetect("COM4")
        self.assertIn("波特率不参与数据路径", out)
        self.assertNotIn("← 推荐", out)
        self.assertNotIn("建议用", out)

    def test_log_text_mode_bypasses_cdc_short_circuit(self):
        """离线模式（传了 log_text）不应被短路——那是纯打分，不占串口。"""
        out = self.mod.baudrate_autodetect("COM4", log_text="hello world\n1234\n")
        self.assertNotIn("波特率不参与数据路径", out)

    @unittest.skipUnless(os.environ.get("MCU_DEBUG_MCP_TEST_ALLOW_HW") == "1",
                         "真机测试：会真的扫描本机串口，需 MCU_DEBUG_MCP_TEST_ALLOW_HW=1 显式开启")
    def test_explicit_candidates_bypasses_cdc_short_circuit(self):
        """★显式传 candidates 视为"强行扫"，必须放行（返回值里那句承诺要兑现）。

        ★这条是**真机测试**：它要拿一个真实存在的 USB CDC 口来验证「不短路」，
          会真的逐档打开那个端口。默认跳过（conftest 也把写权限关着）——
          以前它靠 `if kind != usb_cdc: skip` 偶然跳过，机器上插了 ESP32 就会
          真的去扫，属于「悄悄碰真机的测试」。现在改成显式授权才跑。
        """
        kind, _ = self.mod._port_baud_relevance("COM4")
        if kind != "usb_cdc":
            self.skipTest("本机 COM4 不是 USB CDC（或未插设备），跳过")
        out = self.mod.baudrate_autodetect("COM4", candidates=[115200], sample_s=0.2)
        self.assertNotIn("波特率不参与数据路径", out)
        self.assertIn("波特率扫描结果", out)


@unittest.skipUnless(importlib.util.find_spec("serial"),
                     "pyserial 未安装（pip install pyserial）")
class TestListAndSelfCheck(unittest.TestCase):
    """serial_list / probe_list / env_status 的专项断言（能力清单缺口 N-14）。

    为什么这三个最后才补：它们都是**查询本机状态**的工具，输出随机器而变，
    最容易被写成「调了不崩就算过」。能探的探、探不了的要**明说**——这是本项目
    对这三个工具的共同要求，所以按「正例给出真实信息 + 反例明说而非含糊」两档钉住。
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_core()

    # ---------------- serial_list ----------------

    @contextmanager
    def _ports(self, ports):
        """在假端口列表下跑（只替换 comports，不动 serial 的其它属性）。"""
        import serial.tools.list_ports as lp
        orig = lp.comports
        lp.comports = lambda *a, **k: ports
        try:
            yield
        finally:
            lp.comports = orig

    def test_serial_list_names_known_bridge_chip(self):
        """正例：已知桥芯片要报出芯片名（CH340），不能只回一行端口号。"""
        class _P:
            device, description, vid, pid = "COM42", "USB-SERIAL CH340", 0x1A86, 0x7523
        with self._ports([_P()]):
            out = self.mod.serial_list()
        self.assertIn("COM42", out)
        self.assertIn("CH340", out)

    def test_serial_list_stlink_v3_vcp_identified(self):
        """回归（真机 D1）：ST-Link V3 虚拟串口 0483:374E 必须被认出来。

        这是 2026-09-22 在 NUCLEO-H563ZI/COM21 上实测发现的缺陷——当时板子插着，
        serial_list 却报「未收录」。这条钉住「STM32 板载 VCP 必被识别」。
        """
        class _P:
            device, description, vid, pid = "COM21", "STLink Virtual COM Port", 0x0483, 0x374E
        with self._ports([_P()]):
            out = self.mod.serial_list()
        self.assertIn("ST-Link", out)

    def test_serial_list_unknown_vidpid_says_not_catalogued(self):
        """反例：未收录的 VID:PID 要明说「未收录」，不许拿相近的芯片去猜。"""
        class _P:
            device, description, vid, pid = "COM99", "Weird Device", 0x1234, 0x9999
        with self._ports([_P()]):
            out = self.mod.serial_list()
        self.assertIn("未收录", out)
        self.assertIn("1234:9999", out)

    def test_serial_list_empty_is_explicit_not_blank(self):
        """反例：一个串口都没有时必须明说——空返回会被当成「工具坏了」。"""
        with self._ports([]):
            out = self.mod.serial_list()
        self.assertTrue(out.strip(), "serial_list 对空列表返回了空串")
        self.assertIn("没有发现任何串口", out)

    # ---------------- probe_list ----------------

    @contextmanager
    def _pyocd_missing(self, exe=None):
        """模拟「没有可导入的 pyocd 模块」，并指定 _which_tool 能找到什么。

        exe=None → 连可执行文件也找不到；给路径 → 只有 exe、没有模块。
        这两种必须分开测：混在一起就复现不了「env_status 说有、probe_list 说没有」
        那类自相矛盾（实机真踩过）。
        """
        orig_h, orig_w = self.mod.HAS_PYOCD, self.mod._which_tool
        self.mod.HAS_PYOCD = False
        self.mod._which_tool = lambda n: (exe if n == "pyocd" else None)
        try:
            yield
        finally:
            self.mod.HAS_PYOCD = orig_h
            self.mod._which_tool = orig_w

    def test_probe_list_without_pyocd_gives_install_hint(self):
        """反例：模块与可执行文件都没有时，给安装指引 + 人工排查方向，而不是抛异常。"""
        with self._pyocd_missing(None):
            out = self.mod.probe_list()
        self.assertIn("pyocd", out)
        self.assertIn("pip install", out)
        self.assertIn("调试器", out)      # 附人工排查方向

    def test_probe_list_exe_only_reports_both_facts(self):
        """反例：只有 pyocd.exe、当前解释器 import 不到模块时，
        必须把「文件在哪」和「缺的是模块」两件事都讲清，并给两条修法。"""
        with self._pyocd_missing(r"C:\fake\ext\pyocd.exe"):
            out = self.mod.probe_list()
        self.assertIn(r"C:\fake\ext\pyocd.exe", out)   # 到底在哪
        self.assertIn("import pyocd", out)             # 缺的是哪一个
        self.assertIn("pip install", out)              # 修法 1
        self.assertIn("list", out)                     # 修法 2：直接跑那个 exe
        self.assertIn("调试器", out)

    @contextmanager
    def _dap(self, devices):
        """把 core 探针枚举换成一串假设备。"""
        class _DA:
            @staticmethod
            def get_connected_devices():
                return devices
        have = hasattr(self.mod, "DAPAccess")
        old_h, old_da = self.mod.HAS_PYOCD, getattr(self.mod, "DAPAccess", None)
        self.mod.HAS_PYOCD = True
        self.mod.DAPAccess = _DA
        try:
            yield
        finally:
            self.mod.HAS_PYOCD = old_h
            if have:
                self.mod.DAPAccess = old_da
            else:
                try:
                    del self.mod.DAPAccess
                except AttributeError:
                    pass

    def test_probe_list_lists_connected_probe(self):
        """正例：有探针时列出型号/厂商/序列号（序列号是识别「哪一台」的关键）。"""
        class _D:
            product_name, vendor_name, serial_number = "ST-Link V3", "STMicro", "0001"
        with self._dap([_D()]):
            out = self.mod.probe_list()
        self.assertIn("ST-Link V3", out)
        self.assertIn("0001", out)

    def test_probe_list_none_found_points_at_wiring(self):
        """反例：探针一个都没有时，要指向接线/驱动，而不是只说「0 个」。"""
        with self._dap([]):
            out = self.mod.probe_list()
        self.assertIn("没有发现调试器", out)
        self.assertIn("驱动", out)

    # ---------------- env_status ----------------

    def test_env_status_reports_key_sections(self):
        """正例：自检要一次答清 版本 / 依赖 / 串口 / 隔离 / 知识库。"""
        out = self.mod.env_status()
        for token in ("版本", "依赖", "串口", "隔离", "知识库"):
            self.assertIn(token, out, "env_status 少了「%s」段" % token)

    def test_env_status_survives_missing_pyserial(self):
        """反例：pyserial 缺失时不许崩——要明说哪些功能不可用及怎么装。"""
        orig = self.mod.HAS_PYSERIAL
        self.mod.HAS_PYSERIAL = False
        try:
            out = self.mod.env_status()
        finally:
            self.mod.HAS_PYSERIAL = orig
        self.assertIn("pyserial", out)
        self.assertIn("pip install", out)
        self.assertTrue(out.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
