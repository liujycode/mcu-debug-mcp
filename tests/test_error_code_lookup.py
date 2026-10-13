#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B② 错误码查询（error_code_lookup）双向回归。

重点：
- 正向——已收录的码必须查得到，且带出处；
- 反向——**未收录必须明说查不到**，不准返回任何释义（防"看似合理的假答案"）；
- 防死数据——新加的知识库表如果没被这个入口扫到，就等于白加，这里钉死。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402


class TestErrorCodeLookup(unittest.TestCase):

    # ---- 列表模式 ----
    def test_list_all_tables(self):
        out = core.error_code_lookup()
        for t in ("esp_idf", "at_common", "cwjap", "cme", "modbus_exception", "mqtt_rc"):
            self.assertIn(t, out, "知识库表 %s 没被列出（可能没扫到）" % t)

    def test_new_tables_are_not_dead_data(self):
        """B② 新增的两张表必须真的能被查到，否则就是装饰性产出。"""
        self.assertIn("ESP_ERR_NOT_FOUND", core.error_code_lookup("esp_idf", "0x105"))
        self.assertIn("发送失败", core.error_code_lookup("at_common", "SEND FAIL"))

    def test_list_single_table(self):
        out = core.error_code_lookup("at_common")
        self.assertIn("SEND FAIL", out)
        self.assertIn("11 条", out)

    # ---- 正向命中 ----
    def test_exact_hit_with_provenance(self):
        out = core.error_code_lookup("esp_idf", "0x105")
        self.assertIn("ESP_ERR_NOT_FOUND", out)
        self.assertIn("出处", out, "命中时必须给出处（护城河）")

    def test_numeric_without_0x_prefix(self):
        """ESP-IDF 惯例是写 0x，但裸写 105 也该能命中同一条，并回显规范写法。"""
        out = core.error_code_lookup("esp_idf", "105")
        self.assertIn("0x105", out)
        self.assertIn("ESP_ERR_NOT_FOUND", out)

    def test_case_insensitive(self):
        self.assertIn("发送失败", core.error_code_lookup("at_common", "send fail"))

    def test_negative_code(self):
        self.assertIn("踢", core.error_code_lookup("mqtt_rc", "-5"))

    def test_old_tables_still_work(self):
        self.assertIn("密码错误", core.error_code_lookup("cwjap", "2"))
        self.assertIn("SIM 未插入", core.error_code_lookup("cme", "10"))
        self.assertIn("非法数据值", core.error_code_lookup("modbus_exception", "3"))

    # ---- 反向：必须拒答 ----
    def test_unknown_code_refuses_and_lists_known(self):
        out = core.error_code_lookup("esp_idf", "0x999")
        self.assertIn("未收录", out)
        self.assertIn("0x101", out, "未收录时应列出该表已收录的码，方便用户自查")

    def test_unknown_code_returns_no_meaning(self):
        """★ 核心防线：未收录时绝不能带出任何别的码的释义。"""
        out = core.error_code_lookup("esp_idf", "0x999")
        for leak in ("ESP_ERR_NO_MEM", "内存不足", "排查："):
            self.assertNotIn(leak, out, "未收录却泄漏了其它码的释义：%s" % out)

    def test_unknown_table_lists_available(self):
        out = core.error_code_lookup("nope", "1")
        self.assertIn("没有名为", out)
        self.assertIn("esp_idf", out)

    def test_table_prefix_fuzzy_unique(self):
        """表名前缀唯一时应容错命中；不做模糊猜测。"""
        out = core.error_code_lookup("esp", "0x101")
        self.assertIn("ESP_ERR_NO_MEM", out)

    def test_ambiguous_table_prefix_refuses(self):
        """前缀命中多张表时（c 同时匹配 cme/cwjap？这里用 c 验证），必须拒答不猜。"""
        out = core.error_code_lookup("c", "1")
        # c 匹配 cme/cwjap 两张 → 应报「没有名为」而不是任选一张
        self.assertIn("没有名为", out)

    # ---- 知识库健全性 ----
    def test_every_entry_has_meaning(self):
        """所有表所有条目都要有 meaning，否则查出来是空的。"""
        for t in core._errcode_tables():
            with open(core._errcode_tables()[t], encoding="utf-8") as f:
                import json
                d = json.load(f)
            for k, v in (d.get("entries") or {}).items():
                self.assertTrue(v.get("meaning"), "%s 的码 %s 缺 meaning" % (t, k))


if __name__ == "__main__":
    unittest.main()
