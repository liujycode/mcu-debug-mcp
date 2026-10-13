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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

TARGETS = [
    ("M1 _bridge_lines 半包", "core.py",
     '    for ln in parts[:-1]:                       # 丢掉未终结的尾段',
     '    for ln in parts:',
     "tests/test_bus_tools.py::BridgeHandshakeTest::test_unterminated_reply_is_not_taken_as_complete"),
    ("M2 _at_line_match 半包", "core.py",
     '    for raw in parts[:-1]:\n        line = raw.strip()\n        if line and rx.fullmatch(line):\n            return True, line\n    return False, ""',
     '    for raw in parts:\n        line = raw.strip()\n        if line and rx.fullmatch(line):\n            return True, line\n    return False, ""',
     "tests/test_hw_findings.py::TestOreTrapHints::test_at_line_match_ignores_unterminated_tail"),
    ("M3 flash_verify 假一致", "core.py",
     '            if not seg_ok:\n                # 2026-10-13：此前这里只判 seg_bad == 0，回读失败也打印\n                # "✓ 完全一致" —— 校验工具给假通过（探针掉线也报固件已正确烧入）。\n                lines.append("  段%d @0x%08X  %6d 字节  ？ 未能校验（回读失败，"\n                             "不能认定一致）" % (idx, addr, len(data)))\n            elif seg_bad == 0:',
     '            if seg_bad == 0:',
     "tests/test_probe_direct.py::FlashVerifyTest::test_read_failure_is_reported_not_treated_as_match"),
    ("M4 SENSOR_FF 无守卫", "analyzer.py",
     '    wordy = re.findall(\n        r"(?im)^(?![^\\n]*(?:strength|rssi|snr|signal|count|queue|level|dropped"',
     '    wordy = re.findall(\n        r"(?im)^[^\\n\\d-]*(?:255|65535|-1|0[xX][fF]{1,2})\\b',
     "tests/test_analyzer.py::TestSensorFF::test_wifi_strength_not_misjudged"),
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
