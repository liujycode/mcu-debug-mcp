/* link.ld —— H563ZI 最小链接脚本
 * Flash 0x08000000 / 2MB；RAM 0x20000000 / 640KB
 * 低 0x40 字节留给故障现场结构（固定地址 0x20000000），链接器不许分配那里。
 */
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 2048K
  RAM   (rwx) : ORIGIN = 0x20000040, LENGTH = 640K - 0x40
}

/* 栈顶 = RAM 最高地址；栈向下生长 */
_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
  .isr_vector : {
    KEEP(*(.isr_vector))
  } > FLASH

  .text : {
    *(.text*)
    *(.rodata*)
  } > FLASH

  _sidata = LOADADDR(.data);

  .data : {
    _sdata = .;
    *(.data*)
    _edata = .;
  } > RAM AT > FLASH

  .bss : {
    _sbss = .;
    *(.bss*) *(COMMON)
    _ebss = .;
  } > RAM

  /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
