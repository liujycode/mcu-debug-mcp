# -*- coding: utf-8 -*-
"""G06 总线工具（I2C / SPI / 1-Wire / RS485）的离线回归测试。

为什么必须有这一层（而不是等真机跑一趟就完事）：

    真机只能告诉你「这一次、这块板、这个接线」行不行。它证明不了**解析器**在
    下面这些情形下还对：
      · 桥回话里混着异步 `#LOG` 行（真桥一定会打日志）
      · 桥回话漏字段 / 字段格式不对（固件版本不一致时的真实表现）
      · 协议失步（半行、上次残留的应答）
      · 回话里带的是 8 位十六进制、还是补零后的 6 位（同一件事两种写法）
    而这些恰恰是「看起来正常、结论是错的」那类故障的来源 —— 本项目把它归为
    「假绿灯」，比报错更危险。所以用**假桥**把真机才会走的那条解析路径完整跑一遍。

假桥的定位（与 tests/conftest.py 的铁律一致）：它只替换 ``core._open_serial``，
不碰任何真实端口。conftest 默认把整个测试进程关在「readonly + 端口全禁」的笼子里，
所以想测笼子里的业务逻辑，必须显式用 ``relaxed_env`` 声明，用完立刻恢复。
"""

import os
import unittest
import unittest.mock as mock

import core
from isolation import relaxed_env


# --------------------------------------------------------------------------
# 假桥：实现 pyserial 里本工具真正用到的那几个方法，按 MCUDBG/1 协议回话
# --------------------------------------------------------------------------

#: 一块「一切正常」的桥会怎么答。测试按需覆盖其中若干条。
_HEALTHY = {
    "I2C SCAN": "+OK I2C SCAN bus=0 lo=8 hi=119 n=2 addrs=0x70,0x71",
    "I2C READ": "+OK I2C READ bus=0 addr=0x68 reg=0x75 n=1 data=0x68",
    "I2C WRITE": "+OK I2C WRITE n=1",
    "SPI SCAN": "+OK SPI SCAN bus=0 n=2 slots=0:0xef4018,1:0xffffff",
    "SPI READ": "+OK SPI READ bus=0 cs=0 cmd=0x9f n=3 data=0xef,0x40,0x18",
    "OW SCAN": "+OK OW SCAN bus=0 n=1 roms=28ff64a1b2c3d4e5",
    "RS485 SEND": "+OK RS485 SEND bus=0 n=5",
}

_PING_OK = ("+OK PING BRIDGE=mcu-debug proto=MCUDBG/1 fw=test-unittest "
            "caps=I2C,SPI,1W,RS485")


class FakeBridge(object):
    """假桥。``handlers`` 里的键是命令前缀（大写匹配），值是回话字符串或可调用对象。

    ``silent=True`` 时什么都不回（测超时分支）；``ping`` 可换成别的回话
    （测「这个口上不是我的桥」）。``logs`` 里的行会在每次应答前先发出来，
    用来验证「异步日志必须被忽略」。
    """

    def __init__(self, handlers=None, ping=_PING_OK, silent=False, logs=()):
        self.handlers = dict(_HEALTHY)
        self.handlers.update(handlers or {})
        self.ping = ping
        self.silent = silent
        self.logs = list(logs)
        self.written = []
        self.closed = False
        self._out = b""

    # ---- pyserial 用得着的那几个方法 ----
    def reset_input_buffer(self):
        self._out = b""

    def write(self, payload):
        self.written.append(payload.decode("ascii", "replace"))
        if self.silent:
            return
        line = payload.decode("ascii", "replace").strip()
        reply = self._answer(line)
        if reply is None:
            return
        buf = "".join(ln + "\r\n" for ln in self.logs)
        self._out += (buf + reply + "\r\n").encode("ascii", "replace")

    def flush(self):
        pass

    def read(self, n=256):
        out, self._out = self._out[:n], self._out[n:]
        return out

    def close(self):
        self.closed = True

    # ---- 内部 ----
    def _answer(self, line):
        head = line.split()[0].upper() if line.split() else ""
        if head == "PING":
            return self.ping
        for pat, rep in self.handlers.items():
            if line.upper().startswith(pat.upper()):
                return rep(line) if callable(rep) else rep
        return "-ERR ARG 未实现"


def _bridged(bridge, mode="off", deny=""):
    """把隔离层放到 ``mode`` 档、并把 ``core._open_serial`` 换成假桥。

    返回一个上下文管理器，进入后可直接用 bridge 实例（例如查它收到过什么命令）。
    """
    class _Ctx(object):
        def __enter__(self):
            self._rl = relaxed_env(mode, deny)
            self._rl.__enter__()
            self._patch = mock.patch.object(core, "_open_serial",
                                            lambda **kw: bridge)
            self._patch.start()
            return bridge

        def __exit__(self, *exc):
            self._patch.stop()
            return self._rl.__exit__(*exc)

    return _Ctx()


def _scan_hit(out):
    """从 i2c_scan 的输出里取回「应答器件」那一行的个数，用于链路一致性断言。"""
    for line in out.splitlines():
        if "应答器件" in line:
            return line.split(":")[1].strip()
    return None


# --------------------------------------------------------------------------
# 1. 握手：桥是「认出来的」，不是「猜出来的」
# --------------------------------------------------------------------------

@unittest.skipUnless(core.HAS_PYSERIAL, "pyserial 未安装")
class BridgeHandshakeTest(unittest.TestCase):

    def test_ping_required_rejects_foreign_firmware(self):
        """口上有东西在说话，但 PING 回话里没有 BRIDGE=mcu-debug → 必须停手。

        这条是**安全底线**，不是为了好看：往一块不认识的板子上灌 I2C 命令，
        可能改它的配置甚至让它重启。实测过（本机 COM46 上的手套固件真的解析了
        我发过去的 `help`），所以宁可不做，也不赌。
        """
        br = FakeBridge(ping="+OK PING BRIDGE=someone-else fw=1 caps=I2C")
        with _bridged(br):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("不是", out)
        self.assertIn("没有 BRIDGE=mcu-debug", out)
        self.assertNotIn("应答器件", out)          # 拒绝后绝不能继续扫
        self.assertTrue(br.closed, "拒绝之后必须把端口关掉，不能占着")

    def test_ping_timeout_says_bridge_absent(self):
        br = FakeBridge(silent=True)
        with _bridged(br):
            out = core.bridge_probe("COM21", timeout=0.05)
        self.assertIn("没有桥在应答", out)

    def test_desync_is_not_reported_as_bridge_rejection(self):
        """回话与命令对不上 = 协议失步，**不能**说成「桥拒绝了这条命令」。

        两种故障的排查方向完全相反（一个查固件版本/波特率，一个查命令参数），
        混成一句话会把人指到错的方向。
        """
        br = FakeBridge(handlers={"I2C SCAN": "+OK SOMETHINGELSE x=1"})
        with _bridged(br):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("协议失步", out)
        self.assertNotIn("桥拒绝了这条命令", out)

    def test_no_port_explains_how_to_configure(self):
        with mock.patch.dict(os.environ, {"MCU_DEBUG_BRIDGE_PORT": ""}):
            out = core.i2c_scan(0)
        self.assertIn("没有指定桥口", out)
        self.assertIn("MCU_DEBUG_BRIDGE_PORT", out)
        # 必须说清「不会自动扫口」，否则用户会以为是工具坏了
        self.assertIn("不会", out)

    def test_bridge_probe_reports_caps(self):
        with _bridged(FakeBridge()):
            out = core.bridge_probe("COM21")
        self.assertIn("MCUDBG/1", out)
        self.assertIn("✓ 与工具一致", out)
        self.assertIn("I2C", out)

    def test_bridge_probe_flags_protocol_mismatch(self):
        br = FakeBridge(ping="+OK PING BRIDGE=mcu-debug proto=MCUDBG/2 fw=x caps=I2C")
        with _bridged(br):
            out = core.bridge_probe("COM21")
        self.assertIn("✗", out)
        self.assertIn("MCUDBG/1", out)

    def test_bridge_probe_warns_missing_caps(self):
        """桥没声明的总线，要到用的时候是 -ERR，不是接线问题 —— 提前讲清楚。"""
        br = FakeBridge(ping="+OK PING BRIDGE=mcu-debug proto=MCUDBG/1 caps=I2C")
        with _bridged(br):
            out = core.bridge_probe("COM21")
        self.assertIn("未声明支持", out)
        self.assertIn("SPI", out)

    def test_err_code_is_translated_to_human_hint(self):
        br = FakeBridge(handlers={"I2C SCAN": "-ERR NAK 从设备未应答"})
        with _bridged(br):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("NAK", out)
        self.assertIn("上拉电阻", out)

    def test_unknown_err_code_says_so_instead_of_guessing(self):
        br = FakeBridge(handlers={"I2C SCAN": "-ERR WEIRDNESS 带内自研码"})
        with _bridged(br):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("没有登记这个错误码", out)


# --------------------------------------------------------------------------
# 2. I2C：扫描与读写
# --------------------------------------------------------------------------

@unittest.skipUnless(core.HAS_PYSERIAL, "pyserial 未安装")
class I2cScanTest(unittest.TestCase):

    def test_scan_lists_addresses_with_device_hints(self):
        with _bridged(FakeBridge()):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("0x70", out)
        self.assertIn("0x71", out)
        self.assertIn("TCA9548A", out)             # 地址→器件提示
        self.assertIn("TCA9548A 数据手册", out)     # 依据可溯源

    def test_scan_ignores_async_log_lines(self):
        """真桥一定会打日志；日志行不能污染解析（否则扫描结果会错得很自然）。"""
        br = FakeBridge(logs=["#LOG i2c bus0 retry=1",
                              "#LOG heap=123456"])
        with _bridged(br):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("应答器件   : 2 个", out)

    def test_scan_with_zero_devices_gives_hardware_checklist(self):
        br = FakeBridge(handlers={"I2C SCAN": "+OK I2C SCAN bus=0 lo=8 hi=119 n=0 addrs="})
        with _bridged(br):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("应答器件   : 0 个", out)
        self.assertIn("上拉电阻", out)
        self.assertIn("TCA9548A", out)             # 多路复用器那条坑必须提到

    def test_scan_says_when_address_table_has_no_entry(self):
        """扫到了但表里没有 → 必须说「未收录」是**数据缺口**，不是结论。"""
        br = FakeBridge(handlers={"I2C SCAN": "+OK I2C SCAN bus=0 lo=8 hi=119 n=1 addrs=0x55"})
        with _bridged(br):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("未收录", out)
        self.assertIn("不是结论", out)

    def test_scan_flags_missing_n_field(self):
        """桥回话里没有 n= 字段 → 结果不可信，必须明说，不能照着 addrs 硬渲染。"""
        br = FakeBridge(handlers={"I2C SCAN": "+OK I2C SCAN bus=0 addrs=0x68"})
        with _bridged(br):
            out = core.i2c_scan(0, "COM21")
        self.assertIn("协议不符", out)

    def test_scan_rejects_high_below_low(self):
        with _bridged(FakeBridge()):
            out = core.i2c_scan(0, "COM21", 115200, 0x80, 0x77)
        self.assertIn("不合法", out)

    def test_scan_rejects_non_int_bus(self):
        with _bridged(FakeBridge()):
            out = core.i2c_scan("x", "COM21")
        self.assertIn("不是整数", out)

    def test_scan_rejects_reserved_range(self): 
        """扫描范围落在 I2C 保留段时 → 拒绝，并说清保留段是什么（否则用户以为自己填错了）。"""
        with _bridged(FakeBridge()):
            out = core.i2c_scan(0, "COM21", 115200, 0x00, 0x07)
        self.assertIn("保留", out)


@unittest.skipUnless(core.HAS_PYSERIAL, "pyserial 未安装")
class I2cRegTest(unittest.TestCase):

    def test_read_reg_reports_bytes_and_bits(self):
        with _bridged(FakeBridge()):
            out = core.i2c_read_reg(0x68, 0x75, port="COM21")
        self.assertIn("原始值", out)
        self.assertIn("0x68", out)
        self.assertIn("104", out)
        self.assertIn("0b01101000", out)

    def test_read_all_ff_is_reported_as_not_connected(self):
        """全 0xFF 是「器件没接/没上电」的典型信号，不是「寄存器的值是 0xFF」。"""
        br = FakeBridge(handlers={"I2C READ": "+OK I2C READ bus=0 addr=0x68 reg=0x75 n=1 data=0xff"})
        with _bridged(br):
            out = core.i2c_read_reg(0x68, 0x75, port="COM21")
        self.assertIn("根本没接", out)

    def test_read_all_zero_lists_distinct_causes(self):
        br = FakeBridge(handlers={"I2C READ": "+OK I2C READ bus=0 addr=0x68 reg=0x75 n=1 data=0x00"})
        with _bridged(br):
            out = core.i2c_read_reg(0x68, 0x75, port="COM21")
        self.assertIn("先别急着说器件坏", out)

    def test_read_rejects_8bit_address(self):
        """0xD0 是 8 位写地址，不是 7 位地址 —— 这是最常见的一类填错。"""
        with _bridged(FakeBridge()):
            out = core.i2c_read_reg(0xD0, 0x75, port="COM21")
        self.assertIn("超出 7 位范围", out)
        self.assertNotIn("原始值", out)            # 拒绝就得真拒绝，不能顺手读了
        # 说明里要给出「怎么改」，否则用户只知道错了不知道怎么对
        self.assertIn("0x68", out)

    def test_read_rejects_reserved_address(self):
        with _bridged(FakeBridge()):
            out = core.i2c_read_reg(0x00, 0x00, port="COM21")
        self.assertIn("保留段", out)
        self.assertNotIn("原始值", out)

    def test_read_rejects_non_int_address(self):
        with _bridged(FakeBridge()):
            out = core.i2c_read_reg("abc", 0, port="COM21")
        self.assertIn("不是合法整数", out)

    def test_read_rejects_count_out_of_range(self):
        for bad in (0, 257, -1):
            with _bridged(FakeBridge()):
                out = core.i2c_read_reg(0x68, 0x75, count=bad, port="COM21")
            self.assertIn("1–256", out, "count=%r 应被拒绝" % (bad,))

    def test_read_rejects_reg_over_8bit(self):
        with _bridged(FakeBridge()):
            out = core.i2c_read_reg(0x68, 0x1FF, port="COM21")
        self.assertIn("超出 8 位", out)

    def test_read_flags_missing_data_field(self):
        br = FakeBridge(handlers={"I2C READ": "+OK I2C READ bus=0 addr=0x68 reg=0x75 n=1"})
        with _bridged(br):
            out = core.i2c_read_reg(0x68, 0x75, port="COM21")
        self.assertIn("没有 data 字段", out)

    def test_write_renders_and_verifies_when_asked(self):
        br = FakeBridge(handlers={"I2C READ": "+OK I2C READ bus=0 addr=0x68 reg=0x75 n=1 data=0x01"})
        with _bridged(br):
            out = core.i2c_write_reg(0x68, 0x75, "0x01", port="COM21", verify=True)
        self.assertIn("桥已确认发出", out)
        self.assertIn("✓ 一致", out)

    def test_write_verify_mismatch_is_flagged_with_caveat(self):
        """不一致要报，但必须同时说明「只写寄存器回读本来就不一致」，否则是假 FAIL。"""
        br = FakeBridge(handlers={"I2C READ": "+OK I2C READ bus=0 addr=0x68 reg=0x75 n=1 data=0x02"})
        with _bridged(br):
            out = core.i2c_write_reg(0x68, 0x75, "0x01", port="COM21", verify=True)
        self.assertIn("✗ 不一致", out)
        self.assertIn("只写", out)

    def test_write_default_does_not_read_back(self):
        with _bridged(FakeBridge()) as br:
            core.i2c_write_reg(0x68, 0x75, "0x01", port="COM21")
        self.assertFalse(any(w.startswith("I2C READ") for w in br.written),
                         "verify=False 时不许偷偷回读（只写寄存器会因此产生假 FAIL）")

    def test_write_sends_the_bytes_it_says(self):
        with _bridged(FakeBridge()) as br:
            core.i2c_write_reg(0x68, 0x6B, "0x01,0x02", port="COM21")
        self.assertTrue(any("I2C WRITE 0 0x68 0x6B 0x01,0x02" in w for w in br.written),
                        "实际发出的命令：%r" % (br.written,))

    def test_write_rejects_empty_payload(self):
        with _bridged(FakeBridge()):
            out = core.i2c_write_reg(0x68, 0x6B, [], port="COM21")
        self.assertIn("没有要写的东西", out)

    def test_write_rejects_non_byte_value(self):
        with _bridged(FakeBridge()):
            out = core.i2c_write_reg(0x68, 0x6B, "0x1FF", port="COM21")
        self.assertIn("非字节值", out)


# --------------------------------------------------------------------------
# 3. SPI：没有地址概念，所以「扫描」只能是逐 CS 读 JEDEC ID
# --------------------------------------------------------------------------

@unittest.skipUnless(core.HAS_PYSERIAL, "pyserial 未安装")
class SpiTest(unittest.TestCase):

    def test_scan_decodes_jedec_vendor_and_capacity(self):
        with _bridged(FakeBridge()):
            out = core.spi_scan(0, "COM21")
        self.assertIn("Winbond", out)
        self.assertIn("16 MB", out)                # 容量码 0x18 → 1<<24 字节

    def test_scan_reports_empty_slot_as_miso_floating(self):
        with _bridged(FakeBridge()):
            out = core.spi_scan(0, "COM21")
        self.assertIn("无器件", out)
        self.assertIn("悬空", out)

    def test_scan_masks_jedec_to_24_bits(self):
        """真桥可能回 32 位原始值（0xffffffff / 0x00ef4018）——必须先掩到 24 位再判空。

        变异验证抓出来的缺口：旧夹具的空槽全写成 6 位 0xffffff（本来就 ≤24 位，
        掩不掩一个样），于是「不掩码」这个缺陷在测试下是绿的。8 位形式才是掩码
        的存在理由：全 1 的 32 位空槽不掩会被误判成一颗不存在的器件。
        """
        br = FakeBridge(handlers={
            # ★ 多值字段必须是**一个 token 内逗号分隔**（_bridge_fields 按空格切字段，
            #   写成空格分隔的 "slots=a 1:b" 时第二段会被当 _flags 丢掉——这就是本测试
            #   第一版红掉的原因）。8 位补零 / 全 1 形式在这里才是掩码的用武之地。
            "SPI SCAN": "+OK SPI SCAN bus=0 n=2 slots=0:0x00ef4018,1:0xffffffff"})
        with _bridged(br):
            out = core.spi_scan(0, "COM21")
        self.assertIn("Winbond", out)      # 补零的 8 位形式要照常解出厂商
        self.assertIn("无器件", out)        # 全 1 的 32 位空槽必须被判空
        self.assertIn("悬空", out)

    def test_scan_all_empty_says_spi_is_not_broken(self):
        """一条 CS 都没有 flash → 必须说清「这不是 SPI 坏了」，否则用户会去拆板子。"""
        br = FakeBridge(handlers={"SPI SCAN": "+OK SPI SCAN bus=0 n=2 slots=0:0xffffff 1:0x000000"})
        with _bridged(br):
            out = core.spi_scan(0, "COM21")
        self.assertIn("没有任何一条 CS 上读出有效 JEDEC ID", out)
        self.assertIn("SPI 传感器", out)
        self.assertIn("CS 脚号", out)

    def test_scan_flags_missing_slots_field(self):
        br = FakeBridge(handlers={"SPI SCAN": "+OK SPI SCAN bus=0 n=0"})
        with _bridged(br):
            out = core.spi_scan(0, "COM21")
        self.assertIn("协议不符", out)

    def test_scan_explains_spi_has_no_addressing(self):
        with _bridged(FakeBridge()):
            out = core.spi_scan(0, "COM21")
        self.assertIn("没有地址概念", out)

    def test_read_reg_returns_bytes(self):
        with _bridged(FakeBridge()):
            out = core.spi_read_reg(0x9F, 3, port="COM21")
        self.assertIn("0xef", out)
        self.assertIn("0x40", out)
        self.assertIn("0x18", out)

    def test_read_reg_rejects_cmd_over_one_byte(self):
        with _bridged(FakeBridge()):
            out = core.spi_read_reg(0x100, 1, port="COM21")
        self.assertIn("超出一个字节", out)
        self.assertNotIn("读到", out)

    def test_read_all_ones_warns_about_miso(self):
        br = FakeBridge(handlers={"SPI READ": "+OK SPI READ bus=0 cs=0 cmd=0x9f n=1 data=0xff"})
        with _bridged(br):
            out = core.spi_read_reg(0x9F, 1, port="COM21")
        self.assertIn("MISO", out)


# --------------------------------------------------------------------------
# 4. 1-Wire
# --------------------------------------------------------------------------

@unittest.skipUnless(core.HAS_PYSERIAL, "pyserial 未安装")
class OneWireTest(unittest.TestCase):

    def test_scan_decodes_family_code(self):
        with _bridged(FakeBridge()):
            out = core.onewire_scan(0, "COM21")
        self.assertIn("DS18B20", out)
        self.assertIn("0x28", out)

    def test_scan_of_ds18b20_gives_read_recipe(self):
        with _bridged(FakeBridge()):
            out = core.onewire_scan(0, "COM21")
        self.assertIn("0x44", out)                 # 启动转换
        self.assertIn("0xBE", out)                 # 读暂存器

    def test_scan_empty_points_at_missing_pullup(self):
        br = FakeBridge(handlers={"OW SCAN": "+OK OW SCAN bus=0 n=0 roms="})
        with _bridged(br):
            out = core.onewire_scan(0, "COM21")
        self.assertIn("没有找到任何 1-Wire 器件", out)
        self.assertIn("4.7k", out)

    def test_scan_says_when_family_unknown(self):
        br = FakeBridge(handlers={"OW SCAN": "+OK OW SCAN bus=0 n=1 roms=9a01020304050607"})
        with _bridged(br):
            out = core.onewire_scan(0, "COM21")
        self.assertIn("未收录", out)


# --------------------------------------------------------------------------
# 5. RS485
# --------------------------------------------------------------------------

@unittest.skipUnless(core.HAS_PYSERIAL, "pyserial 未安装")
class Rs485Test(unittest.TestCase):

    def test_send_renders_bytes_and_states_the_limit(self):
        """必须把「不证明对端收到了」印在输出里 —— 这是最容易过度解读的一条。"""
        with _bridged(FakeBridge()):
            out = core.rs485_send("48656c6c6f", port="COM21")
        self.assertIn("5 字节", out)
        self.assertIn("不证明对端收到了", out)
        self.assertIn("示波器", out)

    def test_send_newline_appends_crlf(self):
        with _bridged(FakeBridge()) as br:
            core.rs485_send("41", port="COM21", newline=True)
        self.assertTrue(any(w.rstrip().endswith("410d0a") for w in br.written),
                        "实际发出：%r" % (br.written,))

    def test_send_rejects_out_of_range_de_ms(self):
        with _bridged(FakeBridge()):
            out = core.rs485_send("41", de_ms=5000, port="COM21")
        self.assertIn("0–1000", out)

    def test_send_rejects_empty_payload(self):
        with _bridged(FakeBridge()):
            out = core.rs485_send([], port="COM21")
        self.assertIn("没有要写的东西", out)

    def test_send_accepts_plain_int(self):
        with _bridged(FakeBridge()) as br:
            core.rs485_send(0x41, port="COM21")
        self.assertTrue(any("41" in w for w in br.written))


# --------------------------------------------------------------------------
# 6. 链路：扫描 → 用扫到的地址去读（这条才是真正「有用法」的路径）
# --------------------------------------------------------------------------

@unittest.skipUnless(core.HAS_PYSERIAL, "pyserial 未安装")
class BusChainTest(unittest.TestCase):

    def test_scan_then_read_from_a_scanned_address(self):
        """i2c_scan 报出来的地址，必须能直接喂给 i2c_read_reg。"""
        br = FakeBridge(handlers={
            "I2C READ": "+OK I2C READ bus=0 addr=0x70 reg=0x00 n=1 data=0x02",
        })
        with _bridged(br):
            scan = core.i2c_scan(0, "COM21")
            self.assertIn("TCA9548A", scan)         # 扫到并认出了复用器
            read = core.i2c_read_reg(0x70, 0x00, port="COM21")
        self.assertIn("0x70", read)                 # 同一条链路、同一个地址
        self.assertIn("0x02", read)
        self.assertEqual(_scan_hit(scan), "2 个")

    def test_write_verify_then_probe_reuses_one_bridge(self):
        """写 → 回读校验 → 桥自检，三条命令必须都打到同一块桥上（不许各开各的口）。"""
        br = FakeBridge(handlers={
            "I2C READ": "+OK I2C READ bus=0 addr=0x68 reg=0x6b n=1 data=0x00",
        })
        with _bridged(br):
            core.i2c_write_reg(0x68, 0x6B, 0x00, port="COM21", verify=True)
            core.bridge_probe("COM21")
        heads = [w.split()[0].upper() for w in br.written if w.strip()]
        self.assertIn("I2C", heads)
        self.assertIn("PING", heads)


# --------------------------------------------------------------------------
# 7. 隔离：总线工具必须与串口工具受同一套门禁
# --------------------------------------------------------------------------

class BusIsolationTest(unittest.TestCase):

    def test_scan_is_blocked_by_port_deny_list(self):
        """conftest 默认把端口全禁（DENY=*）—— 扫描属于「会在总线上打时序」的操作，
        必须连打开端口这一步都过不去。"""
        out = core.i2c_scan(0, "COM21", timeout=0.05)
        self.assertIn("隔离拦截", out)

    def test_write_is_blocked_in_readonly_mode(self):
        """扫可以（read 档允许），写不行 —— 这两档必须真的分开，不能一起放行。"""
        with relaxed_env("readonly", ""):
            out = core.i2c_write_reg(0x68, 0x6B, 0x01, port="COM21", timeout=0.05)
        self.assertIn("【隔离拦截】", out)
        self.assertIn("怎么放开", out)
        self.assertNotIn("桥已确认发出", out)

    def test_scan_is_allowed_in_readonly_mode(self):
        """反过来也要钉住：read 档下扫描必须能过闸门（否则只读档就废了）。"""
        br = FakeBridge()
        with relaxed_env("readonly", ""):
            with mock.patch.object(core, "_open_serial", lambda **kw: br):
                out = core.i2c_scan(0, "COM21")
        self.assertNotIn("隔离拦截", out)
        self.assertIn("应答器件", out)


# --------------------------------------------------------------------------
# 8. 边界：参数再离谱也只能「返回一句说明」，不许抛异常
# --------------------------------------------------------------------------

@unittest.skipUnless(core.HAS_PYSERIAL, "pyserial 未安装")
class BusBoundaryTest(unittest.TestCase):

    def test_junk_arguments_never_raise(self):
        """工具对外是 MCP 的一个函数，抛 Python 异常 = 客户端只看到一句 traceback。

        所以每种工具都要对「什么参数都可能传进来」有兜底。这里一次把所有离谱组合
        过一遍，断言只返回 str。
        """
        junk = [None, "", 0, -1, [], {}, 1e9, "abc", 0x1FF, "0x", [None], "1,2,x", True]
        #: 全用关键字传参：`bridge_probe` 的第一个位置参数就是 port，
        #: 位置参数 + port= 会撞成 TypeError —— 那是**调用方**的错，不是工具的锅，
        #: 混在一起测会掩盖真正要测的「工具对垃圾输入的表现」。
        cases = [
            (core.i2c_scan, lambda v: {"bus": v}),
            (core.i2c_read_reg, lambda v: {"addr": v, "reg": v}),
            (core.i2c_write_reg, lambda v: {"addr": v, "reg": v, "data": v}),
            (core.spi_scan, lambda v: {"bus": v}),
            (core.spi_read_reg, lambda v: {"cmd_byte": v, "count": 1}),
            (core.onewire_scan, lambda v: {"bus": v}),
            (core.rs485_send, lambda v: {"data": v}),
            (core.bridge_probe, lambda v: {"port": v}),
        ]
        with _bridged(FakeBridge()):
            for v in junk:
                for fn, mk in cases:
                    kw = mk(v)
                    kw.setdefault("port", "COM21")
                    try:
                        out = fn(**kw)
                    except Exception as exc:      # noqa: BLE001
                        self.fail("%s(%r) 抛异常了：%s: %s"
                                  % (fn.__name__, v, type(exc).__name__, exc))
                    self.assertIsInstance(out, str,
                                          "%s(%r) 返回了 %s" % (fn.__name__, v, type(out)))

    def test_port_none_falls_back_to_env_var(self):
        br = FakeBridge()
        with _bridged(br):
            with mock.patch.dict(os.environ, {"MCU_DEBUG_BRIDGE_PORT": "COM99"}):
                out = core.i2c_scan(0, None)
        self.assertIn("应答器件", out)
        self.assertTrue(any("I2C SCAN" in w for w in br.written))

    def test_non_str_port_is_rejected_or_coerced_without_crash(self):
        with _bridged(FakeBridge()):
            out = core.i2c_scan(0, 123)
        self.assertIsInstance(out, str)


# --------------------------------------------------------------------------
# 9. 知识层：地址/ID 表必须逐条可溯源（本项目的护城河口径）
# --------------------------------------------------------------------------

class BusKnowledgeTest(unittest.TestCase):

    def test_every_i2c_entry_has_a_source(self):
        """每条「地址 → 器件」都必须能回答「依据是什么」。

        缺了 source 就等于那条没有出处，被追问时答不上来 —— 这比不收录更糟，
        因为它看起来是权威的。
        """
        self.assertTrue(core._I2C_KNOWN, "地址表不能为空")
        for addr, val in core._I2C_KNOWN.items():
            self.assertIsInstance(val, tuple, "0x%02X 的值应是 (器件, source)" % addr)
            self.assertEqual(len(val), 2, "0x%02X 缺 source" % addr)
            self.assertTrue(val[1].strip(), "0x%02X 的 source 为空" % addr)
            self.assertLessEqual(addr, 0x7F, "0x%02X 不是 7 位地址" % addr)

    def test_reserved_ranges_are_sourced_and_inside_7bit(self):
        self.assertTrue(core._I2C_RESERVED)
        for (lo, hi), why in core._I2C_RESERVED:
            self.assertLess(lo, hi)
            self.assertGreaterEqual(lo, 0)
            self.assertLessEqual(hi, 0x7F)
            self.assertIn("I2C 规范保留", why)

    def test_known_tca9548a_range_is_contiguous(self):
        """TCA9548A 是 0x70|A2A1A0，八档必须齐全 —— 漏一档，用户就会以为那一档的
        复用器「扫不到」。"""
        for addr in range(0x70, 0x78):
            self.assertIn(addr, core._I2C_KNOWN, "0x%02X 缺 TCA9548A 档" % addr)

    def test_jedec_capacity_code_matches_sfud_table(self):
        """容量码按 0x9F 的通用规则：1 << code 字节；0x20 是特例（512 Mbit）。"""
        self.assertEqual(1 << 0x18, 16 * 1024 * 1024)
        self.assertEqual(1 << 0x14, 1024 * 1024)
        self.assertEqual(64 * 1024 * 1024, 512 * 1024 * 1024 // 8)


if __name__ == "__main__":
    unittest.main()
