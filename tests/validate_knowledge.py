# -*- coding: utf-8 -*-
"""
mcu-debug-mcp · 知识库数据核验（validate_knowledge.py）

为什么存在：pin_check 的命是数据准。pinmap/码表/故障规则全靠人工录入，
录错一条，「幻觉照妖镜」就变成「幻觉放大器」。本脚本做机械校验：
- pinmap：结构完整性、封装引脚数、引脚号唯一、SWD 一致性、
  fixed_mux 引擎下默认功能全局唯一（F1 固定复用：一功能一脚是硬约束）
- error_codes：结构完整、meaning 非空
- fault_patterns：正则可编译（坏正则上线即废）、source/diagnosis/next_steps 非空
- boards：结构完整
- fault_bits：位域 bit 号不重叠不越界、meaning/hint 非空、severity 合法、
  segment 归属一致、valid_flag 不悬空引用、栈帧字序含 PC
- 条目级 source（全库通用）：pins / special / entries / onboard / registers /
  位域 / 栈帧，每条数据都要能回答「依据是什么」——可溯源就是这套库的护城河，
  缺一条就等于那一条没有出处，追问时答不上来

运行：python tests/validate_knowledge.py   （零第三方依赖，CI 可挂）
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB = os.path.join(ROOT, "knowledge")

ERRORS = []


def err(msg):
    ERRORS.append(msg)


def _require_source(items, fname, where):
    """条目级 source：每条数据都要能回答「依据是什么」。

    只查 dict 条目——列表型字段（caveats / board_issues / common_conflicts）无处挂
    source，由文件级 source 统一覆盖，不在这里报。
    """
    for k, v in (items or {}).items():
        if isinstance(v, dict) and not v.get("source"):
            err("%s: %s.%s 缺条目级 source（护城河要求每条数据可溯源）"
                % (fname, where, k))


def check_pinmap(path, fname):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    mcu = data.get("mcu", fname)
    for field in ("mcu", "package", "source", "pins"):
        if not data.get(field):
            err("%s: 缺必填字段 %s" % (fname, field))

    engine = data.get("engine")
    if engine not in ("fixed_mux", "af_table", "io_mux"):
        err("%s: engine 缺失或非法（%r），合法：fixed_mux/af_table/io_mux" % (fname, engine))

    # 封装引脚数约束
    pkg = (data.get("package") or "")
    m = re.search(r"(\d+)", pkg)
    if m:
        pkg_n = int(m.group(1))
        pins = data.get("pins", {})
        gpio_n = len(pins)
        special_refs = 0
        sp = data.get("special", {})
        power = sp.get("power_pins", {})
        for v in power.values():
            special_refs += len(v) if isinstance(v, list) else 1
        if gpio_n + special_refs > pkg_n:
            err("%s: GPIO %d + 特殊引用 %d > 封装 %d 脚，数字对不上" % (fname, gpio_n, special_refs, pkg_n))

    # 引脚号唯一
    seen_num = {}
    for pin, info in pins.items():
        num = info.get("num")
        if num is not None:
            if num in seen_num:
                err("%s: 引脚号 %d 重复（%s 与 %s）" % (fname, num, seen_num[num], pin))
            seen_num[num] = pin

    # SWD 一致性
    dbg = (data.get("special") or {}).get("debug") or {}
    swdio = dbg.get("SWDIO") or []
    swclk = dbg.get("SWCLK") or []
    for role, lst, want in (("SWDIO", swdio, "SWDIO"), ("SWCLK", swclk, "SWCLK")):
        for p in lst:
            info = pins.get(p)
            if info is None:
                err("%s: special.debug.%s 引用 %s 不在 pins 里" % (fname, role, p))
            elif not any(x.upper().startswith(want) for x in info.get("functions", [])):
                err("%s: %s 的 functions 里没有 %s" % (fname, p, want))

    # 功能唯一性（仅 fixed_mux：F1 固定复用，一功能一脚是硬约束）
    if engine == "fixed_mux":
        owner = {}
        for pin, info in pins.items():
            for fn in info.get("functions", []):
                key = fn.upper()
                if key in owner:
                    err("%s: 功能 %s 同时标在 %s 和 %s（fixed_mux 不允许，af_table 引擎才可多脚）"
                        % (fname, fn, owner[key], pin))
                else:
                    owner[key] = pin

    # ft 字段（io_mux 引擎全 3.3V，ft 可省；其余必须有）
    if engine != "io_mux":
        for pin, info in pins.items():
            if "ft" not in info:
                err("%s: %s 缺 ft（5V 容忍）字段" % (fname, pin))

    # common_conflicts
    if not data.get("common_conflicts"):
        err("%s: 缺 common_conflicts（至少写接线高频坑）" % fname)

    _require_source(data.get("pins"), fname, "pins")
    _require_source(data.get("special"), fname, "special")


def check_error_code(path, fname):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for field in ("id", "desc", "source", "entries"):
        if not data.get(field):
            err("%s: 缺必填字段 %s" % (fname, field))
    for code, v in (data.get("entries") or {}).items():
        if not isinstance(v, dict) or not v.get("meaning"):
            err("%s: 异常码 %s 缺 meaning" % (fname, code))
    _require_source(data.get("entries"), fname, "entries")


def check_fault_patterns(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("source"):
        err("fault_patterns.json: 缺顶层 source（文件级出处，与逐条 source 互补）")
    rules = data.get("rules") or []
    if not rules:
        err("fault_patterns.json: 没有任何规则")
    ids = set()
    for r in rules:
        rid = r.get("id")
        if not rid:
            err("fault_patterns.json: 有规则缺 id")
            continue
        if rid in ids:
            err("fault_patterns.json: 规则 id 重复 %s" % rid)
        ids.add(rid)
        pats = r.get("patterns") or []
        if not pats:
            err("fault_patterns.json: %s 缺 patterns" % rid)
        for p in pats:
            try:
                re.compile(p)
            except re.error as e:
                err("fault_patterns.json: %s 正则不可编译 %r（%s）" % (rid, p, e))
        if not r.get("source"):
            err("fault_patterns.json: %s 缺 source（领域级可信度要求逐条标注）" % rid)
        if not r.get("diagnosis") or not r.get("next_steps"):
            err("fault_patterns.json: %s 缺 diagnosis/next_steps" % rid)


def check_board(path, fname):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for field in ("board", "mcu", "source", "onboard"):
        if not data.get(field):
            err("%s: 缺必填字段 %s" % (fname, field))
    if not data.get("board_issues"):
        err("%s: 缺 board_issues（板级坑清单）" % fname)
    _require_source(data.get("onboard"), fname, "onboard")
    for key, v in (data.get("onboard") or {}).items():
        if not isinstance(v, dict):
            err("%s: onboard.%s 应为对象" % (fname, key))
            continue
        if not v.get("note"):
            err("%s: onboard.%s 缺 note（没 note 的外设条目价值减半）" % (fname, key))
        if not v.get("pins") and not v.get("pin"):
            err("%s: onboard.%s 缺 pins（列表）或 pin（单值）——引脚占用无法做 pin_check 交叉检查" % (fname, key))
        elif v.get("pin") and v.get("pins"):
            err("%s: onboard.%s 同时有 pin 和 pins，二选一" % (fname, key))


def check_fault_bits(path, fname):
    """校验 fault 位域表。

    为什么单列一类：fault 解码的命和 pinmap 一样靠数据准，而且多一类特有陷阱——
    位域比特号重复（两个名字占同一位）、位段越界、valid_flag 指向不存在的位，
    这些错上线后会直接给出【错的诊断结论】，比没诊断更糟。
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for field in ("id", "desc", "arch", "source", "registers"):
        if not data.get(field):
            err("%s: 缺必填字段 %s" % (fname, field))

    regs = data.get("registers") or {}
    if not regs:
        err("%s: registers 为空" % fname)
        return
    _require_source(regs, fname, "registers")

    # 收集全部寄存器里「位域名 -> (寄存器, bit)」，供 valid_flag 交叉引用检查
    all_bits = {}

    for reg_name, reg in regs.items():
        if not isinstance(reg, dict):
            err("%s: registers.%s 应为对象" % (fname, reg_name))
            continue

        width = reg.get("width", 32)
        if not isinstance(width, int) or width <= 0:
            err("%s: %s.width 非法（%r）" % (fname, reg_name, width))
            width = 32

        # 寄存器自身要有地址（要么 addr_offset，要么 address）
        if not reg.get("addr_offset") and not reg.get("address"):
            err("%s: %s 缺 addr_offset/address" % (fname, reg_name))

        if not reg.get("desc"):
            err("%s: %s 缺 desc" % (fname, reg_name))

        bits = reg.get("bits") or {}
        if not bits:
            err("%s: %s 缺 bits（没有位域定义）" % (fname, reg_name))
            continue
        _require_source(bits, fname, "%s.bits" % reg_name)

        occupied = {}   # bit 号 -> 位域名
        for bit_name, info in bits.items():
            if not isinstance(info, dict):
                err("%s: %s.%s 应为对象" % (fname, reg_name, bit_name))
                continue

            bit = info.get("bit")
            if not isinstance(bit, int):
                err("%s: %s.%s 缺 bit 或 bit 非整数（%r）" % (fname, reg_name, bit_name, bit))
                continue
            if bit < 0 or bit >= width:
                err("%s: %s.%s 的 bit=%d 超出 %d 位宽度"
                    % (fname, reg_name, bit_name, bit, width))
                continue

            w = info.get("width", 1)
            if not isinstance(w, int) or w <= 0:
                err("%s: %s.%s 的 width 非法（%r）" % (fname, reg_name, bit_name, w))
                w = 1
            if bit + w > width:
                err("%s: %s.%s 位段 [%d,%d] 越出 %d 位"
                    % (fname, reg_name, bit_name, bit, bit + w - 1, width))

            # 逐位检查重叠（这是 fault 表最致命的错：两个名字解释同一位）
            for b in range(bit, min(bit + w, width)):
                if b in occupied:
                    err("%s: %s 的 bit %d 被 %s 和 %s 同时占用（位域重叠，诊断会给出冲突结论）"
                        % (fname, reg_name, b, occupied[b], bit_name))
                else:
                    occupied[b] = bit_name

            # 查重：同名位域在不同寄存器里可以，同一寄存器内不行（上面重叠检查已覆盖）
            all_bits.setdefault("%s.%s" % (reg_name, bit_name), (reg_name, bit))
            all_bits.setdefault(bit_name, (reg_name, bit))

            # 必有 meaning（诊断的主体）
            if not info.get("meaning"):
                err("%s: %s.%s 缺 meaning" % (fname, reg_name, bit_name))

            # 必须有 hint——没有排查方向的位域等于没做诊断
            if not info.get("hint"):
                err("%s: %s.%s 缺 hint（没有排查方向，诊断价值减半）"
                    % (fname, reg_name, bit_name))

            severity = info.get("severity")
            if severity not in ("high", "medium", "low", "info"):
                err("%s: %s.%s 的 severity 非法（%r），合法：high/medium/low/info"
                    % (fname, reg_name, bit_name, severity))

            # segment 若声明，必须落在该寄存器的 segments 范围内
            seg = info.get("segment")
            if seg:
                segs = reg.get("segments") or {}
                if seg not in segs:
                    err("%s: %s.%s 声明 segment=%s，但 %s.segments 里没有它"
                        % (fname, reg_name, bit_name, seg, reg_name))
                else:
                    rng = segs[seg].get("range") or []
                    if len(rng) == 2 and not (rng[0] <= bit <= rng[1]):
                        err("%s: %s.%s 的 bit=%d 不在 segment %s 的范围 %s 内"
                            % (fname, reg_name, bit_name, bit, seg, rng))

        # segments 声明必须与 bits 的 segment 使用一致
        segs = reg.get("segments") or {}
        if segs:
            used = set()
            for info in bits.values():
                if isinstance(info, dict) and info.get("segment"):
                    used.add(info["segment"])
            for u in used:
                if u not in segs:
                    err("%s: %s 的位域用到未声明的 segment %s" % (fname, reg_name, u))
            declared_range = {}
            for sname, sinfo in segs.items():
                rng = sinfo.get("range")
                if not (isinstance(rng, list) and len(rng) == 2):
                    err("%s: %s.segments.%s 的 range 非法（%r）" % (fname, reg_name, sname, rng))
                    continue
                if rng[0] > rng[1]:
                    err("%s: %s.segments.%s 的 range 起止反了（%r）" % (fname, reg_name, sname, rng))
                for b, oname in occupied.items():
                    if rng[0] <= b <= rng[1]:
                        if b in declared_range and declared_range[b] != sname:
                            err("%s: %s 的 bit %d 同时落在 segment %s 和 %s"
                                % (fname, reg_name, b, declared_range[b], sname))
                        declared_range[b] = sname

    # valid_flag 交叉引用：BFAR.valid_flag 必须能在 CFSR 里找到对应位域名
    for reg_name, reg in regs.items():
        if not isinstance(reg, dict):
            continue
        vf = reg.get("valid_flag")
        if not vf:
            continue
        # valid_flag 形如 "BFARVALID（CFSR 的 bit15）"——取括号前的中文/英文名
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", vf)
        flag_name = m.group(1) if m else None
        if not flag_name:
            err("%s: %s.valid_flag 无法解析出位域名（%r）" % (fname, reg_name, vf))
            continue
        if flag_name not in all_bits:
            err("%s: %s.valid_flag 引用的 %s 在任何寄存器的位域里都不存在（悬空引用，"
                "校验时会给不出「有效位」判断）" % (fname, reg_name, flag_name))

    # 栈帧定义若存在，必须给出标准帧的字序（否则回溯的工具没法实现）
    sf = data.get("stack_frame")
    if sf:
        _require_source(sf, fname, "stack_frame")
        std = sf.get("standard_frame")
        if not isinstance(std, dict) or not std.get("words"):
            err("%s: stack_frame.standard_frame 缺 words（栈帧字序）" % fname)
        elif "PC" not in std["words"]:
            err("%s: stack_frame.standard_frame.words 里没有 PC（回溯的落点就是 PC）" % fname)


def check_drivers():
    """驱动模板库（ROOT/drivers/）完整性。

    为什么在这儿查：driver_get / mcu://driver/{name} 把目录里的文件原样喂给用户。
    少 meta.json 就是「这个驱动还没写全」，少源码就是空壳——但目录还在、
    driver_list 照样列出来，用户从头到尾不会知道里面是空的。
    """
    ddir = os.path.join(ROOT, "drivers")
    if not os.path.isdir(ddir):
        err("drivers/ 目录不存在（预验证驱动模板库是 README 承诺的能力）")
        return
    names = sorted(d for d in os.listdir(ddir) if os.path.isdir(os.path.join(ddir, d)))
    if not names:
        err("drivers/ 下没有任何驱动目录")
    for name in names:
        d = os.path.join(ddir, name)
        files = os.listdir(d)
        mp = os.path.join(d, "meta.json")
        if not os.path.isfile(mp):
            err("drivers/%s: 缺 meta.json（driver_get 会把它判成没写全）" % name)
        else:
            try:
                with open(mp, encoding="utf-8") as fh:
                    m = json.load(fh)
            except Exception as e:
                err("drivers/%s/meta.json: JSON 解析失败 %s" % (name, e))
            else:
                for k in ("name", "desc", "verified"):
                    if not m.get(k):
                        err("drivers/%s/meta.json: 缺字段 %s" % (name, k))
                if m.get("name") and m["name"] != name:
                    err("drivers/%s: meta.name=%r 与目录名不一致" % (name, m["name"]))
        if not [f for f in files if f.endswith((".h", ".c"))]:
            err("drivers/%s: 没有任何 .h/.c 源码（只有说明没有代码 = 空壳驱动）" % name)


def main():
    pinmap_dir = os.path.join(KB, "pinmap")
    for f in sorted(os.listdir(pinmap_dir)) if os.path.isdir(pinmap_dir) else []:
        if f.endswith(".json"):
            check_pinmap(os.path.join(pinmap_dir, f), f)

    ec_dir = os.path.join(KB, "error_codes")
    for f in sorted(os.listdir(ec_dir)) if os.path.isdir(ec_dir) else []:
        if f.endswith(".json"):
            check_error_code(os.path.join(ec_dir, f), f)

    fp = os.path.join(KB, "fault_patterns.json")
    if os.path.exists(fp):
        check_fault_patterns(fp)
    else:
        err("fault_patterns.json 不存在")

    bd_dir = os.path.join(KB, "boards")
    for f in sorted(os.listdir(bd_dir)) if os.path.isdir(bd_dir) else []:
        if f.endswith(".json"):
            check_board(os.path.join(bd_dir, f), f)

    fb_dir = os.path.join(KB, "fault_bits")
    for f in sorted(os.listdir(fb_dir)) if os.path.isdir(fb_dir) else []:
        if f.endswith(".json"):
            check_fault_bits(os.path.join(fb_dir, f), f)

    check_drivers()

    print("知识库核验：%d 项错误" % len(ERRORS))
    for e in ERRORS:
        print("  ✗ %s" % e)
    return 0 if not ERRORS else 1


if __name__ == "__main__":
    sys.exit(main())
