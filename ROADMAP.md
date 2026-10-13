# 嵌入式开发全链路 · 覆盖盘点与 MCP 开发方向

> ⚠️ **待办清单的权威已迁至 [PLAN.md](PLAN.md)**（总规划：广度×深度×工程完备 + 缺口全集 + 批次 + 决策点）。
> 本文档保留为**全链路盘点依据**（8 段职能量表）与**历史记录**（第一批详细设计、B 线过程），不再承担待办职责。

> 目的：把嵌入式开发全链路完整列出，找出所有空白，据此决定 mcu-debug-mcp 该开发什么。
> 定稿：2026-09-21
> 方向已定：**先补 MCP 事实层空格；首批做 Cortex-M 排故链。**

---

## 一、方法论（先说清怎么定的方向）

### 走过的弯路

**错误顺序**：先看手上有什么（已有 skill、已有 MCP 工具）→ 想办法把它们串起来。
**后果**：必然出现「为串联而串联」——两块东西本不相干，硬接一个接口，中间永远是断的。

### 正确顺序

1. **列全链路**：一个嵌入式项目从零到交付，工程师实际要做的每一步，**不管现在有没有工具支持**
2. **标归属**：每一步该由 MCP（事实）还是 Skill（方法）承担；也可能是手工做、或根本不需要工具
3. **找空格**：归属明确、但两边都没有的，才是要开发的
4. **补空格**：针对空格单独开发，**不与已有资产强行挂钩**
5. **查接口**：补完后再检查相邻两段的数据能不能接上（这一步在最后，不是最先）

### 判据

**链路要能从头走到尾不断。断在哪一段，那一段就是开发目标。**

---

## 二、MCP 与 Skill 的分工判据

| | MCP（事实层） | Skill（方法层） |
|---|---|---|
| **回答** | 「是什么」 | 「所以该怎么办、怎么证明它对了」 |
| **产出** | 可复现的原始事实 | 可复用的工作流 |
| **判据来源** | 数据手册、SVD、厂商头文件、硬件实测 | 实战经验、踩坑记录、领域方法论 |
| **能否独立验证** | 能（真机跑一遍，结果必须一致） | 难（靠真实场景跑通） |
| **扩展方式** | `knowledge/*.json` 加数据；`core.py` 加解析器 | 新增 `SKILL.md` |
| **典型形态** | 一次调用给一个事实 | 一串步骤带判据和陷阱 |

**一句话口径**：
- 需要**碰硬件**或**查权威表**才能得到答案 ⟹ MCP
- 需要**经验判断**、**按项目变化**、**要给人讲清为什么** ⟹ Skill

---

## 三、全链路完整盘点（8 段 · 不预设任何已有资产）

### 段 1 · 立项与选题

| 要做的事 | 归属 | 现状 |
|---|---|---|
| 判断题目难度、周期、预算 | Skill | 有 |
| 查题目是否被做烂、如何差异化 | Skill | 有 |
| 开题报告结构、技术路线 | Skill | 有 |

> 纯判断，不碰硬件，MCP 不介入。**无空格。**

---

### 段 2 · 选型与硬件设计

| 要做的事 | 归属 | 现状 |
|---|---|---|
| 选 MCU（引脚数/外设/Flash 够不够） | MCP（查数据手册表） | **空** |
| 查开发板有什么、板载外设占了哪些脚 | MCP（查板级表） | 有 |
| 引脚分配冲突检查（UART/I2C/SPI/PWM 复用） | MCP（查复用映射表） | 有 |
| 外设资源预算（定时器/ADC 通道够不够） | MCP（查数据手册表） | **空** |
| 电源预算（总电流、LDO 够不够） | 手工/MCP | **空**（可选） |
| 选外设模块（传感器型号对比、量程、接口） | MCP（器件库）+ Skill | **空** |
| 原理图审查 | 手工（超范围） | 不做 |

> **空格 1**：器件/选型库。现在只有 MCU 引脚表和板级表，缺「传感器/模块」维度（接口、地址、量程、坑）。

---

### 段 3 · 搭环境与工具链

| 要做的事 | 归属 | 现状 |
|---|---|---|
| 判断编译器/工具链是否装好、版本对不对 | MCP（探测本机） | 部分（只探烧录工具） |
| 工具链静默失败的诊断 | Skill | **空** |
| SDK/框架版本兼容判断（IDF 版本 vs 芯片） | MCP（版本表）+ Skill | **空** |
| 依赖库、头文件缺失排查 | 手工 | 不做 |

> **空格 2**：工具链探测。`flash_tool_detect` 只探烧录器，没探编译器/GDB/SDK 版本。

---

### 段 4 · 写代码

| 要做的事 | 归属 | 现状 |
|---|---|---|
| 生成通用驱动（I2C/SPI/UART 收发） | Skill（模板库） | 有 |
| 生成器件驱动（读某传感器） | MCP（预验证驱动）+ Skill | 有 9 款 |
| 查寄存器怎么配、位域含义 | MCP（SVD 解码） | 有 |
| 静态代码审查（ISR 安全、MISRA） | Skill | 有 |
| 判断 API 用法对不对（某外设怎么初始化） | MCP（外设知识表） | **空** |
| RTOS 任务/优先级设计 | Skill | **空** |

> **空格 3**：外设 API 用法知识层。`decode_register` 能解释位域，但「这个外设怎么用、初始化顺序、常见配错」没有。

---

### 段 5 · 编译与烧录

| 要做的事 | 归属 | 现状 |
|---|---|---|
| 编译报错解读（链接错误、段溢出） | MCP（错误码表）+ Skill | 部分 |
| 查看 RAM/Flash 占用（.map 解析） | MCP（解析器） | **空** |
| 栈用量分析（最大栈深） | MCP（解析器） | **空** |
| 烧录固件 | MCP（调工具） | 有 |
| 烧录失败诊断 | MCP（知识表） | 有（8 条族） |
| 烧录后回读校验（写的对不对） | MCP（碰硬件） | **空** |
| 芯片保护位/读保护状态 | MCP（碰硬件） | **空** |

> **空格 4**：构建产物分析（.map 解析：段占用、栈深、符号地址表）。
> **空格 5**：烧录后验证（回读比对、读保护状态）。

---

### 段 6 · 上电调试与联调

| 要做的事 | 归属 | 现状 |
|---|---|---|
| 打开串口、读日志 | MCP | 有 |
| 波特率判断 | MCP | 有（含 USB CDC 短路） |
| 日志故障模式识别 | MCP（45 条模式） | 有 |
| RTT/SWD 读日志 | MCP | 有（实验性） |
| 总线扫描（I2C 上有谁、地址对不对） | MCP（碰硬件） | **空** |
| 总线时序分析（波形、丢帧、时钟拉伸） | MCP（碰硬件） | **空** |
| 时序/节拍变化检测（日志率突变、周期抖动） | MCP（数据流分析） | **空** |
| 两设备对话录制 | MCP | 有 |
| Modbus/协议帧解析 | MCP | 有（Modbus） |
| 其他协议帧解析（自定义二进制、NMEA） | MCP（帧解析器） | **空** |

> **空格 6**：总线工具（I2C/SPI 扫描 + 读写寄存器）。
> **空格 7**：时序与数据率分析。
> **空格 8**：通用帧解析器（现在只有 Modbus 和 AT）。

---

### 段 7 · 排故

| 要做的事 | 归属 | 现状 |
|---|---|---|
| 认出「跑飞/死机」（HardFault/看门狗复位） | MCP（模式识别） | 有 |
| **定位跑飞到哪一行** | MCP（fault 解码 + 符号反查） | **空 ← 最大空白** |
| 栈溢出判断（跑飞 vs 栈爆） | MCP + Skill | **空** |
| 死锁/卡死定位 | MCP（RTOS 状态）+ Skill | **空** |
| 内存问题（堆碎片、越界踩内存） | MCP + Skill | **空** |
| 上电不启动排查 | MCP（知识表）+ Skill | 有（部分） |
| 烧不进排查 | MCP（知识表） | 有 |
| 边界问题（看门狗、时钟、复位原因） | MCP（碰硬件读复位源） | **空** |

> **空格 9**：**故障定位链**（fault 寄存器解码 + `.map` 符号反查 + 栈回溯）。全链路最大空白。
> **空格 10**：复位原因与运行状态读取。
> **空格 11**：内存问题诊断方法论（Skill）。

---

### 段 8 · 验证与交付

| 要做的事 | 归属 | 现状 |
|---|---|---|
| 驱动自测 | Skill | 有 |
| 参数计算验证（PID、PWM 定时器值） | Skill | 有 |
| 打包固件（CRC、分区、镜像头） | Skill（工具集） | 有 |
| 防砖审计 | Skill | 有 |
| 老化/压力测试数据采集 | MCP（碰硬件，长时间录） | **空** |
| 生成接线图 | MCP（查表出图）+ 手工 | **空**（低优先） |
| 生成交付 README/调试记录 | Skill | **空**（低优先） |
| 出厂测试脚本与判定 | Skill + MCP | **空** |

> **空格 12**：长时录测与数据采集。
> **空格 13**：交付物自动生成。

---

## 四、空格汇总（开发清单）

| # | 空格 | 段 | 归属 | 卡链路 | 状态 | 备注 |
|---|---|---|---|---|---|---|
| **9** | **故障定位链**（fault 解码 + 符号反查 + 栈回溯） | 7 | MCP | ★★★ | ✅ 已完成 | 认出跑飞但定不到位，链路走到这里就断 |
| **4** | 构建产物分析（.map：段/栈深/符号表） | 5 | MCP | ★★★ | ✅ 已完成 | 也是空格 9 的前置依赖 |
| **6** | 总线工具（I2C/SPI 扫描 + 读写寄存器） | 6 | MCP | ★★★ | ✅ 已完成 | 8 个工具（bridge_probe/i2c_scan/i2c_read_reg/i2c_write_reg/spi_scan/spi_read_reg/onewire_scan/rs485_send）+ MCUDBG/1 桥固件（`tools/bridge_fw/`，NUCLEO-H563ZI）；离线 62 断言 + 双变异验证；**真机闭环待做**：板子回插后跑 `python tools/bridge_fw/live_check.py`（自动烧录→验证→恢复原固件） |
| **5** | 烧录后验证（回读比对、读保护） | 5 | MCP | ★★ | ⬜ 待做 | 烧完不知道对不对 |
| **10** | 复位原因与运行状态读取 | 7 | MCP | ★★ | ⬜ 待做 | 死机后第一手证据 |
| **3** | 外设 API 用法知识层 | 4 | MCP | ★★ | ⬜ 待做 | 配错外设是最常见问题 |
| **1** | 器件选型库（传感器/模块维度） | 2 | MCP | ★★ | ⬜ 待做 | 只有 MCU 表和板表 |
| **7** | 时序 / 数据率分析 | 6 | MCP | ★ | ⬜ 待做 | 丢帧、节拍变慢 |
| **8** | 通用帧解析器（非 Modbus） | 6 | MCP | ★ | ⬜ 待做 | |
| **2** | 工具链探测（编译器/GDB/SDK 版本） | 3 | MCP | ★ | ⬜ 待做 | |
| **11** | 内存问题诊断方法论 | 7 | Skill | ★ | ⬜ 待做 | |
| **12** | 长时录测与采集 | 8 | MCP | ★ | ⬜ 待做 | |
| **13** | 交付物自动生成 | 8 | Skill | ○ | ⬜ 待做 | 低优先 |

**进度：13 个空格已完成 2 个（9 + 4，同一批），剩 11 个。**

**关键结论**：13 个空格里，**Skill 侧只有 3 个**（11 / 13 / 段 3 工具链诊断），其余 10 个全是 MCP 事实层空格。
⟹ **mcu-debug-mcp 的完善，主体就是补这 10 个事实层空格；Skill 只在「有事实但不会判断」处补。**

---

## 五、开发方向（按空格，不按已有资产）

### 开发批次

| 批次 | 内容 | 空格 | 状态 |
|---|---|---|---|
| **第一批** | 排故链（Cortex-M 优先） | 9 + 4 | ✅ **已完成**（离线全链 + **真机全链**：`tools/fault_live.py --flash` 在 NUCLEO-H563ZI 上 9/9 PASS） |
| 第二批 | 接外设（I2C/SPI 扫描 + 烧录后验证） | 6 + 5 | **下一批** |
| 第三批 | 运行诊断（复位原因 + 时序分析） | 10 + 7 | 待定 |
| 第四批 | 知识层补广度（外设用法 + 器件库 + 帧解析） | 3 + 1 + 8 | 待定 |

### B 线：对已有资产的改进升级（原稿只有 A 线，2026-09-21 补）

A 线 = 「加新工具填空白」；B 线 = 「把已经有的做扎实」。两条线都要走，否则等于把新工具堆在没验过的地基上。

| # | 改进项 | 状态 | 说明 |
|---|---|---|---|
| **B①** | 知识条目级 source 溯源 | ✅ **已完成** | 11 个文件 **193 条**数据全部补条目级 source；`validate_knowledge.py` 从「只查文件级」升级为「**强制条目级**」；另补上 fault_patterns.json 缺失的顶层 source。**真正的价值是建立机制**：原来靠自觉，现在漏标会直接让校验红掉 |
| **B②** | 知识库广度 | ✅ **已完成** | ① 新增 `error_code_lookup` 统一查询工具（6 张表 48 条码，每条带出处，未收录明说查不到）；② 扩错误码表 4→6 族（+`esp_idf` esp_err_t 17 条、+`at_common` ESP-AT 通用响应 11 条）；③ 扩开发板 2→3 款（+ESP32-DevKitC-32E，含 8 条板级坑）。**pinmap 刻意不扩**——逐脚功能必须对数据手册核，凭记忆批量灌 = 制造假数字，留给「逐条对源」的独立任务 |
| **B③** | 老 28 工具真实数据双向验证 | ✅ **已完成** | analyze_log（44 规则）+ modbus_parse + decode_register_svd + at_sequence + baudrate_autodetect + pin_check + driver_get + flash_tool_detect 全部过了真实风格数据回放；抓出并修掉 **9 类假答案**（帧长静默/超位宽静默/非法 bitRange 恒 0/空驱动返回空串/remap 双占不提示/口全打不开无结论等），新增 32 条双向回归 |
| **B④** | 同一事实多处定义的一致性审计 | ✅ **已完成** | 新增 `tests/audit_constants.py`（+ `test_constants_audit.py` 纳入回归，含**反自证**注入测试）：机器校验 USB VID:PID 三处定义 / 4 张错误码表 fallback↔知识库 / HardFault 位 规则↔知识库 / 外置规则条数。当场抓到并修掉：`core._USB_CDC_IDS` 的 `2341:804D` 在 analyzer 表缺失（与 serial_list 同型）；Modbus 异常码 fallback 与知识库释义文案不一致 |
| **B⑤** | 真机闭环 | ✅ **已完成** | 第一批收口判据 1（NRF52840 / STM32H5 人为触发 HardFault 抓寄存器走全链）——**2026-09-22 在 NUCLEO-H563ZI（STM32H5 / Cortex-M33）上跑通**，一键复现 `python tools/fault_live.py --flash`。★ 顺手抓到并修掉一个**只有真机才暴露**的缺陷：`stack_unwind` 把 EXC_RETURN 的 bit2/bit3 含义对调（ARMv7-M 标准值里这两位恒同向，旧测试永远假通过） |
| **B⑥** | 协议面 + 入参边界验证 | ✅ **已完成** | ① **MCP resources 全量实测**：3 静态 + 4 模板共 7 个全部可读、错误分支友好（顺带抓出文档写「8 个」的错，实际 7 个）；② **性能压测**：analyze_log 线性 ~1.1 MB/s（2 MB 仅 1.85 s）、44 条规则**无灾难性回溯**；③ **33 个工具边界入参实测**抓出 4 处硬伤并修复：`decode_register(width=-1)` **抛未捕获异常**（negative shift count）、`serial_read/send/session_log(无效句柄)` **抛异常**而 `serial_close` 友好拒绝（同一件事两套口径）、`pin_check({"PA9":""})` **空功能名被静默当 GPIO** 并给出「无硬冲突」假通过、`driver_get("")` 走错分支输出「驱动  目录存在」；④ 纠正 2 条**保护错误设计**的自证式测试（`assertRaises(ValueError)` 要求工具抛异常）。新增 `tests/test_boundary_robustness.py` 18 条双向回归 |
| **批次 0** | 工程地基（打包 / CI / 自检 / 审计） | ✅ **已完成** | 详见 `PLAN.md` §4。修掉 **P0「pip 装出来是空壳」**（`setup.py` 的 `build_py` 钩子整份复制 `knowledge/`+`drivers/`；数据根分层 `$MCU_DEBUG_MCP_HOME` > `~/.mcu-debug-mcp` > 包内置，外置层合并+覆盖）；新增 `env_status` 自检工具（34 个工具）、`LICENSE`/`CHANGELOG.md`/GitHub Actions CI；新增 **`coverage.json` + `tests/audit_capability.py`**——把「文档 / 清单 / 实现」三方对账机器化，26 项缺口与逐工具 L0–L6 深度阶梯**自动浮出，不靠人发现**（这才是根治「一次次补」的那一步）。新增 `tests/test_package_smoke.py`（真装+子进程+剔除仓库根）与 `tests/test_capability_audit.py`（含注入法反自证）。全量 **435 tests 0 fail**，审计 0 脱节 0 死代码 |
| **B⑧** | **隔离与安全**（默认禁写硬件 / 端口名单 / 不可逆二次确认 / 测试强制隔离） | ✅ **已完成** | 新增 `guard.py` 运行时隔离层：默认 readonly 挡掉写串口/AT/波特率扫描/双口对录/烧录；端口黑/白名单（DENY 优先）；不可逆操作双闸门（`dry_run=False` + `confirm=True`）；`_open_serial` 收成唯一开端口入口（DTR/RTS 抑制在此生效）。测试侧 `conftest.py` 强制隔离（锁档位 + 端口全禁 + 替换串口构造），`isolation.py` 是临时放行唯一入口。审计新增【六】隔离与安全静态对账（串口构造唯一入口 / OPS↔调用点双向 / confirm 是否真传 / 测试基础设施在位）。顺带修两处真问题：**打包冒烟把构建失败 skip 成环境问题**（P0 缺陷借它活下来）、**连续两次 pip install 第二次必失败**（bdist 残留 WinError 183）。 |
| **B⑦** | 知识库浏览面补全（资源层对称） | ✅ **已完成** | `error_codes` / `fault_bits` 两个知识库此前**没有任何 MCP 资源入口**（只有 drivers/pinmap/boards 三类可浏览，AI 想主动引用只能靠调工具），护城河的知识层存在浏览盲区。补 4 个资源：`mcu://errcodes`（清单）+ `mcu://errcode/{table}`（整表原文）、`mcu://faultbits`（清单）+ `mcu://faultbits/{arch}`（原文），资源总数 7→11（5 静态 + 6 模板）。顺带修三处：① fault_bits 的**文件名（`cortex_m`）与 `decode_fault(arch=)` 认的架构名（`cortex-m`）不是同一个字符串**——清单里把两者并列，「没有收录」文案也点明该填哪个；② 清掉全仓**零调用**的死代码 `core.deps_status()`；③ 表名解析收敛为唯一口径 `_resolve_errcode_table()`（问单码与读整表共用，防两套解析脱节）。新增 `tests/test_mcp_resources.py` **17 条**（资源清单精确比对 / 11 个资源逐个真读 / 明细资源排版不被重排 / 拒绝分支友好 / 死代码不回归）。全量 **417 tests 0 fail 0 skip** |
| **B⑨** | 广度/深度补齐（prompts / 生态 / 探针 / 工程探测 / 日志治理 / 报文解析 / 文档示例） | ✅ **已完成** | ① **MCP prompts 0→6**——三原语此前只用了两个；prompts 不依赖任何客户端特有 Skill，任何 MCP 客户端都能进，是最便宜的一条广度；② **生态识别** `knowledge/ecosystem.json` 6 种（Zephyr / RT-Thread / PlatformIO / Keil / IAR / Rust defmt）+ `ecosystem_list`/`ecosystem_get` + 2 个资源；③ **探针名单 6→12**（+JLinkExe / nrfjprog / dfu-util / avrdude / UniFlash…）与搜索目录（SEGGER / Nordic / Arduino / TI / PlatformIO）；④ **工程结构 + 工具链探测** `project_detect` / `toolchain_status`（只探一层，认不出明说，未发现≠环境坏）；⑤ **会话日志治理** `logs_prune`（默认 dry-run 只列不删，真删过 guard 闸门且需 confirm）与**交付物** `report_session`（会话日志 → Markdown 报告，不默认覆盖）；⑥ **报文解析** `nmea_parse`（GPS/北斗逐句验 XOR，不过的明说不可信）+ `frame_parse`（帧头+长度字段切帧；**校验字节不计入长度字段**——Modbus RTU 就靠这条，否则 CRC 会被切掉、永远验不过）；切不动/截断/垃圾字节**逐条报出，绝不静默丢**；⑦ 文档与示例：`docs/MEMORY_DEBUG.md`（内存排故方法论）、`docs/KNOWLEDGE_SCHEMA.md`（知识库 schema + 贡献指南）、`examples/offline_demo.py`（无硬件无 pyserial 也能跑 8 项能力）。**当前 MCP 面：工具 59 个 / 资源 16 个 / prompts 6 个**（B⑨ 收口时点为 47；G06 总线工具 +8，2026-09-22；G01/G03 器件选型库 + 外设 API 用法层 +4 工具 +3 资源，2026-10-13） |

---

## 六、第一批详细设计：Cortex-M 排故链（空格 9 + 4）　✅ 已完成（2026-09-22）

> **状态**：4 个工具全部落地并注册进 server（28 → 33 工具）；知识库过校验 0 错误；
> Python 全量 **509 passed / 2 skipped**（两个 skip 均需显式授权：真机 USB CDC 用例 + 该口非 CDC 的对照用例）；全链路用真实 .map/.elf 跑通，并在 **NUCLEO-H563ZI 真机上**跑了完整验收（见文末验收证据）。
> **已做**：STM32H5（NUCLEO-H563ZI / COM21）真机验收——串口全链 + 真实固件 `.elf`/`.map` 符号定位 + 排故链，固化为 `tools/live_acceptance.py`（N-15 关闭）。
> **已补（2026-09-22 真机跑通）**：人为触发 HardFault 抓寄存器（`decode_fault → stack_unwind` 的寄存器侧）——在 NUCLEO-H563ZI 上走通全链，见 §6 收口判据 1 与 `tools/fault_live.py`。

### 为什么这两块必须一起做

它们是同一条数据链的两端，分开做必然接不上：

```
.map/.elf ──[parse_map]──→ 符号地址表 + 段占用 + 栈深
                              │
fault 寄存器 ─[decode_fault]─→ 故障类型 + 出错地址
                              │
                              ↓
                       [symbol_lookup] ← 依赖符号地址表
                              │
                              ↓
                  地址 0x08004A2C → mpu6050_read+0x1C
                              ↓
                     [stack_unwind] → 调用链还原
```

### 新增工具（4 个）

| 工具 | 输入 | 输出 |
|---|---|---|
| `parse_map` | `.map` 或 `.elf` 路径 | 段占用（Flash/RAM 用量）、最大栈深、函数起始地址表 |
| `decode_fault` | CFSR / HFSR / BFAR / MMFAR 原始值 | 人话拆解 + 成因 + 排查方向 |
| `symbol_lookup` | 地址（单个或列表）+ 符号表来源 | `函数名+偏移` |
| `stack_unwind` | fault 时 dump 的栈帧（R0-R3,R12,LR,PC,xPSR）+ `.map` | 调用链还原，标出出错函数 |

### 复用现成基础

- `decode_register(value, fields, width)` 位域引擎**已完整且通用**，直接吃 fault 位域表，不用新写解码逻辑
- `knowledge/` 目录结构、`validate_knowledge.py` 校验流程现成
- `analyze_log` 已能认出 `HARDFAULT`，新工具是接在它后面的一环

### 新增知识库

| 文件 | 内容 |
|---|---|
| `knowledge/fault_bits/cortex_m.json` | CFSR（MMFSR/BFSR/UFSR 三段）/ HFSR / BFAR / MMFAR 全部位域 + 每条成因 + 排查方向 |

**每条位域必须标 `source`**（ARM Cortex-M 权威指南 / 具体芯片手册），过 `validate_knowledge.py` 0 错误。

### 关键陷阱（要写进工具输出，不能只写文档）

- **`IMPRECISERR` 的 BFAR 地址不可信**：总线写已延迟，报告地址与实际出错地址无关，只能当"有个总线访问出错"的信号，不能按地址去查
- **`PRECISERR` 的 BFAR 才准**：精确总线错误，地址可直接反查
- **`MMARVALID` / `BFARVALID` 位必须先查**：valid 位为 0 时 BFAR/MMFAR 是垃圾值，直接读会得出错误结论
- **栈溢出 vs 跑飞的区分**：栈溢出时 PC 往往落在合法函数里、SP 越界；跑飞时 PC 常在非法地址
- **Cortex-M0/M0+ 没有 CFSR 分项**：只有 HardFault，且无 BFAR/MMFAR，解码路径不同

### 验证方式

**真机已验证（2026-09-22）**：
- ✔ **STM32H5（NUCLEO-H563ZI / Cortex-M33）**：故意读非法地址 → HardFault，SWD 抓寄存器走全链，
  9/9 判定 PASS。一键复现：`python tools/fault_live.py --flash`（固件工程在 `tools/fault_demo_fw/`）。
  ★ 该板载 ST-Link 是 V3E，`hotplug` 连接不打扰目标，正好用来读故障现场。
- ⬜ NRF52840（Cortex-M4）：同一套链条本可直接复用，未跑；M4 有同样的 CFSR/HFSR，
  属交叉验证、非阻塞项。
- 需要的 `.map`/`.elf`：`tools/fault_demo_fw/build.py` 用 `arm-none-eabi-gcc` 现场编译产出
  （本机编译器在 msys64 的 `ucrt64/bin`，不在 PATH 上 —— 换机器需改 `build.py` 里的 `BIN`）。

### 收口判据（含实测结论）

| # | 判据 | 状态 | 实测证据 |
|---|---|---|---|
| 1 | **真机可验**：NRF52840 **或 STM32H5** 人为触发 HardFault，寄存器 → 故障类型 → 出错函数全程走通 | ✅ **2026-09-22 达成** | NUCLEO-H563ZI（STM32H563ZI / Cortex-M33）实测：故意读非法地址 `0xFFFFFFF0` → CFSR=**0x8200**（PRECISERR+BFARVALID）、HFSR=**0x40000000**（FORCED）、BFAR=**0xFFFFFFF0**；压栈帧 PC=0x080000A2 → **mpu6050_read+0x2**、LR=0x080000BB → **imu_task+0x7**；SCB 直读与固件自读逐字一致。一键复现 `python tools/fault_live.py --flash`（9/9 PASS）。★ 顺带抓到并修掉 `stack_unwind` 的 EXC_RETURN bit2/bit3 错位（真机 EXC_RETURN=0xFFFFFFB8，bit2=0 却在旧实现里被判成「用 PSP」） |
| 2 | **判别力双向测试**：valid=0 必须拒绝解读；IMPRECISERR 必须标地址不可信 | ✅ | `test_bfar_valid_zero_refuses_to_decode` / `test_imprecise_bus_error_flags_addr_untrustworthy` 等，共 30+ 条"必须拒绝"断言全过 |
| 3 | **知识库核验**：`cortex_m.json` 过 `validate_knowledge.py` 0 错误 | ✅ | `知识库核验：0 项错误`；另用注入法验证校验器非自证（8/8 错误全被抓出） |
| 4 | **不引入假数字**：找不到的地址明说"未找到"，不猜函数名 | ✅ | 实测抓到并修掉两处假答案：`0xdeadbeef → app_state+0xbeadb817 ✓`（`symbol_lookup` 与 `stack_unwind` 均复现过） |

**额外达标（判据外，实测暴露后补的）**：

- **跨段推断保护**：GNU map 无符号大小时按相邻地址推断，必须同段——否则算出 `384MB` 的假大小
- **主机 map 识别**：x86 PE 的 `.map` 会被明确警告「不是 MCU 固件」，并有 ARM map 反向对照确认不误报
- **哨兵值提示**：`BFARVALID=1` 但地址是 `0xDEADBEEF`/全同字节/`0xFFFFFFFF` 时给出"位域有效 ≠ 值合理"的提示
- **M0 专门路径**：不再报"没有收录架构"，而是说明 M0 无 CFSR 并给出替代路径

### 验收证据（可复现）

```bash
# 1. 全量测试（从零删除 build_tmp 也能自举）
python -m pytest tests -q             # → 636 passed, 2 skipped（skip 是需显式授权的真机相关用例）
python tools/live_acceptance.py COM21  # 串口侧真机验收（默认只读；需真板）
python tools/fault_live.py --flash    # ★ HardFault 真机闭环（需真板；会覆盖目标固件）
python tests/validate_knowledge.py  # → 知识库核验：0 项错误
python tests/run_c_tests.py         # → 107 checks, 0 failed

# 2. 离线全链路（用 fixtures 里的 ARM map，模拟一次 HardFault）
#    decode_fault(0x8200, bfar=0x200006f0) → PRECISERR 命中，地址可信
#    → symbol_lookup → 落在 app_state 内（标注"无大小记录，偏移为估计"）
#    → stack_unwind(PC=0x08000180, LR=0x080002d5) → mpu6050_read+0x10 / imu_task+0x1
#    → parse_map → raw_buf 1200B 排在最大符号首位
```

**测试夹具**（`tests/fixtures/`）：
- `arm_mcu.map`——按真实 `arm-none-eabi-ld` 输出格式固化的 ARM MCU map（含 `.isr_vector`/`_Min_Stack_Size`），**用于反向验证「正常 MCU map 不被误报」**
- `fault_demo.c`——主机 x86 演示工程；`setUpModule()` 现场用 gcc 编译出 `fault_demo.map`，找不到 gcc 才 skip（不静默失效）

---

## 七、后续批次概要

| 批次 | 工具 | 归属 |
|---|---|---|
| 第二批 | `i2c_scan` / `i2c_read_reg` / `i2c_write_reg` / `spi_transfer` / `flash_verify` / `flash_readout_status` | MCP |
| 第三批 | `reset_reason` / `rtos_state` / `timing_analyze` / `rate_monitor` | MCP |
| 第四批 | 外设用法表 / 器件库 / 通用帧解析器 | MCP（知识层） |

**第二批前置（2026-09-21 已探，结论：本机硬件路径不成立）**：`pyftdi 0.57.1` 已装，但 **libusb 后端枚举不到 FT2232H**（`usb.core.find(idVendor=0x0403)` 返回 0 个、`Ftdi.list_devices()` 为空）——因为该设备走官方 VCP 驱动（`COM28`/`COM40` 为证）。要跑 I2C/SPI 需先用 Zadig 把接口驱动换成 WinUSB/libusb，**代价是该通道的 COM 口被夺走**（且动驱动有风险），另需外挂真实 I2C/SPI 从设备才有验证价值。⟹ 第二批要么在师兄明确同意换驱动后做，要么只做「离线降级 + 双向测试」的纸面版（不进真机）。

**定案（2026-09-22 师兄拍板）**：**「FT2232H 只能读，不能动」——换驱动这条路正式关闭**（A 方案作废，不再讨论 Zading/WinUSB）。剩下的路：

| 方案 | 做法 | 成本 | 真机？ |
|---|---|---|---|
| **B（当前默认）** | 只做离线降级 + 双向测试的纸面版；后端接口留好，装上真桥即可切换 | 0 | 否 |
| **B+** | 外购**独立** USB-I2C/SPI 桥：`FT232H` 单通道模块（首选，原生 libusb、pyftdi 官方首选、不碰任何现有设备） | ~20-30 元 | 是 |
| **B++** | 用 `CH341A` USB 编程器当桥 | ~10 元 | 是（I2C 支持弱、要另装驱动） |
| **B+++** | 零外购：让 **NUCLEO-H563ZI 自己当桥**（固件收 COM21 命令 → 驱动片上 I2C 外设 → 回结果），工具侧加 `backend="serial"` | 0 | 是（但工具从"独立主机"变成"需目标固件配合"，定位变化需明确写清） |
| ~~C~~ | ~~官方 `STLINK-V3SET` + `B-STLINK-BRIDGE` 扩展板走 bridge~~ | 300+ 元 | **实测已排除**：板载 V3E 报 `BRG_NO_STLINK`，官方 bridge 是独立 V3SET 套装的能力，不值 |

**另需 I2C/SPI 从设备**（受扫目标）：师兄手上的 IMU 模块（BNO080/ICM 系列）即现成从设备，不必另购。

---

## 八、明确不做

| 不做 | 原因 |
|---|---|
| 实时调试变量/断点/watch | 需 GDB server 长连接，MCP 请求-响应模型不适合 |
| 全协议栈解码（CAN 应用层、蓝牙 GATT） | 会变成另一个项目 |
| 原理图审查 | 超范围 |
| 通用 IDE 集成 | 超范围 |

---

## 九、全链路收口判据

每块补完都按这个收口：

1. **真机可验**：能在手上硬件跑出真实结果，不是纸面通过
2. **判别力双向测试**：该报的报、不该报的不报（已吃过自证式测试的亏）
3. **知识库核验**：新数据过 `validate_knowledge.py`，0 错误
4. **不引入假数字**：分不出胜负时明确说「判不出来」
5. **链路连通**：补完后，前后两段的数据能接上（最后统一查一次）
