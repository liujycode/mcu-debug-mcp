# -*- coding: utf-8 -*-
"""能力面审计的回归（N-05）。

盯什么
------
`tests/audit_capability.py` 是「缺口自动浮出」的机制本体。它自己坏了有两种死法：

    1. **静默失效**：文档数字与实现早就脱节了，它却说「无脱节」——比没有审计更糟，
       因为大家会以为有人在看着。
    2. **清单腐化**：coverage.json 里的轴被删光、probe 写成非法 kind，
       于是「达标」变得毫无意义。

所以本文件既检查审计的**结论**（0 脱节），也用**注入法**检查审计的**能力**
（故意把文档数字改错，它必须报出来）——这是本项目防自证测试的一贯做法。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
for p in (ROOT, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import audit_capability as ac  # noqa: E402

VALID_PROBE_KINDS = {
    "core_len", "core_has", "layer_dirs", "layer_files", "errcode_entries",
    "rules", "attr_len", "decorators", "file_exists", "axes_met",
    "tools_with_assertions", "guard_ops", "manual",
}


class TestCoverageManifest(unittest.TestCase):
    """coverage.json 自身的合法性（清单腐化防线）。"""

    @classmethod
    def setUpClass(cls):
        with open(ac.COVERAGE_PATH, encoding="utf-8") as fh:
            cls.cov = json.load(fh)

    def test_has_axes_and_gaps(self):
        self.assertTrue(self.cov["axes"], "能力轴清单为空——审计就没有对象了")
        self.assertTrue(self.cov["gaps"], "缺口清单为空")

    def test_probe_kinds_are_known(self):
        """probe 的 kind 必须是自己实现过的，否则会静默算成 0 ⟹ 假缺口/假达标。"""
        bad = []
        for item in self.cov["axes"] + self.cov["gaps"]:
            kind = item["probe"].get("kind")
            if kind not in VALID_PROBE_KINDS:
                bad.append("%s: %s" % (item["id"], kind))
        self.assertEqual(bad, [], "未知的 probe kind：%s" % bad)

    def test_manual_probes_explain_themselves(self):
        """manual 是最后手段：必须写明「为什么不能自动数」，否则就会变成拍脑袋数字。"""
        bad = [i["id"] for i in self.cov["axes"] + self.cov["gaps"]
               if i["probe"].get("kind") == "manual" and not i["probe"].get("reason")]
        self.assertEqual(bad, [], "这些 manual 探针没写 reason：%s" % bad)

    def test_every_gap_has_owner_batch(self):
        bad = [g["id"] for g in self.cov["gaps"] if "batch" not in g]
        self.assertEqual(bad, [], "缺口没写归到哪个批次：%s" % bad)

    def test_no_duplicate_ids(self):
        ids = [i["id"] for i in self.cov["axes"] + self.cov["gaps"]]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dup, [], "清单里有重复 id：%s" % dup)


class TestAuditResult(unittest.TestCase):
    """审计结论：不许有脱节、不许有死代码。"""

    @classmethod
    def setUpClass(cls):
        cls.rep = ac.run_audit()

    def test_no_mismatches(self):
        """文档数字必须与实测一致；不许有死代码。

        失败时把每条脱节打出来——只看一句 assert 失败没法定位。
        """
        msgs = ["%s：%s" % (m["label"], m["detail"]) for m in self.rep["mismatches"]]
        self.assertEqual(msgs, [], "审计发现脱节/死代码：\n  " + "\n  ".join(msgs))

    def test_gap_count_matches_plan(self):
        """缺口总数必须与 PLAN.md §3 的 26 项一致（防止清单被悄悄删项）。"""
        self.assertEqual(self.rep["summary"]["gaps_total"], 26,
                         "缺口数与 PLAN.md 不一致——要么漏记，要么漏删")

    def test_tools_all_registered_with_depth_row(self):
        """每个注册的工具都必须出现在深度矩阵里（矩阵漏工具=有人没被盯上）。"""
        rows = {r["tool"] for r in self.rep["depth"]["rows"]}
        missing = sorted(set(self.rep["tools"]) - rows)
        self.assertEqual(missing, [], "这些工具没进深度矩阵：%s" % missing)

    def test_mcp_primitive_counts(self):
        """三原语计数：tools/resources 有基线，prompts 目前是计划内缺口（0）。"""
        dec = self.rep["decorators"]
        self.assertGreaterEqual(dec["tool"], 33, "工具数掉了？")
        self.assertGreaterEqual(dec["resource"], 11, "资源数掉了？")


class TestAuditActuallyAudits(unittest.TestCase):
    """注入法：证明审计**真的在对账**，而不是恒返回「无脱节」。"""

    def test_audit_catches_doc_drift(self):
        """把文档数字改成必然对不上的值，审计必须报出来。"""
        with open(ac.COVERAGE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        touched = False
        for d in data["docNumbers"]:
            if d["metric"] == "mcp_tools":
                d["regex"] = d["regex"].replace("(\\d+)", "(999)")
                touched = True
        self.assertTrue(touched, "coverage.json 里居然没有对账 mcp_tools 的条目")

        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="mcu_dbg_cov_")
        os.close(fd)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        old = ac.COVERAGE_PATH
        ac.COVERAGE_PATH = tmp
        try:
            rep = ac.run_audit()
        finally:
            ac.COVERAGE_PATH = old
            os.remove(tmp)
        self.assertTrue(
            any("999" in m["detail"] for m in rep["mismatches"]),
            "把文档数字改错了，审计却说没脱节——它没在对账（自证式假测试）",
        )

    def test_audit_catches_dead_code(self):
        """在临时仓库里放一个零引用函数，审计必须把它挑出来。

        用临时 ROOT 而不是往真实 core.py 注代码——不污染仓库，也不会因为
        「顺手改一下」把死代码审计变成写代码的动作。
        """
        tmp = tempfile.mkdtemp(prefix="mcu_dbg_dead_")
        try:
            # 审计的包内源码目录是 ROOT/src/mcu_debug_mcp（_pkg_dir 动态派生），
            # 假仓库必须镜像同一结构，注入才生效
            pkg = os.path.join(tmp, "src", "mcu_debug_mcp")
            os.makedirs(pkg)
            with open(os.path.join(pkg, "core.py"), "w", encoding="utf-8") as fh:
                fh.write("def used_fn():\n    return 1\n\n\n"
                         "def orphan_fn_xyz():\n    return 2\n\n\n"
                         "VALUE = used_fn()\n")
            for name in ("analyzer.py", "paths.py", "server.py"):
                open(os.path.join(pkg, name), "w", encoding="utf-8").close()
            os.makedirs(os.path.join(tmp, "tests"))
            old_root = ac.ROOT
            ac.ROOT = tmp
            try:
                names = [d["name"] for d in ac.dead_code()]
            finally:
                ac.ROOT = old_root
            self.assertIn("orphan_fn_xyz", names, "零引用函数没被挑出来——死代码审计形同虚设")
            self.assertNotIn("used_fn", names, "被引用过的函数被误判成死代码")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestIsolationAudit(unittest.TestCase):
    """【六】隔离与安全 这一节的结论与能力。

    隔离审计如果自己失效（恒返回"全部通过"），后果比没有审计更严重：
    大家会以为"隔离层有人看着"，而新写的写操作早就绕过去、直接动用户的设备了。
    所以这里也用注入法：在临时仓库里造出各种"隔离失效"的情形，它必须报。
    """

    @classmethod
    def setUpClass(cls):
        cls.checks, cls.failures = ac.isolation_audit()

    def test_current_repo_is_clean(self):
        msgs = ["%s：%s" % (f["label"], f["detail"]) for f in self.failures]
        self.assertEqual(msgs, [], "隔离审计报出问题：\n  " + "\n  ".join(msgs))

    def test_checks_are_meaningful_in_number(self):
        """条目太少说明有人在删检查项——隔离防线被悄悄削弱。"""
        self.assertGreaterEqual(len(self.checks), 8,
                                "隔离审计只剩 %d 项检查" % len(self.checks))

    # ---- 注入法：造出隔离失效的源码，审计必须报出来 ----

    def _fake_root(self, core_src, conftest_src="MCU_DEBUG_MCP_MODE\n"
                                                "MCU_DEBUG_MCP_PORT_DENY\n"
                                                "serial.Serial =",
                   make_isolation=True):
        tmp = tempfile.mkdtemp(prefix="mcu_dbg_iso_")
        pkg = os.path.join(tmp, "src", "mcu_debug_mcp")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "core.py"), "w", encoding="utf-8") as fh:
            fh.write(core_src)
        for name in ("analyzer.py", "paths.py", "server.py"):
            open(os.path.join(pkg, name), "w", encoding="utf-8").close()
        tdir = os.path.join(tmp, "tests")
        os.makedirs(tdir)
        if conftest_src is not None:
            with open(os.path.join(tdir, "conftest.py"), "w", encoding="utf-8") as fh:
                fh.write(conftest_src)
        if make_isolation:
            open(os.path.join(tdir, "isolation.py"), "w", encoding="utf-8").close()
        return tmp

    def _audit_on(self, tmp):
        old = ac.ROOT
        ac.ROOT = tmp
        try:
            return ac.isolation_audit()
        finally:
            ac.ROOT = old

    def _labels(self, failures):
        return " | ".join(f["detail"] for f in failures)

    def test_catches_serial_ctor_outside_entry(self):
        """在 _open_serial 之外直接构造串口 ⟹ 绕过唯一入口，审计必须报。"""
        tmp = self._fake_root(
            "def somedbg():\n"
            "    s = serial.Serial()\n"
            "    return s\n")
        try:
            _checks, failures = self._audit_on(tmp)
            self.assertTrue(any("绕过" in f["detail"] for f in failures),
                            "在别处直接 serial.Serial() 却没被报出来：%s" % self._labels(failures))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_catches_unwired_guard_op(self):
        """guard.OPS 登记了操作，但 core 里一个闸门都没接 ⟹ 审计必须报。"""
        tmp = self._fake_root("def lonely():\n    return 1\n\n\nVALUE = lonely()\n")
        try:
            _checks, failures = self._audit_on(tmp)
            self.assertTrue(any("找不到调用点" in f["detail"] for f in failures),
                            "core 没接任何闸门却没被报出来：%s" % self._labels(failures))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_catches_unregistered_check_call(self):
        """core 里调用了没登记的 op ⟹ guard 会保守拒绝，功能是坏的，必须报。"""
        tmp = self._fake_root(
            "def f():\n"
            "    return guard.check(\"totally_new_op\", \"COM1\")\n"
            "\n\nVALUE = f\n")
        try:
            _checks, failures = self._audit_on(tmp)
            self.assertTrue(any("未登记" in f["detail"] for f in failures),
                            "调用了未登记的操作却没被报出来：%s" % self._labels(failures))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_catches_missing_conftest(self):
        """测试隔离基础设施被删 ⟹ 审计必须报（否则测试已经在碰真机了还全绿）。"""
        tmp = self._fake_root("def f():\n    return 1\n\n\nVALUE = f()\n",
                              conftest_src=None)
        try:
            _checks, failures = self._audit_on(tmp)
            self.assertTrue(any("conftest" in f["detail"] for f in failures),
                            "缺 conftest.py 却没被报出来：%s" % self._labels(failures))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDepthMatrixHeuristic(unittest.TestCase):
    """深度阶梯的启发式必须**不因同名测试而漏判**。

    ★ 回归（真机验收那批工具顺手暴露的）：`_split_test_funcs` 原来只用**函数名**当
      字典键，同名测试互相覆盖。`tests/test_probe_direct.py` 里
      ``FlashVerifyTest.test_refuses_bad_inputs``（含 L3 要的 ``assertIn("只支持", …)``）
      被 ``TimingAnalysisTest.test_refuses_bad_inputs``（不含）覆盖 ⟹
      **flash_verify 的 L3 被算成 False**。

      这是质量门自己的**假阴性**，比假阳性危险：明明有判别力断言，审计却说没有，
      于是"缺口还没补"的错觉一直挂着，人还会去补一遍已经有的东西。
    """

    _SRC = (
        "class ATest(unittest.TestCase):\n"
        "    def test_same_name(self):\n"
        '        self.assertIn("只支持", core.some_tool("x"))\n'
        "\n"
        "class BTest(unittest.TestCase):\n"
        "    def test_same_name(self):\n"
        "        self.assertTrue(core.other_tool())\n"
    )

    def test_same_named_tests_in_different_classes_do_not_shadow(self):
        funcs = ac._split_test_funcs(self._SRC)
        self.assertEqual(len(funcs), 2, "同名测试被合并成一个了：%r" % list(funcs))
        self.assertIn("ATest::test_same_name", funcs)
        self.assertIn("BTest::test_same_name", funcs)

    def test_enclosing_class_is_found(self):
        pos = self._SRC.index("def test_same_name")
        self.assertEqual(ac._enclosing_class(self._SRC, pos), "ATest")
        pos2 = self._SRC.index("def test_same_name", pos + 1)
        self.assertEqual(ac._enclosing_class(self._SRC, pos2), "BTest")

    def test_l3_survives_shadowing(self):
        """端到端：some_tool 的 L3 必须为 True —— 它的拒绝断言就在第一个同名测试里。"""
        rows = ac.depth_matrix(["some_tool", "other_tool"], {"t.py": self._SRC}, [])
        by = {r["tool"]: r for r in rows}
        self.assertTrue(by["some_tool"]["L3"],
                        "同名的第二个测试把第一个覆盖了 ⟹ L3 假阴性，缺口会永远补不完")
        self.assertTrue(by["other_tool"]["L1"])
        self.assertFalse(by["other_tool"]["L3"])


if __name__ == "__main__":
    unittest.main()
