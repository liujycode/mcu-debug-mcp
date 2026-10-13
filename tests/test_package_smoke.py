# -*- coding: utf-8 -*-
"""打包与数据根回归（N-01）。

为什么必须有这个文件
--------------------
PLAN.md §2 记录的那处 P0 硬伤——「pip 装出来是空壳」——**单元测试永远抓不到**：
原地跑源码时 knowledge/ 与 drivers/ 就在旁边，一切正常；只有从**包装产物**出发才会暴露。
所以本文件分两层：

    快层 TestDataRootWiring
        静态守卫 + 外置数据根层叠语义。不装包，秒级，每次跑。
    慢层 TestPackageInstallSmoke
        真装到临时目录、真在子进程里跑知识类工具。*这才是有牙的那一层*。

两条硬约束（写测试时最容易犯、犯了就等于没测）：
    1. 冒烟必须在**子进程**里跑，且把仓库根从 sys.path 里剔除——
       否则 import 到的是原地源码，测了个寂寞；
    2. 断言不能是「没报错」，必须是「知识类工具返回了真实条目」
       （空列表 / 「知识库为空」也算失败——那正是缺陷的本来面目）。
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core      # noqa: E402
import paths     # noqa: E402

#: 环境变量开关：CI 或慢机器上可关掉装包冒烟（默认开——这类缺陷只有它抓得住）
SKIP_SMOKE = os.environ.get("MCU_DEBUG_MCP_SKIP_PACK_SMOKE") == "1"


class TestDataRootWiring(unittest.TestCase):
    """快层：数据根接线正确，且没有回退成「代码目录当数据目录」。"""

    def test_no_code_dir_as_data_dir(self):
        """core.py / analyzer.py 里不许再出现「代码目录当数据目录」的写法。

        这是本处 P0 的**根因写法**：`os.path.join(sys_dir, "knowledge"...)`。
        原地跑看不出问题，装成包就集体失效。静态拦在源头。
        """
        bad = []
        for fn in ("core.py", "analyzer.py"):
            src = open(os.path.join(ROOT, fn), encoding="utf-8").read()
            for i, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if ("sys_dir" in line or "_SYS_DIR" in line or "__file__" in line) \
                        and ("knowledge" in line or "drivers" in line):
                    bad.append("%s:%d  %s" % (fn, i, line.strip()))
        self.assertEqual(bad, [], "又把代码目录当数据目录了：\n" + "\n".join(bad))

    def test_py_modules_cover_all_root_modules(self):
        """pyproject 的 py-modules 必须覆盖仓库根的全部顶层 .py（漏一个就装不进去）。"""
        txt = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
        mods = set()
        for line in txt.splitlines():
            if line.strip().startswith("py-modules"):
                mods = {m.strip().strip('"\'') for m in line.split("=", 1)[1].strip(" []").split(",")}
        self.assertTrue(mods, "pyproject 里没找到 py-modules")
        on_disk = {f[:-3] for f in os.listdir(ROOT)
                   if f.endswith(".py") and f not in ("setup.py", "conftest.py")}
        missing = sorted(on_disk - mods)
        self.assertEqual(missing, [], "这些顶层模块没声明进 py-modules：%s" % missing)

    def test_setup_hook_covers_data_dirs(self):
        """setup.py 的 DATA_DIRS 必须覆盖全部需要随包分发的数据目录。"""
        src = open(os.path.join(ROOT, "setup.py"), encoding="utf-8").read()
        self.assertIn("build_py", src, "setup.py 没有 build_py 钩子——数据目录不会进包")
        for d in ("knowledge", "drivers"):
            self.assertIn('"%s"' % d, src.split("DATA_DIRS", 1)[1].split(")")[0],
                          "DATA_DIRS 缺少 %s" % d)

    def test_no_logs_dir_in_repo_root(self):
        """会话日志不许落在代码目录里（pip 装到只读 site-packages 会写不进去）。"""
        src = open(os.path.join(ROOT, "core.py"), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn('sys_dir, "logs"', code)

    def test_build_failure_is_not_swallowed(self):
        """慢层**测试方法**里不许出现 skipTest —— 那会把「构建失败」伪装成「环境不支持」。

        历史：这里曾经是 `if r.returncode != 0: self.skipTest(...)`，于是打包坏了
        测试照样绿 —— PLAN.md §2 那处 P0（装出来是空壳）正是这样活下来的。
        唯一允许 skip 的地方是 setUpClass 的「本机没有 setuptools」预检：
        那是真环境缺失，而构建失败不是。

        用 AST 而不是字符串匹配：字符串匹配会被本断言的注释自己命中（自指陷阱），
        而且它只看得到文本，看不出 skipTest 到底在哪个函数里。
        """
        tree = ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef)
                    and node.name == "TestPackageInstallSmoke"):
                continue
            for fn in node.body:
                if not isinstance(fn, ast.FunctionDef) or fn.name == "setUpClass":
                    continue
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "attr", "") == "skipTest":
                        offenders.append(fn.name)
        self.assertEqual(offenders, [],
                         "这些慢层测试用 skipTest 吞掉了失败：%s" % offenders)

    def test_override_layer_merges_and_overrides(self):
        """外置数据根：**合并**（内置表仍在）+ **覆盖**（同名以外部为准）。

        这是「知识库社区可贡献」能不能落地的判据。只做覆盖或只做合并都是错的：
        - 只覆盖 → 用户加一张自己的码表，内置 6 张全消失
        - 不覆盖 → 用户改不了内置的同名表
        """
        tmp = tempfile.mkdtemp(prefix="mcu_dbg_home_")
        old_env = os.environ.get(paths.ENV_HOME)
        try:
            os.makedirs(os.path.join(tmp, "knowledge", "error_codes"))
            os.makedirs(os.path.join(tmp, "knowledge", "boards"))
            os.makedirs(os.path.join(tmp, "drivers", "my_sensor"))
            # ① 新增一张自己的码表
            with open(os.path.join(tmp, "knowledge", "error_codes", "mine.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"entries": {"42": {"meaning": "自造码", "hint": "测试"}}}, fh)
            # ② 同名表整体覆盖内置 bluepill
            with open(os.path.join(tmp, "knowledge", "boards", "bluepill.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"board": "BluePill(改)", "mcu": "stm32f103c8t6",
                           "alias": ["bluepill"], "onboard": {},
                           "board_issues": ["外置层生效"], "source": "单测"}, fh)
            # ③ 新增一个自己的驱动
            with open(os.path.join(tmp, "drivers", "my_sensor", "meta.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"name": "my_sensor", "desc": "自造驱动"}, fh)

            os.environ[paths.ENV_HOME] = tmp
            core._clear_data_caches()
            try:
                tables = sorted(core._errcode_tables())
                self.assertIn("mine", tables, "外置新增码表没被合并进来")
                self.assertIn("at_common", tables, "外置层把内置表顶掉了（合并语义坏了）")

                boards = core.board_list()
                self.assertIn("bluepill", boards)
                self.assertEqual(len(boards), 3, "内置板级库条目数被外置层改变：%s" % boards)
                self.assertIn("外置层生效", core.board_get("bluepill"),
                              "同名表没有以外部为准（覆盖语义坏了）")

                drv = [n for n, _p in core.paths.layer_dirs("drivers")]
                self.assertIn("my_sensor", drv, "外置新增驱动没被合并进来")
                self.assertIn("dht11", drv, "外置层把内置驱动顶掉了")

                self.assertEqual(core.pinmap_list(),
                                 ['esp32dev', 'stm32f103c8t6', 'stm32f411ceu6'])
            finally:
                if old_env is None:
                    os.environ.pop(paths.ENV_HOME, None)
                else:
                    os.environ[paths.ENV_HOME] = old_env
                core._clear_data_caches()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_env_home_pointing_nowhere_is_ignored(self):
        """MCU_DEBUG_MCP_HOME 指向不存在的目录：忽略它，但仍能用内置数据（不静默为空）。"""
        old_env = os.environ.get(paths.ENV_HOME)
        try:
            os.environ[paths.ENV_HOME] = os.path.join(ROOT, "__no_such_home__")
            core._clear_data_caches()
            self.assertIn(paths.pkg_dir(), paths.data_roots())
            self.assertTrue(core.pinmap_list(), "数据根被一个不存在的路径搞空了")
        finally:
            if old_env is None:
                os.environ.pop(paths.ENV_HOME, None)
            else:
                os.environ[paths.ENV_HOME] = old_env
            core._clear_data_caches()


@unittest.skipIf(SKIP_SMOKE, "已用 MCU_DEBUG_MCP_SKIP_PACK_SMOKE=1 关闭装包冒烟")
class TestPackageInstallSmoke(unittest.TestCase):
    """慢层：真装真跑。**这条测试是那处 P0 缺陷死掉的原因。**

    ★ 2026-10-13 补：旧版只有 `pip install . --target` 一条安装源 —— 那是
      **从源码树直接建 wheel**，正好绕开 `python -m build` 的标准发布流程
      （sdist → 解包 → 再构 wheel）。于是 sdist 丢了 knowledge/drivers 时
      冒烟依旧 9 passed，空壳照样发出去（审查报告 P0-2，实测复现）。
      现在 setUpClass 真构建一次 sdist + wheel，两个测试分别从
      **tar.gz 与 whl** 安装跑同一套探针断言——两条分发路径都必须有牙。
    """

    @classmethod
    def setUpClass(cls):
        # 只有「真的没有 setuptools」才算环境缺失。构建失败**不是**环境问题，
        # 绝不能跳过——下面 returncode != 0 的分支会直接 fail。
        try:
            import setuptools  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("本环境没有 setuptools，无法构建（真实环境缺失）")
        try:
            import build  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("本环境没有 build 包，无法构建 sdist/wheel（真实环境缺失）")

        cls.dist = tempfile.mkdtemp(prefix="mcu_dbg_dist_")
        r = subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation",
             "--outdir", cls.dist, ROOT],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            # 构建失败就是失败。这里不是环境问题，是打包坏了。
            raise AssertionError(
                "python -m build 失败 —— 这不是环境问题，是打包坏了。\n"
                "原始输出（尾 1500 字）：\n%s" % ((r.stderr or "") + (r.stdout or ""))[-1500:])
        sdists = [f for f in os.listdir(cls.dist) if f.endswith(".tar.gz")]
        wheels = [f for f in os.listdir(cls.dist) if f.endswith(".whl")]
        if not sdists:
            raise AssertionError("build 没产出 sdist（.tar.gz）：%s" % os.listdir(cls.dist))
        if not wheels:
            raise AssertionError("build 没产出 wheel（.whl）：%s" % os.listdir(cls.dist))
        cls.sdist_path = os.path.join(cls.dist, sdists[0])
        cls.wheel_path = os.path.join(cls.dist, wheels[0])
        cls._keep = []

    @classmethod
    def tearDownClass(cls):
        for d in getattr(cls, "_keep", []):
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(getattr(cls, "dist", ""), ignore_errors=True)

    def _install_and_probe(self, artifact, tag):
        """从**一个构建产物**（sdist 或 wheel）装到临时目录，跑探针，返回数据。"""
        tgt = tempfile.mkdtemp(prefix="mcu_dbg_%s_" % tag)
        self._keep.append(tgt)
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", artifact, "--no-deps",
             "--no-build-isolation", "--target", tgt, "-q"],
            cwd=tgt, capture_output=True, text=True,
            # ★ 必须显式 utf-8 + errors="replace"：Windows 下 text=True 走 locale
            #   编码（GBK），而 pip 的输出里带中文（本包 description 就是中文）。
            #   一旦撞上非 GBK 字节，subprocess 的读线程会抛 UnicodeDecodeError，
            #   r.stderr 直接变 None，然后 r.stderr[-1200:] 抛 TypeError——
            #   报错信息指向的是测试自己，而不是真正的原因。
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            # ★这里曾经是 skipTest —— 于是「打包坏了」被伪装成「环境不支持」，
            #   测试永远绿，而缺陷照样发出去（PLAN.md §2 那处 P0 就是这样活下来的）。
            #   安装失败就是失败，没有中间地带。
            self.fail("pip 装 %s 失败 —— 这不是环境问题，是打包坏了。\n原始输出（尾 1200 字）：\n%s"
                      % (tag, (r.stderr or "")[-1200:]))

        # 数据文件必须真的在包里（数量级断言，不是「存在即可」）
        n_know = sum(len(f) for _d, _s, f in os.walk(os.path.join(tgt, "knowledge")))
        n_drv = sum(len(f) for _d, _s, f in os.walk(os.path.join(tgt, "drivers")))
        self.assertGreaterEqual(n_know, 10, "[%s] knowledge/ 没进包" % tag)
        self.assertGreaterEqual(n_drv, 40, "[%s] drivers/ 没进包" % tag)

        probe = os.path.join(tgt, "_smoke_probe.py")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(_PROBE_SRC.format(tgt=tgt, root=ROOT))
        r2 = subprocess.run([sys.executable, probe], cwd=tgt,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        self.assertEqual(r2.returncode, 0, "[%s] 装出来的包跑不起来：%s"
                         % (tag, (r2.stderr or "")[-500:]))
        return json.loads(r2.stdout.strip().splitlines()[-1])

    def _assert_probe(self, data, tag):
        self.assertTrue(data["core_from_target"],
                        "[%s] import 到的不是装出来的包（测试写错了，等于没测）：%s"
                        % (tag, data["core_file"]))
        self.assertGreaterEqual(data["pinmaps"], 3, "[%s] 装出来后引脚库为空" % tag)
        self.assertGreaterEqual(data["boards"], 3, "[%s] 装出来后板级库为空" % tag)
        self.assertGreaterEqual(data["drivers"], 9, "[%s] 装出来后驱动库为空" % tag)
        self.assertGreaterEqual(data["errcode_tables"], 6, "[%s] 装出来后错误码表为空" % tag)
        self.assertGreaterEqual(data["fault_bits"], 1, "[%s] 装出来后故障位域表为空" % tag)
        self.assertGreaterEqual(data["extra_rules"], 10, "[%s] 装出来后外置规则没加载" % tag)
        self.assertTrue(data["driver_get_real"], "[%s] driver_get 读不到真内容" % tag)
        self.assertTrue(data["errcode_real"], "[%s] error_code_lookup 说知识库为空" % tag)

        # 隔离层必须随包分发且默认锁着写 —— 否则 pip 装的版本是「裸奔」的，
        # 用户以为有防护，实际任何一次调用都能直接动他的设备。
        self.assertEqual(data["guard_default_mode"], "readonly",
                         "[%s] 装出来的包默认档位不是 readonly：%s"
                         % (tag, data["guard_default_mode"]))
        self.assertTrue(data["guard_blocks_write"],
                        "[%s] 装出来的包里隔离层没拦住写操作（闸门没随包生效）" % tag)

    def test_install_is_idempotent(self):
        """连续装两次都必须成功。

        历史坑（2026-09-22 实测踩到）：setuptools 的 bdist 目录**不幂等** ——
        第二次构建在拷贝 dist-info 时报 WinError 183「文件已存在」，
        而那条报错完全不指向用户能改的地方。修法见 setup.py 的
        `_isolated_build_base()`：每次构建用一个全新的空目录，让残留根本不产生。

        另一层坑（同日再踩）：先试过「构建前删掉旧的 build/」，在带批量删除保护的
        环境下会被安全闸门拦下（>50 个文件需确认），而 pip 只报
        metadata-generation-failed。所以这里**不能**靠「删干净」来幂等——
        「什么都不复用」才是稳的。
        """
        tgt = tempfile.mkdtemp(prefix="mcu_dbg_idem_")
        try:
            for i in (1, 2):
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", ".", "--no-deps",
                     "--no-build-isolation", "--target", tgt, "-q"],
                    cwd=ROOT, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                self.assertEqual(r.returncode, 0,
                                 "第 %d 次 pip install 失败（构建不幂等）：\n%s"
                                 % (i, (r.stderr or "")[-900:]))
        finally:
            shutil.rmtree(tgt, ignore_errors=True)

    def test_knowledge_from_wheel(self):
        """★ 2026-10-13 新增：从 **wheel** 安装跑探针（分发路径①）。"""
        data = self._install_and_probe(self.wheel_path, "wheel")
        self._assert_probe(data, "wheel")

    def test_knowledge_from_sdist(self):
        """★ 2026-10-13 新增：从 **sdist** 安装跑探针（分发路径②，空壳传染路径）。

        审查报告 P0-1 的实测复现：MANIFEST.in 缺失时 sdist 不含数据目录，
        而标准 `python -m build` 的 wheel 是从 sdist 解包构建的 —— 空壳传染。
        只有这条测试能在发版前抓住它。
        """
        data = self._install_and_probe(self.sdist_path, "sdist")
        self._assert_probe(data, "sdist")


#: 冒烟探针：**必须**先剔掉仓库根，否则 import 的是原地源码
_PROBE_SRC = '''# -*- coding: utf-8 -*-
import json, os, sys
TGT, ROOT = r"{tgt}", r"{root}"
sys.path.insert(0, TGT)
sys.path = [p for p in sys.path
            if os.path.normcase(os.path.abspath(p or os.getcwd()))
            != os.path.normcase(ROOT)]
import core, analyzer, paths, guard

drv_txt = core.driver_list()
# 隔离层端到端：显式设成只读档，再试一个写操作，必须被拦。
# （父进程可能通过 conftest 塞了 PORT_DENY=*，这里清掉，确保测的是档位本身）
os.environ["MCU_DEBUG_MCP_MODE"] = "readonly"
os.environ.pop("MCU_DEBUG_MCP_PORT_DENY", None)
_guard_blocked = core.at_sequence("COM99", [{{"cmd": "AT", "expect": "OK"}}])
result = {{
    "core_file": core.__file__,
    "core_from_target": os.path.normcase(os.path.dirname(core.__file__))
                        == os.path.normcase(TGT),
    "data_roots": paths.data_roots(),
    "pinmaps": len(core.pinmap_list()),
    "boards": len(core.board_list()),
    "drivers": len(core.paths.layer_dirs("drivers")),
    "errcode_tables": len(core._errcode_tables()),
    "fault_bits": len(core.fault_bits_list()),
    "extra_rules": len(analyzer._EXTRA_RULES),
    "driver_get_real": "验证记录" in core.driver_get("dht11"),
    "errcode_real": "为空" not in core.error_code_lookup(),
    "driver_list_ok": "共 9 个预验证驱动" in drv_txt,
    "guard_default_mode": guard.DEFAULT_MODE,
    "guard_blocks_write": "隔离拦截" in _guard_blocked,
}}
print(json.dumps(result, ensure_ascii=False))
'''


if __name__ == "__main__":
    unittest.main()
