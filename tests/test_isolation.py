# -*- coding: utf-8 -*-
"""mcu-debug-mcp · 隔离层专项测试（test_isolation.py）

隔离层最容易出的一类问题不是"拦不住"，而是「**看着拦住了，其实绕得过去**」：
  · 新写的写操作没登记进 guard.OPS，于是悄悄放行
  · 端口名单配了却被档位判断提前 return 掉
  · 为了让人干活方便，给某个 URL 形态开了口子，结果那个口子通向真设备
  · 隔离拦住了"打开端口"，测试于是改去测别的，掩盖了它原本会碰真机的事实

所以本文件的写法是：**每条隔离规则都有一条会失败的测试守着**，
并且刻意包含"必须放行"的反例（离线分析、dry-run 预览、纯软件回环）——
只测"该拦的拦住了"，隔离层很容易退化成"什么都拦"，那用户就直接绕开它了。

关于本条测试自身所处的环境：conftest.py 已把整个进程锁成 readonly + 端口全禁，
真正需要"已授权环境"的用例用 mock.patch.dict 局部打开，退出即恢复。
"""

import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from mcu_debug_mcp import core        # noqa: E402
from mcu_debug_mcp import guard       # noqa: E402

R = guard.MODE_READONLY
W = guard.MODE_WRITE
OFF = guard.MODE_OFF

#: 需要写权限的硬件操作（与 guard.OPS 的 needs 字段对应）
WRITE_OPS = [k for k, v in guard.OPS.items() if v["needs"] == "write"]


def env(**kw):
    """临时设定隔离环境变量（退出即恢复，测试之间不互相污染）。"""
    return mock.patch.dict(os.environ, {k: v for k, v in kw.items() if v is not None})


class TestDefaults(unittest.TestCase):
    """默认必须是最保守的那一档——默认放行等于没有隔离。"""

    def test_unset_mode_defaults_readonly(self):
        with mock.patch.dict(os.environ, {"MCU_DEBUG_MCP_MODE": ""}):
            os.environ.pop("MCU_DEBUG_MCP_MODE", None)
            self.assertEqual(guard.mode(), R)

    def test_unknown_mode_falls_back_readonly(self):
        """写错档位名不能静默放行（例如写成 yes / enable / 1）。"""
        for bad in ("yes", "enable", "1", "ReadWrite", "rw"):
            with env(MCU_DEBUG_MCP_MODE=bad):
                self.assertEqual(guard.mode(), R, "档位 %r 被误当成有效值" % bad)
                self.assertIn("不是合法档位", guard.mode_source())

    def test_source_is_always_reported(self):
        """"当前是默认还是他设的"必须问得出——否则用户不知道自己在哪一档。"""
        with mock.patch.dict(os.environ, {"MCU_DEBUG_MCP_MODE": ""}):
            os.environ.pop("MCU_DEBUG_MCP_MODE", None)
            self.assertIn("默认", guard.mode_source())
        with env(MCU_DEBUG_MCP_MODE=W):
            self.assertIn(W, guard.mode_source())


class TestReadonlyBlocksWrites(unittest.TestCase):
    """readonly（默认档）：一切会改变设备状态的操作都要被拦，且给出放开方法。"""

    def test_every_write_op_is_blocked(self):
        self.assertTrue(WRITE_OPS, "guard.OPS 里没有登记任何写操作，隔离形同虚设")
        for op in WRITE_OPS:
            with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY=""):
                out = guard.check(op, "COM99")
            self.assertIsNotNone(out, "%s 在 readonly 档没被拦住" % op)
            self.assertIn("隔离拦截", out)
            self.assertIn("MCU_DEBUG_MCP_MODE=write", out,
                          "%s 的拒绝文案没告诉用户怎么放开——那用户只能猜" % op)

    def test_read_op_is_allowed_in_readonly(self):
        """`serial_open` 只读档也允许——隔离不是「什么都禁」，否则用户会想办法绕过。"""
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY="", ):
            self.assertIsNone(guard.check("serial_open", "COM99"))

    def test_off_mode_allows_everything(self):
        for op in guard.OPS:
            with env(MCU_DEBUG_MCP_MODE=OFF, MCU_DEBUG_MCP_PORT_DENY="*"):
                self.assertIsNone(guard.check(op, "COM6", confirm=True),
                                  "%s 在 off 档被拦了" % op)


class TestIrreversibleNeedsConfirm(unittest.TestCase):
    """不可逆操作（烧录）必须过两道门：写权限 + confirm。"""

    def test_flash_blocked_without_confirm_even_in_write_mode(self):
        with env(MCU_DEBUG_MCP_MODE=W, MCU_DEBUG_MCP_PORT_DENY=""):
            out = guard.check("flash_firmware", "COM99", confirm=False)
        self.assertIsNotNone(out)
        self.assertIn("二次确认", out)
        self.assertIn("confirm=True", out)

    def test_flash_allowed_with_confirm(self):
        with env(MCU_DEBUG_MCP_MODE=W, MCU_DEBUG_MCP_PORT_DENY=""):
            self.assertIsNone(guard.check("flash_firmware", "COM99", confirm=True))

    def test_non_irreversible_does_not_require_confirm(self):
        """别把所有写操作都要求 confirm——那会训练用户无脑点确认，反而更危险。"""
        self.assertFalse(guard.OPS["serial_write"]["irreversible"])
        with env(MCU_DEBUG_MCP_MODE=W, MCU_DEBUG_MCP_PORT_DENY=""):
            self.assertIsNone(guard.check("serial_write", "COM99", confirm=False))


class TestPortGuard(unittest.TestCase):
    """端口名单：独立于档位的第二条防线（保护"你自己正在用的那些口"）。"""

    def test_deny_exact(self):
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY="COM6"):
            out = guard.check("serial_open", "COM6")
        self.assertIsNotNone(out)
        self.assertIn("禁用名单", out)
        self.assertIn("MCU_DEBUG_MCP_PORT_DENY", out)

    def test_deny_wildcard(self):
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY="COM1*"):
            self.assertIsNotNone(guard.check("serial_open", "COM12"))
            self.assertIsNone(guard.check("serial_open", "COM3"))

    def test_deny_case_insensitive(self):
        """Windows 上端口名大小写不固定，名单匹配必须归一化。"""
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY="com6"):
            self.assertIsNotNone(guard.check("serial_open", "COM6"))

    def test_allowlist_mode_blocks_others(self):
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_ALLOW="COM3",
                 MCU_DEBUG_MCP_PORT_DENY=""):
            self.assertIsNone(guard.check("serial_open", "COM3"))
            out = guard.check("serial_open", "COM12")
        self.assertIsNotNone(out)
        self.assertIn("白名单", out)

    def test_deny_wins_over_allow(self):
        """两个名单同时命中时必须拒（否认比许可更强）。"""
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_ALLOW="COM6",
                 MCU_DEBUG_MCP_PORT_DENY="COM6"):
            out = guard.check("serial_open", "COM6")
        self.assertIsNotNone(out)

    def test_pair_record_both_ports_checked(self):
        """双口对录：任一口被禁，整体不执行（不能只查第一个口）。"""
        with env(MCU_DEBUG_MCP_MODE=W, MCU_DEBUG_MCP_PORT_DENY="COM98"):
            out = core.serial_pair_record("COM99", "COM98", seconds=1)
        self.assertIn("隔离拦截", out)


class TestVirtualPortCarveOut(unittest.TestCase):
    """虚拟端口豁免必须**精确到只放 loop://**——这是最容易被开成后门的地方。"""

    def test_loop_is_exempt(self):
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY="*"):
            self.assertIsNone(guard.check("serial_write", "loop://"))
            self.assertIsNone(guard.check("serial_open", "LOOP://"))

    def test_urls_pointing_at_real_resources_are_not_exempt(self):
        """socket/spy/alt/hwgrep 都可能指向真实设备或真实端口，绝不放行。"""
        for url in ("socket://127.0.0.1:23", "spy://COM3", "alt://COM3",
                    "hwgrep://0483:5740"):
            with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY="*"):
                out = guard.check("serial_open", url)
            self.assertIsNotNone(out, "%s 被当成了虚拟端口放行——隔离开了后门" % url)

    def test_exempt_list_is_exactly_loop(self):
        """豁免清单被悄悄加长时，这条会红——名单本身要有守卫。"""
        self.assertEqual(tuple(guard.VIRTUAL_PORT_PREFIXES), ("loop://",))


class TestUnknownOpFailsClosed(unittest.TestCase):
    def test_unregistered_op_is_rejected(self):
        """新写的写操作忘了登记 ⟹ 必须报错，不能默认放行。"""
        with env(MCU_DEBUG_MCP_MODE=OFF):
            out = guard.check("some_future_write_op", "COM99")
        self.assertIsNotNone(out, "未登记的操作为什么能过？必须 fail-closed")
        self.assertIn("未登记", out)

    def test_every_registered_op_documents_risk(self):
        """登记了却没写风险说明 = 拒绝文案没法告诉用户"你在冒什么险"。"""
        for op, spec in guard.OPS.items():
            for field in ("title", "needs", "irreversible", "why", "how"):
                self.assertIn(field, spec, "%s 缺字段 %s" % (op, field))
            self.assertIn(spec["needs"], ("read", "write"), op)
            self.assertTrue(spec["why"].strip(), "%s 的 why 是空的" % op)


class TestCoreWiringEndToEnd(unittest.TestCase):
    """端到端：core 里各工具**真的**接到了闸门上（不只是 guard 单测绿）。"""

    def setUp(self):
        self._saved = dict(core._SESSIONS)
        core._SESSIONS.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        core._SESSIONS.clear()
        core._SESSIONS.update(self._saved)

    def _fake_bin(self):
        """造一个临时假固件（只为过"文件存在"这道参数校验，不参与任何真实写入）。"""
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(b"\x00" * 16)
            path = fh.name
        self.addCleanup(os.remove, path)
        return path

    def _attach_fake(self, portname="COM99"):
        """挂一个假会话：一旦真的写进去就炸——用来证明"根本走不到写"。"""
        class _FakePort(object):
            def write(self, data):
                raise AssertionError("隔离没拦住，真的写到端口上去了！")

            def read(self, n=1):
                return b""

        h = core._NEXT_HANDLE[0]
        core._NEXT_HANDLE[0] += 1
        core._SESSIONS[h] = {"port": _FakePort(), "log": "", "portname": portname,
                             "fh": None, "logfile": None}
        return h

    def test_serial_send_blocked(self):
        h = self._attach_fake()
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY=""):
            out = core.serial_send(h, "AT")
        self.assertIn("隔离拦截", out)

    def test_at_sequence_blocked_before_touching_hardware(self):
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY=""):
            out = core.at_sequence("COM99", [{"cmd": "AT", "expect": "OK"}])
        self.assertIn("隔离拦截", out)

    def test_baudrate_probe_blocked(self):
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY=""):
            out = core.baudrate_autodetect("COM99")
        self.assertIn("隔离拦截", out)

    def test_baudrate_offline_branch_still_works(self):
        """★反例：传了 log_text 就是纯打分，不碰硬件——隔离层不能连这个也挡。"""
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY="*"):
            out = core.baudrate_autodetect("COM99", log_text="rst:0x1 boot:0x13 hello\n")
        self.assertNotIn("隔离拦截", out)
        self.assertIn("波特率扫描结果", out)

    def test_flash_dry_run_not_blocked(self):
        """★反例：dry-run 只打印将要执行的命令、不写 flash，隔离层不该妨碍它。"""
        path = self._fake_bin()
        with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY="*"):
            out = core.flash_firmware(path, dry_run=True)
        self.assertNotIn("隔离拦截", out)

    def test_flash_real_write_requires_confirm(self):
        """readonly 档下即使 confirm=True 也必须拦住——写权限与确认是两道独立门。"""
        path = self._fake_bin()
        with mock.patch.object(core, "flash_tool_detect", return_value={"esptool": "esptool"}), \
                mock.patch.object(core.subprocess, "run",
                                  side_effect=AssertionError("隔离失败：真的去执行烧录了！")):
            with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY=""):
                blocked = core.flash_firmware(path, chip="esp32", dry_run=False, confirm=True)
        self.assertIn("隔离拦截", blocked)

    def test_flash_executes_when_fully_authorized(self):
        """★反例（方向性）：两道门都开了就必须真的执行。

        只测"拦得住"会掩盖反向 bug——比如把闸门写得永远拦住，
        用户按说明放开了却依然烧不进去，而所有"拦截"测试都是绿的。
        """
        path = self._fake_bin()
        fake = mock.Mock(returncode=0, stdout="Hash of data verified.", stderr="")
        with mock.patch.object(core, "flash_tool_detect", return_value={"esptool": "esptool"}), \
                mock.patch.object(core.subprocess, "run", return_value=fake) as run, \
                env(MCU_DEBUG_MCP_MODE=W, MCU_DEBUG_MCP_PORT_DENY=""):
            out = core.flash_firmware(path, chip="esp32", dry_run=False, confirm=True)
        self.assertTrue(run.called, "两道门都开了却没执行——闸门把功能堵死了")
        self.assertIn("退出码 0", out)

    def test_flash_confirm_without_write_mode_is_blocked(self):
        """确认了但没放写权限，同样不许执行（两道门缺一不可，顺序无关）。"""
        path = self._fake_bin()
        with mock.patch.object(core, "flash_tool_detect", return_value={"esptool": "esptool"}), \
                mock.patch.object(core.subprocess, "run",
                                  side_effect=AssertionError("隔离失败：真的去执行烧录了！")):
            with env(MCU_DEBUG_MCP_MODE=R, MCU_DEBUG_MCP_PORT_DENY=""):
                out = core.flash_firmware(path, chip="esp32", dry_run=False, confirm=True)
        self.assertIn("隔离拦截", out)


class TestTestEnvironmentItself(unittest.TestCase):
    """元测试：测试环境**自己**有没有被隔离住。

    这一层防的是"隔离只写在文档里"——即 conftest 被删掉/改坏之后，
    测试仍然全绿，但已经在碰真机了。所以隔离的有效性本身要有断言。
    """

    def test_default_deny_all_ports(self):
        self.assertEqual(os.environ.get("MCU_DEBUG_MCP_PORT_DENY"), "*",
                         "测试进程的端口禁用名单不是 *：测试可能接通真实设备")

    def test_real_serial_constructor_is_disabled(self):
        try:
            import serial
        except ImportError:
            self.skipTest("pyserial 未安装")
        with self.assertRaises(AssertionError) as cm:
            serial.Serial()
        self.assertIn("禁止打开真实串口", str(cm.exception))

    def test_relaxed_env_restores_after_use(self):
        """临时放行必须自动恢复——否则会制造"单跑绿、全跑红"的脏故障。"""
        before = os.environ.get("MCU_DEBUG_MCP_MODE")
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from isolation import relaxed_env
        with relaxed_env():
            self.assertEqual(guard.mode(), OFF)
        self.assertEqual(os.environ.get("MCU_DEBUG_MCP_MODE"), before)


class TestLiveAcceptanceFixture(unittest.TestCase):
    """N-15「真机验收夹具」（tools/live_acceptance.py）：夹具自己必须先安全。

    它比被测对象更危险——一块板子真在手上，配错一次就是一次真实的写操作。
    初版踩过两个坑，各留一条测试守着：
      · 为"只放目标口"把 MCU_DEBUG_MCP_PORT_DENY 设成 "*"，
        可 DENY 优先级最高，反而把目标口自己拦死了（dry-run 时就看得见）；
      · 收尾"拉回安全态"那步无条件记 PASS，板子明明被打死了、报告上还是绿的。
    """

    FIXTURE = os.path.join(ROOT, "src", "mcu_debug_mcp", "tools", "live_acceptance.py")

    def _src(self):
        with open(self.FIXTURE, encoding="utf-8") as f:
            return f.read()

    def _run(self, *args):
        e = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, self.FIXTURE] + list(args),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=90, env=e)

    def test_fixture_exists(self):
        self.assertTrue(os.path.isfile(self.FIXTURE),
                        "N-15 要求的 tools/live_acceptance.py 不见了")

    def test_refuses_to_run_without_port(self):
        """不给端口就不该有任何动作——这是"默认最保守"的第一道。"""
        r = self._run()
        self.assertNotEqual(r.returncode, 0, "不给端口应当拒绝运行")
        self.assertIn("port", r.stderr or "")

    def test_dry_run_opens_no_port(self):
        r = self._run("COM_FAKE_NOT_REAL", "--allow-write", "--dry-run")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertIn("dry-run", out)
        self.assertNotIn("返回句柄", out, "--dry-run 不该真的打开串口")

    def test_deny_list_not_set_to_wildcard(self):
        src = self._src()
        self.assertNotIn('MCU_DEBUG_MCP_PORT_DENY"] = "*"', src,
                         "DENY 优先于 ALLOW，设成 * 会把目标口自己拦死")
        self.assertIn('os.environ.pop("MCU_DEBUG_MCP_PORT_DENY"', src,
                      "应显式清掉外部传入的 DENY，避免继承一个自己没配的禁用名单")

    def test_safe_off_can_report_failure(self):
        src = self._src()
        i = src.index("def _safe_off(")
        body = src[i:i + 2000]
        self.assertIn('"FAIL"', body,
                      "收尾步骤必须能报失败；无条件 PASS 是假成功（初版即如此）")

    def test_destructive_stage_is_gated(self):
        """会打死板子的那一关必须单独开关，不能跟着 --allow-write 一起放行。"""
        r = self._run("COM_FAKE_NOT_REAL", "--allow-write", "--dry-run")
        self.assertIn("第 5 关", r.stdout or "")
        self.assertIn("需 --allow-destructive", r.stdout or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
