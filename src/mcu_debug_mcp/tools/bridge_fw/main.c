/*
 * MCUDBG/1 桥固件 —— NUCLEO-H563ZI（STM32H563ZI）
 * =============================================================
 * 它是什么：把 PC 发来的 ASCII 命令翻译成 I2C / SPI / 1-Wire 的真实时序。
 * mcu-debug-mcp 的 bridge_probe / i2c_scan / spi_scan / onewire_scan 等
 * 8 个总线工具都靠它跑真机。协议定义在 core.py 的「总线工具」区块头
 * 注释里，两边必须逐字对齐 —— 改协议要两边一起改，并补测试。
 *
 * 引脚（NUCLEO-H563ZI，板上有丝印）：
 *   USART3  PD8=TX / PD9=RX  AF7   ← 板载 ST-LINK VCP = PC 侧 COMx，115200 8N1
 *   I2C1    PB8=SCL / PB9=SDA AF4  开漏 + 内部上拉（真总线请外接 4.7k）
 *   SPI1    PA5=SCK / PA6=MISO / PA7=MOSI  AF5，fPCLK/256 ≈ 250kHz
 *   CS0     PA4（推挽输出，空闲高）
 *   CS1     PB0（推挽输出，空闲高）
 *   1-Wire  PC7（开漏 + 内部上拉；真总线请外接 4.7k）
 *   RS485   ★ 本板没收发器 → RS485 SEND 一律回 -ERR NOBUS（如实，不装）
 *
 * 协议要点（与 PC 侧 _bridge_fields 的解析严格对齐）：
 *   · 回话行只有两种：`+OK <命令首词> key=value ...` / `-ERR <码> <说明>`；
 *   · `#` 开头的行是异步日志，PC 侧会忽略；
 *   · 多值字段必须**逗号分隔在同一个 token 里**（如 addrs=0x70,0x71）——
 *     PC 侧按空格切字段，带空格的值后半段会被当成别的字段丢掉。
 *
 * 诚实边界：扫描的「有应答」只代表有人 ACK，不代表器件功能正常。
 *
 * 不吃 HAL，只用 CMSIS 寄存器结构体；不含标准库（-nostdlib）。
 * 位定义全部用本文件的 X_* 宏（值取自 RM0481），不用 CMSIS 位名宏，
 * 避免不同版本头文件的命名差异 —— 寄存器结构体名则是稳定的，直接用。
 */

#include "stm32h563xx.h"

/* ---------------------------------------------------------------
 * 位定义（RM0481）
 * --------------------------------------------------------------- */
#define XBIT(x)           (1u << (x))

/* USART */
#define X_UART_RXNE       XBIT(5)
#define X_UART_TXFNF      XBIT(7)
#define X_UART_ORE        XBIT(3)
#define X_UART_ICR_ORECF  XBIT(3)

/* I2C */
#define X_I2C_PE          XBIT(0)
#define X_I2C_RD_WRN      XBIT(10)
#define X_I2C_START       XBIT(13)
#define X_I2C_STOP        XBIT(14)
#define X_I2C_AUTOEND     XBIT(25)
#define X_I2C_NBYTES_POS  16u
#define X_I2C_TXIS        XBIT(0)
#define X_I2C_RXNE        XBIT(2)
#define X_I2C_NACKF       XBIT(4)
#define X_I2C_STOPF       XBIT(5)
#define X_I2C_TC          XBIT(6)
#define X_I2C_BUSY        XBIT(15)
#define X_I2C_ICR_NACKCF  XBIT(4)
#define X_I2C_ICR_STOPCF  XBIT(5)
#define X_I2C_ICR_ALLERR  (XBIT(8) | XBIT(9) | XBIT(10))   /* BERR|ARLO|OVR */

/* SPI —— H5 是新一代事务式 SPI：TSIZE 定帧数、CR1.CSTART 启动、EOT 收尾；
 * 没有 DR，收发走 TXDR/RXDR（DSIZE=7 → 8 位帧，按 8 位指针访问即一个 packet）。
 * 位值取自 CMSIS 头（stm32h563xx.h，ST 生成，不凭记忆）。 */
#define X_SPI_SPE         XBIT(0)
#define X_SPI_CSTART      XBIT(9)
#define X_SPI_SSI         XBIT(12)
#define X_SPI_RXP         XBIT(0)
#define X_SPI_TXP         XBIT(1)
#define X_SPI_EOT         XBIT(3)
#define X_SPI_OVR         XBIT(6)
#define X_SPI_MODF        XBIT(9)
#define X_SPI_IFC_ALL     ((7u << 3) | XBIT(5) | XBIT(6) | (3u << 8) | XBIT(11))
                          /* EOTC(3)|UDRC(5)|CRCEC(7)|TIFREC(8)|MODFC(9)|SUSPC(11)|TXTFC(4) */
#define X_SPI_MBR_F256    (7u << 28)      /* CFG1.MBR=111 → fPCLK/256 ≈ 250kHz */
#define X_SPI_DSIZE8      (7u << 0)       /* CFG1.DSIZE=7 → 8 位帧 */
#define X_SPI_MASTER      XBIT(22)        /* CFG2 */
#define X_SPI_SSM         XBIT(26)        /* CFG2：软件管理 SS（CS 由 GPIO 出） */

/* 1-Wire 时序（µs，Maxim AN126/AN187 标准档） */
#define OW_RST_LOW_US     480u
#define OW_PRES_SAMPLE_US 70u
#define OW_PRES_TAIL_US   410u
#define OW_SLOT_US        64u
#define OW_LOW_WR0_US     60u
#define OW_LOW_W1_US      6u
#define OW_LOW_RD_US      6u
#define OW_RD_SAMPLE_US   9u
#define OW_MAX_DEVS       16

/* ---------------------------------------------------------------
 * 前置声明（-nostdlib 没有 <string.h>，strcmp 是本地实现）
 * --------------------------------------------------------------- */
static int strcmp_local(const char *a, const char *b);

/* ---------------------------------------------------------------
 * 极小工具：串口输出 / SysTick 微秒时基
 * --------------------------------------------------------------- */
static void uart_putc(char c)
{
    while ((USART3->ISR & X_UART_TXFNF) == 0u) {
    }
    USART3->TDR = (uint32_t)(uint8_t)c;
}

static void uart_puts(const char *s)
{
    while (*s != '\0') {
        uart_putc(*s++);
    }
}

static void delay(volatile uint32_t n)
{
    while (n-- > 0u) {
        __NOP();
    }
}

/* SysTick 自由跑：HCLK/8 = 8MHz ⟹ 1 tick = 0.125µs，2^24 tick ≈ 2.1s 回绕。
 * 无符号差值在「经过时长 < 2^24 tick」前提下天然处理回绕。 */
static uint32_t tick8(void)
{
    return 0x00FFFFFFu - SysTick->VAL;
}

static void udelay(uint32_t us)
{
    uint32_t t0 = tick8();
    while ((tick8() - t0) < (us * 8u)) {
    }
}

static int ms_elapsed(uint32_t t0, uint32_t ms)
{
    return ((tick8() - t0) > ((uint32_t)ms * 8000u)) ? 1 : 0;
}

/* ---------------------------------------------------------------
 * I2C1（bus 0）
 * --------------------------------------------------------------- */
static int i2c_wait_idle(uint32_t ms)
{
    uint32_t t0 = tick8();
    while ((I2C1->ISR & X_I2C_BUSY) != 0u) {
        if (ms_elapsed(t0, ms)) {
            return -1;                     /* 多半 SDA 被器件拉死在低 */
        }
    }
    return 0;
}

/* 等一组标志中任一出现。返回命中的 ISR 值；0 = 超时 */
static uint32_t i2c_wait_any(uint32_t mask, uint32_t ms)
{
    uint32_t t0 = tick8();
    for (;;) {
        uint32_t isr = I2C1->ISR;
        if ((isr & mask) != 0u) {
            return isr;
        }
        if (ms_elapsed(t0, ms)) {
            return 0u;
        }
    }
}

/* 出错收尾：补 STOP + 清标志，把总线放掉，别占死 */
static void i2c_abort(void)
{
    I2C1->CR2 |= X_I2C_STOP;
    I2C1->ICR  = (X_I2C_ICR_NACKCF | X_I2C_ICR_STOPCF | X_I2C_ICR_ALLERR);
}

/* 单地址探测（START+addr+STOP，看有没有 ACK）。0=无应答 1=有 -1=总线卡死 */
static int i2c_probe(uint8_t addr)
{
    if (i2c_wait_idle(20u) != 0) {
        return -1;
    }
    I2C1->ICR = (X_I2C_ICR_NACKCF | X_I2C_ICR_STOPCF | X_I2C_ICR_ALLERR);
    I2C1->CR2 = ((uint32_t)addr << 1) | (1u << X_I2C_NBYTES_POS) |
                X_I2C_AUTOEND | X_I2C_START;
    uint32_t isr = i2c_wait_any(X_I2C_STOPF | X_I2C_NACKF, 30u);
    if (isr == 0u) {
        i2c_abort();
        return -1;
    }
    int present = ((isr & X_I2C_NACKF) == 0u);
    I2C1->ICR   = (X_I2C_ICR_NACKCF | X_I2C_ICR_STOPCF);
    return present;
}

/* 读寄存器：先写 1 字节寄存器地址（不带 STOP），再重启为读 n 字节。
 * 返回 0=成功，-1=总线卡死/TIMEOUT，-2=NAK */
static int i2c_read_regs(uint8_t addr, uint8_t reg, uint8_t *out, uint32_t n)
{
    if (i2c_wait_idle(20u) != 0) {
        return -1;
    }
    I2C1->ICR = (X_I2C_ICR_NACKCF | X_I2C_ICR_STOPCF | X_I2C_ICR_ALLERR);

    I2C1->CR2 = ((uint32_t)addr << 1) | (1u << X_I2C_NBYTES_POS) | X_I2C_START;
    if ((i2c_wait_any(X_I2C_TXIS | X_I2C_NACKF, 30u) & X_I2C_TXIS) == 0u) {
        i2c_abort();
        return -2;
    }
    I2C1->TXDR = reg;
    if ((i2c_wait_any(X_I2C_TC | X_I2C_NACKF, 30u) & X_I2C_TC) == 0u) {
        i2c_abort();
        return -2;
    }

    I2C1->CR2 = ((uint32_t)addr << 1) | ((uint32_t)n << X_I2C_NBYTES_POS) |
                X_I2C_RD_WRN | X_I2C_AUTOEND | X_I2C_START;
    for (uint32_t i = 0u; i < n; i++) {
        if ((i2c_wait_any(X_I2C_RXNE | X_I2C_NACKF, 30u) & X_I2C_RXNE) == 0u) {
            i2c_abort();
            return -2;
        }
        out[i] = (uint8_t)I2C1->RXDR;
    }
    (void)i2c_wait_any(X_I2C_STOPF, 30u);
    I2C1->ICR = (X_I2C_ICR_NACKCF | X_I2C_ICR_STOPCF);
    return 0;
}

/* 写寄存器：reg + n 字节，一笔 STOP 收尾。返回值同上 */
static int i2c_write_regs(uint8_t addr, uint8_t reg,
                          const uint8_t *vals, uint32_t n)
{
    if (i2c_wait_idle(20u) != 0) {
        return -1;
    }
    I2C1->ICR = (X_I2C_ICR_NACKCF | X_I2C_ICR_STOPCF | X_I2C_ICR_ALLERR);

    I2C1->CR2 = ((uint32_t)addr << 1) |
                ((uint32_t)(n + 1u) << X_I2C_NBYTES_POS) |
                X_I2C_AUTOEND | X_I2C_START;
    if ((i2c_wait_any(X_I2C_TXIS | X_I2C_NACKF, 30u) & X_I2C_TXIS) == 0u) {
        i2c_abort();
        return -2;
    }
    I2C1->TXDR = reg;
    for (uint32_t i = 0u; i < n; i++) {
        if ((i2c_wait_any(X_I2C_TXIS | X_I2C_NACKF, 30u) & X_I2C_TXIS) == 0u) {
            i2c_abort();
            return -2;
        }
        I2C1->TXDR = vals[i];
    }
    (void)i2c_wait_any(X_I2C_STOPF, 30u);
    I2C1->ICR = (X_I2C_ICR_NACKCF | X_I2C_ICR_STOPCF);
    return 0;
}

/* ---------------------------------------------------------------
 * SPI1（bus 0，CS0=PA4 / CS1=PB0）
 * --------------------------------------------------------------- */
/* active=1 → 拉低选中；active=0 → 释放回高。
 * ★ BSRR 低 16 位是置高、高 16 位是拉低 —— 别记反。 */
static void spi_cs(int cs, int active)
{
    if (cs == 0) {
        GPIOA->BSRR = active ? (1u << (4 + 16)) : (1u << 4);
    } else {
        GPIOB->BSRR = active ? (1u << (0 + 16)) : (1u << 0);
    }
}

/* 开一笔事务：TSIZE = nframes 帧，SPE + CSTART。调用前先把 CS 拉低。
 * ★ 2026-09-22 真机：MODF 置位时硬件拒收 SPE —— 所以清标志（含 MODFC）
 *   必须放在置 SPE 之前；SSI 也补一手（主模式 SSM=1 要求 SSI=1）。
 *   任何残留状态都别带进新事务。 */
static void spi_begin(uint32_t nframes)
{
    SPI1->CR1 &= ~(X_SPI_SPE | X_SPI_CSTART);/* 配置必须在 SPE=0 时做；清 CSTART 残留 */
    SPI1->IFCR = X_SPI_IFC_ALL;              /* 清上笔/复位残留标志（含 MODF） */
    SPI1->CR2  = nframes;                    /* TSIZE */
    SPI1->CR1 |= X_SPI_SSI;                  /* 保险：内部 NSS=高，防 MODF */
    SPI1->CR1 |= X_SPI_SPE;
    SPI1->CR1 |= X_SPI_CSTART;
}

/* 收尾：等 EOT → 清全部标志 → 关 SPE（下一笔事务重新配 TSIZE） */
static void spi_end(void)
{
    uint32_t t0 = tick8();
    while ((SPI1->SR & X_SPI_EOT) == 0u) {
        if (ms_elapsed(t0, 100u)) {
            break;                           /* 异常也别把固件挂死 */
        }
    }
    SPI1->IFCR = X_SPI_IFC_ALL;
    SPI1->CR1 &= ~X_SPI_SPE;
}

/* 单帧全双工。★ 两个等待都必须带超时（2026-09-22 真机教训：MODF 把 SPE
 *   清掉后 RXP 永远不来，无超时的 while 直接把固件挂死——PC 验尸实锤）。
 *   超时返回 0xFF（MISO 悬空的正常读数恰好也是全 1，语义无害）。 */
static uint8_t spi_xfer8(uint8_t b)
{
    uint32_t t0 = tick8();
    while ((SPI1->SR & X_SPI_TXP) == 0u) {
        if (ms_elapsed(t0, 100u)) {
            return 0xFFu;
        }
    }
    *(volatile uint8_t *)&SPI1->TXDR = b;
    t0 = tick8();
    while ((SPI1->SR & X_SPI_RXP) == 0u) {
        if (ms_elapsed(t0, 100u)) {
            return 0xFFu;
        }
    }
    return *(volatile uint8_t *)&SPI1->RXDR;
}

/* ---------------------------------------------------------------
 * 1-Wire（bus 0，PC7，开漏）
 * --------------------------------------------------------------- */
#define OW_LOW()   (GPIOC->BSRR = (1u << (7 + 16)))   /* ODR=0：开漏拉低 */
#define OW_HIGH()  (GPIOC->BSRR = (1u << 7))          /* ODR=1：释放，靠上拉回高 */
#define OW_SAMPLE() ((GPIOC->IDR >> 7) & 1u)

static int ow_reset_presence(void)
{
    OW_LOW();
    udelay(OW_RST_LOW_US);
    OW_HIGH();
    udelay(OW_PRES_SAMPLE_US);
    int present = (OW_SAMPLE() == 0u);
    udelay(OW_PRES_TAIL_US);
    return present;
}

static void ow_write_bit(int b)
{
    if (b) {
        OW_LOW();
        udelay(OW_LOW_W1_US);
        OW_HIGH();
        udelay(OW_SLOT_US - OW_LOW_W1_US);
    } else {
        OW_LOW();
        udelay(OW_LOW_WR0_US);
        OW_HIGH();
        udelay(OW_SLOT_US - OW_LOW_WR0_US + 4u);
    }
}

static int ow_read_bit(void)
{
    OW_LOW();
    udelay(OW_LOW_RD_US);
    OW_HIGH();
    udelay(OW_RD_SAMPLE_US);
    int v = (OW_SAMPLE() != 0u);
    udelay(OW_SLOT_US - OW_RD_SAMPLE_US + 4u);
    return v;
}

static void ow_write_byte(uint8_t b)
{
    for (int i = 0; i < 8; i++) {
        ow_write_bit(b & 1u);
        b >>= 1;
    }
}

/* Maxim CRC8（多项式 0x31 反射 = 0x8C，初值 0） */
static uint8_t ow_crc8(const uint8_t *p, int n)
{
    uint8_t crc = 0u;
    while (n-- > 0) {
        uint8_t b = *p++;
        for (int i = 0; i < 8; i++) {
            uint8_t mix = (uint8_t)((crc ^ b) & 1u);
            crc >>= 1;
            if (mix) {
                crc ^= 0x8Cu;
            }
            b >>= 1;
        }
    }
    return crc;
}

/* Search ROM（Maxim AN187 标准算法）。返回找到的器件数 */
static int ow_search(uint64_t *out, int max)
{
    int found = 0, last_device = 0, last_disc = 0;
    while (!last_device && found < max) {
        uint64_t rom = 0u;
        int last_zero = 0;
        if (!ow_reset_presence()) {
            break;                                  /* 总线上没有器件 */
        }
        ow_write_byte(0xF0u);
        for (int bit = 1; bit <= 64; bit++) {
            int id  = ow_read_bit();
            int cmp = ow_read_bit();
            if (id != 0 && cmp != 0) {
                return found;                       /* 两位都 1：总线异常，收手 */
            }
            int dir;
            if (id == 0 && cmp == 0) {              /* 冲突位：按 AN187 走分支 */
                if (bit < last_disc) {
                    dir = (int)((rom >> (bit - 1)) & 1u);
                } else if (bit == last_disc) {
                    dir = 1;
                } else {
                    dir = 0;
                }
                if (dir == 0) {
                    last_zero = bit;
                }
            } else {
                dir = id;
            }
            if (dir != 0) {
                rom |= ((uint64_t)1u) << (bit - 1);
            }
            ow_write_bit(dir);
        }
        last_disc = last_zero;
        if (last_disc == 0) {
            last_device = 1;
        }
        /* CRC 校验：坏 ROM 丢弃（噪声/半接触）；搜索分支已推进，不会死循环 */
        uint8_t bytes[8];
        for (int i = 0; i < 8; i++) {
            bytes[i] = (uint8_t)((rom >> (8 * i)) & 0xFFu);
        }
        if (ow_crc8(bytes, 7) == bytes[7]) {
            out[found++] = rom;
        }
    }
    return found;
}

/* ---------------------------------------------------------------
 * 行协议：应答缓冲（带长度，超长截断）
 * --------------------------------------------------------------- */
static char     line_buf[640];
static unsigned line_len = 0u;
static char     out_buf[1200];
static unsigned out_len  = 0u;

static void out_reset(void)
{
    out_len = 0u;
    out_buf[0] = '\0';
}

static void out_add_str(const char *s)
{
    while (*s != '\0' && out_len < (unsigned)(sizeof(out_buf) - 1u)) {
        out_buf[out_len++] = *s++;
    }
    out_buf[out_len] = '\0';
}

static void out_add_u(uint32_t v)
{
    char tmp[11];
    int  i = 0;
    if (v == 0u) {
        out_add_str("0");
        return;
    }
    while (v != 0u) {
        tmp[i++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (i-- > 0) {
        char one[2];
        one[0] = tmp[i];
        one[1] = '\0';
        out_add_str(one);
    }
}

static const char HEXC[] = "0123456789abcdef";

static void out_add_hex8(uint32_t v)      /* "0x%02x" */
{
    char s[5];
    s[0] = '0'; s[1] = 'x';
    s[2] = HEXC[(v >> 4) & 0xFu];
    s[3] = HEXC[v & 0xFu];
    s[4] = '\0';
    out_add_str(s);
}

static void out_add_hex24(uint32_t v)     /* "0x%06x" */
{
    char s[9];
    s[0] = '0'; s[1] = 'x';
    for (int i = 0; i < 6; i++) {
        s[2 + i] = HEXC[(v >> (4 * (5 - i))) & 0xFu];
    }
    s[8] = '\0';
    out_add_str(s);
}

static void reply_ok(const char *echo_head)
{
    uart_puts("+OK ");
    uart_puts(echo_head);
    uart_putc(' ');
    uart_puts(out_buf);
    uart_puts("\r\n");
}

static void reply_err(const char *code, const char *msg)
{
    uart_puts("-ERR ");
    uart_puts(code);
    uart_putc(' ');
    uart_puts(msg);
    uart_puts("\r\n");
}

/* 十六进制/十进制解析（"0x70" / "119"；"2.0" 这类含小数点的串会解析失败——
 * RS485 SEND 的 de_ms 参数本桥用不上，如实回 NOBUS）。成功返回 1 */
static int parse_num(const char *s, uint32_t *out)
{
    uint32_t v = 0u;
    int      any = 0;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        s += 2;
        for (; *s != '\0'; s++) {
            char c = *s;
            uint32_t d;
            if (c >= '0' && c <= '9')      d = (uint32_t)(c - '0');
            else if (c >= 'a' && c <= 'f') d = (uint32_t)(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') d = (uint32_t)(c - 'A' + 10);
            else return 0;
            v = v * 16u + d;
            any = 1;
        }
    } else {
        for (; *s != '\0'; s++) {
            if (*s < '0' || *s > '9') {
                return 0;
            }
            v = v * 10u + (uint32_t)(*s - '0');
            any = 1;
        }
    }
    *out = v;
    return any;
}

/* "0x01,0x02,03" → 字节列表（协议约定：多值一律逗号分隔在同一个 token 里） */
static int parse_hex_list(const char *s, uint8_t *out, uint32_t max)
{
    uint32_t n = 0u;
    if (*s == '\0') {
        return 0;
    }
    for (;;) {
        uint32_t v = 0u;
        int digits = 0;
        /* 可选 0x/0X 前缀：usage 示例就是 "0x01,0x02,03"，但原实现逐字符
         * 解析遇 'x' 直接 return -1 —— 示例输入必被拒（2026-10-14 真机
         * 矩阵抓出）。与 parse_num 的 0x 语义对齐。 */
        if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
            s += 2;
        }
        while (*s != '\0' && *s != ',') {
            char c = *s;
            uint32_t d;
            if (c >= '0' && c <= '9')      d = (uint32_t)(c - '0');
            else if (c >= 'a' && c <= 'f') d = (uint32_t)(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') d = (uint32_t)(c - 'A' + 10);
            else return -1;
            v = v * 16u + d;
            digits++;
            if (digits > 2) {
                return -1;                      /* 一个字节最多两位十六进制 */
            }
            s++;
        }
        if (digits == 0) {
            return -1;
        }
        if (n >= max) {
            return -2;                          /* 超桥侧上限 */
        }
        out[n++] = (uint8_t)v;
        if (*s == '\0') {
            break;
        }
        s++;                                    /* 跳过逗号 */
        if (*s == '\0') {
            return -1;
        }
    }
    return (int)n;
}

/* 把行切成 argv（原地改写，空格分隔，最多 15 段） */
static int tokenize(char *s, char *argv[], int maxv)
{
    int   argc = 0;
    char *p = s;
    while (*p != '\0' && argc < maxv) {
        while (*p == ' ') {
            p++;
        }
        if (*p == '\0') {
            break;
        }
        argv[argc++] = p;
        while (*p != '\0' && *p != ' ') {
            p++;
        }
        if (*p == ' ') {
            *p++ = '\0';
        }
    }
    return argc;
}

static void str_up(char *s)
{
    while (*s != '\0') {
        if (*s >= 'a' && *s <= 'z') {
            *s = (char)(*s - 'a' + 'A');
        }
        s++;
    }
}

static int strcmp_local(const char *a, const char *b)
{
    while (*a != '\0' && *a == *b) {
        a++;
        b++;
    }
    return (int)(uint8_t)*a - (int)(uint8_t)*b;
}

static void handle_line(char *line)
{
    char *argv[15];
    int   argc = tokenize(line, argv, 15);
    if (argc == 0) {
        return;
    }
    str_up(argv[0]);

    /* ---------- PING ---------- */
    if (argc == 1 && strcmp_local(argv[0], "PING") == 0) {
        uart_puts("+OK PING BRIDGE=mcu-debug proto=MCUDBG/1 "
                  "caps=i2c,spi,1w dev=NUCLEO-H563ZI\r\n");
        return;
    }

    /* ---------- I2C ---------- */
    if (strcmp_local(argv[0], "I2C") == 0) {
        if (argc < 2) {
            reply_err("ARG", "i2c needs SCAN/READ/WRITE");
            return;
        }
        str_up(argv[1]);
        uint32_t bus = 0u;
        if (argc >= 3) {
            (void)parse_num(argv[2], &bus);
        }
        if (bus != 0u) {
            reply_err("NOBUS", "only bus 0 is fitted");
            return;
        }

        /* UNSTICK：SCL 补 9 个时钟把卡死的从机状态机顶出来（2026-10-14 真机
         * 需求：IMU 上电即拉死 SDA，复位主控无效，必须从时钟侧解）。
         * PB8(SCL) 切普通开漏输出打脉冲，PB9(SDA) 释放成输入观电平；
         * SDA 一回高就停，最后补一个 STOP 时序（SCL 高时 SDA 0→1），恢复 AF4。
         * 注意：若实际接线 SDA/SCL 互换，这里打的时钟会落到对端 SDA 上——
         * 开漏数字信号，从机只会收到垃圾位不会损坏；先 GPIO READ B 看电平。 */
        if (strcmp_local(argv[1], "UNSTICK") == 0u) {
            /* PP 参数：推挽模式（otsyper 位8=0）。默认开漏：外部强拉时脉冲
             * 打不出高，只能靠内部上拉（~40k）顶不动。推挽能强制驱动线——
             * 用于区分「器件钳位/欠压（能顶高）」vs「硬短路到地（顶不高）」。
             * 推挽顶短路电流受限自保护，短时诊断无损坏。 */
            int push_pull = (argc >= 3 && strcmp_local(argv[2], "PP") == 0u);
            uint32_t moder  = GPIOB->MODER;
            uint32_t otyper = GPIOB->OTYPER;
            /* PB8 → 输出(01)，清 AF；PB9 → 输入(00)。AFR 未动，无需保存 */
            GPIOB->MODER = (moder & ~((3u << 16) | (3u << 18))) | (1u << 16);
            if (push_pull) {
                GPIOB->OTYPER &= ~(1u << 8);      /* PB8 推挽 */
            }
            uint32_t n = 0u;
            /* 2026-10-14 H2 真机修订：原版「SDA 回高就 break」在「SCL 被钳、
             * SDA 一直高」场景 0 个时钟就退出（时钟根本没打进器件）。
             * 标准解卡就该打满 9 个——SDA 高时多打无害，SCL 钳位场景必须
             * 打满（PP 档推挽能把时钟真送进去）。 */
            for (n = 0u; n < 9u; n++) {
                GPIOB->BSRR = (1u << 8);          /* SCL 高（开漏：置位=释放） */
                udelay(5u);
                GPIOB->BSRR = (1u << (8 + 16u));  /* SCL 低 */
                udelay(5u);
            }
            /* STOP 时序：SCL 高电平期间 SDA 从低到高 */
            GPIOB->BSRR      = (1u << (8 + 16u)); /* SCL 低 */
            GPIOB->MODER     = (GPIOB->MODER & ~(3u << 18)) | (1u << 18);
            GPIOB->BSRR      = (1u << (9 + 16u)); /* SDA 低 */
            udelay(5u);
            GPIOB->BSRR      = (1u << 8);         /* SCL 高 */
            /* 背充实验（2026-10-14 H2）：PP 档可加 hold_us——SCL 推挽保持
             * 高电平，往欠压的器件 VCC 轨（经 ESD 二极管）灌电；若器件
             * VCC 虚接全靠上拉背充活着，灌几百 ms 后器件输入级起振、
             * 总线钳位消失。仅 PP 档有意义（开漏顶不动钳位）。 */
            uint32_t hold_us = 5u;
            if (push_pull && argc >= 4) {
                (void)parse_num(argv[3], &hold_us);
                if (hold_us > 500000u) {
                    hold_us = 500000u;            /* 上限 0.5s，防长时间灌流 */
                }
            }
            udelay(hold_us);
            GPIOB->BSRR      = (1u << 9);         /* SDA 释放→高 = STOP */
            udelay(5u);
            /* 恢复 AF **之前**读电平：推挽模式下若 SCL 能顶到高（vs 外部强拉），
             * 只有此刻能看出来——恢复 AF（开漏+上拉）后线会被重新拉低，看不出差别 */
            uint32_t scl_driven = (GPIOB->IDR >> 8) & 1u;
            GPIOB->MODER = moder;                 /* 原样恢复（PB8/PB9 AF4） */
            GPIOB->OTYPER = otyper;
            out_reset();
            out_add_u(n);
            out_add_str(" sda_now=");
            out_add_u((GPIOB->IDR >> 9) & 1u);
            out_add_str(" scl_released=");
            /* 1=推挽/上拉能拉回高（弱拉低：器件钳位/欠压），
             * 0=外部仍然强拉（硬短路到地） */
            out_add_u(scl_driven);
            reply_ok("UNSTICK");
            return;
        }

        /* XBANG：交叉探测（2026-10-14 H2 真机需求）。硬件 I2C 外设的 SCL/SDA
         * 角色是 PB8/PB9 固定的——若实际接线把两根线接反，地址位全错位，
         * SCAN 永远 NAK 且无从分辨。这里用 GPIO 位摔在两个极性下各扫一遍：
         *   极性 A：PB8=SCL  PB9=SDA（与硬件外设一致）
         *   极性 B：PB9=SCL  PB8=SDA（交叉）
         * 只发 START+addr(RD)+ACK 采样+STOP，不收数据，开漏+内部上拉，
         * 对从机无破坏。判读：
         *   A 有 ACK / B 全 NAK → 接线正，器件地址见 a=
         *   B 有 ACK / A 全 NAK → SDA/SCL 接反实锤，换线即可
         *   两极性全 NAK        → 器件不在位/未上电/非标准应答 */
        if (strcmp_local(argv[1], "XBANG") == 0u) {
            /* 开漏位摔探测：scl/sda 为 BSRR 位掩码。返回 1=ACK 0=NAK -1=死 */
            uint32_t hits_a = 0u, hits_b = 0u;
            uint32_t skip   = 0u;                  /* 线低作废的探测数 */
            uint8_t  la[112], lb[112];

            /* 两线切开漏输出（AFR/OTYPER/PUPDR 不动，上拉保持生效） */
            uint32_t moder_sv = GPIOB->MODER;
            GPIOB->MODER = (moder_sv & ~((3u << 16) | (3u << 18)))
                           | ((1u << 16) | (1u << 18));
            GPIOB->BSRR = (1u << 8) | (1u << 9);   /* 先双释放 */
            udelay(5u);

            for (uint32_t a = 8u; a <= 0x77u; a++) {
                for (uint32_t pol = 0u; pol < 2u; pol++) {
                    uint32_t m_scl = (pol == 0u) ? (1u << 8) : (1u << 9);
                    uint32_t m_sda = (pol == 0u) ? (1u << 9) : (1u << 8);
                    int      ack   = 0;
                    /* 总线任一线被外部拉低 → 本极性作废（计入 skip，
                     * 全 NAK 与「根本没探到」两种结果必须可区分） */
                    if (((GPIOB->IDR & (m_scl | m_sda))
                         != (m_scl | m_sda))) {
                        skip++;
                        continue;
                    }
                    /* START：SDA 低 → SCL 低 */
                    GPIOB->BSRR = m_sda;
                    udelay(5u);
                    GPIOB->BSRR = m_scl;
                    udelay(5u);
                    /* 8 bit：MSB 先，SCL 低放数据、SCL 高采样 */
                    for (int32_t i = 7; i >= 0; i--) {
                        if ((a >> i) & 1u) {
                            GPIOB->BSRR = m_sda;       /* 1 = 释放 */
                        } else {
                            GPIOB->BSRR = m_sda << 16; /* 0 = 拉低 */
                        }
                        udelay(4u);
                        GPIOB->BSRR = m_scl;           /* SCL 高 */
                        udelay(5u);
                        GPIOB->BSRR = m_scl << 16;     /* SCL 低 */
                        udelay(4u);
                    }
                    /* 第 9 位：释放 SDA 读 ACK（0=从机应答） */
                    GPIOB->BSRR = m_sda;
                    udelay(4u);
                    GPIOB->BSRR = m_scl;
                    udelay(5u);
                    ack = ((GPIOB->IDR & m_sda) == 0u);
                    GPIOB->BSRR = m_scl << 16;
                    udelay(4u);
                    /* STOP：SDA 低 → SCL 高 → SDA 高 */
                    GPIOB->BSRR = m_sda << 16;
                    udelay(4u);
                    GPIOB->BSRR = m_scl;
                    udelay(5u);
                    GPIOB->BSRR = m_sda;
                    udelay(5u);
                    if (pol == 0u) {
                        if (ack) {
                            la[hits_a++] = (uint8_t)a;
                        }
                    } else {
                        if (ack) {
                            lb[hits_b++] = (uint8_t)a;
                        }
                    }
                }
            }
            GPIOB->MODER = moder_sv;                   /* 恢复 AF4 */
            /* I2C1 软复位：位摔期间外设与引脚断开，防 BUSY 卡死殃及 SCAN */
            I2C1->CR1 &= ~X_I2C_PE;
            udelay(20u);
            I2C1->TIMINGR = (3u << 28) | (4u << 20) | (0u << 16)
                            | (74u << 8) | 74u;
            I2C1->CR1 |= X_I2C_PE;

            out_reset();
            {
                /* 恢复 AF 后实测电平：区分「SCL 低=外设自锁」vs「外部钳位」——
                 * 外设刚软复位还 SCL 低 = 外部真有东西在拉（欠压钳位特征） */
                uint32_t v = GPIOB->IDR;
                char     hx[5];
                hx[0] = HEXC[(v >> 12) & 0xFu];
                hx[1] = HEXC[(v >> 8) & 0xFu];
                hx[2] = HEXC[(v >> 4) & 0xFu];
                hx[3] = HEXC[v & 0xFu];
                hx[4] = '\0';
                out_add_str("idr=0x");
                out_add_str(hx);
                out_add_str(" ");
            }
            out_add_str("nA=");
            out_add_u(hits_a);
            if (hits_a != 0u) {
                out_add_str(" a=");
                for (uint32_t i = 0u; i < hits_a; i++) {
                    if (i != 0u) {
                        out_add_str(",");
                    }
                    out_add_hex8(la[i]);
                }
            }
            out_add_str(" nB=");
            out_add_u(hits_b);
            if (hits_b != 0u) {
                out_add_str(" b=");
                for (uint32_t i = 0u; i < hits_b; i++) {
                    if (i != 0u) {
                        out_add_str(",");
                    }
                    out_add_hex8(lb[i]);
                }
            }
            out_add_str(" skip=");
            out_add_u(skip);
            if (skip != 0u) {
                out_add_str(" (wire low, result partial)");
            }
            reply_ok("I2C XBANG");
            return;
        }

        if (strcmp_local(argv[1], "SCAN") == 0) {
            uint32_t lo = 8u, hi = 119u;
            if (argc >= 4) {
                (void)parse_num(argv[3], &lo);
            }
            if (argc >= 5) {
                (void)parse_num(argv[4], &hi);
            }
            if (lo > 0x7Fu || hi > 0x7Fu || lo > hi) {
                reply_err("ARG", "bad scan range");
                return;
            }
            uint8_t hit[128];
            uint32_t n = 0u;
            int      stuck = 0;
            for (uint32_t a = lo; a <= hi; a++) {
                int r = i2c_probe((uint8_t)a);
                if (r < 0) {
                    stuck = 1;
                    break;
                }
                if (r > 0) {
                    hit[n++] = (uint8_t)a;
                }
            }
            if (stuck) {
                /* 外设视角 BUSY 与 GPIO 视角线电平可能矛盾（欠压钳位时
                 * GPIO 读高但外设起不来）——两个视角一起报，别只信一个。 */
                uint32_t idr    = GPIOB->IDR;
                unsigned sda_lv = (idr >> 9) & 1u;
                unsigned scl_lv = (idr >> 8) & 1u;
                uart_puts("-ERR TIMEOUT i2c busy (gpio sda=");
                uart_putc('0' + (char)sda_lv);
                uart_puts(" scl=");
                uart_putc('0' + (char)scl_lv);
                uart_puts(")\r\n");
                return;
            }
            out_reset();
            out_add_str("bus=0 lo=");
            out_add_u(lo);
            out_add_str(" hi=");
            out_add_u(hi);
            out_add_str(" n=");
            out_add_u(n);
            if (n != 0u) {
                out_add_str(" addrs=");
                for (uint32_t i = 0u; i < n; i++) {
                    if (i != 0u) {
                        out_add_str(",");
                    }
                    out_add_hex8(hit[i]);
                }
            }
            reply_ok("I2C SCAN");
            return;
        }

        if (strcmp_local(argv[1], "READ") == 0) {
            uint32_t addr, reg, cnt;
            if (argc < 6 || !parse_num(argv[3], &addr) ||
                !parse_num(argv[4], &reg) || !parse_num(argv[5], &cnt) ||
                addr > 0x7Fu || reg > 0xFFu || cnt < 1u || cnt > 256u) {
                reply_err("ARG", "usage: I2C READ <bus> <addr> <reg> <n>");
                return;
            }
            uint8_t data[256];
            int     rc = i2c_read_regs((uint8_t)addr, (uint8_t)reg, data, cnt);
            if (rc == -1) {
                reply_err("TIMEOUT", "bus busy");
            } else if (rc == -2) {
                reply_err("NAK", "no ack from device");
            } else {
                out_reset();
                out_add_str("bus=0 addr=");
                out_add_hex8(addr);
                out_add_str(" reg=");
                out_add_hex8(reg);
                out_add_str(" n=");
                out_add_u(cnt);
                out_add_str(" data=");
                for (uint32_t i = 0u; i < cnt; i++) {
                    if (i != 0u) {
                        out_add_str(",");
                    }
                    out_add_hex8(data[i]);
                }
                reply_ok("I2C READ");
            }
            return;
        }

        if (strcmp_local(argv[1], "WRITE") == 0) {
            uint32_t addr, reg;
            if (argc < 6 || !parse_num(argv[3], &addr) ||
                !parse_num(argv[4], &reg) || addr > 0x7Fu || reg > 0xFFu) {
                reply_err("ARG", "usage: I2C WRITE <bus> <addr> <reg> <b0[,b1..]>");
                return;
            }
            uint8_t vals[256];
            int     n = parse_hex_list(argv[5], vals, 256u);
            if (n <= 0) {
                reply_err("ARG", "bad byte list");
                return;
            }
            int rc = i2c_write_regs((uint8_t)addr, (uint8_t)reg, vals, (uint32_t)n);
            if (rc == -1) {
                reply_err("TIMEOUT", "bus busy");
            } else if (rc == -2) {
                reply_err("NAK", "no ack from device");
            } else {
                out_reset();
                out_add_str("bus=0 addr=");
                out_add_hex8(addr);
                out_add_str(" reg=");
                out_add_hex8(reg);
                out_add_str(" n=");
                out_add_u((uint32_t)n);
                reply_ok("I2C WRITE");
            }
            return;
        }

        reply_err("ARG", "unknown i2c subcommand");
        return;
    }

    /* ---------- SPI ---------- */
    if (strcmp_local(argv[0], "SPI") == 0) {
        if (argc < 2) {
            reply_err("ARG", "spi needs SCAN/READ/WRITE");
            return;
        }
        str_up(argv[1]);
        uint32_t bus = 0u;
        if (argc >= 3) {
            (void)parse_num(argv[2], &bus);
        }
        if (bus != 0u) {
            reply_err("NOBUS", "only bus 0 is fitted");
            return;
        }

        if (strcmp_local(argv[1], "SCAN") == 0) {
            uint32_t ids[2];
            for (int cs = 0; cs < 2; cs++) {
                spi_cs(cs, 1);                  /* 选中 */
                spi_begin(4u);                  /* 1 命令 + 3 数据 = 4 帧 */
                (void)spi_xfer8(0x9Fu);
                uint32_t b0 = spi_xfer8(0x00u);
                uint32_t b1 = spi_xfer8(0x00u);
                uint32_t b2 = spi_xfer8(0x00u);
                spi_end();
                spi_cs(cs, 0);                  /* 释放 */
                ids[cs] = ((b0 << 16) | (b1 << 8) | b2) & 0xFFFFFFu;
            }
            out_reset();
            out_add_str("bus=0 n=2 slots=0:");
            out_add_hex24(ids[0]);
            out_add_str(",1:");
            out_add_hex24(ids[1]);
            reply_ok("SPI SCAN");
            return;
        }

        if (strcmp_local(argv[1], "READ") == 0) {
            uint32_t cs, cmd, cnt;
            if (argc < 6 || !parse_num(argv[3], &cs) ||
                !parse_num(argv[4], &cmd) || !parse_num(argv[5], &cnt) ||
                cs > 1u || cmd > 0xFFu || cnt < 1u || cnt > 256u) {
                reply_err("ARG", "usage: SPI READ <bus> <cs> <cmd> <n>");
                return;
            }
            if (cs > 1u) {
                reply_err("ARG", "cs must be 0 or 1");
                return;
            }
            uint8_t data[256];
            spi_cs((int)cs, 1);
            spi_begin(1u + cnt);                /* 1 命令 + n 数据 */
            (void)spi_xfer8((uint8_t)cmd);
            for (uint32_t i = 0u; i < cnt; i++) {
                data[i] = spi_xfer8(0x00u);
            }
            spi_end();
            spi_cs((int)cs, 0);
            out_reset();
            out_add_str("bus=0 cs=");
            out_add_u(cs);
            out_add_str(" cmd=");
            out_add_hex8(cmd);
            out_add_str(" n=");
            out_add_u(cnt);
            out_add_str(" data=");
            for (uint32_t i = 0u; i < cnt; i++) {
                if (i != 0u) {
                    out_add_str(",");
                }
                out_add_hex8(data[i]);
            }
            reply_ok("SPI READ");
            return;
        }

        if (strcmp_local(argv[1], "WRITE") == 0) {
            uint32_t cs, cmd;
            if (argc < 6 || !parse_num(argv[3], &cs) ||
                !parse_num(argv[4], &cmd) || cs > 1u || cmd > 0xFFu) {
                reply_err("ARG", "usage: SPI WRITE <bus> <cs> <cmd> <b0[,b1..]>");
                return;
            }
            if (cs > 1u) {
                reply_err("ARG", "cs must be 0 or 1");
                return;
            }
            uint8_t vals[256];
            int     n = parse_hex_list(argv[5], vals, 256u);
            if (n <= 0) {
                reply_err("ARG", "bad byte list");
                return;
            }
            spi_cs((int)cs, 1);
            spi_begin(1u + (uint32_t)n);        /* 1 命令 + n 数据 */
            (void)spi_xfer8((uint8_t)cmd);
            for (int i = 0; i < n; i++) {
                (void)spi_xfer8(vals[i]);
            }
            spi_end();
            spi_cs((int)cs, 0);
            out_reset();
            out_add_str("bus=0 cs=");
            out_add_u(cs);
            out_add_str(" n=");
            out_add_u((uint32_t)n);
            reply_ok("SPI WRITE");
            return;
        }

        reply_err("ARG", "unknown spi subcommand");
        return;
    }

    /* ---------- OW ---------- */
    if (strcmp_local(argv[0], "OW") == 0) {
        if (argc < 2) {
            reply_err("ARG", "ow needs SCAN");
            return;
        }
        str_up(argv[1]);
        uint32_t bus = 0u;
        if (argc >= 3) {
            (void)parse_num(argv[2], &bus);
        }
        if (bus != 0u) {
            reply_err("NOBUS", "only bus 0 is fitted");
            return;
        }
        if (strcmp_local(argv[1], "SCAN") != 0) {
            reply_err("ARG", "unknown ow subcommand");
            return;
        }
        uint64_t roms[OW_MAX_DEVS];
        int      n = ow_search(roms, OW_MAX_DEVS);
        out_reset();
        out_add_str("bus=0 n=");
        out_add_u((uint32_t)n);
        if (n > 0) {
            out_add_str(" roms=");
            for (int i = 0; i < n; i++) {
                if (i != 0) {
                    out_add_str(",");
                }
                /* 线序打印：家族码在最前（LSB first，与 PC 侧 rom[:2]=家族码 对齐） */
                for (int b = 0; b < 8; b++) {
                    char hx[3];
                    uint8_t v = (uint8_t)((roms[i] >> (8 * b)) & 0xFFu);
                    hx[0] = HEXC[v >> 4];
                    hx[1] = HEXC[v & 0xFu];
                    hx[2] = '\0';
                    out_add_str(hx);
                }
            }
        }
        reply_ok("OW SCAN");
        return;
    }

    /* ---------- RS485 ---------- */
    if (strcmp_local(argv[0], "RS485") == 0) {
        /* 本板没收发器 —— 如实拒绝。装了 MAX485 的话在这里补 DE 时序即可 */
        reply_err("NOBUS", "rs485 transceiver not fitted on this bridge");
        return;
    }

    /* ---------- GPIO：读引脚电平（总线诊断；IDR 与模式无关，总反映实际电平） ---------- */
    if (strcmp_local(argv[0], "GPIO") == 0u) {
        if (argc < 3 || strcmp_local(argv[1], "READ") != 0u) {
            reply_err("ARG", "usage: GPIO READ <A|B|C|D|E>");
            return;
        }
        GPIO_TypeDef *g = (GPIO_TypeDef *)0;
        switch (argv[2][0]) {
        case 'A': case 'a': g = GPIOA; break;
        case 'B': case 'b': g = GPIOB; break;
        case 'C': case 'c': g = GPIOC; break;
        case 'D': case 'd': g = GPIOD; break;
        case 'E': case 'e': g = GPIOE; break;
        default:
            reply_err("ARG", "port must be A..E");
            return;
        }
        /* 修复：回复头原写死 "GPIO B"——读 A/C/D/E 时头也是 B，误导诊断 */
        {
            char     head[12];
            uint32_t v = g->IDR;
            char     hx[5];
            head[0] = 'G'; head[1] = 'P'; head[2] = 'I'; head[3] = 'O';
            head[4] = ' '; head[5] = argv[2][0]; head[6] = '\0';
            if (head[5] >= 'a' && head[5] <= 'z') {
                head[5] = (char)(head[5] - 'a' + 'A');
            }
            hx[0] = HEXC[(v >> 12) & 0xFu];
            hx[1] = HEXC[(v >> 8) & 0xFu];
            hx[2] = HEXC[(v >> 4) & 0xFu];
            hx[3] = HEXC[v & 0xFu];
            hx[4] = '\0';
            out_reset();
            out_add_str("IDR=0x");
            out_add_str(hx);
            reply_ok(head);
        }
        return;
    }

    reply_err("ARG", "unknown command");
}

/* ---------------------------------------------------------------
 * 初始化 + 主循环
 * --------------------------------------------------------------- */
void Default_Handler(void)
{
    for (;;) {
    }
}

void Reset_Handler(void)
{
    /* 0) 裸机启动代码：拷 .data 初值 + 清零 .bss（没有 C 启动库，必须自己干；
     *    必须先于一切访问全局变量的代码）。★ 漏掉这条 = line_len 初值 0xFFFFFFFF，
     *    所有命令一律报 line too long（2026-09-22 真机踩过，见 linker.ld 注释）。 */
    {
        extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss;
        uint32_t *src = &_sidata, *dst = &_sdata;
        while (dst < &_edata) {
            *dst++ = *src++;
        }
        dst = &_sbss;
        while (dst < &_ebss) {
            *dst++ = 0u;
        }
    }

    /* 1) 时钟：HSI 64MHz（HSIDIV 复位值不是 /1，实机踩过 —— 与探针固件同款处理） */
    RCC->CR &= ~RCC_CR_HSIDIV;
    while ((RCC->CR & RCC_CR_HSIRDY) == 0u) {
    }
    delay(20000u);

    /* SysTick 自由跑：HCLK/8，给 udelay/超时用 */
    SysTick->LOAD = 0x00FFFFFFu;
    SysTick->VAL  = 0u;
    SysTick->CTRL = SysTick_CTRL_ENABLE_Msk;

    /* 2) 时钟门控：GPIO 在 AHB2；USART3/I2C1 在 APB1L；SPI1 在 APB2 */
    RCC->AHB2ENR  |= (RCC_AHB2ENR_GPIOAEN | RCC_AHB2ENR_GPIOBEN |
                      RCC_AHB2ENR_GPIOCEN | RCC_AHB2ENR_GPIODEN);
    RCC->APB1LENR |= (RCC_APB1LENR_USART3EN | RCC_APB1LENR_I2C1EN);
    RCC->APB2ENR  |= RCC_APB2ENR_SPI1EN;
    (void)RCC->APB2ENR;

    /* 3) USART3：PD8/PD9 AF7，115200 8N1（与探针固件同款，已在本板验证） */
    GPIOD->MODER = (GPIOD->MODER & ~((3u << 16) | (3u << 18)))
                   | ((2u << 16) | (2u << 18));
    GPIOD->AFR[1] = (GPIOD->AFR[1] & ~((0xFu << 0) | (0xFu << 4)))
                    | ((7u << 0) | (7u << 4));
    GPIOD->OSPEEDR |= (3u << 16) | (3u << 18);
    GPIOD->PUPDR = (GPIOD->PUPDR & ~(3u << 18)) | (1u << 18);
    USART3->CR1   = 0u;
    USART3->CR2   = 0u;
    USART3->CR3   = 0u;
    USART3->PRESC = 0u;
    USART3->BRR   = 0x22Cu;                    /* 64e6/(16*115200) = 34.72 */
    USART3->CR1   = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

    /* 4) I2C1：PB8=SCL / PB9=SDA，AF4，开漏 + 上拉，~100kHz
     *    TIMINGR：PRESC=3 → tpresc=62.5ns；SCLH=SCLL=74 → 75×62.5≈4.69µs/半周期
     *    ⟹ ≈106kHz（100k 档容差内）；SCLDEL=4 → 数据建立 312ns ≥ 250ns（标准档） */
    GPIOB->MODER = (GPIOB->MODER & ~((3u << 16) | (3u << 18)))
                   | ((2u << 16) | (2u << 18));
    GPIOB->AFR[1] = (GPIOB->AFR[1] & ~((0xFu << 0) | (0xFu << 4)))
                    | ((4u << 0) | (4u << 4));
    GPIOB->OTYPER  |= (1u << 8) | (1u << 9);
    GPIOB->PUPDR   = (GPIOB->PUPDR & ~((3u << 16) | (3u << 18)))
                     | ((1u << 16) | (1u << 18));
    GPIOB->OSPEEDR |= (3u << 16) | (3u << 18);
    I2C1->CR1 &= ~X_I2C_PE;
    I2C1->TIMINGR = (3u << 28) | (4u << 20) | (0u << 16) | (74u << 8) | 74u;
    I2C1->CR1 |= X_I2C_PE;

    /* 5) SPI1：PA5/6/7 AF5；CS0=PA4、CS1=PB0 推挽输出，先置空闲高
     *    H5 事务式 SPI：CFG1 定波特率/帧宽，CFG2 定主机/SSM，CR2.TSIZE 每笔配 */
    GPIOA->MODER = (GPIOA->MODER & ~((3u << 8) | (3u << 10) | (3u << 12)
                                     | (3u << 14)))
                   | ((1u << 8) | (2u << 10) | (2u << 12) | (2u << 14));
    GPIOA->AFR[0] = (GPIOA->AFR[0] & ~((0xFu << 20) | (0xFu << 24) | (0xFu << 28)))
                    | ((5u << 20) | (5u << 24) | (5u << 28));
    GPIOA->OSPEEDR |= (3u << 8) | (3u << 10) | (3u << 12) | (3u << 14);
    GPIOB->MODER = (GPIOB->MODER & ~(3u << 0)) | (1u << 0);
    GPIOB->OSPEEDR |= (3u << 0);
    GPIOA->BSRR = (1u << 4);                    /* CS0 置高（释放） */
    GPIOB->BSRR = (1u << 0);                    /* CS1 置高（释放） */
    SPI1->CR1  = 0u;
    SPI1->CFG1 = X_SPI_MBR_F256 | X_SPI_DSIZE8;
    SPI1->CFG2 = X_SPI_MASTER | X_SPI_SSM;
    SPI1->CR1  = X_SPI_SSI;                     /* SSM=1 时内部 SS 电平=SSI，置 1 */

    /* 6) 1-Wire：PC7 开漏 + 内部上拉（真总线请外接 4.7k，PC 侧输出里会提示） */
    GPIOC->MODER = (GPIOC->MODER & ~(3u << 14)) | (1u << 14);
    GPIOC->OTYPER |= (1u << 7);
    GPIOC->PUPDR = (GPIOC->PUPDR & ~(3u << 14)) | (1u << 14);
    GPIOC->OSPEEDR |= (3u << 14);
    OW_HIGH();

    /* 7) 起手横幅（# 开头 = 异步日志，PC 侧协议解析会忽略） */
    uart_puts("#LOG mcu-debug bridge MCUDBG/1 ready (h563zi)\r\n");

    /* 8) 命令主循环：行缓冲 + 同步应答 */
    line_len = 0u;
    for (;;) {
        if ((USART3->ISR & X_UART_RXNE) != 0u) {
            uint8_t c = (uint8_t)USART3->RDR;
            if ((USART3->ISR & X_UART_ORE) != 0u) {
                USART3->ICR = X_UART_ICR_ORECF;  /* 溢出：清掉，别卡死 */
            }
            if (c == '\r' || c == '\n') {
                if (line_len > 0u) {
                    line_buf[line_len] = '\0';
                    handle_line(line_buf);
                    line_len = 0u;
                }
            } else if (line_len < (sizeof(line_buf) - 1u)) {
                line_buf[line_len++] = (char)c;
            } else {
                line_len = 0u;                   /* 超长行：整行丢弃 */
                reply_err("ARG", "line too long");
            }
        }
    }
}

/* 向量表放在 Flash 最前面（0x08000000），第 0 项是初始 MSP */
extern uint32_t _estack;

__attribute__((section(".isr_vector"), used))
const uint32_t g_vectors[16] = {
    (uint32_t)&_estack,          /* 0x00 初始主栈指针 */
    (uint32_t)Reset_Handler,     /* 0x04 复位 */
    (uint32_t)Default_Handler,   /* NMI      */
    (uint32_t)Default_Handler,   /* HardFault*/
    (uint32_t)Default_Handler,   /* MemManage*/
    (uint32_t)Default_Handler,   /* BusFault */
    (uint32_t)Default_Handler,   /* UsageFault*/
    0u, 0u, 0u, 0u,              /* 保留 */
    (uint32_t)Default_Handler,   /* SVCall   */
    (uint32_t)Default_Handler,   /* DebugMon */
    0u,                          /* 保留 */
    (uint32_t)Default_Handler    /* PendSV   */
};
