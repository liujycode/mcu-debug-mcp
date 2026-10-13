# -*- coding: utf-8 -*-
"""工程结构 / 生态识别 / 工具链探测 / 探针名单 的回归（N-09 · N-10 · G02）。

这三项都是「先认清环境再动手」的前置能力，做错了后面全歪：

    · 认错生态   → 按错的构建方式去排查（拿着 Keil 的 L6218E 去查 PlatformIO）；
    · 探不到探针 → 给的烧录命令根本用不上；
    · 工具链探测漏项或瞎猜 → 用户在错误的路上装半天。

所以每条都按「正例给真信息 + 反例明说而非含糊」两档钉住。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core  # noqa: E402


class TestEcosystemKnowledge(unittest.TestCase):
    """N-09：生态识别知识库。"""

    def setUp(self):
        core._ECOSYSTEM_CACHE.clear()          # 免疫别的测试留下的缓存

    def test_all_six_ecosystems_present(self):
        """六种目标生态一个都不能少（这就是 N-09 的判据）。"""
        self.assertEqual(
            {"zephyr", "rtthread", "platformio", "keil", "iar", "rust_defmt"},
            set(core._ecosystem_entries()))

    def test_every_entry_has_source(self):
        """条目级 source 铁律：每条都要能说出处，不许凭记忆灌。"""
        for k, e in core._ecosystem_entries().items():
            self.assertTrue(str(e.get("source", "")).strip(), "%s 缺 source" % k)
            self.assertTrue(str(e.get("name", "")).strip(), "%s 缺 name" % k)
            self.assertTrue(e.get("project_markers"), "%s 缺识别特征" % k)

    def test_get_returns_real_content(self):
        out = core.ecosystem_get("keil")
        self.assertIn("Keil", out)
        self.assertIn("工程识别特征", out)
        self.assertIn("来源", out)
        self.assertNotIn("没有收录", out)

    def test_list_shows_all_with_first_marker(self):
        """清单页：每种生态都要出现，且各给一条识别特征（不然等于只给了名字）。"""
        out = core.ecosystem_list()
        for k in ("zephyr", "rtthread", "platformio", "keil", "iar", "rust_defmt"):
            self.assertIn(k, out, "ecosystem_list 漏了 %s" % k)
        self.assertIn("识别特征", out)
        self.assertIn("ecosystem_get", out)      # 告诉用户下一步怎么查详情

    def test_get_fuzzy_name(self):
        """显示名模糊匹配：'keil mdk' 要能命中 keil。"""
        self.assertIn("Keil", core.ecosystem_get("keil mdk"))

    def test_unknown_ecosystem_never_guesses(self):
        """反例：没有的生态要明说「没有收录」并列出可选项。"""
        out = core.ecosystem_get("不存在的生态")
        self.assertIn("没有收录", out)
        self.assertIn("zephyr", out)


class TestProjectDetect(unittest.TestCase):
    """G02 前半：工程结构识别。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcu_dbg_proj_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, *names):
        for n in names:
            with open(os.path.join(self.tmp, n), "w", encoding="utf-8") as f:
                f.write("x")
        return self.tmp

    def test_detect_platformio(self):
        self.assertIn("platformio", core.project_detect(self._mk("platformio.ini")))

    def test_detect_keil_by_extension(self):
        """后缀匹配：Keil 工程名随人取，靠 *.uvprojx 认，不能写死文件名。"""
        self.assertIn("keil", core.project_detect(self._mk("MyProject.uvprojx")))

    def test_detect_iar(self):
        self.assertIn("iar", core.project_detect(self._mk("app.ewp")))

    def test_detect_zephyr(self):
        self.assertIn("zephyr", core.project_detect(self._mk("prj.conf")))

    def test_detect_rtthread_by_sconstruct(self):
        """SConstruct 与 sconstruct 大小写都要认。"""
        self.assertIn("rtthread", core.project_detect(self._mk("SConstruct")))

    def test_detect_rust_by_cargo(self):
        self.assertIn("rust_defmt", core.project_detect(self._mk("Cargo.toml")))

    def test_empty_dir_says_no_match_not_guesses(self):
        """反例：认不出来就明说，并给该看什么——绝不硬套一个框架。"""
        out = core.project_detect(self.tmp)
        self.assertIn("没有命中", out)
        self.assertIn("CMakeLists.txt", out)

    def test_missing_path_is_friendly(self):
        out = core.project_detect(os.path.join(self.tmp, "no_such_dir"))
        self.assertIn("不是目录", out)

    def test_only_one_level_no_recursion(self):
        """只探一层：子目录里的标志文件不算——否则 vendor/ 会被误判成工程。"""
        sub = os.path.join(self.tmp, "vendor")
        os.makedirs(sub)
        with open(os.path.join(sub, "platformio.ini"), "w", encoding="utf-8") as f:
            f.write("x")
        self.assertIn("没有命中", core.project_detect(self.tmp))


class TestProbeAndToolchain(unittest.TestCase):
    """N-10 探针名单 + G02 后半：工具链探测。"""

    def test_flash_tool_list_covers_major_vendors(self):
        """N-10 判据：≥10 条，且主流多探针一个不缺。"""
        self.assertGreaterEqual(len(core._FLASH_TOOLS), 10, "探针名单又缩回不足 10 条")
        names = set(core._FLASH_TOOLS)
        for need in ("STM32_Programmer_CLI", "pyocd", "openocd", "esptool",
                     "JLinkExe", "nrfjprog", "dfu-util", "avrdude", "UniFlash"):
            self.assertIn(need, names, "探针名单缺 %s" % need)

    def test_toolchain_status_runs_and_is_honest(self):
        """工具链探测：跑得起来，且找不到就明说，不编。"""
        out = core.toolchain_status()
        self.assertIn("本机嵌入式工具链探测", out)
        self.assertIn("已发现", out)
        self.assertIn("未发现", out)

    def test_toolchain_missing_is_not_declared_a_failure(self):
        """「未发现」不等于环境坏了——必须说清 IDE 自带工具不一定暴露到命令行。"""
        self.assertIn("IDE 自带工具", core.toolchain_status())


if __name__ == "__main__":
    unittest.main(verbosity=2)
