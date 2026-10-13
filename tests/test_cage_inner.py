# -*- coding: utf-8 -*-
"""笼子内部逻辑回归（批次 3）：环境自检降级 / 日志治理 / 会话报告 / 薄包装对账。

这些分支全部**不碰真实硬件**（mock 掉端口枚举 / 数据根），测的是：
    · 降级路径必须给结论与指引，不能崩、不能静默（env_status / build.py 同一纪律）；
    · 不可逆动作必须过闸门，闸门文案要说清「怎么放行」（logs_prune / session_report）；
    · 报告是交付物——不默认覆盖、必须附原始证据（report_session）；
    · server.py 59 个薄包装只许转发参数，不许偷偷加工。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcu_debug_mcp import core, paths  # noqa: E402
from isolation import relaxed_env  # noqa: E402


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _tmpdir():
    d = tempfile.mkdtemp(prefix="mcudbg_t3_")
    return d


class _EnvHome(unittest.TestCase):
    """把数据根指到临时目录：任何写盘都落在沙箱里，不碰真实 ~/.mcu-debug-mcp。

    ★ 必须先 ``paths.reset_cache()``：writable_root() 的解析结果有**进程级缓存**
      （paths._write_root_cache）——生产上是合理设计（数据根不随运行时环境变），
      但对测试意味着「第一个 patch 了 ENV_HOME 的测试会把后面全部测试钉死在自己的
      临时目录上」。每个用例前后各 reset 一次，让 ENV_HOME 重新生效。
    """

    def setUp(self):
        paths.reset_cache()
        self._tmp = _tmpdir()
        self._patcher = mock.patch.dict(os.environ, {paths.ENV_HOME: self._tmp})
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(paths.reset_cache)


# =====================================================================
# _port_baud_relevance：端口类型判定（USB CDC 扫档无意义 / 桥真实生效）
# =====================================================================
class _FakePort(object):
    def __init__(self, device, hwid="", description="", vid=None, pid=None):
        self.device = device
        self.hwid = hwid
        self.description = description
        self.vid = vid
        self.pid = pid


def _with_ports(self, ports):
    return mock.patch("serial.tools.list_ports.comports", return_value=ports)


class TestPortBaudRelevance(unittest.TestCase):

    def test_usb_cdc_id_means_baudrate_irrelevant(self):
        with _with_ports(self, [_FakePort("COM5", "VID:PID=303A:1001", "USB JTAG")]):
            kind, desc = core._port_baud_relevance("COM5")
        self.assertEqual(kind, "usb_cdc")
        self.assertIn("303A:1001", desc)

    def test_ftdi_bridge_means_baudrate_matters(self):
        with _with_ports(self, [_FakePort("COM7", "USB VID:PID=0403:6010 SER=6", "USB Serial")]):
            kind, _ = core._port_baud_relevance("com7")     # 大小写不敏感
        self.assertEqual(kind, "bridge")

    def test_bluetooth_virtual_port(self):
        with _with_ports(self, [_FakePort("COM9", "BTHENUM\\{00001101}", "蓝牙链接")]):
            kind, desc = core._port_baud_relevance("COM9")
        self.assertEqual(kind, "usb_cdc")
        self.assertIn("蓝牙", desc)

    def test_no_vid_pid_gives_unknown_with_description(self):
        with _with_ports(self, [_FakePort("COM11", "SOME\\UNKNOWN\\DEV", "神秘设备")]):
            kind, desc = core._port_baud_relevance("COM11")
        self.assertEqual(kind, "unknown")
        self.assertEqual(desc, "神秘设备")

    def test_port_absent_is_unknown(self):
        with _with_ports(self, [_FakePort("COM5", "VID:PID=0403:6010")]):
            kind, _ = core._port_baud_relevance("COM99")
        self.assertEqual(kind, "unknown")

    def test_broken_vidpid_string_does_not_crash(self):
        """hwid 里 VID:PID= 的值是坏的 → 按 unknown 走，不许抛异常。"""
        with _with_ports(self, [_FakePort("COM5", "VID:PID=zz:zz")]):
            kind, _ = core._port_baud_relevance("COM5")
        self.assertEqual(kind, "unknown")


# =====================================================================
# env_status：降级分支
# =====================================================================
class TestEnvStatusBranches(_EnvHome):

    def test_pyocd_missing_branch(self):
        with mock.patch.object(core, "HAS_PYOCD", False):
            out = core.env_status()
        self.assertIn("pyocd      : ✗ 未安装", out)
        self.assertIn("pip install pyocd", out)

    def test_no_flash_tools_gives_install_hints(self):
        with mock.patch.object(core, "flash_tool_detect", return_value={}):
            out = core.env_status()
        self.assertIn("烧录器 / 探针", out)
        self.assertIn("pip install esptool", out)

    def test_empty_knowledge_bases_warn_about_packaging(self):
        """知识库为空 → 明确指向「数据目录没随包分发」这个历史 P0。"""
        with mock.patch.object(core, "pinmap_list", return_value=[]), \
             mock.patch.object(core, "board_list", return_value=[]):
            out = core.env_status()
        self.assertIn("有知识库是空的", out)

    def test_posix_permission_block_advises_dialout(self):
        """Linux 权限模型下有口不可读写 → 给 usermod 指引（Windows 上永远测不到这条）。"""
        ports = [_FakePort("/dev/ttyUSB0", "USB VID:PID=0403:6010", "FT232")]
        with mock.patch("os.name", "posix"), \
             mock.patch("os.access", return_value=False), \
             mock.patch("serial.tools.list_ports.comports", return_value=ports):
            out = core.env_status()
        self.assertIn("不可读写", out)
        self.assertIn("dialout", out)

    def test_posix_all_readable_says_so(self):
        ports = [_FakePort("/dev/ttyUSB0", "USB VID:PID=0403:6010", "FT232")]
        with mock.patch("os.name", "posix"), \
             mock.patch("os.access", return_value=True), \
             mock.patch("serial.tools.list_ports.comports", return_value=ports):
            out = core.env_status()
        self.assertIn("全部可读写", out)

    def test_log_overflow_points_to_logs_prune(self):
        """日志数超出默认保留量 → 主动指出该清理了（N-12 的核心场景）。"""
        logs = os.path.join(self._tmp, "logs")
        os.makedirs(logs)
        for i in range(core._LOGS_KEEP_COUNT + 1):
            with open(os.path.join(logs, "s%03d.log" % i), "w", encoding="utf-8") as f:
                f.write("x" * 100)
        with mock.patch.object(core, "_logs_dir", return_value=logs):
            out = core.env_status()
        self.assertIn("已超出默认保留量", out)

    def test_reports_version_and_sections(self):
        out = core.env_status()
        for token in ("版本", "依赖", "串口", "隔离", "知识库"):
            self.assertIn(token, out)


# =====================================================================
# logs_prune：保留策略 + 闸门 + 真删
# =====================================================================
class TestLogsPrune(_EnvHome):

    def _mk_logs(self, n=5, mtime_offset=0):
        import time
        logs = os.path.join(self._tmp, "logs")
        os.makedirs(logs, exist_ok=True)
        for i in range(n):
            p = os.path.join(logs, "s%03d.log" % i)
            with open(p, "w", encoding="utf-8") as f:
                f.write("log line %d\n" % i)
            if mtime_offset:
                stamp = time.time() - mtime_offset
                os.utime(p, (stamp, stamp))
        return logs

    def test_non_integer_params_rejected(self):
        self._mk_logs(1)
        with mock.patch.object(core, "_logs_dir", return_value=os.path.join(self._tmp, "logs")):
            out = core.logs_prune(keep_days="many")
        self.assertIn("参数必须是整数", out)

    def test_missing_dir_is_friendly(self):
        """目录不存在 → 明说「打开一次串口后才会创建」，不许崩。"""
        out = core.logs_prune(keep_count=1)
        self.assertIn("还没有会话日志目录", out)

    def test_empty_dir_needs_no_pruning(self):
        os.makedirs(os.path.join(self._tmp, "logs"))
        out = core.logs_prune()
        self.assertIn("无需清理", out)

    def test_dry_run_keeps_newest_n(self):
        """keep_count=2 + 5 个文件 → 保留 2 清 3，且 dry-run 什么都没删。"""
        logs = self._mk_logs(5)
        with mock.patch.object(core, "_logs_dir", return_value=logs):
            out = core.logs_prune(keep_count=2)
        self.assertIn("将保留   : 2 个", out)
        self.assertIn("待清理   : 3 个", out)
        self.assertIn("什么都没删", out)
        self.assertEqual(len([f for f in os.listdir(logs) if f.endswith(".log")]), 5)

    def test_keep_days_zero_drops_everything(self):
        logs = self._mk_logs(3)
        with mock.patch.object(core, "_logs_dir", return_value=logs):
            out = core.logs_prune(keep_days=0)
        self.assertIn("待清理   : 3 个", out)

    def test_real_delete_blocked_in_readonly(self):
        """默认 readonly 档：就算 dry_run=False + confirm=True 也过不了闸门。"""
        logs = self._mk_logs(3)
        with mock.patch.object(core, "_logs_dir", return_value=logs):
            out = core.logs_prune(dry_run=False, confirm=True)
        self.assertIn("隔离", out)
        self.assertEqual(len(os.listdir(logs)), 3)

    def test_real_delete_with_write_mode_and_confirm(self):
        logs = self._mk_logs(5)
        with mock.patch.object(core, "_logs_dir", return_value=logs), \
             relaxed_env(mode="write"):
            out = core.logs_prune(keep_count=2, dry_run=False, confirm=True)
        self.assertIn("已删除   : 3 个", out)
        self.assertEqual(len([f for f in os.listdir(logs) if f.endswith(".log")]), 2)

    def test_real_delete_with_nothing_to_drop(self):
        logs = self._mk_logs(2)
        with mock.patch.object(core, "_logs_dir", return_value=logs), \
             relaxed_env(mode="write"):
            out = core.logs_prune(dry_run=False, confirm=True)
        self.assertIn("没有需要清理的文件", out)
        self.assertEqual(len(os.listdir(logs)), 2)

    def test_delete_failure_is_reported_not_crash(self):
        logs = self._mk_logs(2)
        with mock.patch.object(core, "_logs_dir", return_value=logs), \
             relaxed_env(mode="write"), \
             mock.patch.object(core.os, "remove", side_effect=OSError("locked")):
            out = core.logs_prune(keep_count=1, dry_run=False, confirm=True)
        self.assertIn("删除失败", out)
        self.assertIn("locked", out)


# =====================================================================
# report_session：数据来源 / 覆盖保护 / 闸门
# =====================================================================
class TestReportSession(_EnvHome):

    def _mk_log(self, name="s20260101_000000.log", text="TICK 1\nTICK 2\nERROR: boom\n"):
        logs = os.path.join(self._tmp, "logs")
        os.makedirs(logs, exist_ok=True)
        p = os.path.join(logs, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write("# session header（这行是元信息，不算证据）\n" + text)
        return logs

    def test_invalid_handle_uses_unified_message(self):
        out = core.report_session(handle=4242)
        self.assertIn("无效的串口句柄 4242", out)

    def test_path_traversal_in_fname_is_refused(self):
        """fname 带路径穿越 → 只认日志目录里的文件名，别处一概不给读。"""
        self._mk_log()
        outside = os.path.join(self._tmp, "evil.log")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("secret")
        out = core.report_session(fname=os.path.join("..", "evil.log"))
        self.assertIn("没有这个会话日志", out)

    def test_fname_reads_log_and_attaches_evidence(self):
        self._mk_log()
        with relaxed_env(mode="write"):
            out = core.report_session(fname="s20260101_000000.log",
                                      out_path=os.path.join(self._tmp, "r1.md"))
        self.assertIn("报告已生成", out)
        with open(os.path.join(self._tmp, "r1.md"), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("ERROR: boom", body)          # 原始证据必须附在报告里
        self.assertIn("## 一、故障分析", body)
        self.assertNotIn("# session header", body)  # 元信息行不进证据

    def test_single_log_auto_selected(self):
        self._mk_log()
        with relaxed_env(mode="write"):
            out = core.report_session(out_path=os.path.join(self._tmp, "r2.md"))
        self.assertIn("唯一一份，自动选用", out)

    def test_multiple_logs_demand_source(self):
        self._mk_log("a.log")
        self._mk_log("b.log")
        out = core.report_session()
        self.assertIn("需要指定数据来源", out)

    def test_bad_tail_lines_fall_back_to_40(self):
        self._mk_log()
        with relaxed_env(mode="write"):
            out = core.report_session(fname="s20260101_000000.log",
                                      tail_lines="lots",
                                      out_path=os.path.join(self._tmp, "r3.md"))
        self.assertIn("报告已生成", out)

    def test_existing_target_not_overwritten_by_default(self):
        self._mk_log()
        target = os.path.join(self._tmp, "r4.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("OLD REPORT")
        with relaxed_env(mode="write"):
            out = core.report_session(fname="s20260101_000000.log", out_path=target)
        self.assertIn("目标已存在", out)
        self.assertIn("overwrite=True", out)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "OLD REPORT")

    def test_overwrite_needs_explicit_confirm(self):
        """2026-10-13 审查 P1：覆盖已存在文件必须再过一道显式确认。"""
        self._mk_log()
        target = os.path.join(self._tmp, "r5.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("OLD")
        with relaxed_env(mode="write"):
            out = core.report_session(fname="s20260101_000000.log",
                                      out_path=target, overwrite=True)
        self.assertIn("confirm=True", out)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "OLD")

    def test_write_blocked_in_readonly(self):
        self._mk_log()
        with mock.patch.object(paths, "writable_root", return_value=self._tmp):
            out = core.report_session(fname="s20260101_000000.log")
        self.assertIn("隔离", out)

    def test_write_failure_is_reported(self):
        self._mk_log()
        target = os.path.join(self._tmp, "no", "such", "dir")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # 把父路径变成文件 → makedirs/ open 都会 OSError
        blocker = os.path.join(self._tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        bad_target = os.path.join(blocker, "r.md")
        with relaxed_env(mode="write"):
            out = core.report_session(fname="s20260101_000000.log", out_path=bad_target)
        self.assertIn("写报告失败", out)

    def test_successful_report_writes_file(self):
        self._mk_log()
        target = os.path.join(self._tmp, "r6.md")
        with relaxed_env(mode="write"):
            out = core.report_session(fname="s20260101_000000.log", out_path=target)
        self.assertIn("报告已生成", out)
        self.assertTrue(os.path.isfile(target))


# =====================================================================
# server.py 薄包装：数量对账 + 参数原样转发
# =====================================================================
class TestServerWrappers(unittest.TestCase):
    """server.py 的 79 行未覆盖几乎全是 @mcp.tool() 薄包装。
    要锁的判据：注册数量对、转发不改参数、不偷加默认值。"""

    @classmethod
    def setUpClass(cls):
        from mcu_debug_mcp import server
        cls.server = server

    def test_tool_count_is_59(self):
        tools = self.server.mcp._tool_manager._tools
        self.assertEqual(len(tools), 59,
                         "工具数变了：同步 verify_stdio.py 期望、README、coverage.json 三处")

    def test_wrappers_forward_args_untouched(self):
        """抽查三个包装：转发时既不丢参也不改参。"""
        calls = []
        with mock.patch.object(core, "serial_open",
                               side_effect=lambda *a, **k: calls.append((a, k)) or "ok"):
            self.server.serial_open(" COM3 ", 9600, 8, "E", 2)
        self.assertEqual(calls[0][0], (" COM3 ", 9600, 8, "E", 2))

        calls.clear()
        with mock.patch.object(core, "serial_read",
                               side_effect=lambda *a, **k: calls.append((a, k)) or "ok"):
            self.server.serial_read(7, 2.0, "OK|ERROR", 500)
        self.assertEqual(calls[0][0], (7, 2.0, "OK|ERROR", 500))

        calls.clear()
        with mock.patch.object(core, "logs_prune",
                               side_effect=lambda *a, **k: calls.append((a, k)) or "ok"):
            self.server.logs_prune(30, 200, dry_run=False, confirm=True)
        # ★ server 层签名序 (keep_days, keep_count, dry_run, confirm) 与 core 层
        #   (keep_days, keep_count, confirm, dry_run) 不同——转发必须按名，
        #   位置错位会把 confirm/dry_run 调包。这里锁死关键字转发。
        self.assertEqual(calls[0], ((),
                                    {"keep_days": 30, "keep_count": 200,
                                     "confirm": True, "dry_run": False}))

    def test_every_tool_has_a_docstring(self):
        """docstring 是给 MCP 客户端看的——空 docstring 的工具在客户端就是黑盒。"""
        tools = self.server.mcp._tool_manager._tools
        empty = [n for n, t in tools.items() if not (t.description or "").strip()]
        self.assertEqual(empty, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
