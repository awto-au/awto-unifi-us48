@ a2 test program, runs from unit-1 SRAM at 0x1b000000: writes a marker, then counts forever
        .arm
        .text
        .globl _start
_start:
        ldr     r0, =0x1b000100
        ldr     r1, =0xa2a2a2a2
        str     r1, [r0]
loop:   ldr     r2, [r0, #4]
        add     r2, r2, #1
        str     r2, [r0, #4]
        b       loop
        .ltorg
