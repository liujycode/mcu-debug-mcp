# -*- coding: utf-8 -*-
"""MCP 资源层回归：知识库必须「浏览得到、读得对、错得明白」。

为什么单开一个文件盯资源：资源层是 AI 主动引用知识的入口，它坏了不会有人报错——
AI 只是"少知道一点"，然后凭记忆答。这种静默退化必须靠清单断言兜住。

本文件回答三个问题：
1. 清单是否与预期完全一致（多一个少一个都红）——防止资源被删/改名而无人知；
2. 每个资源读得到内容，且内容是真的（模板绑错函数会读成「没有收录…」）；
3. 明细资源给的就是知识库原文（逐字一致）——防止哪天有人"顺手"用 json.dump
   重排一遍，把「一行一条」的手写排版洗成标准缩进（历史上真踩过，见 UAMS B①）。

需要 mcp 依赖；未安装时整体 skip，不让核心测试陪跑失败。
"""

import asyncio
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcu_debug_mcp import core  # noqa: E402

server = None
try:
    from mcu_debug_mcp import server as _server  # noqa: E402
    server = _server
except Exception:                                    # pragma: no cover
    server = None

KB = os.path.join(ROOT, "src", "mcu_debug_mcp", "knowledge")

# 资源清单的**唯一权威口径**：改资源必须同时改这里，否则测试红。
EXPECTED_STATIC = {
    "mcu://drivers",       # 驱动清单
    "mcu://pinmaps",       # 引脚库 MCU 清单
    "mcu://boards",        # 板级库清单
    "mcu://errcodes",      # 错误码表清单（本轮补）
    "mcu://faultbits",     # 故障位域库清单（本轮补）
    "mcu://ecosystems",    # 生态/框架清单（N-09 补）
}
EXPECTED_TEMPLATES = {
    "mcu://driver/{name}",
    "mcu://wiring/{name}",
    "mcu://pinmap/{mcu}",
    "mcu://board/{name}",
    "mcu://errcode/{table}",      # 本轮补
    "mcu://faultbits/{arch}",     # 本轮补
    "mcu://ecosystem/{name}",     # N-09 补
}


def _read(uri):
    """同步读一个资源（MCP 的 read_resource 是 async）。"""
    res = asyncio.run(server.mcp.read_resource(uri))
    item = res[0]
    return item.content


@unittest.skipIf(server is None, "未安装 mcp 依赖，跳过资源层测试")
class TestResourceRegistry(unittest.TestCase):
    """资源清单：数量、URI 全名、静态/模板的分工。"""

    def test_static_set_exactly_matches(self):
        got = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
        self.assertEqual(EXPECTED_STATIC, got,
                         "静态资源清单变了：要么补文档，要么这里同步")

    def test_template_set_exactly_matches(self):
        got = {t.uriTemplate for t in asyncio.run(server.mcp.list_resource_templates())}
        self.assertEqual(EXPECTED_TEMPLATES, got,
                         "模板资源清单变了：要么补文档，要么这里同步")

    def test_static_resource_wins_over_template_prefix(self):
        """mcu://faultbits 与 mcu://faultbits/{arch} 同前缀，读到的必须是清单。

        这是容易出岔子的一处：若静态资源被模板抢走，arch="" 会拿到
        「没有收录架构 ''」——用户想看的清单反而读不到。
        """
        out = _read("mcu://faultbits")
        self.assertIn("已收录故障位域表", out)
        self.assertNotIn("没有收录", out)

    def test_every_knowledge_dir_is_covered(self):
        """knowledge/ 下每个知识目录都要有对应资源——护城河不能有浏览盲区。

        ★ 注意 ecosystem.json 是**单文件知识**（直接在 knowledge/ 下，不占子目录），
          所以它不在下面的 dirs 集合里，但同样要有「清单页 + 明细页」。
        """
        dirs = {d for d in os.listdir(KB)
                if os.path.isdir(os.path.join(KB, d))}
        self.assertEqual({"pinmap", "boards", "error_codes", "fault_bits"}, dirs,
                         "knowledge/ 目录清单变了，资源覆盖率要重新确认")
        # 每类知识都必须有一张「清单页」+ 一个「明细页」，缺一不算覆盖
        self.assertIn("mcu://pinmaps", EXPECTED_STATIC)
        self.assertIn("mcu://pinmap/{mcu}", EXPECTED_TEMPLATES)
        self.assertIn("mcu://boards", EXPECTED_STATIC)
        self.assertIn("mcu://board/{name}", EXPECTED_TEMPLATES)
        self.assertIn("mcu://errcodes", EXPECTED_STATIC)
        self.assertIn("mcu://errcode/{table}", EXPECTED_TEMPLATES)
        self.assertIn("mcu://faultbits", EXPECTED_STATIC)
        self.assertIn("mcu://faultbits/{arch}", EXPECTED_TEMPLATES)
        # 单文件知识（knowledge/ecosystem.json）同样要求清单页 + 明细页
        self.assertTrue(os.path.isfile(os.path.join(KB, "ecosystem.json")),
                        "knowledge/ecosystem.json 不见了（N-09 生态识别库）")
        self.assertIn("mcu://ecosystems", EXPECTED_STATIC)
        self.assertIn("mcu://ecosystem/{name}", EXPECTED_TEMPLATES)


@unittest.skipIf(server is None, "未安装 mcp 依赖，跳过资源层测试")
class TestResourceReadable(unittest.TestCase):
    """每个资源都要读得到真内容（模板绑错函数会读成「没有…」）。"""

    def _assert_real(self, uri, out):
        self.assertTrue(out and out.strip(), "%s 读出空内容" % uri)
        for bad in ("没有名为", "没有收录", "读取失败", "Traceback"):
            self.assertNotIn(bad, out, "%s 读成了错误分支：%s" % (uri, bad))

    def test_static_all_readable(self):
        for uri in sorted(EXPECTED_STATIC):
            self._assert_real(uri, _read(uri))

    def test_template_all_readable_with_real_names(self):
        drv = sorted(n for n, p in core.paths.layer_dirs("drivers")
                     if os.path.isfile(os.path.join(p, "meta.json")))
        self.assertTrue(drv, "驱动库为空，异常")
        cases = []
        for d in drv:                       # 9 款驱动逐个读，不是抽一个代表
            cases.append("mcu://driver/%s" % d)
            cases.append("mcu://wiring/%s" % d)
        cases += ["mcu://pinmap/%s" % m for m in core.pinmap_list()]
        cases += ["mcu://board/%s" % b for b in core.board_list()]
        cases += ["mcu://errcode/%s" % t for t in core._errcode_tables()]
        cases += ["mcu://faultbits/%s" % c["table"] for c in core.fault_bits_catalog()]
        cases += ["mcu://ecosystem/%s" % k for k in core._ecosystem_entries()]
        for uri in cases:
            self._assert_real(uri, _read(uri))

    def test_unknown_name_branches_are_friendly(self):
        """查不存在的名字：必须说清「没有」并列出可用的，不许抛异常。"""
        checks = [
            ("mcu://errcode/nope", "没有名为"),
            ("mcu://faultbits/riscv-nope", "没有收录"),
            ("mcu://driver/nope", "没有名为"),
            ("mcu://ecosystem/nope", "没有收录"),
        ]
        for uri, must in checks:
            out = _read(uri)
            self.assertIn(must, out, uri)
            self.assertTrue(len(out) > 20, "%s 的拒绝理由太短，用户没线索：%r" % (uri, out))


def _norm_nl(s):
    """统一换行后比较。

    ★ 边界说清楚：这些资源走的是 Python 文本模式读文件，CRLF 会被**归一成 LF**
      （universal newlines，Python 的既定行为，pinmap/board 等老资源也一样）。
      所以「逐字一致」的准确说法是「排版逐行一致、只差换行符」——
      本测试要防的是有人用 json.dump 顺手续写一遍，把「一行一条」的手写排版
      洗成标准缩进（UAMS B① 真踩过这个坑），不是防换行符。
    """
    return s.replace("\r\n", "\n").replace("\r", "\n")


@unittest.skipIf(server is None, "未安装 mcp 依赖，跳过资源层测试")
class TestDetailResourcesKeepOriginalLayout(unittest.TestCase):
    """明细资源 = 知识库原文（逐行排版一致、无重排、无美化）。"""

    def test_errcode_tables_layout_preserved_and_valid_json(self):
        for t in core._errcode_tables():
            path = os.path.join(KB, "error_codes", t + ".json")
            with io.open(path, "r", encoding="utf-8", newline="") as f:
                raw = f.read()
            got = _read("mcu://errcode/%s" % t)
            self.assertEqual(_norm_nl(raw), got,
                             "%s 资源内容与原文排版不一致（被重排了？）" % t)
            json.loads(got)          # 原文必须是合法 JSON

    def test_fault_bits_layout_preserved_and_valid_json(self):
        for c in core.fault_bits_catalog():
            path = os.path.join(KB, "fault_bits", c["table"] + ".json")
            with io.open(path, "r", encoding="utf-8", newline="") as f:
                raw = f.read()
            got = _read("mcu://faultbits/%s" % c["table"])
            self.assertEqual(_norm_nl(raw), got,
                             "%s 资源内容与原文排版不一致" % c["table"])
            d = json.loads(got)
            self.assertTrue(d.get("source"), "故障位域表顶层必须带 source")

    def test_fault_bits_accepts_both_filename_and_arch(self):
        """两种写法都要认：文件名 cortex_m、架构名 cortex-m。

        只认其中一种的话，用户/AI 照着清单填就会被自己的记性坑（本轮修的坑）。
        """
        cat = core.fault_bits_catalog()
        self.assertTrue(cat)
        for c in cat:
            self.assertEqual(_read("mcu://faultbits/%s" % c["table"]),
                             _read("mcu://faultbits/%s" % c["arch"]),
                             "%s：文件名与 arch 名读到的内容不一致" % c["table"])


class TestErrcodeTableResolution(unittest.TestCase):
    """表名解析必须只有一套口径（lookup 与 table_get 共用一个解析器）。"""

    def test_prefix_resolution_consistent(self):
        key, tables, err = core._resolve_errcode_table("esp")
        self.assertIsNone(err)
        self.assertEqual("esp_idf", key)
        # 同一个输入，两条路径必须落到同一张表
        self.assertIn("esp_err_t", core.errcode_table_get("esp"))

    def test_no_false_match_refused(self):
        _, _, err = core._resolve_errcode_table("nope")
        self.assertIn("没有名为", err)
        _, _, err2 = core._resolve_errcode_table("nope")
        self.assertEqual(err, err2)

    def test_empty_table_is_not_an_error(self):
        """空表名不是错误（调用方决定怎么说：lookup 列清单、table_get 给用法）。"""
        key, tables, err = core._resolve_errcode_table("")
        self.assertIsNone(key)
        self.assertIsNone(err)
        self.assertIn("esp_idf", tables)

    def test_unknown_table_never_guesses(self):
        """解析不出表时必须拒绝（并列出可用的），不许挑一张最像的糊弄。"""
        key, _, err = core._resolve_errcode_table("zzz")
        self.assertIsNone(key)
        self.assertIn("可用", err)
        self.assertIn("esp_idf", err)        # 拒绝时要把可选项给出来，用户才走得下去


class TestDeadCodeRemoved(unittest.TestCase):
    """deps_status 是死代码（全仓零调用），已删。

    留着它的坏处不是占几行，而是让人以为"环境状态有统一出口"——
    实际每个调用点都是各自读 HAS_PYSERIAL/HAS_PYOCD。
    """

    def test_deps_status_is_gone(self):
        self.assertFalse(hasattr(core, "deps_status"),
                         "deps_status 又被加回来了？它没有任何调用方")

    def test_dependency_flags_still_there(self):
        """真正在用的那两个标志不能被顺手删掉。"""
        self.assertIsInstance(core.HAS_PYSERIAL, bool)
        self.assertIsInstance(core.HAS_PYOCD, bool)

    def test_missing_pyserial_message_reachable(self):
        """降级提示还在（工具层依赖它给出安装指引）。"""
        self.assertIn("pip install pyserial", core._deps_msg())


if __name__ == "__main__":
    unittest.main()
