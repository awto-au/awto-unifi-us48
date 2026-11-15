@ a2 self-test firmware entry: I-cache on (SCTLR.I), stack at the carveout end, then main().
@ Entered by us48_a2 (remoteproc start) with the MMU off and caches in reset state.
@ Probe variants (#79 H1): ENTRY_PROBE records CP15 state, ENTRY_FULL invalidates all before enabling, ENTRY_Z0/Z1 force SCTLR.Z.
        .arm
        .section .text.start, "ax"
        .globl  _start
_start:
        cpsid   if
#ifdef ENTRY_NOPS
        .rept   ENTRY_NOPS                @ code-layout shift (#79): moves everything after by 4 B per nop
        nop
        .endr
#endif
#ifdef ENTRY_PROBE
        ldr     r5, =entry_regs           @ SCTLR, ACTLR, Diagnostic, MIDR as found at reset (#79 H1)
        mrc     p15, 0, r4, c1, c0, 0
        str     r4, [r5, #0]
        mrc     p15, 0, r4, c1, c0, 1
        str     r4, [r5, #4]
        mrc     p15, 0, r4, c15, c0, 1
        str     r4, [r5, #8]
        mrc     p15, 0, r4, c0, c0, 0
        str     r4, [r5, #12]
#endif
#ifdef ENTRY_FULL
        @ H1: invalidate everything with the MMU and caches off, before any enable
        mrc     p15, 0, r0, c1, c0, 0
        bic     r0, r0, #0x1000
        bic     r0, r0, #0x5
        mcr     p15, 0, r0, c1, c0, 0
        isb
        mov     r1, #0
        mcr     p15, 0, r1, c7, c5, 0     @ ICIALLU
        mcr     p15, 0, r1, c7, c5, 6     @ BPIALL
        mcr     p15, 0, r1, c8, c7, 0     @ TLBIALL
        dsb
        isb
        mov     r0, #0
        mcr     p15, 2, r0, c0, c0, 0     @ CSSELR = L1 D
        isb
        mrc     p15, 1, r0, c0, c0, 0     @ CCSIDR
        movw    r1, #0x7fff
        and     r2, r1, r0, lsr #13
        movw    r1, #0x3ff
        and     r3, r1, r0, lsr #3
        add     r2, r2, #1
        and     r0, r0, #7
        add     r0, r0, #4
        clz     r1, r3
        add     r4, r3, #1
1:      sub     r2, r2, #1
        mov     r3, r4
2:      subs    r3, r3, #1
        mov     r5, r3, lsl r1
        mov     r6, r2, lsl r0
        orr     r5, r5, r6
        mcr     p15, 0, r5, c7, c6, 2     @ DCISW
        bgt     2b
        cmp     r2, #0
        bgt     1b
        dsb
        isb
#endif
        mov     r0, #0
        mcr     p15, 0, r0, c7, c5, 0     @ ICIALLU
        mcr     p15, 0, r0, c7, c5, 6     @ BPIALL
        mrc     p15, 0, r0, c1, c0, 0
#ifdef ENTRY_Z0
        bic     r0, r0, #0x800            @ SCTLR.Z: branch prediction off
#endif
#ifdef ENTRY_Z1
        orr     r0, r0, #0x800            @ SCTLR.Z: branch prediction on
#endif
#ifndef NO_ICACHE
        orr     r0, r0, #0x1000           @ SCTLR.I
#endif
        mcr     p15, 0, r0, c1, c0, 0
        isb
        ldr     sp, =__stack_top
        bl      main
1:      b       1b
        .ltorg
#ifdef ENTRY_PROBE
        .section .bss
        .globl  entry_regs
entry_regs:
        .space  16
#endif
