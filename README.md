# mcu-debug-mcp · 嵌入式调试 MCP（领域级）

让 AI"看到并看懂"你手上的板子：串口读写 + 故障模式库（45 条规则）+ 烧录失败模式直译 + Modbus 帧解析 + 编译报错解析 + 引脚冲突检测（三引擎）+ 板级知识库 + 波特率自动探测 + AT 序列回归 + 双口对录 + 会话持久化 + RTT 通道 + 烧录桥接 + 寄存器位域解码 + 调试器探测 + 预验证驱动模板库（9 款）+ **器件选型库（按接口/地址/时序要求）** + **外设 API 用法层（初始化顺序硬要求）** + **错误码统一查询（6 张表·带出处）** + **报文解析（自定义二进制帧 / NMEA-GPS）** + **工程结构与工具链探测** + **会话日志治理与交付报告**。共 59 个工具 + 16 个 MCP resources + 6 个 prompts（8 静态 resources + 8 模板 resources）。

**和"直接问 AI"的本质区别**：AI 不再听你转述"传感器好像不太对"，而是亲自读到串口里的真实字节——AT 指令返回了什么、数据是全 0 还是乱码、程序是不是在反复重启、**Keil 为什么烧不进去**——然后按**出厂预置的故障模式库**给出诊断和排查步骤。引脚分配前用数据手册级映射表核对（能抓 AI 幻觉接线）。驱动代码来自验证过的模板库（纯函数 PC 可测 + Python 交叉验证），不是 AI 现编的。

## 安全与隔离：默认不碰硬件

这个 MCP 的工具会**真的**打开串口、写字节、烧 flash。开发机上常年挂着正在使用的设备
（UART 桥、调试器 CDC 口、正在跑的调试会话），一次误调用就能打断它们。所以默认档位是**只读**：

| 项 | 默认 | 说明 |
|---|---|---|
| 隔离档位 | **readonly** | 读/枚举允许；写串口、AT、波特率扫描、双口对录、烧录**一律拒绝**，并在拒绝信息里写清「为什么拒 + 什么风险 + 怎么放开」 |
| 端口名单 | 未设置 | `MCU_DEBUG_MCP_PORT_DENY=COM6,COM28` 把你在用的口锁死（支持 `*` 通配，DENY 优先于 ALLOW） |
| 不可逆操作 | **双闸门** | 烧录要 `dry_run=False` **且** `confirm=True` 才会写 flash；只给预览 |
| 测试隔离 | **强制** | 测试进程锁 readonly + 端口全禁 + 真实串口构造被替换；跑测试不会碰你的设备 |

```bash
MCU_DEBUG_MCP_MODE=write              # 放开写权限（做真机操作时）
MCU_DEBUG_MCP_PORT_DENY=COM6,COM28    # 把在用的口锁死（即使放开了写权限也打不开）
MCU_DEBUG_MCP_NO_TOUCH_DTR=1          # 打开端口时不翻转 DTR/RTS，尽量不触发开发板自动复位
```

完整的风险矩阵（每个操作会做什么、需要什么才能执行、**哪些操作没纳入闸门及原因**）见
**[`SECURITY.md`](SECURITY.md)**。随时可用 `env_status` 查当前档位、名单与「因此被禁用了哪些操作」。

## 与同类工具的差异（2026-09 竞品调研结论）

同类串口 MCP（qarnet/serial-mcp、mcp-serial、uart-mcp 等）只做「串口通道」；本项目的差异化在**知识层**：

| 能力 | 串口通道类 MCP | 本项目 |
|------|----------------|--------|
| 串口读写 | ✅（普遍更全） | ✅ expect 模式等待响应 |
| 日志分析 | ❌ 把原始日志丢给 LLM 现看 | ✅ **45 条模式库**（30 内置 + 15 外置），规则出厂预置+带排查步骤 |
| 烧录失败诊断 | ❌ | ✅ ST-Link 连接失败/Keil 找不到算法/esptool 进不了下载模式/RDP 读保护…8 条规则族 |
| 错误码查询 | ❌ | ✅ **error_code_lookup**：ESP-IDF esp_err_t / ESP-AT 通用响应 / CWJAP / CME / PubSubClient rc / Modbus 异常码，共 6 张表 51 条，**每条带出处**；未收录明说查不到 |
| 编译/链接报错解析 | ❌ | ✅ gcc / Keil / armcc / MSVC 四种风格 |
| 引脚冲突检测 | ❌ | ✅ pin_check **三引擎**：F1 固定复用 / F4 AF 表 / ESP32 io_mux 硬约束，可交叉板载外设 |
| 板级知识库 | ❌ | ✅ board_get：板载外设占用+坑清单按翻车率排序+供电说明（蓝药丸/黑药丸已收录） |
| 会话持久化 | ❌ | ✅ 串口会话自动落盘 logs/，隔多少轮对话都能 sessions_list + sessions_get 回看现场 |
| 波特率自动探测 | ❌ | ✅ baudrate_autodetect 逐档评分排序（支持离线日志样本评分） |
| AT 批量回归 | ❌ | ✅ at_sequence 逐条断言 PASS/FAIL + serial_pair_record 双口对录时间线 |
| 预验证驱动库 | ❌ | ✅ 9 款，纯函数分离+交叉验证+坑清单+答辩加分点 |
| 寄存器位域解码 | embedded-mcp 有 | ✅ 支持手工 fields + CMSIS-SVD 文件 |
| RTT 通道 | ❌ | ✅ rtt_read（pyocd 桥接，实验性） |
| 知识库质量保障 | ❌ | ✅ **validate_knowledge.py** 机械核验（引脚数/唯一性/复用唯一/正则可编译），CI 可挂 |
| **知识库可扩展** | ❌ | ✅ 故障模式/错误码表/引脚映射/板级知识全部 JSON 外置，社区可贡献（逐条标 source） |

## 工具清单（59 个）

| 工具 | 功能 | 依赖 |
|------|------|------|
| serial_list | 枚举串口，识别 CH340/CP2102/FT232 并给驱动建议 | pyserial |
| serial_open / serial_close | 打开/关闭串口（返回句柄；**会话日志自动落盘**，落点见 env_status） | pyserial |
| serial_read | 读数据（**expect 正则命中即返回** + idle_ms 静默超时 + 总超时），文本+HEX 双视图 | pyserial |
| serial_send | 发指令（AT 自动加 \r\n；支持 HEX；**expect 等响应**），自动读回 | pyserial |
| serial_session_log | 取全量会话日志 | pyserial |
| sessions_list | 列出已落盘会话日志（新→旧），隔轮次可回看 | 无 |
| sessions_get | 回看指定会话日志内容（默认尾部 200 行），可喂 analyze_log | 无 |
| logs_prune | **会话日志保留策略与清理入口**（默认 dry-run 只列不删；真删需 write 档 + confirm=True） | 无 |
| baudrate_autodetect | **波特率自动探测**：逐档采样评分排序给推荐（log_text 可离线评分） | pyserial |
| at_sequence | **AT 指令序列批量回归**：逐条正则断言 PASS/FAIL/TIMEOUT + 回显尾 | pyserial |
| serial_pair_record | **双口对录**：两串口时间线交错录制（RS485 主从/双机对话），文本+HEX | pyserial |
| analyze_log | **45 条故障模式分析**（见下），每条给人话诊断+排查步骤 | 无 |
| modbus_crc | Modbus RTU CRC16 计算/校验（RS485 毕设） | 无 |
| modbus_parse | **Modbus RTU 帧解析**：拆寄存器值/线圈，异常响应直译异常码 01-06 | 无 |
| error_code_lookup | **错误码统一查询**（esp_idf / at_common / cwjap / cme / modbus_exception / mqtt_rc）：释义+排查方向+**出处**；未收录明说查不到、不猜 | 无 |
| pin_check | **引脚冲突检测（三引擎）**：幻觉接线/同功能双占/SWD 挪用/remap 档/ESP32 硬约束/板载外设交叉 | 无 |
| pinmap_list / pinmap_get | 引脚知识库浏览（列表 / JSON 原文） | 无 |
| board_list / board_get | **板级知识库**：板载外设+坑清单+供电说明（bluepill / blackpill_f411 / esp32_devkitc32） | 无 |
| probe_list | 探测调试器与目标芯片 | pyocd（可选） |
| rtt_read | **RTT 通道读日志**（⚠ 实验性，固件需含 SEGGER RTT 控制块） | pyocd（可选） |
| flash_tool_detect | 检测本机烧录工具 | 无 |
| flash_firmware | 烧录固件，桥接 STM32_Programmer_CLI/esptool/st-flash/pyocd，**默认 dry-run**；烧完自动复位 | 各工具（可选） |
| driver_list / driver_get | 预验证驱动模板库（9 款，含接线表+验证记录） | 无 |
| driver_wiring | 驱动接线知识库：接线表+头号坑+日志解读+答辩加分点 | 无 |
| component_list / component_get | **器件选型库**：按接口（i2c / uart / timer_pwm / gpio_timing）列器件，带 7 位地址、**微秒时序要求**、头号坑、出厂验证记录；数据全部派生自预验证驱动库的 meta.json 与 *_port.h，**零凭记忆条目** | 无 |
| peripheral_api_list / peripheral_api_get | **外设 API 用法层**：每个驱动要占哪些外设（I2C1 / TIM1 / UART2…）+ **初始化顺序硬要求**（取自适配层注释原句，如「工程里先完成 HAL_I2C_MspInit 与 hi2c1 初始化」） | 无 |
| decode_register | 寄存器值按位域拆解 | 无 |
| decode_register_svd | 按 CMSIS-SVD 文件解码位域（含枚举提示） | 无 |
| **decode_fault** | **Cortex-M 故障寄存器解码**（CFSR/HFSR/BFAR/MMFAR）：20 位逐条给人话成因+排查；**三类陷阱在输出里强制拦截**（见下） | 无 |
| **parse_map** | **.map 解析**（GNU ld / Keil / IAR）：段占用 + 最大符号 + 栈深线索；**认不出格式就说认不出，不猜**；主机 map 会报警 | 无 |
| **symbol_lookup** | **地址→符号反查**：给出函数名+偏移；越界/落在空隙/野指针**拒绝硬套函数名** | 无 |
| **stack_unwind** | **栈回溯**：硬件 8 字栈帧还原调用链 + EXC_RETURN 判栈类型（MSP/PSP、标准/扩展帧）+ 跑飞识别 | 无 |
| **env_status** | **环境自检**：依赖 / 串口（含 VID:PID 识别与 Unix 权限）/ 烧录器 / 知识库根与条目数 / 版本，一次问清；**探不到的会明说为什么探不到** | 无 |
| **ecosystem_list / ecosystem_get** | **生态/框架识别知识库**（Zephyr / RT-Thread / PlatformIO / Keil / IAR / Rust defmt）：识别特征 + 构建产物 + 日志签名 + 各自的坑，条目级带出处 | 无 |
| **project_detect** | **工程结构识别**：按标志文件/后缀认生态（`platformio.ini` / `*.uvprojx` / `prj.conf` / `Cargo.toml`…），**只探一层**（避免 vendor/ 误判）；认不出明说该看什么 | 无 |
| **toolchain_status** | **工具链探测**：编译器 / GDB / SDK 逐个探版本；**「未发现」不等于环境坏了**——会说明 IDE 自带工具不一定暴露到命令行 | 无 |
| **report_session** | **交付物生成**：会话日志 → Markdown 报告（故障分析 + 原始日志附后）；目标已存在**不默认覆盖** | 无 |
| **nmea_parse** | **NMEA 0183 解析**（GPS/北斗）：GGA/RMC/GSA/GSV/VTG/GLL 逐句给人话（经纬度度分+十进制/定位质量/星数/HDOP/速度）；**每句验 XOR，不过的明说不可信**；非 NMEA 行单独点名跳过 | 无 |
| **frame_parse** | **通用二进制帧解析**：帧头 + 长度字段切帧，支持 sum8 / CRC16-Modbus；**校验字节不计入长度字段**（Modbus 就靠这条）；切不动/截断/垃圾字节**逐条报出，绝不静默丢** | 无 |
| **chip_info** | **芯片身份与存储信息**：型号 / Device ID / 修订 / 内核类型 / Flash 容量 / 96 位 UID / 选项字节（读保护、TZEN、写保护组）；默认 hotplug 档**不复位不停内核**，要 UID 得显式 `mode="full"`（会复位目标，受隔离层管辖） | STM32_Programmer_CLI |
| **reset_reason** | **复位原因 + 内核运行状态**：RCC_RSR 逐位直译（引脚/欠压/软件/看门狗/低功耗）并给可执行建议；**讲清标志是「累积」的**——多个位同时为 1 ≠ 一次同时发生；`clear_flags+confirm` 支持「清 → 复现 → 再读」定位这一次 | STM32_Programmer_CLI |
| **flash_verify** | **烧录后逐字节校验**：把文件与芯片上实际内容比对，报出**首个不一致的偏移 + 绝对地址 + 前后 8 字节上下文**；支持 .bin / .hex / .elf（ELF 按段表偏移取值）；只读走 hotplug | STM32_Programmer_CLI |
| **timing_analysis** | **时序/数据率分析**：丢帧（序号跳号）、卡顿（间隔离群）、节拍漂移（累计变化量 vs 实际离散度）；**窗口不足一个时间戳刻度时明说不给指标**，不编数据率 | 无 |
| **stream_capture** | **长时录测**：串口持续采集，**按到达时刻逐行打时间戳落盘**（无时间戳 = 时序信息永久丢失）；产物可直接喂 `timing_analysis(path=...)`；重名不覆盖旧现场 | pyserial
| **bridge_probe** | **桥自检**：确认那块跑桥固件的 MCU 活着、认 MCUDBG/1 协议、支持哪些总线——纯握手不碰总线；**绝不自动扫口找桥**（实测过往别人正在跑的固件里灌探测字节的后果） | pyserial
| **i2c_scan** | **I2C 总线扫描**：逐地址 ACK 探测，给出 7 位地址 + 读/写地址 + **可能的器件提示**（最小表，逐条来自器件数据手册，只提示不判定）；扫不到时给 5 步排查清单（上电/接反/上拉/复用器/共地） | 桥固件 + pyserial
| **i2c_read_reg** | **I2C 读寄存器**（"这颗 IMU 活着吗"第一动作）；读回全 0 / 全 0xFF 分别给**不同**的成因清单，不代用户判"器件坏了" | 桥固件 + pyserial
| **i2c_write_reg** | **I2C 写寄存器**，可选 verify=True 回读比对（默认关：只写寄存器回读必然不一致，默认开会造成假 FAIL）；7 位地址校验拦下 8 位写地址 | 桥固件 + pyserial
| **spi_scan** | **SPI 扫描**：逐 CS 读 JEDEC ID（0x9F），解厂商/容量；**明说 SPI 没有地址概念**，扫不到传感器/ADC 属正常；空槽（全 0/全 1）如实区分"读回全 0"和"MISO 悬空" | 桥固件 + pyserial
| **spi_read_reg** | **SPI 按命令字读 N 字节**（0x9F 读 JEDEC / 传感器 WHO_AM_I 都通用） | 桥固件 + pyserial
| **onewire_scan** | **1-Wire 搜索 ROM**（AN187 算法 + CRC8 校验）：列出 ROM 码并解家族码（DS18B20 等）；搜不到先怀疑**数据线缺 4.7k 上拉** | 桥固件 + pyserial
| **rs485_send** | **RS485 发送**（桥负责 DE 时序）；**输出里强制写明**"只证明发得出去，不证明对端收到"——不当成链路已通的证据 | 桥固件 + pyserial

> 总线工具需要一块跑 **MCUDBG/1 桥固件**的 MCU 当执行端：固件源码在 `tools/bridge_fw/`
>（NUCLEO-H563ZI 现成，附"编译 → 烧录 → 真机验证 → 自动恢复原固件"一条龙脚本）。
> 为什么不自动扫串口找桥、为什么 SP-Link bridge 和 FT2232H 走不通，见 `core.py` 总线工具区块头注释。 |

## MCP resources（16 个：8 静态 + 8 模板）

知识库只读浏览，AI 可主动引用——**每类知识都是「清单 + 明细」两段**：清单看有哪些，明细读原文（条目级 `source` 出处一并可见）。

| 知识库 | 清单 | 明细 |
|--------|------|------|
| 预验证驱动 | `mcu://drivers` | `mcu://driver/{name}` · `mcu://wiring/{name}`（接线） |
| 器件选型库 | `mcu://components` | `mcu://component/{name}`（选型详情+接线坑） |
| 外设 API 用法 | `mcu://peripheralapis` | （取自各驱动 `*_port.h`，随驱动走，无独立明细资源） |
| 引脚复用映射 | `mcu://pinmaps` | `mcu://pinmap/{mcu}`（pinmap JSON 原文） |
| 板级知识 | `mcu://boards` | `mcu://board/{name}` |
| 错误码直译表 | `mcu://errcodes` | `mcu://errcode/{table}`（整表 JSON 原文） |
| 故障位域表 | `mcu://faultbits` | `mcu://faultbits/{arch}`（如 `cortex_m`，两种写法都认） |
| 生态/框架识别 | `mcu://ecosystems` | `mcu://ecosystem/{name}`（如 `keil`、`rtthread`，显示名模糊匹配） |

## pin_check 三引擎

| engine | 适用 | 检查逻辑 |
|--------|------|----------|
| fixed_mux | F1 系列（stm32f103c8t6） | 固定复用表：功能默认档全局唯一，remap 档提示，SWD 挪用警告 |
| af_table | F4 系列（stm32f411ceu6） | AF 复用表：一功能可映射多脚，按表核对 |
| io_mux | ESP32 系列（esp32dev） | GPIO Matrix 任意路由，查硬约束：flash 保留脚（GPIO6-11，必砖级）/ 仅输入脚（GPIO34-39）/ strapping 脚（0/2/5/12/15） |

可带 `board` 参数交叉检查板载外设占用（如 `pin_check("stm32f103c8t6", {...}, board="bluepill")`——PC13 被板载 LED 占用会警告）。

## analyze_log 覆盖的模式（45 条）

**内置 30 条**：

总线/传感器层（13）：MOJIBAKE 乱码→波特率 · AT_ERROR · AT_NO_RESP · BOOT_LOOP · HARDFAULT+CFSR 位级解读 · SENSOR_ZERO 全 0 · SENSOR_FF 全 FF · DS18B20_85 恒 85°C · DHT_TIMEOUT · NEG_INVALID 错误码负值 · I2C_NACK · I2C_SCAN_EMPTY 扫描零设备 · ADC_SATURATED 恒满量程

ESP 生态（5）：ESP_PANIC Guru Meditation · ESP_BROWNOUT 掉电 · ESP_WDT 看门狗 · ESP_FLASH_CORRUPT 镜像损坏 · CWJAP_ERR 连 WiFi 错误码 1-4

AT/网络协议（4）：AT_CME_ERROR（3GPP 码表）· NET_LINK WiFi 断链 · MQTT_FAIL（PubSubClient rc 码表）· HTTP_ERR 4xx/5xx/curl

RTOS/系统层（4）：RTOS_ASSERT 断言 · RTOS_STACK_OVF 栈溢出 · HAL_ASSERT STM32 HAL 参数断言 · UART_FRAMING 帧错误/溢出

开发期报错（3）：COMPILE_UNDEF 链接未定义符号 · COMPILE_SYNTAX 编译错误（gcc/Keil/armcc/MSVC）· SD_INIT_FAIL

**外置 15 条（knowledge/fault_patterns.json，社区可贡献）**：

- 烧录失败族（8）：STLINK_CONNECT_FAIL · KEIL_NO_ALGO 找不到 Flash 算法 · KEIL_FLASH_DL_FAIL 下载失败 · OPENOCD_TARGET_FAIL · ESPTOOL_FAIL 进不了下载模式 · PYOCD_PROBE_FAIL · FLASH_READOUT_PROTECT 读保护激活 · SWD_COMM_FAIL 调试中失联
- 总线/网络族（6，v4 新增）：CAN_BUS_OFF（bus-off/error passive，终端电阻 60Ω/ACK 原理）· WIFI_RECONNECT_STORM（断连 ≥3 次计数门槛，供电头号原因）· BLE_CONN_LOST（连接参数/MTU/断开原因码 0x08/0x13）· JLINK_FAIL（克隆许可/DLL 冲突）· DNS_FAIL（DNS 配置/内网域名）· USB_ENUM_FAIL（DP 1.5kΩ 上拉/48MHz 晶振/R10）

**串口报错贴进 analyze_log 一样能分析**——编译报错、烧录失败日志直接粘进来就行。

## 预验证驱动库（9 款）

dht11（温湿度）· hcsr04（超声波）· **ds18b20**（单总线温度，CRC8+补码换算）· **mpu6050**（六轴，量程换算+静止自检）· **esp8266at**（AT 响应解析库，IPD 帧+错误码）· ssd1306（OLED，帧缓冲+页寻址）· **servo_sg90**（舵机，角度→脉宽→定时器 CCR）· **stepper_28byj48**（步进电机，精确减速比 63.684:1）· **hc05**（蓝牙 AT 助手，响应分类+命令生成）

每个驱动：纯函数与总线时序分离（PC 可测）· wiring.md（接线表+头号坑清单按翻车率排序+答辩加分点）· meta.json 如实标注验证状态。

## 会话持久化（"记得住"）

serial_open 自动在 `logs/` 落盘会话日志（端口/波特率/时间戳元信息 + 全部收发内容），读写时实时追加。调试被中断、换会话、隔几天之后：`sessions_list` 列出历史会话 → `sessions_get` 回看现场 → 整段喂 `analyze_log`。只读文件系统下静默降级，不影响主流程。

## 安装

```bash
# 方式一：PyPI / pip（发布形态，src 正规包——不污染 site-packages 顶层）
pip install mcu-debug-mcp          # 核心（MCP 框架）
pip install "mcu-debug-mcp[all]"   # + pyserial（串口）+ pyocd（调试器/RTT）

# 方式二：从源码目录安装
pip install .[all]

# 方式三：开发模式（改代码即时生效；测试/审计直接从 src/ 导入）
pip install -e .[all]

# 装完后用 console script 起服务（等价于 python -m mcu_debug_mcp.server）
mcu-debug-mcp
```

## 客户端配置

**WorkBuddy**（连接器管理 → 自定义 MCP → 信任后生效）：
```json
{
  "mcpServers": {
    "mcu-debug-mcp": {
      "command": "python",
      "args": ["<本项目路径>/server.py"]
    }
  }
}
```

**Claude Desktop**（claude_desktop_config.json）与 **Cursor**（mcp.json）同格式。

## 验证状态（2026-10-13 复验，v4.5.5）

| 模块 | 验证方式 | 状态 |
|------|----------|------|
| analyzer 异常模式库（30 内置 + 15 外置） | tests/test_analyzer.py：99 项断言（正例命中+计数门槛+不误报+永不抛异常） | ✅ 本机实测通过 |
| 驱动纯函数算法（9 款驱动全量） | tests/test_driver_logic_py.py：49 项断言（与 C 实现同源对拍） | ✅ 本机实测通过 |
| **9 款 C 驱动编译 + 逻辑测试** | tests/run_c_tests.py（gcc 16.2.0 mingw64）：**107 checks, 0 failed，9 款全部 -Wall -Wextra 零警告** | ✅ 本机实测通过 |
| **串口读写 + expect/idle_ms（loop:// 回环）** | tests/test_serial_tools.py TestSerialLoop：全过 0 skip（Python 3.12 + pyserial 3.5） | ✅ 本机实测通过 |
| modbus/pin_check 三引擎/boards/sessions/RTT 降级 | tests/test_serial_tools.py | ✅ 本机实测通过 |
| **知识库数据核验** | tests/validate_knowledge.py（引脚数/唯一性/fixed_mux 功能全局唯一/SWD 一致性/onboard pin 字段/正则可编译/**条目级 source 强制**） | ✅ 0 错误（曾抓出 3 处 F103 录入错误） |
| **知识库条目级溯源** | 15 个知识文件 **250 条**数据全部带条目级 source；校验器强制（注入缺 source 必报错），并有测试保证注入后文件**逐字复原**（防止测试改写手写排版） | ✅ 本机实测通过 |
| server.py MCP 注册层（59 工具 + 16 resources + 6 prompts） | **真 MCP 客户端 stdio 握手实测**：`list_tools()`=59 · 静态 resources=8 · 模板 resources=8 | ✅ 本机实测通过 |
| **MCP 协议层 + 资源层真机** | **16 个资源全部 `read_resource` 可读**（8 个模板资源 driver/wiring/component/pinmap/board/ecosystem/errcode/faultbits 逐项实测，9 款驱动逐个读）；查不存在的名字一律友好拒绝并列出可选项、不抛异常；`tests/test_mcp_resources.py` 17 条把资源清单精确锁死 | ✅ 本机实测通过 |
| **符号反查 / 栈回溯（真工具链夹具）** | `tests/test_symbol_lookup.py` 用仓库自带的 `bridge_fw/main.c` + `linker.ld` **现场编译真 Cortex-M33 ELF**（复用 build.py 的工具链探测，不另写一套），与 `arm-none-eabi-readelf` 真值交叉核对：符号数/地址/大小逐一吻合；工具链缺失时整组 skip（CI 不红） | ✅ 本机实测通过 |
| **离线全量（Python 全套 944 项）** | `python -m pytest tests -q`：**942 passed, 2 skipped**（两个 skip 都需显式授权：真机 USB CDC 用例，以及「该口不是 USB CDC」的对照用例；`tests/test_symbol_lookup.py` 里另有 25 条在无 ARM 工具链时整组 skip，本机装了 arm-none-eabi-gcc 全部实跑；收集总数由 `tests/audit_capability.py` 的 `pytest_collected` 探针机器锁定，手改数字会被审计抓出） | ✅ 本机实测通过 |
| **隔离与安全（37 条）** | `tests/test_isolation.py`：档位/名单/不可逆确认/虚拟端口豁免/未登记操作 fail-closed，**含必须放行的反例**（离线打分、dry-run 预览、`loop://`）；另有静态对账与注入法反自证；并列 6 条守着`tools/live_acceptance.py`夹具自身（无端口拒跑、dry-run 不开口、DENY 不得设 `*`、收尾不得假 PASS、破坏关必须单独授权） | ✅ 本机实测通过 |
| **排故链四工具（fault/map/符号/栈）** | tests/test_fault_chain.py：**67 项断言**，其中 30+ 条是「必须拒绝给结论」的假答案防线；**从零删掉 build_tmp 也能自举**（setUpModule 现场 gcc 生成夹具） | ✅ 本机实测通过 |
| **真机验收夹具（N-15）** | `tools/live_acceptance.py` 在 **NUCLEO-H563ZI（STM32H563/Cortex-M33，板载 ST-Link V3EC 虚拟串口 COM21）** 上实跑：**PASS 16 · FAIL 0 · SKIP 1**；可反复跑不打死板子；破坏关单独授权后 **3/3 PASS** | ✅ 真机实测通过 |
| 真机联调（硬件相关项） | 见下方清单；已跑：4/6/7/8/10/11 部分 + **13（NUCLEO-H563ZI，一键全链）** | ✅ 部分完成（见清单标注） |

**MCP 真机实测抓出的四个真问题（已修，均为"看似合理的假通过"类）**：

1. **`baudrate_autodetect` 在 USB CDC 口上原理上无意义，必须先识别并短路**（2026-09-21 师兄指出 ESP32 用的是 USB CDC）。
   USB CDC 走芯片自带 USB 外设（CDC ACM），**波特率参数不参与数据路径**，各档收到的字节流完全相同 ⟹ 所谓"各档评分"只是同一批字节被反复读。
   **实测铁证**：COM4 上同一波特率（115200）连测 3 轮全 0 字节，而另一次 9600 拿到 **88064 字节**——**同波特率结果从 88KB 到 0**，差异只来自设备当时有没有在输出。
   ⟹ 该口扫档 = 在测"这次打开时设备有没有在说话"，纯随机。原实现给的"9 档同分 0.99"主因也不是采样不足（我上一版归因偏了），而是**根本没有波特率差异**。
   修法：新增 `_port_baud_relevance()`，按 VID/PID 识别 CDC（`303A:1001` Espressif、`2E8A:*` RP2040 等）与 UART 桥（`0403:6010` FT2232H、`10C4:EA60` CP210x、`1A86:7523` CH340、`1366:0105` J-Link 等），CDC 口直接短路并给出「该口不用管波特率 / 读不到别换档 / 真要判必须接独立 UART 桥听 TX 脚」三步指引；`serial_open` 也附同类提示。**显式传 `candidates` 可强行扫**（怀疑枚举成 CDC 实为桥的场合）。同时给 `analyze_log` 的 `MOJIBAKE` 诊断加了 USB CDC 例外（原建议「把波特率逐个换」在该口是纯无效动作）。

2. **`baudrate_autodetect` 在真 UART 上自信地给错档**（COM4 实测，判别力门禁的由来）：11 个候选里 9 个同分 0.99，工具推荐 `460800`，真实是 `115200`；而**正确的 115200 反而排最低（0.071）**。
   根因链：① 打开串口翻转 DTR/RTS ⟹ ESP32 复位 ⟹ 每个档位收到同一段纯 ASCII ROM 启动日志；② `if len(buf) > 2048: break` 在应用数据到达前退出；③ 同分时稳定排序回退到候选表顺序，而候选表是**降序**的 ⟹ 永远推荐最高档。
   修法：采样上限 2048→8192；新增 `_BAUD_MIN_MARGIN = 0.10` **判别力门禁**——最高分与次高分差距不足门限时，明确输出「★判不出来……别照它改代码」并给手工确认步骤，而不是硬推一个。

3. **`at_sequence` / `serial_read` / `serial_send` 的 `expect` 是全文子串搜索 ⟹ 假 PASS**（COM4 实测）：设备跑的是 BQ27421 电量计固件、根本没有 AT 应答，但日志行 `I (371) led: init OK  R=IO21 G=IO48 B=IO47` 里含 "OK" ⟹ 判 `[PASS]`。
   修法：新增 `_at_line_match()`，默认 **整行匹配**（行首尾允许空白，`re.fullmatch`），并回显「命中行: …」便于核对；确需旧行为可在该步骤写 `{"loose": true}`。修复后同场景返回 `[TIMEOUT]`，而 `loose=true` 对照组仍 `[PASS]` —— 两相对照即为铁证。

4. **`pin_check` 的 io_mux 引擎（ESP32）不校验引脚存在性 ⟹ 拼错引脚名给"通过"**：`{"GPIO999": "GPIO"}` 返回「✅ 任意路由（GPIO Matrix）… 结论：**全部通过，可以按此接线**」。`fixed_mux` 引擎（STM32）有「该 MCU 没有此引脚」的拦截，**两个引擎判定口径不对称**，任何手滑拼写（`GPI08`）或幻觉引脚都会拿到"通过"结论。
   修法：io_mux 分支加引脚存在性前置校验，并列出相近的已知引脚，结论改为「必须改」。

**同时并存的可用性缺陷（未改代码，需决策）——参数名/参数集与直觉不符**：
`analyze_log(log_text=)` 不是 `text`；`modbus_crc(hex_frame=)` 不是 `data`；`pin_check(pins=)` 不是 `assignments`；`flash_firmware(file_path=)` 不是 `firmware`；`at_sequence(steps=)` 且步骤内是 `cmd` 不是 `send`、`timeout_s` 不是 `wait_s`。
另有一处**参数集不一致**（已修）：`core.baudrate_autodetect` 有 `candidates` 但 `server.py` 的 MCP 工具签名未暴露 ⟹ MCP 客户端无法「强行扫」，文档承诺无法兑现。已补上 `candidates` 参数。
首次调用按直觉猜参数名的失败率极高（本轮实测**猜错 7 次全部报 pydantic 校验错**）。建议在 README/工具描述里补「易错参数名对照表」，或在 `server.py` 加别名兼容。

**排故链（Cortex-M 故障链）实测抓出的真问题（已修，均为"看似合理的假答案"类，2026-09-22）**：

这一批全部是靠**真实 gcc 生成的两份 .map**（主机 x86 + 目标 ARM）跑出来才暴露的，单测的 happy path 一条都测不出：

1. **`symbol_lookup` 把 `0xdeadbeef` 反查成 `app_state+0xbeadb817` 并打勾 ✓**。
   根因：GNU ld 的 .map **不记符号大小**，推断出的 size 对 `.bss` 段末符号为 0，于是「偏移 < size 才越界」的检查被短路，任何巨大偏移都被判成「在符号内」。
   修法：给每个符号算**地址上界**（有 size 用 size；无 size 用下一符号地址；段尾用段末），地址越出上界就**拒绝给函数名**，并说明「落在符号表空隙里 = 野指针」。同一份解析逻辑被抽成 `_build_bounds`，**`symbol_lookup` 与 `stack_unwind` 共用**——避免只修一处、另一处照旧放行（实测中 `stack_unwind` 起初就复现了同样的假答案）。

2. **`_infer_sizes` 跨段推断，算出 384MB 的「符号大小」**。
   `.text` 段最后一个符号拿 `.data` 段首地址当「下一个符号」，`0x20000000 − 0x0800039c = 402652260` 被当成真实大小印进「占用最大的符号」视图。
   修法：推断时做**跨段保护**（两符号必须同段）+ **段大小上限**（推断值不能超过所属段）。

3. **主机程序（x86 mingw PE）的 .map 被当成 MCU 固件照单全收**。
   输出里混着 `__mingw_module_is_dll`、`.idata/.rsrc/.pdata` 等主机特征，用户拿错 map 会得到一堆无关符号还以为是自己的固件。
   修法：新增 `_map_target_check`，命中主机特征（运行时符号 ≥3 条 或 主机专有段 ≥3 个）时**明确警告「这不是 MCU 固件的 map」**；同时用一份按真实 `arm-none-eabi-ld` 格式固化的 ARM map 做**反向对照**，确认正常 MCU map **不误报**。

4. **`serial_list` 对已插着的 FT2232H / J-Link 显示「未收录」**。
   根因：`core._USB_UART_BRIDGE_IDS` 认识 `0403:6010`/`1366:0105`，但 `analyzer.USB_SERIAL_VIDPID` 没有这两条——**同一 VID:PID 在一处被识别、另一处说不认识**。
   修法：补齐 analyzer 表（FT2232H/FT232H/CH9102 变体/J-Link CDC/RP2040/ESP32 USB-JTAG 等），并加**跨表一致性测试**，锁住两张表不再脱节。

5. **`decode_fault` 把 `cortex-m0` 报成「没有收录架构」**——M0 是**已知架构、只是没有 CFSR**，这不是"缺个 json"的问题，而是**判定路径根本不同**（需走压栈帧 PC 反查）。
   修法：M0/M0+ 走专门分支，说明为什么用不了并给出替代路径（`symbol_lookup` + `stack_unwind` 不受架构限制）。
   附带修：`BFARVALID=1`（位域说"有效"）时不再无脑放行——**识别哨兵值**（`0xDEADBEEF`、全同字节、`0xFFFFFFFF`、`<0x100`）并提示「位域有效 ≠ 值本身合理」；`valid=0` 且用户没传该地址时，不再断言"读到的值是垃圾值"（根本没读到）。

**测试自身抓出的两个静默失效（已修）**：
- `@unittest.skipUnless(os.path.isfile(MAP))` 装饰器在**类定义时**求值，而 `setUpModule` 之后才生成 map ⟹ **map 明明刚生成，14 个测试却全被 skip**。改为运行时判断函数 `_have_demo()`。
- `shutil.which("gcc")` 成功 ≠ gcc 能跑：msys2 的 gcc 找不到 `cc1`/`as`/`ld` 时会 **rc≠0 + 空输出静默失败**。改为**始终把 msys2 bin 前置进 PATH**，并加 `_gcc_works()` 真跑一次 `--version` 确认。


1. `ssd1306.h` 的 include guard（`SSD1306_H`）与屏高宏（`SSD1306_H = 64`）**撞名**——文件卫士被屏高宏覆盖，任何 `#ifdef SSD1306_H` 判断恒为真。已把卫士改为 `SSD1306_DRIVER_H`
2. `mpu6050_read_raw` 的 `uint8_t b[14]` 未初始化，gcc 报 `-Wmaybe-uninitialized`（桩函数空实现时尤其明显）；真机上 I2C 部分读失败会留脏数据。已改 `= {0}`

**串口回环实测抓出的测试代码 bug（已修）**：`TestSerialLoop` 从未真正运行过，第一次跑就暴露三处——`serial_for_url("loop://")` 返回的对象已自动 open（显式 `.open()` 抛 "Port is already open."）、`setUpClass` 共享端口被各测试互相 close、`_get_session` 方法名写错（实为 `get_session`）。重写为每测试独立端口 + `addCleanup`。

## 真机验证清单（拿到有硬件的环境后）

1. ~~`pip install -r requirements.txt` → `python tests/test_serial_tools.py`~~ ✅ 已跑（全套 222 项 OK）
2. ~~`python tests/run_c_tests.py`~~ ✅ 已跑（9 款驱动零警告 + 107 checks 全过）
3. ~~`python tests/validate_knowledge.py`~~ ✅ 已跑（知识库核验 0 错误）
4. 插上任意 STM32/ESP 板子 → `serial_list` 确认识别芯片 ✅ 已跑（COM4=ESP32-S3 `303A:1001`，板载固件 `bno085_imu V2.4.3-20260920_a023`）
5. 故意改错波特率读日志 → `baudrate_autodetect` 扫档 → 推荐档位与真实波特率一致
   ⚠️ **分两种口**：① **USB CDC 口**（ESP32-S3/C3、RP2040 等）——波特率不参与数据路径，工具会**识别并短路**，拒绝给排序（这是正确行为，不是失败）；② **真 UART / 独立桥**（FT2232H、CH340、CP210x、J-Link CDC）——波特率真实生效，扫档有意义。
   要验证「能推荐对」，需一台**非 USB CDC、且以非 115200 输出的设备**接在 UART 桥上。本机 COM4 是 CDC 故只验了短路路径；COM7（FT2232H）无设备接入，扫出全 0 分并被正确提示"设备没在说话"。
6. `serial_open` + `serial_read` 读启动日志 → `analyze_log` 应报 MOJIBAKE ✅ 已跑（COM4 读到 114319 B 真实日志，命中 `#EVT type=FAULT fault=0x02000000`）
7. `serial_send("AT", expect="OK", wait_s=3)` 确认 expect 提前返回；`at_sequence` 跑一组 AT 用例 ✅ 已跑（**并因此抓出假 PASS 缺陷**，见上）
8. 关掉串口 → `sessions_list` → `sessions_get` → 能完整回看刚才的现场 ✅ 已跑（自动落盘 `20260921_115559_COM4_h1.log` 132.8 KB，可回看）
9. ESP8266 故意输错密码连 WiFi → 日志应命中 `CWJAP_ERR`（码 2=密码错误）⏳ 无 ESP8266 AT 固件设备
10. RS485 从站故意回异常帧 → `modbus_parse` 应直译异常码；`serial_pair_record` 对录主从对话 ✅ 已跑（`modbus_parse` 异常码 0x02 正确直译；`serial_pair_record` 用 FT2232H COM7/COM11 实跑，空数据降级提示正确）
11. 在蓝药丸上把 PB7 配成 USART1_RX → `pin_check` 应提示需要 remap；PC13 配 GPIO + board=bluepill 应报板载 LED 占用 ✅ 已跑（知识层三引擎全部正确；**并因此抓出 io_mux 不校验引脚存在性的缺陷**）
12. J-Link/DAPLink 连带 RTT 固件的板子 → `rtt_read` 读日志 ⏳ **J-Link 在位（COM6），但本机只有 VS Code ARM 扩展自带的那份 `pyocd.exe`，当前解释器 `import pyocd` 失败**，所以 `rtt_read` 还跑不起来。已验的是「模块缺失时如实说明、不崩」的降级路径。`pip install pyocd` 后可用 NRF52840 板补测（也可直接调那份 exe：`pyocd list`）。
13. **NUCLEO-H563ZI（STM32H563 / Cortex-M33，板载 ST-Link V3EC 虚拟串口 COM21）** → 一键 `python tools/live_acceptance.py COM21 --allow-write --elf <fw.elf> --map <fw.map>` ✅ 已跑（PASS 16 / FAIL 0 / SKIP 1；并抓出 6 个只靠离线测试抓不到的缺陷，见 `CHANGELOG` 4.2.0）

**硬件盘点（本机实测）**：COM4=ESP32-S3 原生 USB（`303A:1001`，跑 BNO085 IMU 固件）｜COM6=JLink CDC UART（`1366:0105`）｜COM7/COM11=FT2232H 双通道（`0403:6010`）｜COM8/COM9=蓝牙幻影口。
**注意**：NRF52840 板只有 SWD + SEGGER RTT，未引出 UART ⟹ 串口类清单项需用上述其他硬件；RTT 类需装 pyocd。

## 目录结构

```
mcu-debug-mcp/
├── src/mcu_debug_mcp/  # ★ 正规包（2026-10-13 C 档重构：wheel 不再污染 site-packages 顶层）
│   ├── __init__.py     # 版本单一来源（pyproject dynamic attr 从这读）
│   ├── server.py       # MCP 薄壳（FastMCP 注册 55 工具 + 13 resources + 6 prompts）
│   ├── core.py         # 全部工具实现（零框架依赖，可独立测试）
│   ├── analyzer.py     # 故障模式引擎（30 内置规则 + JSON 外置规则加载 + min_count 计数门槛）
│   ├── guard.py        # 运行时隔离层（默认禁写硬件 + 端口名单 + 不可逆操作二次确认）
│   ├── paths.py        # 数据根定位（$HOME env > ~/.mcu-debug-mcp > 包内置；外置层可合并覆盖）
│   ├── knowledge/      # 领域知识库（JSON 外置，社区可贡献；随包分发）
│   │   ├── fault_patterns.json  # 可外置故障模式规则（正则类，逐条标 source，支持 min_count）
│   │   ├── error_codes/         # 错误码直译表（esp_idf/at_common/cwjap/cme/mqtt_rc/modbus_exception）
│   │   ├── pinmap/              # MCU 引脚复用映射（数据手册级，三引擎：f103c8t6/f411ceu6/esp32dev）
│   │   └── boards/              # 板级知识库（bluepill / blackpill_f411）
│   ├── drivers/        # 预验证驱动模板 9 款（LLM 只选型，不现编代码；.h/.c/port/wiring.md/meta.json）
│   └── tools/          # live_acceptance.py 真机验收夹具（默认只读）+ bridge_fw 桥固件
│                        # + verify_stdio.py 端到端 + mutation_check.py 变异验证
├── setup.py            # 仅保留构建临时目录重定向（打包声明全在 pyproject.toml）
├── coverage.json       # 能力面清单（26 缺口 + 27 能力轴，审计脚本据此对账）
├── SECURITY.md         # 安全与隔离模型 + 破坏性操作矩阵 + 漏洞报告通道
├── pyproject.toml      # src 布局 + include-package-data + console script（v4.5.5）
├── MANIFEST.in         # sdist 收录清单（漏一行就复发「装出来是空壳」）
├── requirements.txt
├── examples/           # offline_demo.py：离线可跑的示例
├── docs/               # AUDIT-2026-10-13.md 审查报告（含修复记录）等
└── tests/
    ├── test_analyzer.py        # 101 项，✅ 已验证
    ├── test_driver_logic_py.py # 49 项，✅ 已验证（C 算法对拍）
    ├── test_driver_logic.c     # C 版同源测试，✅ 107 checks 0 failed（9 款驱动全量）
    ├── run_c_tests.py          # C 测试运行器（无需 make，自动处理 msys2 的 cc1 PATH 坑）
    ├── Makefile.logic          # C 测试 makefile（等价路径）
    ├── validate_knowledge.py   # 知识库核验脚本（CI 可挂），✅ 0 错误
    ├── audit_capability.py     # 能力面审计：广度/深度阶梯/缺口/文档对账/**隔离与安全**
    ├── audit_constants.py      # 常量一致性审计（含版本单一来源 ↔ README）
    ├── conftest.py             # 测试期强制硬件隔离（锁档位 + 禁端口 + 替换串口构造）
    ├── isolation.py            # 临时解除隔离的唯一入口（给「测笼子内部逻辑」的测试用）
    ├── test_isolation.py       # 隔离层专项
    ├── test_package_smoke.py   # 打包冒烟（真构建 sdist+wheel 双安装 + 子进程 + 剔除源码树）
    ├── stub/                   # C 测试桩（port_all.h + 各驱动桩头文件）
    └── test_serial_tools.py    # ✅ 全过（含 loop:// 回环端到端）
```

## Windows 上的 gcc 坑（实测记录）

如果你在 Windows + msys2 下跑 C 测试遇到「gcc 明明在，但编译 rc=1 且没有任何输出」，**不是沙箱也不是权限问题**：

msys2 的 `gcc.exe` 单独执行正常（`--version` 有输出），但真正编译时它要 fork `cc1.exe`，而 `cc1.exe` 依赖同目录的运行时 DLL（`libgmp-10.dll`/`libmpfr-6.dll`/`libisl-*.dll`/`libmpc-3.dll`）。Windows 的 DLL 搜索路径**不含 exe 所在目录**（除非该目录在 PATH 里），于是 `cc1.exe` 崩在 `STATUS_DLL_NOT_FOUND (0xC0000135)`——由于崩溃发生在子进程，父进程拿不到任何 stderr，表现为「静默失败」。

验证方法：`gcc -dumpmachine` 能输出（驱动本身不 fork cc1），`gcc -E foo.c` 无输出失败（要 fork cc1）。

修法：把 gcc 的 bin 目录加进 PATH。`tests/run_c_tests.py` 与 `Makefile.logic` 都已自动处理，直接用它们即可。

## 贡献指南

### 知识库贡献（最高价值，无需写代码）

- **新增故障模式（正则类）**：knowledge/fault_patterns.json 加条目（id/patterns/diagnosis/next_steps/**source**；需要计数门槛的加 `min_count`，如 WiFi 断连 ≥3 次才算风暴），无需改代码，重启生效
- **新增错误码表**：knowledge/error_codes/<表名>.json（entries + source），analyzer 的 `_load_code_table` 挂载
- **新增 MCU 引脚库**：knowledge/pinmap/<型号>.json，必填 `engine` 字段（fixed_mux/af_table/io_mux 三选一），来源标数据手册；不确定的字段留空并在 caveats 说明，**不要编**
- **新增开发板**：knowledge/boards/<板名>.json（onboard 每项必填 pins 列表或 pin 单值 + note；board_issues 按翻车率排序）
- **改完必跑核验**：`python tests/validate_knowledge.py`——它抓得出引脚数录错、功能名录错、同功能双占、正则写坏、外设条目缺引脚字段（已实测抓出 3 处 F103 数据错误）

### 代码贡献

- 新增驱动：drivers/<name>/ 下放 `<name>.h/.c/<name>_port.h/wiring.md/meta.json`，meta.json 的 verified 字段**必须如实写验证状态**——评测员和学生都会实测
- 新增异常模式（复杂逻辑类）：analyzer.py 加规则函数+注册 RULES+_DIAG 配文案，test_analyzer.py 补正例+反例（正例命中 + 正常日志不误报，缺一不可）
- 新增工具：core.py 实现（零 MCP 依赖）→ server.py 注册（FastMCP 薄壳）→ tests/ 补用例；能离线测的必须离线可测
- 判断放哪条路：正则能匹配的走 JSON；需要计算（比例/计数/阈值）的写 Python
