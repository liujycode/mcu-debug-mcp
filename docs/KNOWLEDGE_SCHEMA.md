# 知识库 Schema 与贡献指南（能力项 N-13）

本仓库的**护城河不是代码，是知识**。代码量不大、也不难抄；「出厂验证过的引脚
映射、错误码出处、板级坑清单」才是别处拿不到的东西。所以知识库的格式与准入
规则要写清楚——**能贡献**的前提是**知道怎么写才算合格**。

## 一、四条设计原则

1. **代码目录 ≠ 数据目录**：知识库与驱动库走 `paths.*` 定位（外置层 > 包内置），
   代码只在自己的目录里。pip 装到只读 `site-packages` 也不会把数据往那儿塞。
2. **条目级 `source` 是硬要求**：每一条知识都要写明出处（手册页 / 官方文档章节 /
   实测平台与日期）。校验器会**强制**检查，缺了直接报错。
   —— 理由很直接：**不许凭记忆灌**。AI 幻觉出来的引脚映射比没有更危险。
3. **外置层可叠加**：用户可以把补充知识放 `~/.mcu-debug-mcp/` 或
   `$MCU_DEBUG_MCP_HOME/`，与内置层合并；**同名以高优先级（外置）为准**。
4. **不重排、不美化**：明细资源读的是 JSON **原文**（逐字）。测试里有一条专门
   保证「注入 → 校验 → 逐字复原」——防止哪天有人顺手 `json.dump` 重排，把
   手写的「一行一条」排版洗成标准缩进。

## 二、目录结构

```
knowledge/
├── ecosystem.json            # 生态/框架识别（单文件知识）
├── fault_patterns.json       # 故障模式库（外置补充规则）
├── pinmap/<mcu>.json         # 每款 MCU 一个文件
├── boards/<board>.json       # 每块板一个文件
├── error_codes/<table>.json  # 每张码表一个文件
└── fault_bits/<arch>.json    # 每种架构一个文件
drivers/
└── <driver_name>/            # 每款驱动一个目录
    ├── meta.json             # 元信息（接口/引脚/验证记录）
    └── *.c / *.h             # 预验证驱动源码
```

分层查找规则（`paths` 模块）：

| 优先级 | 位置 | 用途 |
|---|---|---|
| 1 | `$MCU_DEBUG_MCP_HOME` | 显式指定的数据根 |
| 2 | `~/.mcu-debug-mcp` | 用户外置补充（推荐给贡献者） |
| 3 | 包目录（源码树 / 安装包） | 内置知识 |

## 三、各文件 Schema

### 3.1 `pinmap/<mcu>.json`

```json
{
  "mcu": "STM32F103C8T6",
  "package": "LQFP48",
  "source": "STM32F103x8/xB 数据手册（DS5319）· 表 5 引脚定义",
  "pins": {
    "PA9": {
      "source": "DS5319 表 5",
      "functions": ["USART1_TX", "TIM1_CH2"],
      "fixed_mux": false,
      "note": "复用功能需先开 AFIO 时钟"
    }
  },
  "caveats": ["PB3/PB4/PA15 默认是 JTAG 脚，当普通 IO 要先 remap"]
}
```

要点：
- `functions` 里的功能名会与全局 `fixed_mux` 做**全局唯一性**校验（一个固定复用功能
  只能出现在一个脚上）；
- `SWD` 相关脚（PA13/PA14）会被 `pin_check` 特别提示；
- 每个引脚级条目也要带 `source`。

### 3.2 `boards/<board>.json`

```json
{
  "board": "BluePill（STM32F103C8T6 蓝药丸）",
  "alias": ["bluepill", "蓝药丸"],
  "mcu": "stm32f103c8t6",
  "source": "蓝药丸常见版本原理图 V2.x",
  "onboard": {
    "LED1": { "source": "…原理图 V2.x", "pin": "PC13", "active": "low", "note": "低电平点亮" }
  },
  "board_issues": ["按翻车率排序的坑清单"],
  "power_notes": "供电说明"
}
```

要点：`onboard` 里**必须有 `pin` 或 `pins`**（校验器强制），否则 `pin_check(..., board=)`
没法交叉检查板载占用。

### 3.3 `error_codes/<table>.json`

```json
{
  "id": "at_common",
  "desc": "AT 指令集通用响应/错误串",
  "source": "Espressif ESP-AT 用户指南 · 基础 AT 指令集",
  "entries": {
    "ERROR": {
      "source": "ESP-AT 用户指南 · 通用响应",
      "meaning": "指令执行失败",
      "hint": "逐条单发定位是哪条失败；查指令拼写、参数个数"
    }
  }
}
```

要点：`hint` 要写**可执行的动作**，不要只重复 `meaning`。

### 3.4 `fault_bits/<arch>.json`

```json
{
  "table": "cortex_m",
  "arch": "cortex-m",
  "desc": "ARM Cortex-M 故障寄存器位域",
  "source": "ARM Cortex-M 技术参考手册 · CFSR/HFSR/BFAR/MMFAR",
  "registers": { "CFSR": { "fields": [ … ] } }
}
```

要点：`arch` 字段就是 `decode_fault(arch=...)` 要填的值（**不是文件名**，两者常被
混用，工具会同时接受但会在输出里点明区别）。

### 3.5 `ecosystem.json`（单文件知识）

```json
{
  "id": "ecosystem",
  "desc": "生态/框架识别",
  "source": "各生态官方文档；每条带条目级 source",
  "entries": {
    "zephyr": {
      "source": "Zephyr 官方文档",
      "name": "Zephyr RTOS",
      "project_markers": ["west.yml", "prj.conf"],
      "build_outputs": ["build/zephyr/zephyr.elf"],
      "log_signatures": ["*** Booting Zephyr OS build … ***"],
      "gotchas": "Kconfig 选项名拼错不会报错，只会静默少功能"
    }
  }
}
```

### 3.6 `drivers/<name>/meta.json`

```json
{
  "name": "mpu6050",
  "desc": "六轴 IMU",
  "interface": "i2c",
  "verified": "在 stm32f103c8t6 上实测通过（2026-09-18）",
  "pins": { "SCL": "…", "SDA": "…" }
}
```

要点：`verified` 要写**在哪块板、什么时候**实测过；只有 `driver_list` 会把它显示给
用户——这是我这个库与「AI 现写代码」的根本区别。

## 四、校验：写完必须跑

```bash
python tests/validate_knowledge.py     # 知识库结构与条目级 source
python tests/audit_capability.py       # 能力面清单 × 实现 × 文档三方对账
python -m pytest tests -q              # 全量回归
```

`validate_knowledge.py` 检查什么：

- 引脚数 / 唯一性 / `fixed_mux` 功能**全局唯一**；
- SWD 脚一致性、`onboard` 必须有 pin(s) 字段；
- 所有正则**可编译**（知识里带正则的条目）；
- **条目级 `source` 强制**：任何一层缺 source 都报错，并指出是哪一条。

## 五、贡献一个新知识：五步

1. **建/改文件**：按上面 schema 放好，**每条都写 `source`**；
2. **跑校验**：`python tests/validate_knowledge.py`，必须 0 错误；
3. **跑全量**：`python -m pytest tests -q`，必须全绿；
4. **看资源是否覆盖**：新知识类别要在 `server.py` 里补「清单 + 明细」两个 resource，
   并同步 `tests/test_mcp_resources.py` 的 `EXPECTED_STATIC` / `EXPECTED_TEMPLATES`
   （这份清单是**唯一权威口径**，不更新就会红）；
5. **更新文档数字**：README 里的款数/条数由 `coverage.json` 的 `docNumbers` 自动对账，
   改了知识量就要同步，否则 `audit_capability.py` 直接报脱节。

> 关于「产能口径」：**宁可少而准，不许凭记忆灌**。一条带出处、可复现的知识，
> 价值高于十条听起来合理但没人验证过的。
