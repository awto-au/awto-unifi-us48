@ a2 timing payload: r0 = loop count; returns elapsed A9 global-timer ticks (periph clock). Position independent.
@ Variant with -DICACHE enables the L1 I-cache first (SCTLR.I) and invalidates it.
        .arm
        .text
        .globl  _start
_start:
#ifdef ICACHE
        mov     r1, #0
        mcr     p15, 0, r1, c7, c5, 0     @ ICIALLU
        mrc     p15, 0, r1, c1, c0, 0
        orr     r1, r1, #0x1000           @ SCTLR.I
        mcr     p15, 0, r1, c1, c0, 0
        isb
#endif
        ldr     r2, =0x19020200           @ global timer counter (low)
        ldr     r3, [r2]
1:      subs    r0, r0, #1
        bne     1b
        ldr     r0, [r2]
        sub     r0, r0, r3
        bx      lr
        .ltorg
