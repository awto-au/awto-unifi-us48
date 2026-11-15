@ I-fetch / D-fill probe code (#79). r0 = src, r1 = dst, (dloop: r2 = lines); returns r0 = running sum.
@ Per line: 1 load (D fill), 1 add, 1 store (dirty line, later eviction). dloop = small loop, copied to SRAM by iftest.c.
        .arm
        .section .text.dloop, "ax"
        .globl  dloop_start
        .globl  dloop_end
dloop_start:
        mov     r3, #0
1:      ldr     r12, [r0], #32
        add     r3, r3, r12
        str     r3, [r1], #32
        subs    r2, r2, #1
        bne     1b
        mov     r0, r3
        bx      lr
        .rept   32
        nop
        .endr
dloop_end:

@ bigcode: BIG_LINES x 32 B straight-line code (> 32 KiB L1 I), one load/add/store per 32 B line; WRAP_DATA: pointers
@ wrap every 128 lines (4 KiB window, for data in SRAM).
        .section .text.bigcode, "ax"
        .balign 32
        .globl  bigcode
bigcode:
        mov     r3, #0
        nop
        nop
        nop
        nop
        nop
        nop
        nop
        .set    n, 0
        .rept   BIG_LINES
        ldr     r12, [r0], #32
        add     r3, r3, r12
        str     r3, [r1], #32
        .if     (n & 127) == 127 && WRAP_DATA
        sub     r0, r0, #4096
        sub     r1, r1, #4096
        nop
        nop
        nop
        .else
        nop
        nop
        nop
        nop
        nop
        .endif
        .set    n, n + 1
        .endr
        mov     r0, r3
        bx      lr
