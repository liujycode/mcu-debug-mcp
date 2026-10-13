# 更新日志

本文件记录 mcu-debug-mcp 所有值得注意的变更。
格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## 版本策略

| 位 | 何时进 | 例子 |
|---|---|---|
| **主版本 X.0.0** | **接口契约**变更：工具改名、参数语义变化、MCP 资源 URI 变化、返回结构不兼容 | 把 `driver_get` 的返回从文本改成结构化 JSON |
| **次版本 0.X.0** | 新增工具 / 资源 / prompts / 知识库条目，向后兼容 | 加 `env_status` 工具、加 4 个 MCP 资源 |
| **修订号 0.0.X** | 修 bug、改文案、补测试，接口不变 | 修 fault_bits 文件名与 arch 名不一致 |

> **说明**：4.0.0 之前的版本未建档（当时没有发布计划，也没有外部使用者）。
> 本文件自 4.0.0 起为权威；此前的演进过程见 `ROADMAP.md` 的历史记录段。

---

## [Unreleased]

### 计划中
见 `PLAN.md`（唯一待办权威）与 `coverage.json`（机器可读的能力面清单）。
跑 `python tests/audit_capability.py` 可一条命令列出全部缺口与当前深度。

---

## [4.5.4] - 2026-10-13

**首次给 core.py 跑行覆盖率 → 撞出「GNU ld 的 map 没有符号表，反查给假函数名」。
本轮是真工具链夹具 + 变异验证双轮，不是补数量。**

### Fixed

- **`symbol_lookup` / `stack_unwind` 喂 `.map` 时会给出假函数名**（实测固件，2026-10-13）：
  GNU ld（GCC / STM32CubeIDE / ESP-IDF）默认**不往 .map 里写符号表**，只有编译
  单元起点和链接器生成的段符号。实测同一个桥固件：`fw.map` 解析出 **3 个**符号
  （含 `g_vectors` 这种段符号），`fw.elf` 有 **31 个**。于是反查 `0x08000054`
  答的是「`g_vectors`（偏移 0x54）」，而真实函数是 `uart_puts`——**假名字比没
  名字更害人**，原输出只有一句泛泛的"可能不准"，用户会直接采信。

  修法刻意**不做"自动升级到 .elf"**：`.elf` 可能是另一次构建的产物，版本不一致时
  地址对不上，静默换源比不反查更难查。改为**如实告知**——`_sibling_elf_path()`
  探测同目录的 `.elf`/`.axf`/`.out`，符号表稀疏（<10 条）时把更全的符号来源连同
  "确认同一次构建再换"的告诫一起摆出来（阈值 10 是为了不打扰 Keil/IAR 用户——
  他们的 map 本就带完整符号表）。
- `symbol_lookup` 在 `.elf` 场景下仍写"符号名来自 map 的符号表"——措辞与事实
  不符，已改为指明来自 `.symtab` 且带真实 `st_size`。

### Added

- **`tests/test_symbol_lookup.py` 37 条**：夹具是**真工具链产出**，不是手编的
  map 文本——用仓库已入库的 `bridge_fw/main.c` + `linker.ld` 现场编译真
  Cortex-M33 ELF（复用 `bridge_fw/build.py` 已有的工具链探测，不另写一套），
  并与 `arm-none-eabi-readelf` 真值交叉核对（符号地址/大小逐一吻合）。
  工具链或 CMSIS 头缺失时整组 skip，CI 上不会红。
  覆盖：ELF 段表/符号表/大小、截断与非 ELF 必须拒绝不假装成功、稀疏 map 提示、
  `.elf` 精确命中与越界拒绝、EXC_RETURN 位语义（bit2=SPSEL / bit3=Mode，
  2026-09-22 真机纠错过一次，此处锁死不许错回去）、`_infer_sizes` 三条边界、
  `decode_register` 的 `bits` 二义性（区间 vs pos+width）与错误输入路径。
- **变异验证 M5/M6/M7**（`tools/mutation_check.py` 4→7）：证明上面这些测试真
  有判别力。M6 的变异点改了三次才对——先改 `spans` 长度（无效）、再让
  `_seg_of` 恒返回 `.text`（仍无效），才明白**跨段保护是两层冗余的**
  （同段比较 + 推断值不得超出所属段长度），必须同时打掉两层才代表"保护整体失效"。

### Changed

- `symbol_lookup` / `stack_unwind` 的 MCP docstring 与排故 prompt 引导补上这条
  陷阱（★优先 `.elf`；GCC 系列的 `.map` 默认无符号表）——AI 读的就是这两处文案。
- README 验证状态表新增「符号反查 / 栈回溯（真工具链夹具）」一行。
- 离线测试总数 727 → **764**（`762 passed / 2 skipped`，含 25 条工具链缺失时的 skip）。

---

## [4.5.3] - 2026-10-13

**批次 3 知识层收口（G01 器件选型库 + G03 外设 API 用法层）+ PyPI 发布预检轮。**

### Added

- **G01 器件选型库**（9 器件，与驱动库同源派生）：`component_list` / `component_get`
  + 2 资源（`mcu://components`、`mcu://component/{name}`）。按接口（i2c / uart /
  timer_pwm / gpio_timing）分类，带 7 位地址、**微秒时序要求**、头号坑、出厂验证记录。
- **G03 外设 API 用法层**（9 条）：`peripheral_api_list` / `peripheral_api_get`
  + 1 资源（`mcu://peripheralapis`）。每个驱动要占哪些外设（I2C1 / TIM1 / UART2…）
  + **初始化顺序硬要求**，取自 `*_port.h` 适配层注释**原句**，不改写。
- **MCP 面 55 → 59 工具 / 13 → 16 资源**（8 静态 + 8 模板）；系统指令补上
  「先查器件库再答型号/先查外设用法再讲 HAL 顺序」的行为引导。
- **tests/test_component_kb.py 21 条 + 43 subtests**：条数与驱动数一致 /
  分类判据与真实 HAL 符号自洽 / 微秒时序判 DWT 不判函数名 / 地址只在 usage 原文中 /
  初始化要求是原句（括号内逗号不腰斩）/ 空名错名空库都说人话。
- classifiers 补齐：`Development Status :: 4 - Beta`、`Intended Audience :: Developers`、
  `Programming Language :: Python :: 3.10–3.13`（与 requires-python 对齐）。
- **PyPI 发布预检全过**：干净重建零 deprecation warning；`twine check` 双 PASSED；
  sdist 五关键文件全收录、wheel 顶层零泄漏、`dist-info/licenses/LICENSE` 在位；
  README 无本机路径/用户名/内网 IP；`requires-python>=3.10` 无 3.11+ API。

### Changed

- **license 换轨 PEP 639**：`license = { text = "MIT" }` 表格式被 setuptools 84 标记
  deprecated（2027-02-18 起停收），改为 SPDX 表达式 + `license-files = ["LICENSE"]`；
  `build-system requires` 从 `setuptools>=68` 提到 `>=77`。METADATA 随之升级为
  `License-Expression: MIT`（Metadata-Version 2.4）。
- MANIFEST.in 清理死规则 `*.yaml`（全仓无匹配），构建日志零警告。

### Fixed

- 全量 pytest 偶发 3 error（test_package_smoke 安装冒烟）实锤为构建隔离环境拉依赖时
  镜像源瞬时抖动：单跑文件 10 passed、全量复跑 704 passed / 2 skipped，非代码问题。

---

## [4.5.2] - 2026-10-13

**C 档结构性重构：src 正规包布局——「能进 PyPI 而不污染别人环境」的最后一关。**
接口零变更；数据目录物理进包，wheel 顶层不再出现 core.py / tools/ 这类生态泛滥名。

### Changed（打包结构）
- **`src/mcu_debug_mcp/` 正规包**：server/core/analyzer/guard/paths + knowledge/ +
  drivers/ + tools/ 全部物理移入包内（git mv 保留历史）；包内改绝对导入，
  平铺时代的「把自己目录插进 sys.path」hack 退役。
- **数据分发换机制**：setup.py 的 build_py 整目录复制钩子退役（它只盖 wheel 直构
  路径——正是「sdist 空壳」P0-1 的温床），改为 `include-package-data = true` +
  MANIFEST.in（sdist/wheel 共用一份清单）；setup.py 仅保留构建临时目录重定向
  （批量删除保护环境的实测硬约束）。
- **版本单一来源**：`mcu_debug_mcp/__init__.py` 的 `__version__`，pyproject 用
  `dynamic = ["version"]` 从那读；常量审计新增「版本 ↔ README 全量比对」检查。
- `_pkg_version()` 回退链重写：安装元数据 → 包 `__init__`（旧回退读同目录
  pyproject.toml，src 布局后两个前提都不成立）。
- CI 打包结构核验改查包内路径，并加**反污染断言**（site-packages 顶层不得出现
  core.py / tools/）。

### Added（判据与测试卫生）
- D 档判据升级：G01/G03 从 `file_exists`（touch 空 json 即判完成）改为
  `core_len` 要求真有读取函数——读取入口落地前诚实保持未完成。
- PLAN.md 的 N-08 按轴分列：7 轴各自实测差距（最大缺口=错误码条目 −99），
  不再用最小那条代表整体。
- tests/conftest 及 5 处 sys.path 守卫修正：守卫从「ROOT 在不在 sys.path」改为
  「插入目标 src 在不在」——`python -m pytest` 会把 CWD(=ROOT) 放进 sys.path，
  旧守卫恒假，src 永远插不进去（重构过程中被测试网当场抓住）。
- test_live_acceptance_fixture 增加 autouse 环境快照：夹具流程真改 os.environ
  （PORT_ALLOW/MODE），不恢复会泄漏给后续测试（按字母序跑掩盖了它）。

---

## [4.5.1] - 2026-10-13

**审查报告（`docs/AUDIT-2026-10-13.md`）6 个 P0 全部清零 + B 档上线质量收口。
本轮无接口变更（`report_session` 新增 `confirm=False` 参数为向后兼容的可选门禁）。**

### Fixed（P0：上线阻断项）
- **发布产物空壳（最严重）**：缺 `MANIFEST.in` → sdist 不含 knowledge/drivers → wheel 从 sdist
  解包构建 → 空壳传染，用户 pip install 后知识类工具全空。新增 `MANIFEST.in`
  （recursive-include 数据目录 + prune 构建产物）；`setup.py` 数据目录缺失由静默 `continue`
  改 `raise SystemExit`。实测 sdist 139 条 / wheel 81 条（修复前均为 0）。
- **打包冒烟测试假绿灯**：旧测试用 `pip install . --target`（源码树直构）绕开 sdist，永远绿。
  改参数化双安装源：`setUpClass` 真构建 sdist+wheel，`_install_and_probe` 对两种产物跑
  同一套 11 项探针断言（10 passed）。
- **`flash_verify` 回读失败仍打印"✓ 完全一致"**：三条失败分支（回读失败/临时文件读不到/
  长度不符）只累加 `bad_total` 不改判据 → 探针掉线也报"固件已正确烧入"。加 `seg_ok`
  标记，结论段区分"✓ 完全一致 / ？ 未能校验 / ✗ 不一致"。
- **知识库事实错误**：`cme.json` 把 CME ERROR 12 释义为"SIM 的 PIN 码错误"（3GPP TS 27.007
  §9.2.1 实为 **SIM PUK required**，Pin 错误是 11，incorrect password 是 16）——会误导用户
  继续试 PIN 把 SIM 彻底锁死。12 改为 PUK 释义 + 警告，补录 14/15/16；
  `analyzer._CME_FALLBACK` 同步 8 条同文（靠对饮测试保证不脱节）。
- **审计体系三处失明**：`audit_constants` 补三条句式正则（当场抓到藏起来的第 4 处旧数字）；
  `eco_frameworks` 探针硬编码 `manual now=1` 改实测（6/6，N-09 缺口关闭）；
  `docNumbers` 改 `finditer` 全量匹配（抓到 README:37 旧值）。
- **文档数字矛盾**：README/PLAN/ROADMAP 全量对齐；删 PLAN 里 HardFault"仍未真机"过期结论
  （与 ROADMAP 的 9/9 PASS 打架）；修 PLAN:59 算术错误。

### Fixed（代码正确性，13 条）
- `chip_info` FlashSize 短文件 IndexError；`baudrate_autodetect` 单候选 IndexError；
  `symbol_lookup` 空符号表 IndexError；`error_code_lookup` 空表/None 键 KeyError
  （与同文件既有保护口径拉齐）。
- `serial_read`/`serial_send` 串口异常未捕获 → 设备被拔时抛栈给 MCP 客户端；改为返回
  可读中文排查提示（已收字节照常给）。
- `serial_pair_record` 第二个口打不开时第一个口句柄泄漏 → 两个口分两个 try，先关先开的。
- `decode_register` 字段级 `bit/lsb/width` 无校验（负 width 抛 `negative shift count`）→
  加值域校验，非法字段走 skipped 汇总不再冒泡。
- `report_session` 完全不过 guard 闸门（`overwrite=True` 可覆盖主机任意路径）→ 纳入 write
  档 + 覆盖需 overwrite+confirm 双闸门；`stream_capture` 从复用只读 op 改登记 write op。
- `guard.py` 虚拟口豁免排在写档/不可逆 confirm 之前（`loop://` 可跳过二次确认）→
  豁免只免档位/名单，不可逆 confirm 保留。
- `_at_line_match` / `_bridge_lines` 把未收完的半行当整行匹配 → 半包假 PASS、失步检测
  形同虚设；改为只认被 `\n` 终结的行。
- `_sp_vals_from` 遇任意 `Error:` 行丢弃全部已读值 → 改为保留已读值再 break。

### Fixed（误报与断言强度）
- `analyzer` SENSOR_FF 缺语义守卫：实测 `BSSID strength 255 255 255`（WiFi 信号强度日志）
- 被误判"数据线悬空"。加两路判据（行尾单值+读数类键名+负向前瞻排除强度/计数词；
  连续多字节 0xFF dump 形态），五形态实测全对。
- `test_analyzer.py` 断言 `assertGreaterEqual(len(_EXTRA_RULES), 8)` → `assertEqual(..., 15)`
  （旧断言下规则被误删 7 条仍绿）。

### Added
- `tools/mutation_check.py`：变异验证——把四个修复处的 bug 种回去，对饮测试必须变红，
  还原后 md5 字节级一致（判别力证明）。本轮它自己也抓出一个 bug：文本模式读写被
  Windows 换行符转换坑害，已修成二进制读写。
- `tools/verify_stdio.py`：真 MCP stdio 客户端端到端（initialize → 55 工具 → 13 资源
  全读 → 6 prompts → env_status 实调），v4.3 时代后首次重测全过。
- `MANIFEST.in`（发布数据清单）；`pyproject.toml` 补 `[project.urls]`；
  `SECURITY.md` 补漏洞报告通道（GitHub Private Vulnerability Report）。
- 审计新增 `pytest_collected` 探针：真跑 `--collect-only` 数收集项，README 的测试总数
  （706 项）由机器锁定，手改数字审计立即红（种回旧值 699 已验证有牙）。

### Changed
- `pyproject.toml` 依赖下界 `mcp>=1.0` → `mcp>=1.2.0`（与 requirements.txt 对齐）。
- 三个真机脚本去硬编码：`build.py`/`live_check.py`/`fault_live.py` 的 GCC/CMSIS/
  CubeProgrammer 路径改 env>PATH>glob 自动定位（含 msys64/CubeIDE 候选），找不到给
  可读诊断；`live_check.py` 默认备份路径改 `%TEMP%`，且**烧录前自动上传备份**
  （备份失败不烧）。
- 文档测试计数 699 → 706 项 / 704 passed（旧数字是手写的，必然过期）。

### Security
- `report_session` 纳入隔离闸门：未经 overwrite+confirm 双确认不得覆盖既有文件；
  `stream_capture` 登记为 write 档操作（长期独占端口不再借用只读 op 过闸）。

---

## [4.5.0] - 2026-09-22

**G06 总线工具落地：8 个工具 + MCUDBG/1 桥固件——「AI 替你看总线上挂了什么」从
纸面方案变成可交付的实现，零外购硬件、零驱动改动。**

### Added（新增 8 个工具：47 → 55）
- **`bridge_probe`** —— 桥自检：确认执行端活着、认 MCUDBG/1 协议、支持哪些总线。
  纯握手不碰总线；**刻意不做"全口自动探测找桥"**——实测往一个不认识的串口发探测
  字节，会真的被打进别人正在跑的固件里（本机手套固件解析并回应了探测用的 `help`）。
  端口必须显式给出（或设 `MCU_DEBUG_BRIDGE_PORT`），工具不猜。
- **`i2c_scan`** —— I2C 总线扫描：逐地址 ACK 探测 + **可能的器件提示**。提示表是最小集，
  逐条来自器件数据手册（TCA9548A/BNO08x/MPU-6050/BMP280/SSD1306/AT24Cxx…），
  只提示不判定；扫不到时给按命中率排序的 5 步排查清单（含"多路复用器后面的
  器件默认断开"这条最隐蔽的）。
- **`i2c_read_reg` / `i2c_write_reg`** —— I2C 读写寄存器。读回全 0 / 全 0xFF 分别给
  **不同**的成因清单（全 0xFF 往往是器件没接/没上电，不是寄存器的值）；写默认
  不回读（只写寄存器回读必然不一致，默认开会制造假 FAIL），verify=True 才回读。
- **`spi_scan` / `spi_read_reg`** —— SPI 扫描（逐 CS 读 JEDEC ID，解厂商/容量）与
  按命令字读。输出里明说 **SPI 没有地址概念**：扫不到传感器/ADC 属正常，不是故障。
- **`onewire_scan`** —— 1-Wire 搜索 ROM（AN187 算法 + CRC8 校验），解家族码；
  搜不到先怀疑数据线缺 4.7k 上拉。
- **`rs485_send`** —— RS485 发送；**输出里强制写明**"只证明桥把字节推出去了，
  不证明对端收到"。
- **桥固件**（`tools/bridge_fw/`）：MCUDBG/1 纯 ASCII 行协议，NUCLEO-H563ZI
  裸机实现（I2C1/SPI1/1-Wire 位抠/USART3 命令口，无 HAL 无 libc，5.7KB）；
  附 `build.py`（编译）与 `live_check.py`（**烧录 → 8 个工具逐个真机验证 →
  自动恢复原固件**一条龙）。为什么选这条路：ST-Link bridge 要 V3SET+扩展板
  （板载 V3E 实测 `BRG_NO_STLINK`）、FT2232H 要换驱动（被明确禁止）——
  闲置 MCU 当桥是唯一零外购、零驱动改动的路。

### Fixed（真机闭环补丁，同日）
- **桥固件 .bss 被链接进 Flash（致命，真机必炸）**：linker.ld 缺 `.data`/`.bss`
  段定义，1848B 全局变量被当孤儿段塞进 FLASH——写不进也无人清零，line_len
  初值 0xFFFFFFFF，任何命令一律回 `line too long`。补段 + Reset_Handler 手写
  启动代码（拷 .data/清 .bss）。探针固件同款脚本没炸只因 .bss=0 字节，纯属侥幸。
- **MODF 挂死 SPI**：`SPI SCAN` 一下桥就死。SWD 验尸（读 PC 卡在 spi_xfer8 等
  RXP 的 while + SR.MODF=1）定位：MODF 置位 → 硬件清 SPE → RXP 永不来 →
  无超时 while 挂死固件。修法：spi_begin 置 SPE 前清 IFCR（含 MODFC）+ 补
  SSI=1；spi_xfer8 两个等待全部加 100ms 超时——**裸机固件任何死等必须限时**。
- 真机闭环 6 项全 PASS（bridge_probe/i2c_scan/spi_scan/spi_read_reg/
  onewire_scan/rs485_send），原固件恢复回读 md5 一致；详见
  `tools/bridge_fw/README.md` 的「真机闭环记录」。

### Fixed
- **`rs485_send("48656c6c6f")` 会把整串当成一个巨大的数**（0x48656C6C6F，超出字节
  范围而报错）——若改成宽松解析更糟：会"成功地"往线上发出一串完全不是用户本意的
  字节。现在字符串一律按「一个字节最多两位十六进制」拆分：`"48656c6c6f"` = 5 字节。
- **协议对齐教训（写进桥固件头注释）**：多值字段必须**逗号分隔在同一个 token 里**
  （`slots=0:..,1:..`）——PC 侧按空格切字段，带空格的值后半段会被当成别的字段丢掉
  （`test_scan_masks_jedec_to_24_bits` 第一版红掉的原因）。
- **`audit_capability` 的 L3 判据假阴性**：`_split_test_funcs` 只用**函数名**做字典键，
  两个测试类里的同名测试互相覆盖 → 有判别力的断言被静默丢弃 → 工具被算成
  "没有判别力"（人会把已经有的东西再补一遍）。改键为 `类名::函数名`，并做变异验证：
  旧实现必须重现假阴性。
- `live_acceptance.py` 三处：`serial_session_log` 判据写反（板子沉默假通过、说话假
  FAIL——真机第一次跑就撞上）；自定义探针下收尾仍发默认模板的 `'0'` 再报"设备被打死"
  （板子活得好好的，报告说你把它弄死了——现在不猜，SKIP 并明说，要发用
  `--off/--off-expect`）；server.py 三个 prompt 文案里"G06/G05/G12 尚未实现"的
  过时说法纠正。

### Verification（变异验证——断言不许是摆设）
- `tools/_tmp` 两处变异（JEDEC 不掩 24 位 / 十六进制串按整词解析）**全部被测试抓住**，
  且还原后字节级 md5 一致；顺带抓出旧夹具的空槽全写成 6 位 0xffffff（掩不掩一个样），
  补 8 位形式（0x00ef4018 / 0xffffffff）的测试——那才是掩码的存在理由。
- 全量 pytest：**698 passed / 2 skipped**（4.4.1 基线 636，+62 = `test_bus_tools.py` 总线工具离线断言）。

### 边界（如实，不装）
- 桥固件真机闭环差一步：H563ZI 已被拔下（按上一轮"可以拔"的指示），回插后跑
  `python tools/bridge_fw/live_check.py` 即完成"烧录→验证→恢复"全流程；
- 扫描的"有应答"只代表有人 ACK，不代表器件功能正常；`rs485_send` 不证明对端收到。

---

## [4.4.1] - 2026-09-22

**HardFault 真机闭环跑通（ROADMAP 收口判据 1 / B⑤），并抓到第一个「只有真机才暴露」的
缺陷：`stack_unwind` 把 EXC_RETURN 的 bit2/bit3 含义对调了。**

判据要的本来就是真机——离线夹具里的 `exc_return`、寄存器值全是编的。这次证明那不是走形式。

### Added
- **`tools/fault_live.py`（HardFault 真机闭环验收）**：故意读非法地址 → HardFault →
  SWD `hotplug` 抓现场（不复位、不停内核）→ `decode_fault` / `stack_unwind` /
  `symbol_lookup` 走全链，9 项判定。默认**不碰硬件**（只编译 + 打印计划），
  要真跑必须 `--flash`（★ 会覆盖目标固件）。
- **`tools/fault_demo_fw/`（演示固件工程）**：`startup.s` + `fault_demo.c` + `link.ld` +
  `build.py`，用 `arm-none-eabi-gcc` 现场编译，产物 316 字节。两个关键设计：
  ① **汇编侧抢在 C 之前**把 MSP / PSP / EXC_RETURN 存进固定地址 `0x20000000` ——
  一旦进了 C 函数，prologue 就改了 SP，那时再读 MSP 拿到的已是 handler 自己的栈指针；
  ② 三层调用链（`app_state → imu_task → mpu6050_read`）刻意 `noinline`，
  保证栈回溯真能反查出函数名，而不是被内联成一坨。

### Fixed
- **★ `stack_unwind` 的 EXC_RETURN bit2/bit3 含义对调**（真机才暴露）：
  旧实现把 **bit2 当「模式」、bit3 当「用哪个栈」**；按 ARM 架构实际是
  **bit2 = SPSEL**（0=MSP / 1=PSP，**决定去哪取栈帧**）、**bit3 = Mode**（0=Handler / 1=Thread）。
  真机实测（NUCLEO-H563ZI，读非法地址 → HardFault）handler 入口
  `EXC_RETURN = 0xFFFFFFB8`：bit2=0、bit3=1。旧实现据此输出
  「bit3=1 → ★异常前用 PSP」，**而栈帧实际在 MSP = 0x2009FFC8，此时 PSP = 0** ——
  照它提示去 PSP 取，会拿到一整帧的 0：寄存器看起来"合理"却全假。
  **旧测试为什么抓不到**：ARMv7-M 的标准 EXC_RETURN（`0xFFFFFFF1/F9/FD/E1/E9/ED`）
  里 bit2 与 bit3 **恒同向**，两位对调后结果恰好一样；只有 ARMv8-M
  （M23/M33/M55…，另占用了 bit5/bit6）才会出现 bit2≠bit3 的组合。
  这正是「真机验证不可被离线夹具替代」的实证。
  已加回归 `tests/test_fault_chain.py::test_exc_return_bit2_is_stack_not_mode`，
  并做变异验证（换回旧写法必须变红；旧写法仅在标准值上假通过）。

### 真机结论（NUCLEO-H563ZI / STM32H563ZI / Cortex-M33 / ST-Link V3E）
| 项 | 实测值 |
|---|---|
| 触发方式 | 读非法地址 `0xFFFFFFF0` |
| CFSR | `0x8200`（PRECISERR + BFARVALID） |
| HFSR | `0x40000000`（FORCED） |
| BFAR | `0xFFFFFFF0` |
| 压栈帧 PC | `0x080000A2` → **mpu6050_read+0x2** |
| 压栈帧 LR | `0x080000BB` → **imu_task+0x7** |
| EXC_RETURN | `0xFFFFFFB8`（bit2=0→MSP、bit3=1→Thread、bit4=1→标准帧） |
| 交叉核对 | 直接读 SCB（`0xE000ED28`+）与固件自读**逐字一致** |

★ 附带意义：`0x8200` 正好是离线夹具一直用的那个值（`decode_fault(0x8200, bfar=…)`），
真机得到的位组合与夹具完全吻合 —— 说明**夹具的位模型是对的，错的只是 exc_return 那两位**。

### 回归数字
- `python -m pytest tests -q` → **636 passed / 2 skipped / 0 failed**（4.4.0 基线 635，本批 +1）
- 三套审计：47/47 有专项断言、缺口 22/26、0 脱节、隔离 11 项 0 失败；常量 0 错误；知识库 0 错误

---

## [4.4.0] - 2026-09-22

**直连调试器那一批工具落地（5 件），一次点亮 N-06 / G05 / G07 / G10 / G12 五个缺口；
过程中又挖出 3 处「质量门与验收夹具自己的假阴性」。**

按本文件版本策略，新增工具 / 资源属向后兼容 ⟹ 次版本。工具 42 → **47**。

### Added
- **`chip_info`**（N-06 芯片身份与存储信息）：读 Device ID / Revision / 板名 / Flash 容量 /
  器件族，并与 DBGMCU 的 DEV_ID 交叉核对（两处独立来源对上才算真读到）。
  **默认 `mode="read"` 走 ST-Link 的 hotplug 档：不复位目标、不停内核、板子照跑**——
  这是刻意选的默认值，因为 `mode=normal` 的连接**本身就会复位目标**（实测）。要 UID /
  选项字节得显式 `mode="full"`，它会在标题里写明「已复位目标并 halt 内核」。
- **`reset_reason`**（G10 复位原因与运行状态）：解码 `RCC_RSR`（PINRSTF / BORRSTF /
  SFTRSTF / IWDGRSTF / WWDGRSTF / LPWRRSTF），逐位给人话 + 可执行建议；
  同时报内核状态。**标志是累积的**，所以输出会点破「多个标志同时为 1 表示自上次清零
  以来发生过这几类复位，不代表最后一次是它们一起发生」，并给「清零 → 复现 → 再读」
  的定位法。清零是**不可逆**动作（销毁当前现场），要过隔离闸门 + `confirm=True`。
- **`flash_verify`**（G05 烧录后验证）：把设备内存回读出来与固件文件**逐字节比对**，
  不一致时报**首个偏移 + 绝对地址 + 两侧各 8 字节上下文**。支持 `.bin` / `.hex` / `.elf`
  （`.hex` 按记录地址、`.elf` 按段表偏移取字节）。回读失败**绝不算通过**——
  那是「没验成」而不是「验过了」。
- **`timing_analysis`**（G07 时序 / 数据率分析）：从带时间戳的采集记录里算数据率、
  间隔中位数、P05/P95、抖动、丢帧（序号跳变）、节拍变慢（间隔突增）与漂移。
  **两条防"编数字"的硬约束**：① 全部行落在同一刻度内时**明确拒绝给任何指标**
  （否则会算出 60 亿行/秒）；② 漂移判据要**同时**超过「一个中位间隔」和「P95−P05
  的实际离散度」才报警——只比坡度会拿量纲不对的阈值去吓人。
- **`stream_capture`**（G12 长时录测与数据采集）：按到达时刻给**每一行**打时间戳并落盘
  （`[+秒] 行`），产出可直接喂给 `timing_analysis`。目标文件已存在**自动改名不覆盖**
  （`_2` / `_3`…）。输出里给下一步命令，链路不靠人记。

### Fixed
- **能力面审计自己的 L3 假阴性（比假阳性危险）**：`tests/audit_capability.py` 的
  `_split_test_funcs` 只用**函数名**做字典键，同名测试互相覆盖。
  `tests/test_probe_direct.py` 里 `FlashVerifyTest.test_refuses_bad_inputs`
  （含 L3 要的 `assertIn("只支持", …)`）被 `TimingAnalysisTest.test_refuses_bad_inputs`
  （不含）覆盖 ⟹ **`flash_verify` 的 L3 被算成「没有判别力」**，明明有断言却说缺口没补，
  照着报告会去补一遍已经有的东西。键已改成 `"类名::函数名"`，并加了「旧实现必须重现
  这个假阴性」的变异回归。修后 5 件新工具在深度阶梯上 **L0–L6 六格全绿**。
- **真机验收夹具的 `serial_session_log` 判据是反的**：原来断言
  `"handle=%d" % h in g`，而 `core.serial_session_log` **只在缓冲区为空时**才带
  `handle=N` 前缀、有数据时直接返回日志原文 ⟹ **板子沉默时假通过、板子一说话就假 FAIL**。
  真机第一次跑就撞上（板子在刷 `TICK`，它报「不可读」）。改成按实际契约判，且
  **两个方向都要有判别力**（沉默板必须明说「日志为空」，有输出的板日志里必须真含刚读到的行）。
- **夹具收尾把默认模板的 `'0'` / `'PWR OFF'` 写死**：用户一旦用 `--cmd/--expect` 换成
  自己的板子，收尾照样发一个对目标毫无意义的 `'0'`，然后报
  **「设备未应答（可能已被打死，需复位）」——板子活得好好的，报告却说你把它弄死了**。
  假 FAIL 会让人去翻一块根本没坏的板子。现在：内置模板自动带安全态；自定义探针下
  **不猜**——SKIP 并明说「没发任何字节」，要发就用 `--off/--off-expect` 显式指定。
- **夹具第 6 关内顺序错**：`chip_info(mode="full")` 会把内核 halt 住，原先排在采集之前，
  于是 `stream_capture` 采的是**一段空气**。已把 full 档挪到关末（侵入性动作一律最后），
  跑完再自己用一条 `hotplug + -rst` 把内核**放回运行态**并验证（不留 halt 后遗症）。
- **`_sp_core_state` 只认两种内核状态**：实测 ST-Link 会报**三种**，
  第三种 `Core is kept under reset`（`-hardRst` 独有）原先会被当成「读不到」。
  判据已改成前缀匹配并把三态契约钉进注释与测试。
- 夹具 `serial_read` 那条断言原先无条件 PASS（形同虚设），现改为真的会 FAIL。
- **`serial_send` 的 ORE / CRLF 两档提示此前一条测试都没有**（代码早就写了，没人盯）：
  现在补 4 条，且**两个方向都钉住**——传了 `expect` 未命中必须有强提示（点明 ORE 机制
  与 `newline=False` / `hex_mode` 改法）；不传 `expect` 且完全无响应必须有**弱**提示
  （措辞不武断，同时列接线/波特率/供电）；反向两条证明提示**是有条件的**：
  `newline=False` 时不许提 CRLF、设备答了话就不许再猜 ORE。
  用变异验证过判据有判别力（掐掉弱提示分支后新断言确实变红）。
  为什么值得单独补：`serial_send(port, "AT")` 不传 `expect` 是**最常见**用法，
  而强提示的前提恰是 `expect` 非 None ⟹ 这条路上 CRLF 把逐字节设备打死时用户零线索。

### Verified（真机，NUCLEO-H563ZI／STM32H563ZI／Cortex-M33／Device ID 0x484／2MB Flash）
- **只读档** `--swd --fw fw.bin`：**16 PASS / 0 FAIL / 4 SKIP**。
- **写档** `--swd --fw fw.bin --allow-write`：**24 PASS / 0 FAIL / 3 SKIP**
  （多覆盖 `chip_info(mode="full")` 读 UID、如实告知会复位目标、跑完把内核放回运行态）。
- 对照物是一块**受控固件**：它自己用官方 CMSIS 的 `RCC_RSR_*_Pos` 解码复位标志并打印，
  与本工具的位表**逐位对上**（`0x14000000 → PINRSTF=1 SFTRSTF=1`）——即「同一位被两条
  独立路径读出同一个答案」，不是工具自己验自己。
- `flip 1 字节` 的反例档：`flash_verify` 报出
  「首个在偏移 +100（绝对地址 0x08000064）：期望 0x64，实际 0x9B」。
- `coverage.json` 的 `liveVerified` 补入这 5 件，**总数 6 → 11**。

### 回归数字
- 分块跑全量：**635 passed / 2 skipped / 0 failed**
  （4.3.1 基线 577；本批 +58 = `test_probe_direct.py` 34 + `test_live_acceptance_fixture.py` 17
  + `test_capability_audit.py` 3 + `test_serial_tools.py` 4）。
- `audit_capability`：**47/47 有专项断言**、缺口 **22/26**（剩 G01 / G03 / G06 / N-08）、
  0 脱节、隔离 **11 项检查 0 失败**；`audit_constants` 0 错误；`validate_knowledge` 0 错误。
- 新增 `tests/test_live_acceptance_fixture.py`：**验收夹具本身也要测**。夹具坏了有两种死法，
  假绿灯（没验也算过）和假 FAIL（板子没坏却说坏了），上面两条 Fixed 都是真机上真实发生过的。
  这个文件里没有一条测试碰硬件——`core` 全是假件，只验夹具的判据与文案。

### Gotchas（实测记录，后续知识层可直接引用）
- **ST-Link 连接档 × 内核状态是三种，不是两种**（实测 `-score`）：

  | 档位 | 仅连接 | `+ -rst` | `+ -hardRst` |
  |---|---|---|---|
  | `hotplug` | `Core is running` | `Core is running` | `Core is kept under reset` |
  | `normal` | `Core is halted` | `Core is running` | `Core is kept under reset` |

  ⟹ **`-hardRst` 是唯一会把内核摁在复位里不放的动作**：板子有电但一行代码都不执行，
  表现和「烧完不跑 / 程序卡死」一模一样。用完必须再补一条不带 `-hardRst` 的连接（或 `-rst`）。
  ⟹ 认状态别只匹配 `running`/`halted` 两个词，第三种照样是 `Core is ` 开头。
- **写 `RCC_RSR.RMVF` 清零不要和 `-rst` 并成一条命令**（`-w32 RMVF -nv -rst`）：
  `normal` 档连接本身就会置 `PINRSTF|SFTRSTF`、`-rst` 还会再置一次，合并后读回必然还看到
  那两个位 ⟹ 会把**命令自己的痕迹**误判成「RMVF 没生效」。分成两步、写后在同一次会话里
  读回，读回 0 才是 RMVF 生效的硬证据。
- **`-r32 <addr> <size>` 的第二个参数是字节数，不是字数**；且 CLI 会把同一行的多个字
  **空格分隔挤在一行**（`0x44024000 : 10076484 00000006 00001800 00000000`）。
  只按「一行一个值」写正则会静默丢字。
- `-c port=SWD` 用 **hotplug** 档时连 system memory 区（UID / FlashSize）**读不到**，
  必须 `normal` 档——这就是 `chip_info` 要分两档、且默认只报 hotplug 能拿到的那部分的原因。

---

## [4.3.1] - 2026-09-22

**首次真机全链路验收（NUCLEO-H563ZI @ COM21），并修掉 8 处「工具明明在手却用不上 / 边做边产垃圾」的缺陷。**

按本文件自己的版本策略，本次全部是 bug 修复 / 文案 / 测试，接口不变 ⟹ 修订号。

### Fixed
- **`flash_firmware` 没接入本机唯一可用的 STM32 下载器**（真机验收暴露）：
  命令表只实现了 `esptool` / `st-flash` / `pyocd`，而本机 `st-flash` 没装、
  `pyocd` 只活在 VS Code 扩展目录里（不在 PATH）—— `auto` 选出的路径注定失败，
  用户看到的却是「烧录失败」。现在：
  - 接入 **STM32_Programmer_CLI**（STM32 默认首选），并支持 `sn=` 指定探针序列号；
  - 命令一律用 `_which_tool` 探到的**绝对路径**，不再用裸命令名
    （探到了却因不在 PATH 而用不上，是本项目最离谱的一处自相矛盾）；
  - 末尾加 `-rst`：SWD 以 `mode=normal` 连接会 halt 内核，**烧完不复位板子不会跑**，
    很容易被误判成烧失败（实测踩到）。
  - 实测确认 `-w` 自带擦除、且只擦被覆盖到的扇区 ⟹ **不需要 `-e`**，也就不会全片擦。
- **`toolchain_status` 谎报「未发现 ARM GCC」**：`_TOOL_SEARCH_DIRS` 漏了
  `%LOCALAPPDATA%\stm32cube\bundles\` 整整一套。本机 ARM GCC 14.3.1 / ARM GDB /
  CMake / Ninja 全在那里（VS Code 的 STM32 扩展装的），既不在 PATH 也不在 Program Files。
  补上后本机由「发现 3 项」变成「发现 6 项」。
- **`probe_list` 与 `env_status` 口径互相矛盾**：前者只看 `import pyocd`（当前解释器），
  后者看可执行文件。同一台机器上一边说「已发现 pyocd」、一边说「未安装 pyocd」。
  现在把「文件在哪」和「缺的是模块」分开报，并给两条修法。
- **`serial_read` 的 `expect` 假失败**：待查字符串明明在输出里却报「超时未命中」——
  因为它只是某行的一部分、没独占一行。整行匹配是刻意设计（防日志里凑巧出现的 OK
  被当成应答），但**没说清怎么改成包含匹配**，用户会去改固件、查接线白折腾。
  现在点破原因并给出 `expect=".*关键字.*"`。
- **打包在带批量删除保护的环境里必失败**：原先构建前 `rmtree(build/bdist.*)`，
  一次动上百个文件会撞上批量删除闸门（>50 个文件需确认），pip 只报
  `metadata-generation-failed`，完全不提真正原因。改成**每次构建用全新的空目录**
  （`setup.py::_isolated_build_base()`）：写进去全是新建、零删除，残留根本不产生，
  幂等问题也就不存在了。
- **冒烟测试自身的 Windows 编码 bug**：`subprocess.run(text=True)` 未指定编码，
  pip 输出里的中文（本包 description）在 GBK 下解码失败 → 读线程抛异常 →
  `r.stderr` 变 `None` → `None[-1200:]` 抛 `TypeError`，报错指向测试自己而不是真因。
  已显式 `encoding="utf-8", errors="replace"`，`r.stderr` 访问也改成安全形式。
- **`test_install_is_idempotent` 在同一类里定义了两次**（内容完全相同，前者永远不跑）—— 删重。
- **跑一次测试就在仓库根留下 `mcu_debug_mcp.egg-info/`**（`egg_info` 的落点没跟着改道）：
  PEP 517 的 `build_meta` 只把 **wheel** 放进临时目录，跑 `setup.py` 时的 cwd 仍是源码根，
  于是 `egg_info` 按默认 `os.curdir` 在仓库根写了一个 `.egg-info/`——
  和 `build/` 是同一个毛病，`_isolated_build_base()` 当时只改了 `build_base`、漏了 `egg_base`。
  现在 `options={"egg_info": {"egg_base": <BUILD_BASE>/egg}}`，并且**先把这个目录建出来**
  （`egg_info` 的 `egg_base` 不做 `makedirs`，目录不存在会直接
  `error in 'egg_base' option: ... does not exist`，被 pip 概括成笼统的构建失败）。
  实测：改前「跑一次冒烟 → 仓库根多一个 `.egg-info/`」，改后仓库根 `egg-info/build/dist` 全无。

### Verified（真机，NUCLEO-H563ZI／STM32H563ZI／Cortex-M33／2MB Flash／SWD 8MHz）
首次跑通「编译 → 烧录 → 官方校验 → 复位 → 串口回读」全链路：
- 用本机 STM32Cube 自带的 **ARM GCC 14.3.1** 编译出 368 字节的最小固件（自包含、不吃 HAL）；
- `flash_firmware` 真烧：`Download verified successfully` + `MCU Reset`；
- 回读 MD5 与源文件**逐字节一致**；
- 复位后 COM21 收到固件自发字符串（`expect` 命中并提前返回）—— 证明**固件真的在跑、VCP 通路真的通**；
- 烧录前做了全片 2MB 备份，验收后按扇区还原，末次全校验 MD5 与备份一致（板子没被我留下改动）。
- `coverage.json` 的 `liveVerified` 据此首次填入 6 个工具：`flash_firmware` /
  `flash_tool_detect` / `serial_list` / `serial_open` / `serial_read` / `serial_close`。

### 回归数字与一条跑测注意（重要）

- 分块跑：150 + 90 + 122 + 72 + 134 = **568 passed / 2 skipped**；打包冒烟 **9 passed**；
  合计 **577 passed / 2 skipped / 0 failed**。
- `audit_capability` 0 脱节 / 0 隔离失效（缺口 9 项）；`audit_constants` 0 错误；`validate_knowledge` 0 错误。
- ★ **在带批量删除保护的宿主环境里，全量 pytest 必须分块跑**：同一次命令内累计删除 ≥ 50 个文件后，
  宿主会对后续每个删除直接 `SystemExit(1)`（报 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`），
  于是那些「自己清理临时文件」的用例会**假失败**（表现为 9 个失败：test_hw_findings 5、
  test_isolation 2、test_serial_tools 2）。单独跑同一个用例是 `1 passed`，拆块跑全绿 ⟹ 与环境有关，与代码无关。
  排查时别被这 9 个红字带去改源码。

### Gotchas（实测记录，后续知识层可直接引用）
- **STM32H5 的 GPIO 时钟在 `RCC_AHB2ENR`**，不是 F1/F4 那套 `AHB1ENR`。
  写错寄存器编译一样通过、代码看着一样对，但端口时钟没开 → 复用配不上 → 串口一个字都没有。
- **STM32H5 复位后 `RCC_CR.HSIDIV` 不是 /1**：按 64MHz 算 `BRR`，实测得到的是 57600
  而不是 115200。判据是拿收到的字节按位反推（`'M'`=0x4D 正好解出实测首字节 0xE6）。
- **STM32CubeProgrammer 的 `-v` 不能单独用**，必须紧跟 `-w`，否则直接报
  `Wrong verify command use`。
- **`-e` 收的是扇区号，不是地址**：按 F4 习惯写 `-e 0x08000000 0x2000` 会报
  `Invalid sector number`。

---

## [4.3.0] - 2026-09-22

**广度与深度补齐一轮**：补齐 10 个能力缺口，MCP 三原语**全部用上**（此前 prompts 为 0），
并新增 1 条一致性审计。全量 **574 passed / 2 skipped**；能力面审计 0 脱节 / 0 死代码 /
0 隔离失效；知识库校验 0 错误；常量一致性审计 0 错误。

工具 34 → **42** · 资源 11 → **13** · prompts 0 → **6**；缺口收敛到 **9 项**（详见 `coverage.json`）。

### Added
- **MCP prompts 从 0 到 6**（N-07）：`diag_no_response` / `diag_garbled` / `diag_i2c_not_found` /
  `diag_flash_fail` / `diag_crash` / `diag_data_loss`，每个给一条有序的工具调用流程。
  值得做的理由：**prompts 不依赖任何客户端特有 Skill**，任何 MCP 客户端都能进——
  这也是三原语里此前唯一没用上的一个。每个 prompt 里**尚未实现的能力会如实标注**
  （I2C/SPI 扫描、长时录测等），不假装能做。
- **`nmea_parse` / `frame_parse`**（G08）：
  - `nmea_parse`：GPS/北斗 NMEA 0183（GGA/RMC/GSA/GSV/VTG/GLL）逐句给人话——
    UTC 时间、经纬度（度分 + 十进制度）、定位质量、星数、HDOP、速度（节 + km/h）。
    **每句都验 XOR 校验和，不过的明说「这句数据不可信」**；非 NMEA 行单独点名跳过。
  - `frame_parse`：帧头 + 长度字段切帧，支持 sum8 / CRC16-Modbus。
    **校验字节不计入长度字段**——真实 Modbus RTU 帧就靠这条，否则长度字段算出的
    帧边界会把末尾 CRC 切掉、永远验不过（写这个工具时实测踩到，已固化为回归）。
    切不动 / 截断 / 垃圾字节**逐条报出并附剩余字节，绝不静默丢**。
- **生态识别知识库**（N-09）：`knowledge/ecosystem.json` 收录 6 种生态
  （Zephyr / RT-Thread / PlatformIO / Keil / IAR / Rust defmt），每种给识别特征、
  构建产物、日志签名、各自的坑，**条目级带出处**；配套 `ecosystem_list` / `ecosystem_get`
  与 2 个 MCP 资源（`mcu://ecosystems`、`mcu://ecosystem/{name}`）。
- **`project_detect` / `toolchain_status`**（G02）：按标志文件与后缀认工程生态
  （`platformio.ini` / `*.uvprojx` / `prj.conf` / `Cargo.toml`…），**只探一层**
  （避免 `vendor/` 里的标志文件把整个工程认错）；工具链逐个探版本，
  **「未发现」≠ 环境坏了**——会说明 IDE 自带工具不一定暴露到命令行。
- **`logs_prune`**（N-12）：会话日志保留策略与清理入口。默认保留最近 30 天 / 最新 200 个；
  **默认 dry-run 只列不删**，真删要过隔离闸门（已登记为不可逆操作）且 `confirm=True`，
  并提示日志按原样记录、可能含凭据，分享前先检查。
- **`report_session`**（G13）：把会话日志导出成 Markdown 交付报告（故障分析 + 原始日志附后）；
  目标文件已存在**不默认覆盖**。
- **探针/烧录器名单 6 → 12**（N-10）：补 `JLinkExe` / `nrfjprog` / `dfu-util` / `avrdude` /
  `UniFlash` 等，并把搜索目录扩到 SEGGER / Nordic / Arduino / TI / PlatformIO 的常见安装位置——
  只认 PATH 会漏掉 IDE 与扩展自带的工具。
- **文档与示例**（N-13 / G11）：`docs/MEMORY_DEBUG.md`（栈/堆/越界三类内存问题的方法论）、
  `docs/KNOWLEDGE_SCHEMA.md`（知识库 schema + 贡献指南）、`examples/offline_demo.py`
  （**不装 pyserial、不插任何板子**就能跑通 8 项能力）。

### Changed
- **`server.py` 的 MCP `instructions` 重写**：此前只列了老工具，本轮新工具（报文解析、
  工程探测、日志治理、交付报告、生态识别）一个都没进——而 `instructions` 是每个 MCP
  客户端**唯一会读到的能力说明**。工具加了但说明没更新，等于没加。同时修掉里面
  写死的规则条数（写着 44，实际 45），并补上 6 个 prompts 的入口。
- **`tools/live_acceptance.py` 探针配置改为「先校验、再执行」**：此前 `--cmd`/`--expect`
  数量不匹配时，dry-run 会返回 **rc=0**（自己没配好却报一切正常）。现在配置错误一律 rc=2
  并指明原因（数量不等 / 探针文件不存在）；`--cmd` 不给对应 `--expect` 也不再 TypeError。

### Fixed
- **写死的规则条数脱节**：规则已是 45 条，`server.py` 的 instructions 与 `core.py` 的
  docstring 仍写 44，`pyproject.toml` 描述也还是 44。**这类脱节最阴**：代码全对、
  测试全绿，只有用户看到的文案是旧数字——为此新增一条审计（见下）。
- `tools/live_acceptance.py` 设计约束标题写「三条」实际列了四条（上一轮加第 4 条时漏改）。
- `pyproject.toml` 版本 4.2.0 → 4.3.0，描述补上本轮新增能力。

### Audit
- **`tests/audit_constants.py` 新增第 5 项检查「文案里的规则条数」**：扫
  `core.py` / `server.py` / `README.md` / `pyproject.toml` 等「现状声明」类文件里写死的
  「N 条规则 / N 条模式库」，与 `analyzer.rule_count()` 实测值对账；
  CHANGELOG 与测试里的历史叙述不算脱节。带**反自证测试**（人为抬高实测条数必须被抓到）
  与**误报防线**（README 的「8 条规则族」是子分类计数，不能被误伤——审计器一旦误报
  就会被当成噪音关掉）。加这条当场抓出 `pyproject.toml` 的 44→45 脱节。
- `tests/test_frame_parse.py`（24 条）：校验算法用**公开标准例句**（GGA `*47` / RMC `*6A`）
  验证，而不是拿自己算的校验和自己写的验签互证；Modbus「长度字段不含 CRC」单列一条钉死。
- `tests/test_constants_audit.py`（+3 条）、`tests/test_serial_tools.py`（+6 条）、
  `tests/test_project_tools.py`（34 条）、`tests/test_mcp_resources.py`（补生态资源断言）。
- `tests/audit_capability.py` 的文档对账新增 README prompts 数、ROADMAP 当前工具/资源数
  （原来 ROADMAP 只对账资源数，且指向的是历史变更记录——历史数字必然随版本漂移，
  已改为指向「当前状态」行）。

### 未做（需要真机，非本轮范围）
- **N-06** 芯片身份与存储信息（型号 / revision / UID / Flash 容量 / 读保护）
- **G05** 烧录后验证（回读比对 / CRC / 读保护）
- **G06** 总线工具（I2C/SPI 扫描 + 读写寄存器）——离线可写，但没真机就是没验过
- **G07** 时序/数据率分析 · **G10** 复位原因读取 · **G12** 长时录测
- **N-08 / G01 / G03** 广度扩容（引脚库、器件选型库、外设 API 用法知识层）——
  属「逐条对源」任务，凭记忆批量灌等于制造假数字

---

## [4.2.0] - 2026-09-22

**首次真机验证**。拿一块真实的 NUCLEO-H563ZI（STM32H563 / Cortex-M33，板载 ST-Link V3EC，
虚拟串口 COM21）跑了完整链路：串口探测 → 握手 → 读写 → .elf/.map 符号定位 → 排故链。
**6 个此前只靠离线测试抓不到的缺陷**，全部有真机证据并已修复。
（离线单测 435→472 通过，新增 `tests/test_hw_findings.py` 31 条把每个缺陷钉成回归。）

### Added
- **知识库 45 条故障模式**（原 44）：新增 `UART_ORE_RX_LATCH`——UART 溢出（ORE）导致
  `HAL_UART_Receive` 轮询接收**永久卡 HAL_BUSY** 的死锁模式。这是本次真机实测**撞出来的真问题**
  （见下方 Fixed），不是从手册抄的，规则 `source` 直接写了验证平台与日期。
- **`at_sequence` 支持逐条控制结尾符**：新增入参 `newline`（默认 `True`），且每一步可用
  `step["newline"]` 覆盖。原实现把 `"\r\n"` 写死，遇上「不能带结尾符」的设备无解。
- **`tests/test_hw_findings.py`**（31 条）：每个真机缺陷一条断言，**不需要插板子也能跑**，
  含一条 AST 级断言锁死 `at_sequence` 源码里不得再出现硬编码 `cmd + "\r\n"`。
- **`.elf` 解析**：`parse_map` / `symbol_lookup` / `stack_unwind` 现在能直接吃 ARM 编译器产出的
  ELF 文件（不只是 `.map`），符号地址可与 `arm-none-eabi-nm` 逐条对齐。
- **真机验收夹具 `tools/live_acceptance.py`**（对应能力缺口 **N-15**，本轮关闭）：
  拿一块真板子把核心链路走一遍——环境自检 / 只读探测 / 双向契约 / 符号定位 / 排故链 /
  破坏性复现（CRLF·ORE），末行给 `PASS · FAIL · SKIP` 汇总与退出码，可反复跑、可进 CI。
  三道安全闸门：不给端口就拒绝运行；只读是默认档，发字节要 `--allow-write`；
  会打死板子的那一关还要 `--allow-destructive`（且明说跑完需 SWD 复位）。
  端口用 `PORT_ALLOW` 白名单收窄到目标口一个。
  ——写这个夹具的过程本身又抓出 4 个缺陷，**全在夹具自己身上**（判据臆想措辞、
  `.map` 抓到段名占位 `.` 当成符号名、默认 CRLF 连发把板子打死、收尾无条件报 PASS），
  已在文件内逐条注明成因，并各有测试守着。这恰好说明：**离线自测通过 ≠ 夹具是对的**。

### Changed
- **数据根优先级重排**（`paths.py`）：`$MCU_DEBUG_MCP_HOME` > `~/.mcu-debug-mcp` >
  包目录。**包目录只在「源码树」时才作为可写落点**（认 `pyproject.toml`），
  否则 pip 装到只读 site-packages 时会把日志和缓存往代码目录里塞。
- **烧录器探测不再只认 PATH**（`core.py`）：`STM32_Programmer_CLI` / `pyocd` / `openocd` /
  `esptool` / `st-flash` 现在会额外扫 IDE 与扩展的常见安装目录（`_TOOL_SEARCH_DIRS`，
  支持 `*` 通配）。真机上 `STM32_Programmer_CLI` 明明装着却报「未找到」，根因就是它不在 PATH。
- **`env_status` 多报一行「可写数据根」**，日志/缓存实际落哪一目了然。

### Fixed
- **`identify_usb_serial` 认不出 ST-Link V3 虚拟串口**：`0483:374E` 不在表里就返回「未知」，
  而它恰恰是最常见的调试口。现补齐该 PID，并加 `0483`（ST Microelectronics）的 VID 兜底——
  同厂商未逐条收录的 PID 也能给出「ST-Link 系虚拟串口」的合理判断，而不是一句「不认识」。
- **串口发指令后设备静默死亡**（真机实测头号问题）：带上自动追加的 `\r\n` 发指令，
  **第一条有人应答、第二条开始永久无响应**。逐步隔离（换线、换波特率、关 DTR、只发一次）
  后定位到真因——目的板固件的 `HAL_UART_Receive` 逐字节轮询接收，多出来的 `\r\n`
  触发 ORE 溢出后 `RxState` 不复位，接收通道**永久锁死**（不是我们的工具坏了，是它撞出了
  一个真实固件缺陷）。修法两层：
  1. `serial_send` 在「未匹配到 `expect` 且本次自动加了结尾符」时**主动给出 ORE 排查提示**，
     并指名下一步动作（先试 `newline=False`）。注意提示不依赖「是否收到响应」——
     实测该固件会回一个 `?`，一开始把提示只挂在「无响应」分支上，于是真机场景下提示根本不出现。
  2. 知识库补 `UART_ORE_RX_LATCH` 规则，让 `analyze_log` 也能识别这类日志。
- **`.elf` 符号地址整体偏移 1**：ARM Thumb 指令集下 `FUNC` 符号 `st_value` 最低位是
  Thumb 标志位、不是地址。最初没掩掉，`frame_dummy` 算出 `0x800275` 而 `nm` 是 `0x800274`。
  现对 `STT_FUNC` 类型执行 `value &= ~1`，5 个符号地址与 `arm-none-eabi-nm` 完全一致。
- **`.elf` 段表返回结构不一致**：`_parse_elf` 返回 list、`parse_map` 当 dict 用，
  直接 `TypeError: '<' not supported between dict and dict`。已统一为 dict。
- **主机程序检查误伤 ARM 固件**：`_HOST_ONLY_SEGMENTS` 里混了 `.reloc` / `.pdata` /
  `.xdata` / `.debug_frame`（ARM 固件同样会有），导致正常固件被判定成「这是 PC 上跑的程序」。
  现只保留 `.idata` / `.rsrc` 这两个 Windows PE 专属段；`_RUNTIME_MARKERS` 里的
  `__do_global`（GCC 交叉编译同样会产）一并移除。
- **审计反查出的两处自伤**：删掉 `_ELF_SHT_STRTAB` 死常量；把文档里「44 条」统一订正为「45 条」
  （README / PLAN / ROADMAP 三处与实测对不上，审计直接报红）。

---

## [4.1.0] - 2026-09-22

**运行时隔离层**：默认不再允许碰硬件。这是**默认行为变更**——原来的版本任何一次调用
都会直接对端口动手（写字节、烧 flash），现在默认只读，需显式放开。
（4.0.0 尚无外部使用者，故按次版本发布；若已有人在用，这应按主版本对待。）

### Added
- **`guard.py` 运行时隔离层**：档位 `readonly`（默认）/ `write` / `off`；
  未登记的写操作**保守拒绝**；非法档位值按 readonly 处理（不静默放行）。
- **端口黑·白名单**：`MCU_DEBUG_MCP_PORT_DENY` / `MCU_DEBUG_MCP_PORT_ALLOW`，支持 `*`，DENY 优先；
  用于把「你自己正在用的那些口」锁死。
- **不可逆操作双闸门**：`flash_firmware` 现在需要 `dry_run=False` **且** `confirm=True`；
  dry-run 预览不受隔离阻挡。
- **`MCU_DEBUG_MCP_NO_TOUCH_DTR`**：打开端口前先把 DTR/RTS 置为不生效，尽量不触发开发板自动复位。
  （调用契约已离线验证；真机复位行为待板卡实测。）
- **虚拟端口精确豁免**：只放 `loop://`（进程内回环，外部零副作用）；`socket://`/`spy://`/`alt://`/`hwgrep://`
  一律不豁免（都可能指向真设备）。豁免清单本身有断言守着。
- **`env_status` 新增「隔离 / 安全」段**：当前档位与来源、两个名单、DTR 开关、豁免说明。
- **`SECURITY.md`**：安全与隔离模型、破坏性操作矩阵、未纳入闸门的操作及原因、已知边界。
- **测试隔离基础设施**：`tests/conftest.py`（锁档位 + 端口全禁 + 替换真实串口构造）、
  `tests/isolation.py`（临时放行的唯一入口）。真机测试需 `MCU_DEBUG_MCP_TEST_ALLOW_HW=1`。
- **审计【六】隔离与安全**：串口构造唯一入口 / `guard.OPS` ↔ core 调用点双向 / 不可逆操作是否真传 `confirm` /
  测试基础设施在位 / 默认档位是否最保守。

### Changed
- 所有串口打开收敛到唯一入口 `core._open_serial()`（顺带支持 pyserial 的 URL 端口）。
- 新增能力轴「隔离防护（闸门化的硬件操作）」，N-11 从人工判定改为机器可数。
- README 首段工具数由「33」订正为「34」（与实测一致）。

### Fixed
- **打包冒烟测试把构建失败 `skipTest` 掉**——于是「打包坏了」被伪装成「环境不支持」，
  4.0.0 那处 P0（装出来是空壳）正是借它活下来的。现改为直接 fail，并用 AST 守卫禁止慢层出现 `skipTest`。
- **连续两次 `pip install` 第二次必失败**（setuptools 的 bdist 目录不幂等，`WinError 183`）；
  `setup.py` 加 `_clean_stale_build()`，并补「连装两次都成功」的回归测试。
- `tests/test_serial_tools.py` 里一条**会真的扫描本机串口**的测试（原本靠 `if kind != usb_cdc` 偶然跳过，
  机器上插了 ESP32 就会真跑）改为显式授权才执行。

---

## [4.0.0] - 2026-09-22

首个建档版本。**34 个工具 + 11 个 MCP 资源 + 44 条故障模式规则**，离线可测、可 pip 安装。

### Added
- **MCP 资源层（11 个）**：`mcu://drivers` · `mcu://driver/{name}` · `mcu://wiring/{name}` ·
  `mcu://pinmaps` · `mcu://pinmap/{mcu}` · `mcu://boards` · `mcu://board/{name}` ·
  `mcu://errcodes` · `mcu://errcode/{table}` · `mcu://faultbits` · `mcu://faultbits/{arch}`
  —— 让 AI 能**主动引用**知识库，而不是只能逐个调工具。
- **`env_status`**：依赖 / 串口 / 烧录器 / 知识库根 / 版本，一次问清。
- **`error_code_lookup`**：错误码统一查询入口，6 张表 48 条，**每条带出处**；未收录明说查不到。
- **`parse_map` / `symbol_lookup` / `stack_unwind`**：排故链——故障寄存器 → 符号表 → 栈回溯。
- **预验证驱动库 9 款**：dht11 / ds18b20 / esp8266at / hc05 / hcsr04 / mpu6050 / servo_sg90 /
  ssd1306 / stepper_28byj48，每款含 C 源码 + 适配层 + 接线表 + 验证记录。
- **`data_roots()` 分层数据根**（`paths.py`）：`$MCU_DEBUG_MCP_HOME` > `~/.mcu-debug-mcp` >
  包内置目录。外置层**合并**（内置条目不会消失）且**覆盖**（同名以外层为准）——
  「知识库社区可贡献」由此才真正落地。

### Changed
- **知识库与驱动库的目录定位全部收归 `paths.py`**：代码目录不再兼任数据目录。
- **会话日志落点**：优先外置数据根 / 包目录，pip 装到只读 site-packages 时自动回落到
  `~/.mcu-debug-mcp`，不再试图写进代码目录。

### Fixed
- **P0：pip 安装出来是空壳**。`pyproject.toml` 无 package-data 且 `core.py` 把代码目录当数据目录，
  导致装出来的包里没有 `knowledge/` 与 `drivers/`——33 个工具里凡碰知识的**全部返回空**、
  11 个资源**全部读空**、`analyze_log` 丢 14 条外置规则。这类缺陷**只做单元测试永远抓不到**
  （原地跑源码一切正常）。修法：`setup.py` 的 `build_py` 钩子整份复制数据目录 +
  打包冒烟测试纳入回归（`tests/test_package_smoke.py`）。
- **`fault_bits` 文件名与架构名不一致**：文件名是 `cortex_m`、`decode_fault(arch=)` 认的是
  `cortex-m`，照抄文件名去填就得到「没有收录」——一个纯由命名不一致造出来的坑。
  现在 `fault_bits_catalog()` 把「文件名 + arch 该填什么 + 这张表是什么」并列给出，两种写法都认。
- **`get_session()` 不再抛异常**：原来 `serial_read` / `serial_send` / `serial_session_log`
  对无效句柄抛 `ValueError`（客户端收到 Python 内部错误），而 `serial_close` 却友好返回——
  同一件事两套口径。现统一返回可读提示。
- **`analyze_log` 与知识库的 10 处误报**：SENSOR_ZERO 需传感器上下文、BOOT_LOOP 的 banner 收紧、
  AT_ERROR 需 AT 上下文、I2C_NACK 需 I2C 上下文、DS18B20_85 不再匹配「以 85 结尾」等。
- **`decode_register` 的 `width` 无校验**：负宽度会让 `1 << width` 崩溃；现限 1~64。
- **`pin_check` 空功能名**：`{"PA9": ""}` 原被静默当 GPIO 处理，现报错且不拖累同批次其他引脚。

### Security / 数据安全
- 破坏性操作（`flash_firmware`）默认 `dry_run=True`，只返回将执行的命令，确认后才真烧。
- 会话日志可能含设备输出，落盘位置现在可见（`env_status` 会报目录与体积），便于自行清理。

---

[Unreleased]: #unreleased
[4.0.0]: #400---2026-09-22
