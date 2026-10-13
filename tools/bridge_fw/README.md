# MCUDBG/1 桥固件（NUCLEO-H563ZI）

把一块闲置 MCU 变成 PC 侧总线工具（`i2c_scan` / `spi_scan` / `onewire_scan` /
`bridge_probe` / …）的**执行端**：它收 ASCII 命令，打真实总线时序，把结果
按协议回给 PC。mcu-debug-mcp 的总线工具 + 本固件 = 不买任何硬件就能跑的
I2C/SPI/1-Wire 探测链。

## 为什么需要桥

PC 没有原生 I2C/SPI 主机。可选路径实测过（详见 core.py「总线工具」区块头注释）：
ST-Link bridge 要 V3SET+扩展板（板载 V3E 实测 `BRG_NO_STLINK`）；FT2232H 需要
换 libusb 驱动（会夺走 COM 口，本机被明确禁止）；CH340/CP2102 是纯 UART 桥。
**闲置 MCU 跑桥固件是唯一零外购、零驱动改动的路。**

## 硬件

| 总线 | 引脚（NUCLEO-H563ZI 丝印） | 说明 |
|---|---|---|
| 命令口 | USART3 PD8/PD9（板载 ST-LINK VCP） | 115200 8N1 |
| I2C bus 0 | PB8=SCL，PB9=SDA | 开漏+内部上拉；**真总线请外接 4.7k 上拉** |
| SPI bus 0 | PA5=SCK，PA6=MISO，PA7=MOSI | ≈250kHz（mode 0） |
| SPI CS0 / CS1 | PA4 / PB0 | 推挽，空闲高 |
| 1-Wire bus 0 | PC7 | 开漏+内部上拉；**真总线请外接 4.7k** |
| RS485 | —（未配收发器） | `RS485 SEND` 如实回 `-ERR NOBUS` |

换到别的板（ESP32-S3 / Arduino 等）：协议是纯 ASCII 行协议，移植只需实现
`main.c` 里 `handle_line` 对应的那几条命令；串口波特率、命令表、应答格式
三样别改，PC 侧零改动。

## 协议（MCUDBG/1）——与 core.py 严格对齐，改要两边一起改

```
发： <CMD> [args...]\r\n
收： +OK <CMD首词> key=value ...     ← 成功
     -ERR <CODE> <说明>              ← 失败（NAK/TIMEOUT/NOBUS/ARG/BUSY/PROTO/NOCS/NOOW）
     #LOG ...                        ← 异步日志，PC 侧忽略
```

| 命令 | 成功回话 |
|---|---|
| `PING` | `+OK PING BRIDGE=mcu-debug proto=MCUDBG/1 caps=i2c,spi,1w dev=NUCLEO-H563ZI` |
| `I2C SCAN <bus> [lo] [hi]` | `+OK I2C SCAN bus=0 lo=8 hi=119 n=1 addrs=0x70` |
| `I2C READ <bus> <addr> <reg> <n>` | `+OK I2C READ bus=0 addr=0x68 reg=0x75 n=1 data=0x68` |
| `I2C WRITE <bus> <addr> <reg> <b0[,b1..]>` | `+OK I2C WRITE bus=0 addr=0x68 reg=0x6b n=1` |
| `SPI SCAN <bus>` | `+OK SPI SCAN bus=0 n=2 slots=0:0xef4018,1:0xffffffff` |
| `SPI READ <bus> <cs> <cmd> <n>` | `+OK SPI READ bus=0 cs=0 cmd=0x9f n=3 data=0xef,0x40,0x18` |
| `SPI WRITE <bus> <cs> <cmd> <b0[,b1..]>` | `+OK SPI WRITE bus=0 cs=0 n=1` |
| `OW SCAN <bus>` | `+OK OW SCAN bus=0 n=1 roms=28ff64a1b2c3d4e5` |
| `RS485 SEND <bus> <de_ms> <hex>` | `+OK RS485 SEND bus=0 n=8`（本板：`-ERR NOBUS`） |

★ 两条容易踩的协议细节（都在 PC 侧解析器上踩过）：
1. **多值字段必须逗号分隔在同一个 token 里**（`slots=0:..,1:..`）——PC 侧按
   空格切字段，带空格的值后半段会被当成别的字段丢掉；
2. **PING 回话必须含 `BRIDGE=mcu-debug`**——没有这道门，任何一根 UART 都可能
   被误当桥灌命令。

## 构建 / 烧录 / 验证

```
python tools/bridge_fw/build.py        # 编译（工具链路径在 build.py 顶部）
python tools/bridge_fw/live_check.py   # 烧录 → 8 个工具逐个打真桥 → 恢复原固件
```

手烧/恢复（备份指向你自己的扇区 0 备份，8KB）：

```
STM32_Programmer_CLI -c port=SWD mode=hotplug mode=normal -w fw.bin 0x08000000 -v -rst
STM32_Programmer_CLI -c port=SWD mode=hotplug mode=normal -w <备份.bin> 0x08000000 -v -rst
```

`live_check.py` 的空总线结论（I2C 0 个 / SPI 无器件 / 1-Wire 无）本身就是
**链路有效证据**：命令到了桥、时序打到了线、回话解析成功——只是线上没挂东西。
挂上真实器件（IMU/EEPROM/DS18B20）后，同一套命令直接给出器件级结论。

## 真机闭环记录（2026-09-22，NUCLEO-H563ZI）

`live_check.py` 6 项全 PASS，原固件恢复回读 md5 一致。第一次上真机就翻车的
三个坑，全部修在固件里、留档在此——**裸机固件没有 C 启动库，这些错静态看
代码几乎看不出来，全靠真机 + SWD 验尸抓的**：

1. **.bss 被链接进 Flash（致命）**：linker.ld 原本没写 `.data`/`.bss` 段，
   链接器把 1848B 全局变量（line_buf/line_len 等）当孤儿段塞进 FLASH——
   变量写不进、也无人清零，line_len 初值=0xFFFFFFFF，任何收到的字节一律
   回 `line too long`。修法：linker.ld 补 `.data AT(_etext)` / `.bss (NOLOAD)`
   两段 + Reset_Handler 开头自己拷 .data/清 .bss（裸机没有启动库，必须手写）。
   验尸方法：fw.map 看 `.bss` 落址是不是 `0x2000xxxx`。
   （探针固件同款脚本没炸只是因为它的 .bss=0 字节——**没有全局变量**，纯属侥幸。）
2. **MODF 挂死 SPI（真机 SWD 验尸定位）**：`SPI SCAN` 一下桥就死。SWD 读 PC=
   0x080000B6 卡在 `spi_xfer8` 等 RXP 的 while；SR=0x1202 解出 **MODF=1**——
   SPE 被硬件清、RXP 永不来、无超时 while 挂死整个固件。修法：① `spi_begin`
   置 SPE 前先 `IFCR=ALL` 清 MODF 残留并补写 SSI=1；② `spi_xfer8` 两个等待
   全部加 100ms 超时（**任何死等都必须限时**）。修后 SPI SCAN/READ 全通。
3. **50 波特 + SWD 读 IDR = 半根逻辑分析仪**：排查 RX 方向时用 pyserial 以
   50 波特发 0x00（低电平持续 ~180ms），同时 CubeProgrammer `-r32` 反复读
   `GPIOD->IDR`（0x52020C10，S 别名）——SWD 毫秒级采样能稳稳抓到电平翻转，
   零成本验证了 PC→PD9 链路是通的。注意：pyserial 的 `break_condition` 在
   ST-Link V3 的 VCP 上**不被转发**（假阴性），别用它做电气验证。

## 边界（与 PC 侧工具同款，如实写在输出里）

- 「有应答」≠ 器件功能正常；「无应答」也可能是器件在复位中；
- SPI「扫描」只对遵守 0x9F 的 NOR/NAND flash 有效，SPI 传感器要用
  `spi_read_reg` 给具体命令字；
- `rs485_send` 只证明桥把字节推出去了，对端是否收到它证明不了。
