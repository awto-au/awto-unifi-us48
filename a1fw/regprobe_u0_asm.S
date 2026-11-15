@ a1 regprobe entry (#86, unit 0): called by awto-uboot `go`. Saves VBAR/SCTLR.V/abort SP/CPSR.A, installs a vector table whose
@ data-abort handler flags the abort and skips the faulting load, runs probe_main, restores everything and returns to U-Boot.
        .arm
        .section .text.start, "ax"
        .globl  _start
_start:
        push    {r4-r11, lr}
        mrc     p15, 0, r5, c1, c0, 0      @ r5 = SCTLR
        mrc     p15, 0, r6, c12, c0, 0     @ r6 = VBAR
        bic     r0, r5, #(1 << 13)         @ V = 0: low vectors, VBAR below
        mcr     p15, 0, r0, c1, c0, 0
        isb
        ldr     r0, =rp_vectors
        mcr     p15, 0, r0, c12, c0, 0
        isb
        cps     #0x17                      @ abort mode: own stack, remember the old one
        mov     r7, sp
        ldr     sp, =abt_stack_top
        cps     #0x13
        cpsie   a                          @ take asynchronous aborts too
        bl      probe_main
        cpsid   a
        dsb
        cps     #0x17
        mov     sp, r7
        cps     #0x13
        mcr     p15, 0, r6, c12, c0, 0
        mcr     p15, 0, r5, c1, c0, 0
        isb
        pop     {r4-r11, pc}

        .align  5
        .globl  rp_vectors
rp_vectors:
        b       rp_park                    @ reset
        b       rp_park                    @ undefined
        b       rp_park                    @ svc
        b       rp_park                    @ prefetch abort
        b       rp_dabt                    @ data abort (precise load fault, or async: same vector)
        b       rp_park                    @ reserved
        b       rp_park                    @ irq
        b       rp_park                    @ fiq
rp_park:
        b       rp_park
rp_dabt:
        push    {r0, r1}
        mrc     p15, 0, r0, c5, c0, 0      @ DFSR
        orr     r0, r0, #0x10000
        ldr     r1, =rp_abt
        str     r0, [r1]
        pop     {r0, r1}
        subs    pc, lr, #4                 @ resume after the faulting load (async abort: next instruction)
        .ltorg

        .section .bss
        .align  3
        .space  1024
abt_stack_top:
