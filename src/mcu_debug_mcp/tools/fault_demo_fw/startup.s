/* startup.s —— STM32H563ZI 最小启动 + HardFault 现场抓取（汇编侧）
 *
 * 存在的理由只有一个：**在调用任何 C 函数之前**把 fault 那一刻的
 * MSP / PSP / EXC_RETURN 存下来。一旦进了 C 函数，prologue 就改了 SP，
 * 那时再读 MSP 拿到的已经是 handler 自己的栈指针，不是 fault 现场的。
 */
    .syntax unified
    .cpu cortex-m33
    .fpu softvfp
    .thumb

    .global __Vectors
    .global Reset_Handler
    .global HardFault_Handler
    .global Default_Handler
    .global HardFault_C

    .section .isr_vector, "a", %progbits
    .align 2
    .type __Vectors, %object
__Vectors:
    .word _estack                  /* +0x00 初始 MSP */
    .word Reset_Handler            /* +0x04 Reset */
    .word Default_Handler          /* +0x08 NMI */
    .word HardFault_Handler        /* +0x0C HardFault  ★ */
    .word Default_Handler          /* +0x10 MemManage */
    .word Default_Handler          /* +0x14 BusFault  */
    .word Default_Handler          /* +0x18 UsageFault */
    .word 0
    .word 0
    .word 0
    .word 0
    .word Default_Handler          /* +0x2C SVCall */
    .word Default_Handler          /* +0x30 DebugMon */
    .word 0
    .word Default_Handler          /* +0x38 PendSV */
    .word Default_Handler          /* +0x3C SysTick */
    .size __Vectors, . - __Vectors

    .text
    .thumb_func
    .type Reset_Handler, %function
Reset_Handler:
    /* .data 从 Flash 搬到 RAM */
    ldr r0, =_sdata
    ldr r1, =_edata
    ldr r2, =_sidata
    b   2f
1:  ldr r3, [r2], #4
    str r3, [r0], #4
2:  cmp r0, r1
    bcc 1b
    /* .bss 清零 */
    ldr r0, =_sbss
    ldr r1, =_ebss
    movs r3, #0
    b   4f
3:  str r3, [r0], #4
4:  cmp r0, r1
    bcc 3b

    bl  main
5:  b   5b
    .size Reset_Handler, . - Reset_Handler

    .thumb_func
    .type HardFault_Handler, %function
HardFault_Handler:
    /* 故障现场结构固定放在 0x20000000：
     *   +0  MSP at fault
     *   +4  PSP at fault
     *   +8  EXC_RETURN (LR)
     *   +12 magic 0x5A5A5A5A
     */
    ldr  r0, =0x20000000
    mrs  r1, msp
    str  r1, [r0, #0]
    mrs  r1, psp
    str  r1, [r0, #4]
    mov  r1, lr
    str  r1, [r0, #8]
    ldr  r1, =0x5A5A5A5A
    str  r1, [r0, #12]
    b    HardFault_C              /* 不返回 */
    .size HardFault_Handler, . - HardFault_Handler

    .thumb_func
    .type Default_Handler, %function
Default_Handler:
    b .
    .size Default_Handler, . - Default_Handler

    .end
