/* fault_demo.c —— 人为触发 HardFault，把故障现场留在固定 RAM 供 SWD 抓取
 *
 * 为什么这么写：
 *  1. 故障现场结构固定在 0x20000000（链接脚本把 RAM 起始让出 0x40 字节），
 *     用一个**常量指针**访问，编译器不会把它当变量优化掉。
 *  2. MSP/PSP/EXC_RETURN 由汇编侧在进 C 之前写入（见 startup.s），
 *     C 侧只负责补齐 CFSR/HFSR/BFAR/MMFAR 与压栈帧内容。
 *  3. 三层调用链（app_state -> imu_task -> mpu6050_read）刻意 noinline，
 *     保证栈回溯能真的反查出三个函数名，而不是全被内联成 main。
 */
#include <stdint.h>

/* ---- 故障现场（固定地址 0x20000000）---- */
typedef struct {
    uint32_t msp;        /* +0  汇编写 */
    uint32_t psp;        /* +4  汇编写 */
    uint32_t exc_return; /* +8  汇编写 */
    uint32_t magic;      /* +12 汇编写 0x5A5A5A5A */

    uint32_t cfsr;       /* +16 C 侧补齐 */
    uint32_t hfsr;       /* +20 */
    uint32_t bfar;       /* +24 */
    uint32_t mmfar;      /* +28 */
    uint32_t shcsr;      /* +32 */
    uint32_t stack_used; /* +36 真正承载压栈帧的那个栈 */
    uint32_t r[8];       /* +40 R0,R1,R2,R3,R12,LR,PC,xPSR */
} fault_info_t;

#define FI ((volatile fault_info_t *)0x20000000u)

/* ---- SCB 寄存器（不依赖 CMSIS 头，直接地址）---- */
#define SCB_SHCSR (*(volatile uint32_t *)0xE000ED24u)
#define SCB_CFSR  (*(volatile uint32_t *)0xE000ED28u)
#define SCB_HFSR  (*(volatile uint32_t *)0xE000ED2Cu)
#define SCB_MMFAR (*(volatile uint32_t *)0xE000ED34u)
#define SCB_BFAR  (*(volatile uint32_t *)0xE000ED38u)

/* ---- 供栈回溯反查的符号（大小刻意留够，方便 symbol_lookup 认出来）---- */
volatile uint32_t g_sink;          /* 承接那次非法读，别被优化掉 */
volatile uint8_t  raw_buf[1200];   /* 一个够大的缓冲区，离线夹具里也用它 */

__attribute__((noinline)) static void mpu6050_read(void)
{
    /* 访问一个未映射地址：精确总线错误 → BusFault 升级为 HardFault
     * BFARVALID=1，BFAR 记录的就是 0xFFFFFFF0 */
    volatile uint32_t *bad = (volatile uint32_t *)0xFFFFFFF0u;
    g_sink = *bad;
}

__attribute__((noinline)) static void imu_task(void)
{
    mpu6050_read();
}

__attribute__((noinline)) static void app_state(void)
{
    imu_task();
}

/* 由 startup.s 的 HardFault_Handler 用 `b` 跳进来，不返回 */
void HardFault_C(void);

void HardFault_C(void)
{
    /* EXC_RETURN bit2 = 0 表示压栈用的是 MSP，= 1 表示 PSP */
    uint32_t stk = (FI->exc_return & 0x4u) ? FI->psp : FI->msp;

    FI->cfsr  = SCB_CFSR;
    FI->hfsr  = SCB_HFSR;
    FI->mmfar = SCB_MMFAR;
    FI->bfar  = SCB_BFAR;
    FI->shcsr = SCB_SHCSR;
    FI->stack_used = stk;

    for (uint32_t i = 0; i < 8u; i++) {
        FI->r[i] = ((volatile uint32_t *)stk)[i];
    }

    for (;;) { }   /* 停在这里：SWD 用 hotplug 连上，不打扰现场 */
}

int main(void)
{
    for (uint32_t i = 0; i < sizeof(raw_buf); i++) {
        raw_buf[i] = (uint8_t)i;
    }
    g_sink = 0;

    app_state();   /* 这一趟必炸 */

    for (;;) { }   /* 正常情况到不了这里 */
}
