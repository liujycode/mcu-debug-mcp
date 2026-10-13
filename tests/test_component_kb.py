# -*- coding: utf-8 -*-
"""器件选型库（G01）与外设 API 用法层（G03）回归。

为什么单开文件盯这两层：它们是**从驱动库派生**的视图——派生逻辑一旦被改坏
（分类判据松了、切句把话腰斩了、地址正则抓到别的十六进制数），不会有人报错，
只会让 AI 拿着错数据去指导毕设学生接线。这类"静默错答案"必须靠断言兜住。

本文件回答六个问题：
1. 视图条数与驱动数一致（少一条 = 某个驱动被漏掉，多一条 = 凭空多出器件）；
2. 接口分类与适配层真实 HAL 符号**自洽**（判据不许比证据宽松或更严）；
3. 微秒时序要求判的是**精度**不是函数名（stepper 有 delay_us 但毫秒粒度就够）；
4. 地址必须是 usage 里那个真 7 位地址，且只此一个；
5. 初始化要求是**原句**，不腰斩括号内的逗号；
6. 空名/错名/空库都给出能看懂的话，不静默返回空。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcu_debug_mcp import core  # noqa: E402


def _driver_names():
    return sorted(n for n, _p in core._driver_dirs())


class TestComponentRecords(unittest.TestCase):
    """器件记录：条数、字段完整性、来源可溯。"""

    def test_one_record_per_driver(self):
        """每个驱动恰好一条器件记录——派生视图不许漏也不许多。"""
        self.assertEqual(_driver_names(),
                         sorted(r["name"] for r in core.component_records()))

    def test_required_fields_present(self):
        for r in core.component_records():
            with self.subTest(comp=r["name"]):
                self.assertTrue(r["desc"], "desc 空")
                self.assertTrue(r["usage"], "usage 空")
                self.assertIn(r["bus"], ("i2c", "uart", "timer_pwm",
                                         "gpio_timing", "unknown"))
                # 每条都要能回答"依据是什么"（护城河与 validate_knowledge 同一要求）
                self.assertTrue(r["source"].startswith("drivers/"))
                self.assertTrue(os.path.isfile(os.path.join(
                    ROOT, "src", "mcu_debug_mcp", r["source"])),
                    "source 指向的文件不存在：%s" % r["source"])

    def test_address_is_seven_bit_hex_from_usage(self):
        """地址只能来自 meta.usage 里真实写的那个，且是 2 位十六进制（7 位+0x）。"""
        for r in core.component_records():
            with self.subTest(comp=r["name"]):
                if r["address"]:
                    self.assertRegex(r["address"], r"^0x[0-9A-Fa-f]{2}$")
                    meta = core._driver_meta(r["name"])
                    self.assertIn(r["address"], meta.get("usage", ""),
                                  "地址不在该驱动 meta.usage 原文里")


class TestBusClassification(unittest.TestCase):
    """接口分类：判据必须与适配层真实符号自洽。"""

    def test_i2c_drivers_have_i2c_hal_symbols(self):
        """判为 i2c 的，适配层里必须真有 I2C 符号——判据不许比证据宽松。"""
        for r in core.component_records():
            if r["bus"] == "i2c":
                with self.subTest(comp=r["name"]):
                    syms = core._PORT_SYM_RE.findall(core._port_source(r["name"]))
                    self.assertTrue(any("I2C" in s for s in syms))

    def test_timer_pwm_drivers_have_timer_symbols(self):
        for r in core.component_records():
            if r["bus"] == "timer_pwm":
                with self.subTest(comp=r["name"]):
                    syms = core._PORT_SYM_RE.findall(core._port_source(r["name"]))
                    self.assertTrue(any(s.startswith("TIM") or "PWM" in s
                                        for s in syms),
                                    "判为 timer_pwm 却没有 TIM/PWM 符号")

    def test_known_devices_classified_as_expected(self):
        """三条真实样本锁死分类结果，防止判据被改松后静默错分类。"""
        want = {"mpu6050": "i2c", "ssd1306": "i2c",
                "hc05": "uart", "servo_sg90": "timer_pwm"}
        got = {r["name"]: r["bus"] for r in core.component_records()}
        for name, bus in want.items():
            with self.subTest(comp=name):
                self.assertEqual(got.get(name), bus)


class TestUsTimingJudgement(unittest.TestCase):
    """微秒时序：判据落在真需求（DWT）上，不落在函数名上。

    ★ 这条判据改错会直接坑人：stepper_28byj48 也有 delay_us，但注释明写
    「HAL_Delay 毫秒粒度即可」；反过来 dht11/ds18b20/hcsr04 注释里明写
    「毫秒粒度不能用」。按函数名判会把 stepper 误标成需微秒时序。
    """

    def test_stepper_not_flagged(self):
        r = next(x for x in core.component_records()
                 if x["name"] == "stepper_28byj48")
        self.assertFalse(r["us_timing"],
                         "stepper 毫秒粒度就够，不该被标成需微秒时序")

    def test_dwt_drivers_flagged(self):
        for name in ("dht11", "ds18b20", "hcsr04"):
            with self.subTest(comp=name):
                r = next(x for x in core.component_records()
                         if x["name"] == name)
                self.assertTrue(r["us_timing"])
                self.assertIn("DWT", core._port_source(name))


class TestClauseSplitting(unittest.TestCase):
    """分句：括号内的逗号不许切（切了就是句残话）。"""

    def test_comma_inside_parens_not_split(self):
        got = core._split_clauses("port 层提供 I2C 读写两函数（7 位地址 0x68，AD0 接地）")
        self.assertEqual(len(got), 1)
        self.assertIn("0x68，AD0 接地", got[0])

    def test_top_level_comma_split(self):
        got = core._split_clauses("在 x_port.h 按注释填 5 个接口，DWT 微秒延时是关键")
        self.assertEqual(len(got), 2)

    def test_init_requirement_keeps_full_paren_clause(self):
        """mpu6050 的移植要求必须整句出现，不能腰斩成「7 位地址 0x68」。"""
        r = next(x for x in core.peripheral_records() if x["name"] == "mpu6050")
        joined = " ".join(r["init_requires"])
        self.assertIn("0x68", joined)
        self.assertNotIn("7 位地址 0x68 ", joined + " ")


class TestPeripheralRecords(unittest.TestCase):
    """外设用法：符号真实、要求非虚构、纯解析库不硬凑。"""

    def test_only_drivers_with_port_layer(self):
        """esp8266at 是纯解析库（无 *_port.h），不该凭空出一条外设记录。"""
        names = {r["name"] for r in core.peripheral_records()}
        ddirs = {n for n in _driver_names()
                 if any(f.endswith("_port.h")
                        for f in os.listdir(core._driver_dir(n)))}
        self.assertEqual(names, ddirs)

    def test_init_requirement_quotes_real_source(self):
        """每条初始化要求都必须能在 meta.usage 或 *_port.h 原文里找到（防凭空生成）。"""
        for r in core.peripheral_records():
            with self.subTest(comp=r["name"]):
                hay = core._port_source(r["name"]) + \
                    core._driver_meta(r["name"]).get("usage", "")
                for q in r["init_requires"]:
                    self.assertTrue(q, "空的初始化要求")
                    head = q.split("（")[0].split("(")[0].strip()
                    self.assertIn(head[:12], hay,
                                  "要求原句在源文件里找不到：%s" % q)


class TestFriendlyErrors(unittest.TestCase):
    """拒绝路径必须说人话，不能静默返回空字符串。"""

    def test_empty_name_lists_available(self):
        for out in (core.component_get(""), core.peripheral_api_get(""),
                    core.component_get("  "), core.peripheral_api_get("  ")):
            with self.subTest(out=out[:12]):
                self.assertIn("没给", out)
                self.assertIn("mpu6050", out)

    def test_unknown_name_lists_available(self):
        for out in (core.component_get("__nope__"),
                    core.peripheral_api_get("__nope__")):
            with self.subTest(out=out[:12]):
                self.assertIn("没有名为", out)
                self.assertIn("mpu6050", out)

    def test_unknown_bus_says_which_buses_exist(self):
        out = core.component_brief(bus="spi")
        self.assertIn("没有", out)
        self.assertIn("i2c", out)          # 告诉调用方实际有哪些总线
        self.assertNotEqual(out.strip(), "")

    def test_bus_filter_works(self):
        recs = core.component_list(bus="i2c")
        self.assertTrue(recs)
        self.assertTrue(all(r["bus"] == "i2c" for r in recs))


class TestTextViews(unittest.TestCase):
    """文本视图（MCP 工具直接返回的东西）不许退化成空或报错。"""

    def test_component_brief_lists_all(self):
        out = core.component_brief()
        for n in _driver_names():
            self.assertIn(n, out)

    def test_component_get_embeds_wiring(self):
        out = core.component_get("servo_sg90")
        self.assertIn("接线与避坑", out)
        self.assertIn("来源：", out)

    def test_peripheral_api_brief_non_empty(self):
        out = core.peripheral_api_brief()
        self.assertIn("外设", out)
        for n in _driver_names():
            self.assertIn(n, out)

    def test_component_get_empty_when_no_drivers(self):
        """驱动库空时不许崩，也不许返回半截内容。"""
        orig = core._driver_dirs
        core._driver_dirs = lambda *a, **k: []
        try:
            self.assertIn("空", core.component_get("mpu6050"))
            self.assertIn("空", core.peripheral_api_get("mpu6050"))
            self.assertIn("空", core.component_brief())
            self.assertIn("空", core.peripheral_api_brief())
        finally:
            core._driver_dirs = orig


if __name__ == "__main__":
    unittest.main()
