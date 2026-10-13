# 示例（examples/）

这个目录放**离线可复现**的用法示例——不需要任何硬件、不需要装 pyserial，
拿到仓库就能跑。

## offline_demo.py

```bash
python examples/offline_demo.py
```

一次走完 8 个离线能力，全部只调 `core.py`（零框架依赖），不启动 MCP 服务、
不打开任何串口：

| # | 展示 | 对应工具 |
|---|---|---|
| 1 | 日志分析（真机实测过的 ORE 拖死接收端） | `analyze_log`（45 条故障模式库） |
| 2 | 错误码查询（带出处） | `error_code_lookup` |
| 3 | 反例：故意给没收录的码 → 明说查不到，不猜 | 同上 |
| 4 | Modbus RTU **响应帧**解析 + CRC 计算 | `modbus_parse` / `modbus_crc` |
| 5 | 引脚冲突检测（故意分错脚，看它抓出来） | `pin_check` |
| 6 | 生态识别（Keil 工程的产物与典型报错） | `ecosystem_get` |
| 7 | 工程结构识别（扫标志文件判断生态） | `project_detect` |
| 8 | 环境自检（版本/依赖/串口/烧录器/知识库） | `env_status` |

**为什么值得先跑它**：这些都是「不碰硬件就能看到价值」的部分。想先确认工具在
你这台机器上能不能用、知识库有没有装全，跑这一条最快。

## 与「真机」的分界

上面的示例全离线。真正读硬件的那几步（`serial_list` → `serial_open` →
`serial_read` → `analyze_log`）需要 pyserial 与真实设备，且默认**只读**；
要发字节得显式放写权限。详见仓库根 README 的「安全与隔离」一节。

有板子时，仓库里还有一个真机验收夹具（在包内 `tools/live_acceptance.py`）：

```bash
python tools/live_acceptance.py COM21            # 只读
python tools/live_acceptance.py COM7 --allow-write --cmd "AT" --expect "OK"
```

它默认不发任何字节，换板子要按目标固件的协议给探针（见该文件头部说明）。
