/* NUCLEO-H563ZI / STM32H563ZI 最小链接脚本（与探针/故障演示固件同款）
 * Flash 0x08000000 + 2MB    RAM 0x20000000 + 640KB
 * （RAM 顶端 0x200A0000 与出厂固件的初始 SP 一致，已用 SWD 实测核对）
 */
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 2048K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 640K
}

/* 栈顶：RAM 末端（向下增长） */
_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
  .isr_vector : {
    KEEP(*(.isr_vector))
  } > FLASH

  .text : {
    *(.text*)
    *(.rodata*)
    . = ALIGN(4);
    _etext = .;
  } > FLASH

  /* .data：有初值的全局 —— VMA 在 RAM，LMA 跟在 .text 后，Reset_Handler 启动时拷。
   * ★ 2026-09-22 真机踩坑：不写这两个段，链接器会把 .bss（1848B：line_buf/
   *   line_len 等）当孤儿段塞进 FLASH——变量写在 Flash 上永远写不进去、也没人
   *   清零，line_len 初值=0xFFFFFFFF，任何收到的字节都报 line too long。 */
  .data : AT (_etext) {
    . = ALIGN(4);
    _sdata = .;
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } > RAM
  _sidata = LOADADDR(.data);

  /* .bss：零初始化 —— 启动时由 Reset_Handler 清零；NOLOAD 不占 Flash */
  .bss (NOLOAD) : {
    . = ALIGN(4);
    _sbss = .;
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM

  /DISCARD/ : {
    *(.comment)
    *(.ARM.attributes)
  }
}
