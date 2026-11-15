ENTRY(_start)
SECTIONS {
  . = 0x01000000;
  .text : { *(.text.start) *(.text*) *(.rodata*) }
  .data : { *(.data*) }
  .bss : { *(.bss*) *(COMMON) }
}
