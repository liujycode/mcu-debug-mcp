# -*- coding: utf-8 -*-
"""变异验证：把四个修复处的 bug 种回去，对饮测试必须变红，还原后 md5 一致。

纪律来源：本项目「改代码 → 测试必须变红 → 还原」的判别力验证（曾靠它抓出
旧 SPI 夹具 6 位掩码形同虚设的假绿）。本次验证 2026-10-13 四个修复：
  M1 _bridge_lines 含未终结尾段（半包当完整应答）
  M2 _at_line_match 含未终结尾段（半包假 PASS）
  M3 flash_verify 失败分支不置 seg_ok（回读失败也报"完全一致"）
  M4 _det_sensor_ff 无语义守卫（WiFi 强度误判数据线悬空）
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
# 本脚本随 tools/ 进包后位于 src/mcu_debug_mcp/tools/，仓库根要往上四级
PY = sys.executable

TARGETS = [
    ("M1 _bridge_lines 半包", "src/mcu_debug_mcp/core.py",
     '    for ln in parts[:-1]:                       # 丢掉未终结的尾段',
     '    for ln in parts:',
     "tests/test_bus_tools.py::BridgeHandshakeTest::test_unterminated_reply_is_not_taken_as_complete"),
    ("M2 _at_line_match 半包", "src/mcu_debug_mcp/core.py",
     '    for raw in parts[:-1]:\n        line = raw.strip()\n        if line and rx.fullmatch(line):\n            return True, line\n    return False, ""',
     '    for raw in parts:\n        line = raw.strip()\n        if line and rx.fullmatch(line):\n            return True, line\n    return False, ""',
     "tests/test_hw_findings.py::TestOreTrapHints::test_at_line_match_ignores_unterminated_tail"),
    ("M3 flash_verify 假一致", "src/mcu_debug_mcp/core.py",
     '            if not seg_ok:\n                # 2026-10-13：此前这里只判 seg_bad == 0，回读失败也打印\n                # "✓ 完全一致" —— 校验工具给假通过（探针掉线也报固件已正确烧入）。\n                lines.append("  段%d @0x%08X  %6d 字节  ？ 未能校验（回读失败，"\n                             "不能认定一致）" % (idx, addr, len(data)))\n            elif seg_bad == 0:',
     '            if seg_bad == 0:',
     "tests/test_probe_direct.py::FlashVerifyTest::test_read_failure_is_reported_not_treated_as_match"),
    ("M4 SENSOR_FF 无守卫", "src/mcu_debug_mcp/analyzer.py",
     '    wordy = re.findall(\n        r"(?im)^(?![^\\n]*(?:strength|rssi|snr|signal|count|queue|level|dropped"',
     '    wordy = re.findall(\n        r"(?im)^[^\\n\\d-]*(?:255|65535|-1|0[xX][fF]{1,2})\\b',
     "tests/test_analyzer.py::TestSensorFF::test_wifi_strength_not_misjudged"),
    # ↓ 2026-10-13 符号反查轮新增：这三个变异对应本轮修的假名/假结论路径。
    #   起因是首次给 core.py 跑覆盖率发现 _parse_elf / _parse_map_* 大片零覆盖，
    #   用真工具链夹具一测就撞出「GNU ld 的 map 没有符号表 → 反查给假函数名」。
    ("M5 稀疏 map 静默（不给 elf 提示）", "src/mcu_debug_mcp/core.py",
     '    if nsyms >= 10:\n        return None',
     '    if nsyms >= 0:\n        return None',
     "tests/test_symbol_lookup.py::TestGnuMapSymbolScarcity::test_sibling_elf_detected"),
    # ★ M6 变异点改过三次（2026-10-13），三次失败各暴露一件事，值得记下来：
    #   ① 改 spans 的长度（a + sz → a + 0x10000000）——无效。改完后 .text 的
    #      span 反而把 .data 地址排除在外，_seg_of 返回 None，仍被「段外不推断」
    #      挡住，测试照样绿。**变异点选错，测的是别的东西。**
    #   ② 改 _seg_of 让它永远返回 .text——仍无效。跨段保护是【两层冗余】的：
    #      一层比「本符号与下一个是否同段」，另一层比「推断值是否超出所属段长度」。
    #      只杀第一层，第二层照样把离谱值拦下。
    #   ③ 现在同时打掉两层，才是「跨段保护整体失效」这个真实故障：
    #      推断出 0x17FFD000 字节的函数——正是注释里写明要防的那种离谱数字。
    #   ★ 教训：变异验证报「没判别力」时，先问"是不是另有冗余保护在兜底"，
    #     别急着改测试——测试可能是对的，变异才是那个测错东西的。
    ("M6 跨段推断被打开", "src/mcu_debug_mcp/core.py",
     '        if spans:\n'
     '            sa, sn = _seg_of(a), _seg_of(nxt)\n'
     '            if sa is not None and sa != sn:\n'
     '                continue                  # 跨段 → 差值不是符号大小，不推断\n'
     '            if sa is None:\n'
     '                continue                  # 落在所有已知段之外，不敢推断\n'
     '        # ★上限保护：单个符号推断值不能超过所属段大小（防跨段/对齐造成的离群值）\n'
     '        if spans:\n'
     '            over = False\n'
     '            for s_a, s_end, _nm in spans:\n'
     '                if s_a <= a < s_end and gap > (s_end - s_a):\n'
     '                    over = True\n'
     '                    break\n'
     '            if over:\n'
     '                continue',
     '        if False:\n'
     '            sa, sn = _seg_of(a), _seg_of(nxt)\n'
     '            if sa is not None and sa != sn:\n'
     '                continue\n'
     '            if sa is None:\n'
     '                continue\n'
     '        if False:\n'
     '            over = False\n'
     '            for s_a, s_end, _nm in spans:\n'
     '                if s_a <= a < s_end and gap > (s_end - s_a):\n'
     '                    over = True\n'
     '                    break\n'
     '            if over:\n'
     '                continue',
     "tests/test_symbol_lookup.py::TestInferSizesGuards::test_no_cross_segment_inference"),
    ("M7 bits 字段当区间吞掉", "src/mcu_debug_mcp/core.py",
     '                if b - a <= 16:\n'
     '                    lsb, w = a, b - a + 1\n'
     '                else:\n'
     '                    lsb, w = a, b',
     '                if True:\n'
     '                    lsb, w = a, b - a + 1\n'
     '                else:\n'
     '                    lsb, w = a, b',
     "tests/test_symbol_lookup.py::TestRegisterBitFieldParsing::test_bits_pos_width_form"),

    ("M8 IAR 千位空格汇总读错大小", "src/mcu_debug_mcp/core.py",
     '        m = re.match(r"\\s*((?:\\d[ \\d]*\\d|\\d))\\s+bytes\\s+of\\s+(readonly|readwrite)\\s+(\\S+)", line)\n        if m:\n            seg_map["%s_%s" % (m.group(2), m.group(3))] = {\n                "addr": 0, "size": int(m.group(1).replace(" ", ""))}',
     '        m = re.match(r"\\s*(\\d+)\\s+bytes\\s+of\\s+(readonly|readwrite)\\s+(\\S+)", line)\n        if m:\n            seg_map["%s_%s" % (m.group(2), m.group(3))] = {\n                "addr": 0, "size": int(m.group(1))}',
     "tests/test_artifact_parsers.py::TestIarMapParser::test_bytes_of_summary_lines"),
    ("M9 ihex 校验和被跳过", "src/mcu_debug_mcp/core.py",
     '        if (sum(b) & 0xFF) != 0:',
     '        if False and (sum(b) & 0xFF) != 0:',
     "tests/test_artifact_parsers.py::TestParseIhex::test_bad_checksum_rejected"),
    ("M10 modbus 字节数不校验", "src/mcu_debug_mcp/core.py",
     '        if n == 0 or n % 2 != 0 or n != len(regs):',
     '        if False:',
     "tests/test_diag_branches.py::TestModbusBranches::test_fc3_bytecount_mismatch_is_flagged"),
]



def md5(p):
    with open(p, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def run(test):
    r = subprocess.run([PY, "-m", "pytest", test, "-q", "--no-header", "-x"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    return r.returncode, (r.stdout or "")[-300:]


def main():
    all_ok = True
    for name, rel, good, bad, test in TARGETS:
        path = os.path.join(ROOT, rel)
        # 字节级安全三原则（2026-10-13 踩坑后定）：
        # ① 二进制读写——Windows 文本模式会把 \n 写成 \r\n，还原后 md5 必变；
        # ② 匹配前把 \r\n 归一成 \n（锚点是 \n 风格），写回时恢复文件原有换行符；
        # ③ 还原直接写原始字节，不做任何解码/编码往返之外的处理。
        raw = open(path, "rb").read()
        src = raw.decode("utf-8")
        eol = "\r\n" if "\r\n" in src else "\n"
        norm = src.replace("\r\n", "\n")
        if good not in norm:
            print("[SKIP] %s：锚点没找到（代码已变？）" % name)
            all_ok = False
            continue
        before = md5(path)
        try:
            mutated = norm.replace(good, bad, 1)
            out = mutated.replace("\n", eol) if eol == "\r\n" else mutated
            open(path, "wb").write(out.encode("utf-8"))
            rc, tail = run(test)
            if rc != 0:
                print("[PASS] %s：种回 bug 后测试变红 ✓（判别力在）" % name)
            else:
                print("[FAIL] %s：种回 bug 测试还绿 —— 测试没有判别力！" % name)
                print("       " + test)
                all_ok = False
        finally:
            open(path, "wb").write(raw)
            after = md5(path)
        if before != after:
            print("[FAIL] %s：还原后 md5 不一致（%s → %s）" % (name, before, after))
            all_ok = False
    print()
    print("变异验证：%s" % ("全部通过（bug 种回必红 + 字节级还原）" if all_ok else "有问题，见上"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
