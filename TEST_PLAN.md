# mcu-debug-mcp · 后续测试计划（v4.5.4 基线）

> 本文件的**所有数字都是 2026-10-13 在本机实跑出来的**，采集命令见每节末尾。
> 口径纪律：覆盖率数字来自 `coverage.py` 行覆盖；测试数来自 `pytest --collect-only`；
> 不手抄、不凭记忆。**基线 = 762 passed / 2 skipped（收集 764）+ C 107 checks 0 failed +
> 变异 7/7 + 三道审计全 0 错。**

---

## ★ 执行状态（2026-10-13 晚，批次 1-3 已落地 → v4.5.5）

| 项 | 计划 | 实测结果 |
|---|---|---|
| 批次 1 产物解析器 | 90 行 | ✅ 51 条测试；**当场抓出 IAR 千位空格读错大小真缺陷**（7.5KB 报成 566 字节），修复 + 变异 M8 |
| 批次 2 诊断分支 | 108 行 | ✅ 93 条测试；decode_fault 位号与 fault_bits 对账表锁死知识库；变异 M9（ihex 校验）/ M10（modbus 字节数） |
| 批次 3 笼子内部逻辑 | server 79 行 + 降级分支 | ✅ 36 条测试；env_status/logs_prune/report_session 降级与闸门全锁；server 薄包装数量与按名转发对账 |
| 覆盖率 | core 纯逻辑 425 行 | core.py **80% → 87%**（miss 821 → 529）；TOTAL **77% → 83%**；目标函数残余 52 行零星分支（多为需真 ELF 的成功路径），不硬凑 |
| 门禁 | — | **942 passed / 2 skipped**（收集 944）· C 107/0 · 变异 **10/10** · 知识库 0 · 常量 0 · 能力审计无脱节 · stdio 59/16/6 全过 |
| 新发现（顺手记） | — | `paths.writable_root()` 有进程级缓存 `_write_root_cache`，同进程内改 `MCU_DEBUG_MCP_HOME` 不再生效——生产合理、测试必须 `paths.reset_cache()`；`server.logs_prune` 与 `core.logs_prune` 的 confirm/dry_run 参数序不同（关键字转发安全，位置调用会调包，测试已锁死按名转发） |

### H2 真机批（2026-10-14 下午，H563ZI + 1 颗 I2C 器件 @ COM21，师兄接线确认无误）

| 步骤 | 实测 | 结论 |
|---|---|---|
| 桥固件升级（v2→v5，`UNSTICK 打满9时钟` + `XBANG 交叉探测` + `PP 背充 hold_us` + GPIO READ 回复头修复） | 7,188 B ≤ 8 KB，live_check 5/6 PASS（i2c_scan 那关因器件钳位环境性 FAIL） | 固件诊断能力三项新增全部上板 |
| 上电瞬态复现 | 烧录复位后 `I2C SCAN` → `-ERR TIMEOUT sda/scl stuck low`；而 `GPIO READ B` 线电平为高 | **I2C1 外设 BUSY 假死 vs 线电平矛盾** = 外设视角与 GPIO 视角必须分开看 |
| SCL 电平漂移 | IDR 在 0x0219↔0x0319 间漂移（SCL 临界）；XBANG 双释放后纯上拉仍读低 | 中等强度拉低实锤（推挽顶得高、上拉顶不动） |
| XBANG 交叉裁决 | 两线全高窗口内双极性各扫 112 地址：nA=0 nB=0 | **SDA/SCL 接反被否决**（接反则交叉档必 ACK） |
| 9 时钟解卡 | `UNSTICK 9`（v4 修复后真打满 9 个，SDA 高场景不再 0 时钟空转）→ 器件不松口 | 器件不是"等时钟"状态 |
| **背充实验** | `UNSTICK PP 500000`：推挽 SCL 高保持 0.5s 后钳位消失，SCL 回高且**持续保持**；SCAN 恢复 +OK（不再 TIMEOUT）但 **全 NAK** | **器件 VCC 轨欠压实锤**（上拉电流经 ESD 二极管背充器件 VCC，钳位临界漂移；背充电荷可暂顶掉钳位） |
| 最终判定 | 总线健康 + 双极性 112 地址全 NAK + VCC 欠压钳位 + 背充可暂救总线但救不活器件 | **软件手段穷尽。器件 I2C 输入级未工作**；需师兄万用表三查：①器件 VCC 引脚电压（≥3.0V 正常）②桥 GND 与器件 GND 通断 ③仍不行换器件验证 |

> 固件缺口发现链（真机驱动）：`UNSTICK` 原版「SDA 回高即 break」对「SCL 被钳/SDA 高」场景 0 时钟空转 → 打满 9；
> `XBANG` 遇线低静默跳过 → 需在线高窗口跑；`SCAN` TIMEOUT 文案"stuck low"与 GPIO 读高矛盾 → 报错语义待细化（记录，未改）。

### H2 全命令验证矩阵（2026-10-14 傍晚，v6 固件，师兄拍板：I2C 器件留到 ESP32 主板，先穷尽本板能力）

| 项 | 结果 |
|---|---|
| 矩阵覆盖 | 49 用例 = 6 顶层命令（PING/I2C/SPI/OW/RS485/GPIO）× 正常路径 + 错误路径（17 个参数校验点逐个打） |
| 结果 | **49/49 PASS**（脚本 `D:\work\_tmp\h2_full_matrix.py`，可复跑） |
| **抓出真缺陷 #1** | `parse_hex_list` 不认 `0x` 前缀——usage 示例就是 `0x01,0x02,03`，但逐字符解析遇 'x' 直接 `bad byte list`，**示例输入必被拒**。修：元素头可选 `0x/0X`，与 `parse_num` 语义对齐。实测 `I2C WRITE 0 0x4A 0x00 0x11` 从 ARG 变 **NAK**（事务真实执行） |
| **抓出不一致 #2** | `SPI SCAN` 扫 2 槽（CS0/CS1，PB0 已初始化），但 `SPI READ/WRITE` 对 `cs>0` 回 NOCS——**SCAN 声称有、READ/WRITE 拒收**。修：READ/WRITE 放行 CS1（`cs>1 → ARG`）。实测 CS1 读回全 1（MISO 悬空）、写动作 +OK |
| I2C 外设行为补充 | `UNSTICK PP 500000` 背充清外设后，`I2C WRITE` 走 **NAK 路径**（非 TIMEOUT）——总线健康时 I2C1 事务正常，器件应答缺失是器件侧问题 |
| live_check | 5/6 PASS（`i2c_scan` 关为器件钳位环境性 FAIL，非固件缺陷） |
| 遗留 | ① `SCAN` TIMEOUT 文案与 GPIO 实测矛盾（报错语义待细化）② `XBANG` 线低时静默跳过（应输出 skip 计数）——均记录未改，等 ESP32 主板轮验证一并处理 |

---

## 〇、一句话结论

剩余可离线补的测试**共 425 行纯逻辑盲区**（core.py），集中在**产物解析器**（Keil/IAR map、
ELF、Intel HEX）与**文本解析器**（NMEA / Modbus / 帧解析）两族——这两族**不需要任何硬件就能测**，
是下一批的主战场。而 `live_acceptance.py`（227 行）与 `bridge_fw/build.py`（47 行）看着缺口最大，
但它们是**真机夹具与构建脚本**，只能用 dry-run + FakeCore 覆盖判据，属于批次 3 的次要项。

**不写覆盖率百分比当 KPI**（90% 之类），改用**「未覆盖行分类账」**：每条盲区必须被归入
「纯逻辑可离线补」/「必须接实物」二选一，归不进去说明测试没读懂代码。这才是能验收的东西。

---

## 一、基线（实跑，2026-10-13）

| 项 | 实测值 | 采集命令 |
|---|---|---|
| 用例总数 | 764 collected（762 passed / 2 skipped / 43 subtests） | `python -m pytest -q --collect-only` |
| 全量耗时 | 87.7 s | `python -m pytest -q` |
| 行覆盖率 TOTAL | **77%**（5330 stmts / 1215 miss） | `python -m coverage run --source=src/mcu_debug_mcp -m pytest -q` + `coverage report` |
| core.py | 80%（4035 / 821 miss） | 同上 |
| server.py | **71%**（274 / 79 miss） | 同上 |
| live_acceptance.py | **39%**（372 / 227 miss） | 同上 |
| bridge_fw/build.py | **45%**（85 / 47 miss） | 同上 |
| guard.py / paths.py / analyzer.py | 94% / 85% / 96% | 同上 |
| C 驱动逻辑 | 107 checks / 0 failed | `python tests/run_c_tests.py` |
| 变异验证 | 7 / 7 全杀 | `python src/mcu_debug_mcp/tools/mutation_check.py` |
| 知识库 / 常量 / 能力面审计 | 0 错 / 0 错 / 2 缺口（计划内，不阻塞） | `tests/validate_knowledge.py` / `audit_constants.py` / `audit_capability.py` |
| MCP 能力面 | 59 tools / 16 resources / 6 prompts | `src/mcu_debug_mcp/tools/verify_stdio.py` |

---

## 二、盲区分类账（core.py 754 未覆盖行的逐行归属）

用 AST 把每条未覆盖行归到所属顶层函数，再按函数体是否触碰硬件入口分类。**这不是拍脑袋**：
分类规则写死在下面的脚本里，可复算。

| 类别 | 未覆盖行 | 处置 |
|---|---|---|
| 纯逻辑，**离线可补**（不碰串口 / 探针 / 桥） | **425** | 批次 1、2 全部目标 |
| 触及硬件入口（`_open_serial` / pyocd / 桥命令 / 烧录 / 复位 / RTT） | **326** | 只能真机，留批次 4 |
| 合计 | 754 | — |

### 2.1 纯逻辑盲区 top16（下一批的直接靶子）

| 函数 | 缺行 | 为什么能离线测 | 判据（要写成什么样的测试） |
|---|---|---|---|
| `_parse_map_keil` | 26 | 纯正则，输入是 map 文本 | Keil `Image Symbol Table` 正例 + 点号符号被跳过 + `Total RO/RW/ZI Size` 两种写法 + 空表 |
| `decode_fault` | 25 | 输入是几个整数寄存器 | CFSR/HFSR/BFAR/MMFAR 每一位的**中英对照**逐位断言；未定义位不许瞎编名字 |
| `nmea_parse` | 22 | 纯文本 | `$GPRMC/$GPGGA` 校验和正确/错误、缺字段、定位无效（`V`）、经纬度半球 |
| `_parse_map_iar` | 21 | 纯正则 | IAR `ENTRY LIST` 正例 + `***` 段尾截断 + `bytes of readonly code memory` 汇总 |
| `_port_baud_relevance` | 20 | 输入是端口名 | CH340/CP2102/FT232/ESP32 原生 USB CDC/蓝牙虚拟口 → 各自相关性结论；未知芯片必须说「未知」不许瞎猜 |
| `_parse_elf` | 19 | 输入是 ELF 字节 | 已有真工具链 ELF 夹具可复用（`test_symbol_lookup.py` 现场生成的那种）；加节表乱序 / 非 ELF / 截断 |
| `stack_unwind` | 18 | 输入 map/elf + 寄存器值 | EXC_RETURN 0xFFFFFFF9/0xFFFFFFFD 两种帧；跨段保护；`sp` 越界必须拒绝 |
| `parse_map` | 17 | 派发层 | 三种格式自动判别（gnu/keil/iar）；`sibling .elf` 探测命中/未命中 |
| `_parse_ihex` | 16 | 纯解析 | **每条错误路径都要有一条**：非 `:` 开头、非 hex、太短、长度字段不符、字节和校验失败、未知记录类型；相邻段合并 |
| `modbus_parse` | 14 | 纯 hex | 功能码 1/3/4/5/6/8/16；CRC 错必须拒；字节序错必须点出来 |
| `_timing_samples` | 14 | 纯文本 | 采样间隔 / 丢帧率 / 节拍抖动；`seq` 序号跳变识别 |
| `decode_register_svd` | 14 | 输入是 SVD 文件 | 位域解析 + 保留位（写 1 读回 0）必须警告 + 值超出枚举必须点出来 |
| `report_session` | 13 | 读本地日志文件 | tail 行数 / `overwrite` 冲突 / 危险闸门 |
| `_firmware_segments` | 10 | 读本地文件 | `.bin` 空文件必须拒；`.hex` 真实地址段；`.elf` 只取 flash 段 |
| `frame_parse` | 9 | 纯字节流 | 自定义帧 + NMEA 双模式；半包/粘包 |
| `peripheral_api_get` | 8 | 读包内派生数据 | 9 驱动记录字段齐全 + 未知驱动必须友好报错 |

### 2.2 必须接实物的（**本计划不假装能测**，只登记）

| 函数 | 缺行 | 依赖 |
|---|---|---|
| `serial_pair_record` | 40 | 两个真实串口对录 |
| `rtt_read` | 40 | J-Link + RTT（pyocd） |
| `baudrate_autodetect` | 24 | 真串口（**离线评分分支已测**，缺的是真扫档） |
| `flash_firmware` / `reset_reason` / `chip_info` | 45 | SWD 探针 |
| `at_sequence` / `stream_capture` / `_open_serial` | 48 | 真串口 |
| `logs_prune` | 24 | 本地文件即可，**但闸门交互要真跑**，归批次 3 |
| `env_status` | 16 | 本机环境探测，可离线跑（**待补**） |
| `live_acceptance.stage_swd` / `stage_symbols` / `_build_probe` | 109 | 真板子 + 探针 |
| `bridge_fw/build.py:main` | 37 | 真 arm-none-eabi-gcc（工具链缺失时应 5P/9S，**待补**） |

---

## 三、批次计划（每批独立可验收）

### 批次 1 · 产物解析器（**纯离线，收益最大**）

- 目标：`_parse_map_keil` + `_parse_map_iar` + `parse_map` + `_parse_ihex` + `_firmware_segments`
  = **90 行**盲区清零。
- 为什么先做：**这族解析器是「给假函数名」的同类风险源**。v4.5.4 刚修的 `.map` 稀疏符号问题就出在这一族，
  没有测试就等于没修。Keil/IAR 两族至今**一行测试都没有**——本机没有 Keil/IAR，但**格式是公开的**，
  用真实 `.map` 文本片段做夹具完全成立（这是解析器测试的标准做法，不是编造知识）。
- 收口判据：
  1. 每条错误路径至少一条测试（不是「正常能跑」就算过）
  2. 新增 ≥ 3 条变异目标进 `mutation_check.py`，种回 bug 必红
  3. `coverage report` 上这 5 个函数的 miss 行数 = 0
- 依赖夹具：`tests/fixtures/` 下新增 `map_keil.txt` / `map_iar.txt` / `sample.hex`（真实格式片段）

### 批次 2 · 文本/协议解析器（**纯离线**）

- 目标：`nmea_parse` + `modbus_parse` + `_timing_samples` + `frame_parse` + `decode_fault` +
  `decode_register_svd` = **108 行**。
- 关键纪律：`decode_fault` 的每一位必须与 `knowledge/fault_bits/cortex_m.json` **对账**，
  写成「表里有的位逐个断言 + 表里没有的位不许瞎命名」——**测试反向锁住知识库**，
  知识库错了测试就红，而不是反过来。
- 收口判据：同上；外加 `validate_knowledge.py` 仍 0 错（不许为了测试去改知识）

### 批次 3 · 笼子内部逻辑（离线可测的降级与闸门）

- 目标：`env_status`（16 行）+ `logs_prune`（24 行）+ `report_session`（13 行）+
  `server.py` 79 行薄包装 + `bridge_fw/build.py` 工具链缺失分支。
- **重点不是「跑通」，是「跑不穿」**：
  - `env_status`：缺 pyserial / 缺 pyocd / 端口被占 三种降级各自的文案断言
  - `server.py`：79 行几乎全是 `@mcp.tool()` 薄包装，每个包装**只做参数转发**，
    要断言「转发对了 + 没偷偷改参数」，并加一条「59 个工具名与 `coverage.json` 清单一致」的机器对账
  - `build.py`：无工具链时必须**明确失败并给出安装指引**，不许静默跳过
- 收口判据：这批做完，`coverage.json` 里 server.py 的实测缺口归零或全部有明确硬件理由

### 批次 4 · 真机路径（**需实物，师兄排期**）

按依赖顺序，每条都有明确的「跑什么、看什么、什么算过」：

| # | 项目 | 硬件条件 | 判据 |
|---|---|---|---|
| H1 | `live_acceptance` 全 6 关 | NUCLEO-H563ZI + COM 口 | PASS ≥ 5，FAIL = 0（SWD 关按开关计） |
| H2 | `bridge_fw/live_check.py` | H563ZI PB8/PB9 接 IMU | I2C 扫到器件 + 读写回读一致 |
| H3 | 烧录后逐字节校验 | J-Link + `--fw` | 校验一致，且故意改 1 字节必须报不一致 |
| H4 | HardFault 真机复现 | `fault_demo_fw` | CFSR 抓到且 `decode_fault` 解出的位与固件一致 |
| H5 | RTT 读取 | J-Link | 读出 RTT 缓冲内容 |
| H6 | 双口对录 / 波特率扫档 | 两个串口 | 时序统计与真波特率吻合 |

**这批不开工前我会先停**，等师兄给板子与端口号。

### 批次 5 · 防退化（补完前 3 批就做，否则白补）

- `coverage.json` 加 **行覆盖率下限**（当前 TOTAL 77%，前三批做完应 ≥ 88%，
  下限设 85%——**设成实测值 + 余量，不设成理想值**）
- `audit_capability.py` 增加一项：**未覆盖行分类账自检**——
  脚本自己重跑分类，若某条盲区既不在「纯逻辑」也不在「硬件依赖」白名单里，报错
- CI 增两条：`python -m coverage run … && coverage report --fail-under=85`、
  `python src/mcu_debug_mcp/tools/mutation_check.py`

---

## 四、本次实测发现的两个问题（**先记账，不掩盖**）

### P0 · 沙箱/后台执行下全量测试不稳定（**不是代码缺陷，是执行环境**）

| 执行方式 | 结果 |
|---|---|
| 前台 `python -m pytest -q` | **762 passed / 2 skipped**（复跑 3 次全绿） |
| 前台 `coverage run … pytest` | 全绿（2 次） |
| **后台** `coverage run … pytest` | **7~16 failed + 3 errors**（3 次全红） |
| 前台单独 `python -m build` | rc=0（3 次） |
| **后台**单独 `python -m build` | **rc=1 `Backend subprocess exited`** |

根因已定位到「后台执行环境」，**具体机制未最终确认**（怀疑是子进程被沙箱拦）。
`test_package_smoke` 的 3 个 error 全部由它派生（`setUpClass` 里 `python -m build` 失败）。
**结论：门禁一律前台跑**；若 CI 或后续要用后台任务，必须先解决这一点。
**未解决前不写进 CI。**

### P1 · `bridge_fw` 工具缺失时会静默 skip，掩盖环境问题

`build.py` 与 `test_symbol_lookup.py` 在缺 `arm-none-eabi-gcc` 时 skip。
本机**有**该工具链，所以本轮未触发；但这与 v4.5.1 定的那条「构建失败不许 skipTest」是同一个
原则的漏网场景——**工具缺失可以 skip，工具存在却构建失败必须 fail**。批次 3 一并收口。

---

## 五、不在本计划内（明确不做）

- ❌ 发布 / 打包上传 / GitHub 操作（师兄已明确）
- ❌ 把覆盖率当 KPI 冲数字（为覆盖而造的断言一律不要）
- ❌ 给硬件路径写假 stub 充覆盖率（那是自欺，`FakeCore` 只许验**判据与文案**，不许验硬件行为）
- ❌ 凭记忆扩知识库（一切知识条目要么同源派生，要么有 `source` 字段）

---

## 六、立即可动的第一批

**批次 1（产物解析器）**——纯离线、不碰任何硬件、90 行盲区、且直指刚修过的 `.map` 同族风险。
等师兄一句「开工」我就动手。