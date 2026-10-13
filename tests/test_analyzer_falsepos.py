# -*- coding: utf-8 -*-
"""
mcu-debug-mcp · analyzer 误报回归（test_analyzer_falsepos.py）

来历：B③「老工具真实数据双向验证」（2026-09-21）用真实风格日志回放 44 条规则，
抓出 8 类"看似合理的假答案"（假数字/跨域抢答）。本文件把它们逐条锁死：

  每个案例两个方向——
    ① 正常或跨域的日志必须【不命中】（防假答案）
    ② 对应的真故障日志必须【仍命中】（防修过头把真阳性也砍了）

运行：python -m pytest tests -q
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import analyze_log


def ids(text):
    return [f["id"] for f in analyze_log(text)["findings"]]


class TestAtErrorScope(unittest.TestCase):
    """AT_ERROR：孤立的 ERROR 行（内核/工具链/curl）不是 AT 模块拒指令。"""

    def test_bare_error_line_no_hit(self):
        self.assertNotIn("AT_ERROR", ids("ERROR\nfatal exception in task\n"))

    def test_lowercase_error_line_no_hit(self):
        self.assertNotIn("AT_ERROR", ids("error\nbuild aborted\n"))

    def test_at_error_still_hit(self):
        self.assertIn("AT_ERROR", ids("AT+CWMODE=1\r\nERROR\r\nAT+CIPSTART\r\nERROR\r\n"))


class TestSensorZeroScope(unittest.TestCase):
    """SENSOR_ZERO：计数类日志（队列深度/丢包数）不是传感器没读进数。"""

    def test_queue_counters_no_hit(self):
        log = "[Task List]\nidle  R 0 128\nsend queue: 0\nrecv queue: 0\ndropped: 0\n"
        self.assertNotIn("SENSOR_ZERO", ids(log))

    def test_sensor_zero_still_hit(self):
        self.assertIn("SENSOR_ZERO", ids("temp: 0.0 ℃\r\nhumi: 0.0 %\r\nlux: 0.0\r\n"))

    def test_chinese_sensor_zero_still_hit(self):
        self.assertIn("SENSOR_ZERO", ids("温度: 0\r\n湿度: 0\r\n光照: 0\r\n"))


class TestBootLoopScope(unittest.TestCase):
    """BOOT_LOOP：问候语 "Hello," 重复不算复位循环。"""

    def test_hello_greeting_no_hit(self):
        log = "Hello, device booting\nSystem ready\nHello, mqtt reconnected\n"
        self.assertNotIn("BOOT_LOOP", ids(log))

    def test_esp_banner_still_hit(self):
        banner = "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\nload:0x3fff0018\r\n"
        self.assertIn("BOOT_LOOP", ids(banner + "app running\r\n" + banner))


class TestI2cNackScope(unittest.TestCase):
    """I2C_NACK：CAN 总线上的 NACK 不能算 I2C 从机没应答。"""

    def test_can_nack_no_hit(self):
        log = "CAN0 status: error-passive\nCAN frame NACK received, retrying\n"
        self.assertNotIn("I2C_NACK", ids(log))

    def test_i2c_nack_still_hit(self):
        self.assertIn("I2C_NACK", ids("I2C NACK addr 0x76\r\n"))


class TestUartFramingScope(unittest.TestCase):
    """UART_FRAMING：内存/缓冲区 overrun 不能算 UART 帧错误。"""

    def test_heap_overrun_no_hit(self):
        self.assertNotIn("UART_FRAMING", ids("heap check: buffer overrun detected at 0x20001a40\n"))

    def test_uart_framing_still_hit(self):
        self.assertIn("UART_FRAMING", ids("UART1 framing error detected\r\noverrun error on UART2\r\n"))

    def test_ore_still_hit(self):
        self.assertIn("UART_FRAMING", ids("USART_SR: ORE set, clearing flag\r\n"))


class TestDhtTimeoutScope(unittest.TestCase):
    """DHT_TIMEOUT：裸 timeout / checksum error 是通用词，不是 DHT 时序问题。"""

    def test_tcp_timeout_no_hit(self):
        self.assertNotIn("DHT_TIMEOUT", ids("CONNECT FAIL\nconnection timeout\n"))

    def test_dht_still_hit(self):
        self.assertIn("DHT_TIMEOUT", ids("DHT11 read timeout!\r\nDHT11 checksum error\r\n"))


class TestDs18b20Scope(unittest.TestCase):
    """DS18B20_85：寄存器原始值 "raw: 85" 不是恒 85.0℃。"""

    def test_raw_register_no_hit(self):
        log = "probe ok\ntemp raw: 85\ntemp raw: 85\ntemp raw: 85\n"
        self.assertNotIn("DS18B20_85", ids(log))

    def test_temp_85_still_hit(self):
        self.assertIn("DS18B20_85", ids("Temp: 85.0 ℃\r\nTemp: 85.0 ℃\r\nTemp: 85.0 ℃\r\n"))


class TestBleConnLostScope(unittest.TestCase):
    """BLE_CONN_LOST：TCP/MQTT 的 connection timeout/dropped 不是蓝牙断连。"""

    def test_tcp_dropped_no_hit(self):
        self.assertNotIn("BLE_CONN_LOST", ids("socket closed by peer\r\nTCP connection dropped\r\n"))

    def test_tcp_timeout_no_hit(self):
        log = 'AT+CIPSTART="TCP","1.2.3.4",1883\r\nCONNECT FAIL\r\nconnection timeout\r\n'
        self.assertNotIn("BLE_CONN_LOST", ids(log))

    def test_ble_disconnect_still_hit(self):
        self.assertIn("BLE_CONN_LOST", ids("BLE disconnected (0x08)\r\n"))

    def test_ble_timeout_still_hit(self):
        self.assertIn("BLE_CONN_LOST", ids("BLE connection timeout after 30s\r\n"))


class TestUsbEnumScope(unittest.TestCase):
    """USB_ENUM_FAIL：Linux/驱动层通用的 unknown device 不是 USB 枚举失败。"""

    def test_generic_unknown_device_no_hit(self):
        self.assertNotIn("USB_ENUM_FAIL", ids("v4l2: unknown device /dev/video9 ignored\r\n"))

    def test_usb_enum_still_hit(self):
        self.assertIn("USB_ENUM_FAIL", ids("USB device not recognized\r\nDevice descriptor request failed\r\n"))


class TestKeilFlashScope(unittest.TestCase):
    """KEIL_FLASH_DL_FAIL：通用的 cannot be loaded（如 libusb）不是 Keil 烧录失败。"""

    def test_generic_load_failure_no_hit(self):
        self.assertNotIn("KEIL_FLASH_DL_FAIL", ids("ERROR: module libusb cannot be loaded\r\n"))

    def test_keil_still_hit(self):
        self.assertIn("KEIL_FLASH_DL_FAIL", ids('Flash Download failed - "Cortex-M4"\r\n'))


class TestNoRegressionOnRealFaults(unittest.TestCase):
    """真实故障日志的真阳性必须保持（防修过头）。"""

    def test_esp_panic(self):
        log = ("Guru Meditation Error: Core  0 panic'ed (StoreProhibited).\r\n"
               "Backtrace: 0x400d1234:0x3ffb1f30 0x400d2a5c:0x3ffb1f50\r\n")
        self.assertIn("ESP_PANIC", ids(log))

    def test_freertos_stack_overflow(self):
        self.assertIn("RTOS_STACK_OVF",
                      ids("***ERROR*** A stack overflow in task wifiTask has been detected.\r\n"))

    def test_hardfault_dump(self):
        hit = ids("HardFault_Handler\r\nCFSR=0x00008200 (PRECISERR)\r\nHFSR=0x40000000 (FORCED)\r\n")
        self.assertIn("HARDFAULT", hit)
        self.assertIn("HF_DETAIL", hit)

    def test_can_bus_off(self):
        self.assertIn("CAN_BUS_OFF", ids("CAN transmit failed\r\nError: bus-off state entered\r\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
