@ a2 self-test firmware entry: I-cache on (SCTLR.I), stack at the carveout end, then main().
@ Entered by us48_a2 (remoteproc start) with the MMU off and caches in reset state.
        .arm
        .section .text.start, "ax"
        .globl  _start
_start:
        cpsid   if
        mov     r0, #0
        mcr     p15, 0, r0, c7, c5, 0     @ ICIALLU
        mcr     p15, 0, r0, c7, c5, 6     @ BPIALL
        mrc     p15, 0, r0, c1, c0, 0
        orr     r0, r0, #0x1000           @ SCTLR.I
        mcr     p15, 0, r0, c1, c0, 0
        isb
        ldr     sp, =__stack_top
        bl      main
1:      b       1b
        .ltorg
