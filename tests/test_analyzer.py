# -*- coding: utf-8 -*-
"""
mcu-debug-mcp · analyzer 异常模式库测试（test_analyzer.py）

验证状态：本机实测（纯标准库，无第三方依赖）。
每条规则两个断言方向：典型故障日志必须命中、正常日志必须不命中（防误报）。
运行：python tests/test_analyzer.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import (analyze_log, identify_usb_serial, suggest_baudrates,
                      _printable_ratio, rule_count, _EXTRA_RULES, _EXTRA_DIAG)


def hit_ids(result):
    return [f["id"] for f in result["findings"]]


class TestMojaibake(unittest.TestCase):
    def test_garbage_hit(self):
        # 模拟波特率错位：大量控制字符+替换符
        garbled = "".join(chr(c) for c in [3, 15, 0, 7, 31, 12, 29, 2, 0, 5, 15, 3, 24, 0, 1, 4, 31, 9, 0, 14] * 5)
        r = analyze_log(garbled)
        self.assertIn("MOJIBAKE", hit_ids(r))

    def test_normal_no_hit(self):
        log = "Temp: 25.3 C, Humi: 60.1%\r\nSystem ready.\r\n"
        self.assertNotIn("MOJIBAKE", hit_ids(analyze_log(log)))


class TestAtError(unittest.TestCase):
    def test_error_hit(self):
        log = "AT+CWMODE=1\r\nERROR\r\nAT+CIPSTART\r\nERROR\r\n"
        self.assertIn("AT_ERROR", hit_ids(analyze_log(log)))

    def test_ok_no_hit(self):
        log = "AT\r\n\r\nOK\r\nAT+CWMODE=1\r\n\r\nOK\r\n"
        self.assertNotIn("AT_ERROR", hit_ids(analyze_log(log)))


class TestAtNoResp(unittest.TestCase):
    def test_lonely_hit(self):
        log = "send: AT+CIPSTART\r\nnothing came back at all, board is silent, no ok no error anywhere in this tail"
        self.assertIn("AT_NO_RESP", hit_ids(analyze_log(log)))

    def test_ok_no_hit(self):
        log = "send: AT\r\nOK\r\n"
        self.assertNotIn("AT_NO_RESP", hit_ids(analyze_log(log)))


class TestBootLoop(unittest.TestCase):
    def test_esp_banner_hit(self):
        banner = "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\nload:0x3fff0018\r\n"
        log = banner + "app running...\r\n" + banner
        self.assertIn("BOOT_LOOP", hit_ids(analyze_log(log)))

    def test_single_banner_no_hit(self):
        banner = "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
        self.assertNotIn("BOOT_LOOP", hit_ids(analyze_log(banner + "app running fine, stable, no more resets here\r\n")))


class TestHardFault(unittest.TestCase):
    def test_word_hit(self):
        log = "HardFault_Handler entered\r\nCFSR:00008600\r\n"
        self.assertIn("HARDFAULT", hit_ids(analyze_log(log)))

    def test_detail_hit(self):
        log = "HardFault\r\nIMPRECISERR set\r\n"
        r = analyze_log(log)
        self.assertIn("HF_DETAIL", hit_ids(r))
        hf = [f for f in r["findings"] if f["id"] == "HF_DETAIL"][0]
        self.assertTrue(any("野指针" in e or "总线错误" in e for e in hf["evidence"]))

    def test_clean_no_hit(self):
        self.assertNotIn("HARDFAULT", hit_ids(analyze_log("Task A running\r\n")))


class TestSensorZero(unittest.TestCase):
    def test_zero_hit(self):
        log = "temp: 0.0 ℃\r\nhumi: 0.0 %\r\nlux: 0.0\r\n"
        self.assertIn("SENSOR_ZERO", hit_ids(analyze_log(log)))

    def test_mixed_no_hit(self):
        log = "temp: 25.6 ℃\r\nhumi: 0.0 %\r\nlux: 300\r\n"
        self.assertNotIn("SENSOR_ZERO", hit_ids(analyze_log(log)))


class TestSensorFF(unittest.TestCase):
    def test_ff_hit(self):
        log = "raw: 0xFF 0xFF 0xFF\r\nraw: 0xFF 0xFF 0xFF\r\nraw: 0xFF 0xFF 0xFF\r\n"
        self.assertIn("SENSOR_FF", hit_ids(analyze_log(log)))

    def test_255_hit(self):
        log = "value: 255\r\nvalue: 255\r\nvalue: 65535\r\n"
        self.assertIn("SENSOR_FF", hit_ids(analyze_log(log)))

    def test_normal_no_hit(self):
        log = "value: 255\r\nvalue: 3\r\nvalue: 1024\r\n"
        self.assertNotIn("SENSOR_FF", hit_ids(analyze_log(log)))


class TestDS18B20_85(unittest.TestCase):
    def test_85_hit(self):
        log = "Temp: 85.0 ℃\r\nTemp: 85.0 ℃\r\nTemp: 85.0 ℃\r\n"
        self.assertIn("DS18B20_85", hit_ids(analyze_log(log)))

    def test_single_85_no_hit(self):
        log = "Temp: 85.0 ℃\r\nTemp: 84.2 ℃\r\n"
        self.assertNotIn("DS18B20_85", hit_ids(analyze_log(log)))


class TestDhtTimeout(unittest.TestCase):
    def test_timeout_hit(self):
        log = "DHT11 read timeout!\r\nretry...\r\nDHT11 checksum error\r\n"
        self.assertIn("DHT_TIMEOUT", hit_ids(analyze_log(log)))

    def test_ok_no_hit(self):
        log = "DHT11 temp: 25, humi: 60\r\n"
        self.assertNotIn("DHT_TIMEOUT", hit_ids(analyze_log(log)))


class TestNegInvalid(unittest.TestCase):
    def test_minus127_hit(self):
        log = "temp: -127\r\nsecond probe temp: -127\r\n"
        self.assertIn("NEG_INVALID", hit_ids(analyze_log(log)))

    def test_single_no_hit(self):
        log = "temp: -127\r\n"
        self.assertNotIn("NEG_INVALID", hit_ids(analyze_log(log)))


class TestI2cNack(unittest.TestCase):
    def test_nack_hit(self):
        log = "I2C NACK addr 0x76\r\n"
        self.assertIn("I2C_NACK", hit_ids(analyze_log(log)))


class TestNetLink(unittest.TestCase):
    def test_wifi_hit(self):
        log = "WIFI DISCONNECT\r\n+CME ERROR: 3\r\n"
        self.assertIn("NET_LINK", hit_ids(analyze_log(log)))

    def test_normal_no_hit(self):
        log = "WIFI CONNECTED\r\nGot IP\r\n"
        self.assertNotIn("NET_LINK", hit_ids(analyze_log(log)))


# ---------------- v2 扩充规则（17 类） ----------------

class TestEspPanic(unittest.TestCase):
    def test_guru_hit(self):
        log = "Guru Meditation Error: Core 1 panic'ed (LoadProhibited)\r\nBacktrace: 0x400d1a2f 0x400d1a5c\r\n"
        r = analyze_log(log)
        self.assertIn("ESP_PANIC", hit_ids(r))

    def test_abort_hit(self):
        log = "abort() was called at PC 0x40083df5\r\n"
        self.assertIn("ESP_PANIC", hit_ids(analyze_log(log)))

    def test_normal_no_hit(self):
        self.assertNotIn("ESP_PANIC", hit_ids(analyze_log("meditation is relaxing\r\npanic buying is bad\r\n")))


class TestEspBrownout(unittest.TestCase):
    def test_hit(self):
        self.assertIn("ESP_BROWNOUT", hit_ids(analyze_log("Brownout detector was triggered\r\n")))
    def test_no_hit(self):
        self.assertNotIn("ESP_BROWNOUT", hit_ids(analyze_log("battery low warning\r\n")))


class TestEspWdt(unittest.TestCase):
    def test_task_wdt_hit(self):
        log = "Task watchdog got triggered. The following tasks did not reset the watchdog in time\r\n"
        self.assertIn("ESP_WDT", hit_ids(analyze_log(log)))

    def test_rst_hit(self):
        log = "rst:0x8 (TG1WDT_SYS_RST)\r\n"
        self.assertIn("ESP_WDT", hit_ids(analyze_log(log)))

    def test_no_hit(self):
        self.assertNotIn("ESP_WDT", hit_ids(analyze_log("feeding dog in the yard\r\n")))


class TestEspFlashCorrupt(unittest.TestCase):
    def test_hit(self):
        log = "image magic mismatch\r\nbootloop (load error)\r\n"
        self.assertIn("ESP_FLASH_CORRUPT", hit_ids(analyze_log(log)))
    def test_no_hit(self):
        self.assertNotIn("ESP_FLASH_CORRUPT", hit_ids(analyze_log("flash write ok\r\n")))


class TestCwjapErr(unittest.TestCase):
    def test_code2_hit(self):
        log = "AT+CWJAP=\"ssid\",\"badpwd\"\r\n+CWJAP:2\r\n\r\nFAIL\r\n"
        r = analyze_log(log)
        self.assertIn("CWJAP_ERR", hit_ids(r))
        f = [x for x in r["findings"] if x["id"] == "CWJAP_ERR"][0]
        self.assertTrue(any("密码错误" in e for e in f["evidence"]))
    def test_no_hit(self):
        self.assertNotIn("CWJAP_ERR", hit_ids(analyze_log("WIFI CONNECTED\r\n+CWJAP:1\r\n".replace("+CWJAP:1", ""))))


class TestCmeError(unittest.TestCase):
    def test_code3_hit(self):
        r = analyze_log("+CME ERROR: 3\r\n")
        self.assertIn("AT_CME_ERROR", hit_ids(r))
        f = [x for x in r["findings"] if x["id"] == "AT_CME_ERROR"][0]
        self.assertTrue(any("操作不允许" in e for e in f["evidence"]))
    def test_unknown_code_hit(self):
        r = analyze_log("+CME ERROR: 100\r\n")
        self.assertIn("AT_CME_ERROR", hit_ids(r))
    def test_no_hit(self):
        self.assertNotIn("AT_CME_ERROR", hit_ids(analyze_log("AT+CSQ\r\nOK\r\n")))


class TestRtosAssert(unittest.TestCase):
    def test_hit(self):
        log = "assert failed: queue.c:1234 (pxQueue->uxMessagesWaiting)\r\n"
        self.assertIn("RTOS_ASSERT", hit_ids(analyze_log(log)))
    def test_configassert_hit(self):
        self.assertIn("RTOS_ASSERT", hit_ids(analyze_log("configASSERT( pxQueue != NULL )\r\n")))
    def test_no_hit(self):
        self.assertNotIn("RTOS_ASSERT", hit_ids(analyze_log("all asserts passed in unit test report\r\n")))


class TestRtosStackOverflow(unittest.TestCase):
    def test_freertos_hit(self):
        log = "***ERROR*** A stack overflow in task sensorTask has been detected\r\n"
        self.assertIn("RTOS_STACK_OVF", hit_ids(analyze_log(log)))
    def test_canary_hit(self):
        log = "stack canary watchpoint triggered (mainTask)\r\n"
        self.assertIn("RTOS_STACK_OVF", hit_ids(analyze_log(log)))
    def test_no_hit(self):
        self.assertNotIn("RTOS_STACK_OVF", hit_ids(analyze_log("stack size 4096 configured\r\n")))


class TestCompileUndef(unittest.TestCase):
    def test_gcc_hit(self):
        log = "main.o: In function `main':\r\nmain.c:(.text+0x24): undefined reference to `uart_init'\r\n"
        self.assertIn("COMPILE_UNDEF", hit_ids(analyze_log(log)))
    def test_keil_hit(self):
        log = "linking...\r\nmain.axf: Error: L6218E: Undefined symbol HAL_UART_Init (referred from main.o).\r\n"
        self.assertIn("COMPILE_UNDEF", hit_ids(analyze_log(log)))
    def test_no_hit(self):
        self.assertNotIn("COMPILE_UNDEF", hit_ids(analyze_log("reference manual v3.2 downloaded\r\n")))


class TestCompileSyntax(unittest.TestCase):
    def test_gcc_hit(self):
        log = "main.c:12:5: error: expected ';' before '}' token\r\n"
        self.assertIn("COMPILE_SYNTAX", hit_ids(analyze_log(log)))
    def test_keil_hit(self):
        log = "main.c(24): error: #20: identifier \"temp\" is undefined\r\n"
        self.assertIn("COMPILE_SYNTAX", hit_ids(analyze_log(log)))
    def test_generic_error_no_hit(self):
        # 无 文件:行号 前缀的普通 error: 不该命中编译规则
        self.assertNotIn("COMPILE_SYNTAX", hit_ids(analyze_log("cmd error: 3\r\nparse error near line 1\r\n")))


class TestI2cScanEmpty(unittest.TestCase):
    def test_hit(self):
        log = "I2C scan done, 0 devices found\r\n"
        self.assertIn("I2C_SCAN_EMPTY", hit_ids(analyze_log(log)))
    def test_found_hit(self):
        self.assertIn("I2C_SCAN_EMPTY", hit_ids(analyze_log("No I2C devices found\r\n")))
    def test_normal_no_hit(self):
        self.assertNotIn("I2C_SCAN_EMPTY", hit_ids(analyze_log("I2C scan found 0x76 and 0x68\r\n")))


class TestUartFraming(unittest.TestCase):
    def test_hit(self):
        log = "UART1 framing error detected\r\noverrun error on UART2\r\n"
        self.assertIn("UART_FRAMING", hit_ids(analyze_log(log)))
    def test_ore_hit(self):
        self.assertIn("UART_FRAMING", hit_ids(analyze_log("USART_SR: ORE set, clearing flag\r\n")))
    def test_no_hit(self):
        self.assertNotIn("UART_FRAMING", hit_ids(analyze_log("UART init at 115200 ok\r\n")))


class TestHalAssert(unittest.TestCase):
    def test_hit(self):
        log = "assert_param failed in HAL_UART_Transmit\r\n"
        self.assertIn("HAL_ASSERT", hit_ids(analyze_log(log)))
    def test_wrong_params_hit(self):
        self.assertIn("HAL_ASSERT", hit_ids(analyze_log("Wrong parameters passed to function\r\n")))
    def test_no_hit(self):
        self.assertNotIn("HAL_ASSERT", hit_ids(analyze_log("UART transmit 32 bytes ok\r\n")))


class TestAdcSaturated(unittest.TestCase):
    def test_hit(self):
        log = "ADC CH3: 4095\r\nADC CH3: 4095\r\nADC CH3: 4095\r\n"
        self.assertIn("ADC_SATURATED", hit_ids(analyze_log(log)))
    def test_single_no_hit(self):
        self.assertNotIn("ADC_SATURATED", hit_ids(analyze_log("ADC CH3: 4095\r\nADC CH3: 2048\r\n")))
    def test_low_value_no_hit(self):
        self.assertNotIn("ADC_SATURATED", hit_ids(analyze_log("ADC CH0: 512\r\nADC CH0: 513\r\n")))


class TestMqttFail(unittest.TestCase):
    def test_rc_minus2_hit(self):
        r = analyze_log("MQTT connect rc=-2\r\n")
        self.assertIn("MQTT_FAIL", hit_ids(r))
        f = [x for x in r["findings"] if x["id"] == "MQTT_FAIL"][0]
        self.assertTrue(any("TCP" in e for e in f["evidence"]))
    def test_text_hit(self):
        self.assertIn("MQTT_FAIL", hit_ids(analyze_log("MQTT broker connect failed after 3 retries\r\n")))
    def test_no_hit(self):
        self.assertNotIn("MQTT_FAIL", hit_ids(analyze_log("MQTT publish temp=25 ok\r\n")))


class TestHttpErr(unittest.TestCase):
    def test_404_hit(self):
        self.assertIn("HTTP_ERR", hit_ids(analyze_log("HTTP/1.1 404 Not Found\r\n")))
    def test_curl_hit(self):
        self.assertIn("HTTP_ERR", hit_ids(analyze_log("curl: (7) Failed to connect to 192.168.1.10\r\n")))
    def test_200_no_hit(self):
        self.assertNotIn("HTTP_ERR", hit_ids(analyze_log("HTTP/1.1 200 OK\r\n")))


class TestSdInitFail(unittest.TestCase):
    def test_hit(self):
        log = "SD card init failed\r\ncardType: 0\r\n"
        self.assertIn("SD_INIT_FAIL", hit_ids(analyze_log(log)))
    def test_fr_hit(self):
        self.assertIn("SD_INIT_FAIL", hit_ids(analyze_log("f_mount returned FR_NOT_READY\r\n")))
    def test_no_hit(self):
        self.assertNotIn("SD_INIT_FAIL", hit_ids(analyze_log("SD card mounted, 4GB FAT32\r\n")))


class TestEdgeCases(unittest.TestCase):
    def test_empty_log(self):
        r = analyze_log("")
        self.assertEqual(r["findings"], [])
        self.assertTrue(r["summary"])

    def test_clean_log_summary(self):
        r = analyze_log("all systems nominal, temp 25.3C, humi 55%\r\n")
        self.assertEqual(r["findings"], [])

    def test_findings_sorted_high_first(self):
        garbled = "".join(chr(c) for c in [3, 15, 0, 7, 31, 12, 29, 2, 0, 5] * 4)
        log = garbled + "\r\nWIFI DISCONNECT\r\n"
        ids = hit_ids(analyze_log(log))
        sevs = {"high": 0, "medium": 1}
        orders = [sevs[f["severity"]] for f in analyze_log(log)["findings"]]
        self.assertEqual(orders, sorted(orders))

    def test_never_raises(self):
        for bad in ["\x00\x01\x02", "AT", "a" * 10000, "中文温度：25℃" * 100]:
            try:
                analyze_log(bad)
            except Exception as e:
                self.fail("analyze_log raised on %r: %s" % (bad[:20], e))


# ---------------- v3 外置知识库规则（knowledge/fault_patterns.json） ----------------

class TestPatternRules(unittest.TestCase):
    def test_loaded_from_json(self):
        # 8 条烧录失败规则从 JSON 加载，总量 = 内置 + 外置
        self.assertGreaterEqual(len(_EXTRA_RULES), 8)
        self.assertGreaterEqual(rule_count(), 38)

    def test_source_annotated(self):
        # 领域级可信度：外置规则必须逐条标注来源
        import json
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "knowledge", "fault_patterns.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data["rules"]:
            self.assertTrue(item.get("source"), "规则 %s 缺 source" % item.get("id"))
            self.assertTrue(item.get("next_steps"), "规则 %s 缺 next_steps" % item.get("id"))

    def test_stlink_connect_hit(self):
        r = analyze_log("Error: ST-LINK error (DEV_TARGET_NOT_HALTED)\r\n")
        self.assertIn("STLINK_CONNECT_FAIL", hit_ids(r))

    def test_no_target_hit(self):
        self.assertIn("STLINK_CONNECT_FAIL", hit_ids(analyze_log("No target connected\r\n")))

    def test_keil_no_algo_hit(self):
        log = "No Algorithm found for: 08000000H\r\nLoad \"C\\\\Keil\\\\ARM\\\\FLASH\\\\STM32F10x_128.FLM\"\r\n"
        r = analyze_log(log)
        self.assertIn("KEIL_NO_ALGO", hit_ids(r))
        f = [x for x in r["findings"] if x["id"] == "KEIL_NO_ALGO"][0]
        # JSON 里的 diagnosis/next_steps 必须被合并进结果
        self.assertTrue(f.get("diagnosis"))
        self.assertTrue(f.get("next_steps"))

    def test_keil_dl_fail_hit(self):
        log = 'Flash Download failed - "Cortex-M4"\r\n'
        self.assertIn("KEIL_FLASH_DL_FAIL", hit_ids(analyze_log(log)))

    def test_esptool_hit(self):
        r = analyze_log("A fatal error occurred: Failed to connect to ESP32: Timed out waiting for packet header\r\n")
        self.assertIn("ESPTOOL_FAIL", hit_ids(r))

    def test_readout_protect_hit(self):
        r = analyze_log("Error: flash is protected\r\nRDP Read protection: active\r\n")
        self.assertIn("FLASH_READOUT_PROTECT", hit_ids(r))

    def test_swd_comm_hit(self):
        self.assertIn("SWD_COMM_FAIL", hit_ids(analyze_log("Error: DAP error during transfer\r\n")))

    def test_openocd_hit(self):
        self.assertIn("OPENOCD_TARGET_FAIL", hit_ids(analyze_log("Error: init mode failed (time out)\r\n")))

    def test_normal_no_hit(self):
        # 正常烧录成功输出不该误报
        log = ("Erasing done\r\nWriting done at 0x08000000\r\nVerify OK\r\n"
               "flash write ok\r\nupload complete, rebooting\r\n")
        r = analyze_log(log)
        for rid in ("STLINK_CONNECT_FAIL", "KEIL_NO_ALGO", "KEIL_FLASH_DL_FAIL",
                    "ESPTOOL_FAIL", "FLASH_READOUT_PROTECT", "SWD_COMM_FAIL"):
            self.assertNotIn(rid, hit_ids(r))

    def test_regex_error_tolerated(self):
        # 坏正则在 det 内被吞（re.error），匹配含正则特殊字符的日志也不炸
        r = analyze_log("regex chars: ([unclosed {2,5} [a- \\ done\r\n")
        self.assertIsInstance(r["findings"], list)


# ---------------- v4 外置知识库扩充规则（CAN/蓝牙/WiFi风暴/J-Link/DNS/USB） ----------------

class TestPatternRulesV4(unittest.TestCase):
    def test_can_bus_off_hit(self):
        r = analyze_log("CAN transmit failed\r\nError: bus-off state entered\r\n")
        self.assertIn("CAN_BUS_OFF", hit_ids(r))
        f = [x for x in r["findings"] if x["id"] == "CAN_BUS_OFF"][0]
        self.assertTrue(any("波特率" in s or "终端电阻" in s for s in f["next_steps"]))

    def test_wifi_storm_count_threshold(self):
        # min_count=3：单次断连不报，3 次才报
        one = analyze_log("WIFI DISCONNECT\r\nreconnecting\r\n")
        self.assertNotIn("WIFI_RECONNECT_STORM", hit_ids(one))
        three = analyze_log("WIFI DISCONNECT\r\nWIFI DISCONNECT\r\nWIFI DISCONNECT\r\n")
        self.assertIn("WIFI_RECONNECT_STORM", hit_ids(three))

    def test_ble_lost_hit(self):
        self.assertIn("BLE_CONN_LOST", hit_ids(analyze_log("BLE disconnected (0x08)\r\n")))
        self.assertIn("BLE_CONN_LOST", hit_ids(analyze_log("GATT error during write\r\n")))

    def test_jlink_hit(self):
        self.assertIn("JLINK_FAIL", hit_ids(analyze_log("Cannot connect to J-Link via USB\r\n")))

    def test_dns_fail_hit(self):
        self.assertIn("DNS_FAIL", hit_ids(analyze_log("gethostbyname failed for api.mcu.cn\r\n")))

    def test_usb_enum_hit(self):
        r = analyze_log("USB device not recognized\r\nDevice descriptor request failed\r\n")
        self.assertIn("USB_ENUM_FAIL", hit_ids(r))
        f = [x for x in r["findings"] if x["id"] == "USB_ENUM_FAIL"][0]
        self.assertTrue(any("上拉" in s for s in f["next_steps"]))

    def test_v4_normal_no_hit(self):
        # 正常网络/USB 日志不该误报
        log = "WIFI CONNECTED\r\nGot IP\r\nDNS resolved ok\r\nUSB CDC ready\r\n"
        for rid in ("CAN_BUS_OFF", "WIFI_RECONNECT_STORM", "BLE_CONN_LOST",
                    "JLINK_FAIL", "DNS_FAIL", "USB_ENUM_FAIL"):
            self.assertNotIn(rid, hit_ids(analyze_log(log)))


class TestHelpers(unittest.TestCase):
    def test_usb_serial_map(self):
        self.assertEqual(identify_usb_serial("1A86:7523")[0], "CH340/CH341")
        self.assertEqual(identify_usb_serial("10c4:ea60")[0], "CP2102/CP210x")
        self.assertIsNone(identify_usb_serial("1234:5678"))

    def test_baud_suggestions(self):
        b = suggest_baudrates("")
        self.assertIn(115200, b)
        self.assertIn(9600, b)

    def test_printable_ratio(self):
        self.assertAlmostEqual(_printable_ratio("abc\r\n"), 1.0)
        self.assertLess(_printable_ratio("\x00\x01\x02\x03\x04\x05"), 0.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
