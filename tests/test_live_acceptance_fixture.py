# -*- coding: utf-8 -*-
"""真机验收夹具（tools/live_acceptance.py）的**离线**回归测试。

为什么夹具本身也要测
--------------------
夹具是"用来证明工具能用"的东西。它自己出错时，有两种表现，**两种都很坏**：

    · 假绿灯：板子已经死了 / 配置已经错了，它照样打 PASS → 你以为验过了。
    · 假 FAIL：板子活得好好的，它报"设备未应答（可能已被打死）" → 你去翻一块
      根本没坏的板子。

后者是真机上真实发生过的两起（见下面两条 ★ 测试的注释）。它们都不是被测工具的
缺陷，而是**夹具自己的判据写反 / 写死**，所以必须在这里用假件钉住 —— 不能靠"再
跑一次真机看看"。

本文件不碰任何硬件：`core` 全是假的（FakeCore），只验夹具的判据与文案。
"""

import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import live_acceptance as la  # noqa: E402


# --------------------------------------------------------------------------- #
# 假件
# --------------------------------------------------------------------------- #

class _Sink(object):
    """把 Report 的 print 吞掉，只留 rows。"""

    def __init__(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", io.StringIO())

    @staticmethod
    def rows(rep):
        return [(n, s, d) for _l, n, s, d in rep.rows]

    @staticmethod
    def find(rep, key):
        for _l, n, s, d in rep.rows:
            if key in n:
                return s, n, d
        raise AssertionError("报告里没有含 %r 的行，现有行：%r"
                             % (key, [n for _l, n, _s, _d in rep.rows]))


class FakeCore(object):
    """只实现 stage_readonly / _safe_off 用到的那几个方法。

    关键：`writes` 把"真的写出去的字节"记下来 —— 夹具在没授权/不知道协议时
    **必须一个字节都不发**，这一点只有记录才能验。
    """

    HAS_PYSERIAL = True

    def __init__(self, read_text="", log_text="", open_ok=True, send_reply="✓ 已命中"):
        self.read_text = read_text
        self.log_text = log_text
        self.open_ok = open_ok
        self.send_reply = send_reply
        self.writes = []
        self.opened = []
        self.closed = []

    def serial_open(self, port, baud):
        self.opened.append((port, baud))
        if not self.open_ok:
            return "打不开 %s（测试替身）" % port
        return "已打开 %s @ %d 8N1，句柄 handle=7。用 serial_read 读数据。" % (port, baud)

    def serial_read(self, handle, seconds=2.0, idle_ms=1000):
        assert handle == 7
        return self.read_text

    def serial_session_log(self, handle):
        assert handle == 7
        return self.log_text

    def serial_close(self, handle):
        self.closed.append(handle)
        return "已关闭 COM_TEST（handle=%d）。" % handle

    def serial_send(self, handle, data, hex_mode=False, newline=True,
                    expect=None, wait_s=0.5):
        self.writes.append((data, newline))
        return self.send_reply


def _rep():
    return la.Report()


# --------------------------------------------------------------------------- #
# ★ 缺陷 1：serial_session_log 的判据曾经是【反的】
# --------------------------------------------------------------------------- #

_CHATTY_READ = (
    "handle=7 收到 170 字节（2.0 秒）\n"
    "--- 文本 ---\n"
    "TICK 658\r\nTICK 659\r\nTICK 660\r\n"
)
_CHATTY_LOG = "TICK 658\r\nTICK 659\r\nTICK 660\r\n"

_SILENT_READ = (
    "handle=7 在 2.0 秒内没有收到任何字节。\n"
    "排查：1) 单片机是否在持续打印……\n"
)


def test_session_log_check_passes_when_board_is_chatty(monkeypatch):
    """★ 回归：原判据 `"handle=%d" % h in g` 只在**缓冲区为空**时成立
    （core.serial_session_log 有数据时返回日志原文、不带 handle 前缀），
    于是"板子在刷 TICK"反而被判 FAIL。真机第一次跑就撞上了。"""
    _Sink(monkeypatch)
    core = FakeCore(read_text=_CHATTY_READ, log_text=_CHATTY_LOG)
    rep = _rep()
    assert la.stage_readonly(core, "COM_TEST", 115200, rep) is True
    st, name, detail = _Sink.find(rep, "serial_session_log")
    assert st == "PASS", "板子在说话时这条必须是 PASS，detail=%r" % detail
    # detail 里必须报出**具体**是哪个 token 命中了（不是一句空泛的"可读"），
    # 而且那个 token 得真的是刚读到的内容 —— 否则这条断言等于什么也没验。
    m = re.search(r"'([^']+)'", detail)
    assert m, "detail 应指认具体的样本行，实际 %r" % detail
    assert m.group(1) in ("TICK 658", "TICK 659", "TICK 660"), detail
    assert m.group(1) in _CHATTY_LOG, detail


def test_session_log_check_is_not_a_free_pass_when_log_is_empty(monkeypatch):
    """反方向也要有判别力：读到数据、但会话日志却是空的 → 必须 FAIL（不许自动绿）。"""
    _Sink(monkeypatch)
    core = FakeCore(read_text=_CHATTY_READ, log_text="")   # 日志空 = 不一致
    rep = _rep()
    la.stage_readonly(core, "COM_TEST", 115200, rep)
    st, _name, _d = _Sink.find(rep, "serial_session_log")
    assert st == "FAIL"


def test_session_log_check_passes_for_silent_board_that_says_so(monkeypatch):
    """沉默板：工具如实答"会话日志为空" → PASS。两个方向都要能过。"""
    _Sink(monkeypatch)
    core = FakeCore(read_text=_SILENT_READ,
                    log_text="handle=7 会话日志为空（还没读到过数据）。")
    rep = _rep()
    la.stage_readonly(core, "COM_TEST", 115200, rep)
    st, _n, detail = _Sink.find(rep, "serial_session_log")
    assert st == "PASS", detail
    assert "如实报空" in detail


def test_session_log_check_fails_for_silent_board_with_content_in_log(monkeypatch):
    """沉默板却给出内容 → 也是在编，必须 FAIL。"""
    _Sink(monkeypatch)
    core = FakeCore(read_text=_SILENT_READ, log_text="TICK 1\r\n")
    rep = _rep()
    la.stage_readonly(core, "COM_TEST", 115200, rep)
    st, _n, _d = _Sink.find(rep, "serial_session_log")
    assert st == "FAIL"


def test_read_check_actually_can_fail(monkeypatch):
    """serial_read 那条也不许是"永远 PASS"：真出现异常/Error 必须报 FAIL。"""
    _Sink(monkeypatch)
    core = FakeCore(read_text="Traceback (most recent call last):\nError: boom",
                    log_text="x")
    rep = _rep()
    la.stage_readonly(core, "COM_TEST", 115200, rep)
    st, _n, _d = _Sink.find(rep, "serial_read")
    assert st == "FAIL"


# --------------------------------------------------------------------------- #
# ★ 缺陷 2：_safe_off 曾把默认模板的 '0' / 'PWR OFF' 写死
# --------------------------------------------------------------------------- #

def test_safe_off_never_guesses_when_protocol_is_unknown(monkeypatch):
    """★ 回归：用 --cmd/--expect 换了自己的板子后，收尾不该朝目标发默认模板的 '0'
    再报"设备未应答（可能已被打死）" —— 那是假 FAIL，会让人去翻一块没坏的板子。
    正确行为：SKIP + 明说没发字节，且**一个字节都不许发**。"""
    _Sink(monkeypatch)
    core = FakeCore(send_reply="已发送 1 字节，expect='PWR OFF' ✗ 1.5s 内未命中")
    rep = _rep()
    la._safe_off(core, "COM_TEST", 115200, rep)      # 不传 off_cmd
    st, _n, detail = _Sink.find(rep, "收尾")
    assert st == "SKIP", "不知道协议时必须 SKIP，不是 FAIL"
    assert "没发任何字节" in detail
    assert core.writes == [], "不知道安全态就不许发字节，实际发了 %r" % (core.writes,)


def test_safe_off_sends_given_command_and_passes_on_match(monkeypatch):
    _Sink(monkeypatch)
    core = FakeCore(send_reply="已发送 1 字节，expect='PWR OFF' ✓ 已命中（整行匹配）")
    rep = _rep()
    la._safe_off(core, "COM_TEST", 115200, rep, off_cmd="0", off_expect=r"PWR OFF")
    st, _n, _d = _Sink.find(rep, "收尾")
    assert st == "PASS"
    assert core.writes == [("0", False)], "安全态命令必须按 newline=False 发"


def test_safe_off_fails_when_given_command_gets_no_answer(monkeypatch):
    """给了 --off 但目标没应答 → 这才是真的 FAIL（该报的时候要报）。"""
    _Sink(monkeypatch)
    core = FakeCore(send_reply="已发送 1 字节，expect='PWR OFF' ✗ 1.5s 内未命中")
    rep = _rep()
    la._safe_off(core, "COM_TEST", 115200, rep, off_cmd="0", off_expect=r"PWR OFF")
    st, _n, detail = _Sink.find(rep, "收尾")
    assert st == "FAIL"
    assert "未应答" in detail


def test_safe_off_without_expect_only_claims_what_it_verified(monkeypatch):
    """只给 --off 不给 --off-expect：只能验"发得出去"，文案不许冒充"已进安全态"。"""
    _Sink(monkeypatch)
    core = FakeCore(send_reply="已发送 1 字节")
    rep = _rep()
    la._safe_off(core, "COM_TEST", 115200, rep, off_cmd="0", off_expect=None)
    st, _n, detail = _Sink.find(rep, "收尾")
    assert st == "PASS"
    assert "无法验目标是否真的进了安全态" in detail


def test_safe_off_skips_when_port_cannot_be_opened(monkeypatch):
    _Sink(monkeypatch)
    core = FakeCore(open_ok=False)
    rep = _rep()
    la._safe_off(core, "COM_TEST", 115200, rep, off_cmd="0", off_expect=r"PWR OFF")
    st, _n, _d = _Sink.find(rep, "收尾")
    assert st == "SKIP"
    assert core.writes == []


# --------------------------------------------------------------------------- #
# 默认模板仍然自带安全态（别把能用的功能一起改坏）
# --------------------------------------------------------------------------- #

def test_default_probe_still_carries_its_own_safe_off():
    """内置模板必须仍然自动带上 '0'→PWR OFF，否则原来的用法会静默降级成 SKIP。"""
    assert la.DEFAULT_OFF == ("0", r"PWR OFF")
    assert la.DEFAULT_PROBE[1][0] == la.DEFAULT_OFF[0]
    assert la.DEFAULT_PROBE[1][1] == la.DEFAULT_OFF[1]


def test_default_off_is_only_auto_applied_for_the_builtin_template():
    """`probe is DEFAULT_PROBE` 这个判据是收尾自动带上安全态的唯一开关，
    钉住它：换成自定义探针后这个判据必须为假。"""
    class _Args(object):
        probe_file = None
        cmd = ["", ]
        expect = ["TICK.*"]
        probe_newline = False

    custom, _src = la._build_probe(_Args())
    assert custom is not la.DEFAULT_PROBE

    class _ArgsDefault(object):
        probe_file = None
        cmd = None
        expect = None
        probe_newline = False

    builtin, _src2 = la._build_probe(_ArgsDefault())
    assert builtin is la.DEFAULT_PROBE


# --------------------------------------------------------------------------- #
# 第 6 关：关内顺序（侵入性动作必须最后）
# --------------------------------------------------------------------------- #

def test_stage_swd_is_defined_and_returns_full_ran_flag():
    """stage_swd 必须返回 {full_ran: bool} —— main 靠它决定要不要打印"内核已放回"提示。
    旧版是让 main 自己拿 args.allow_write 猜，猜错了就会在没跑 full 档时也报一句。"""
    src = io.open(os.path.join(_TOOLS, "live_acceptance.py"),
                  encoding="utf-8").read()
    m = re.search(r"^def stage_swd\(.*?\n(.*?)\n(?=^#: 第 6 关)", src,
                  re.M | re.S)
    assert m, "没找到 stage_swd 函数体"
    body = m.group(1)
    assert "return {\"full_ran\": full_ran}" in body, "stage_swd 必须回报 full_ran"


def test_invasive_full_mode_is_last_inside_stage_swd():
    """full 档会把内核 halt 住，必须排在采集之后 —— 否则 stream_capture 采的是空气。"""
    src = io.open(os.path.join(_TOOLS, "live_acceptance.py"),
                  encoding="utf-8").read()
    body = src[src.index("def stage_swd("):src.index("#: 第 6 关的采集目标")]
    i_full = body.index("core.chip_info(mode=\"full\")")
    i_cap = body.index("core.stream_capture(")
    assert i_cap < i_full, "stream_capture 必须在 full 档之前（full 会 halt 内核）"


def test_stage6_runs_before_stage5_in_main():
    """第 6 关要排在第 5 关（会打死板子）之前，否则采到的时序没有意义。"""
    src = io.open(os.path.join(_TOOLS, "live_acceptance.py"),
                  encoding="utf-8").read()
    i6 = src.index("stage_swd(core, args.fw, rep, args.allow_write)")
    i5 = src.index("stage_destructive(core, args.port, args.baud, rep)")
    assert i6 < i5


def test_swd_stage_requires_explicit_flag_in_dry_run(monkeypatch):
    """dry-run 必须如实说第 6 关跑不跑 —— 不给 --swd 就说跳过。"""
    monkeypatch.setenv("MCU_DEBUG_MCP_MODE", "readonly")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = la.main(["COM_TEST", "--dry-run"])
    assert rc == 0
    assert "第 6 关 SWD 直连        : 跳过（需 --swd）" in out.getvalue()


def test_fw_without_swd_is_flagged_not_silently_ignored(monkeypatch):
    """给了 --fw 却没给 --swd：必须明说"这个文件不会被用上"，不能当没看见。"""
    monkeypatch.setenv("MCU_DEBUG_MCP_MODE", "readonly")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = la.main(["COM_TEST", "--dry-run", "--fw", "some.bin"])
    assert rc == 0
    assert "--fw 给了但没加 --swd" in out.getvalue()


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
