@ regprobe (#86): vector table + data-abort handler that flags the abort and skips the faulting load.
@ VBAR points here (regprobe.c); every other vector parks (the host sees the stalled cur= counter).
        .arm
        .section .text.vec, "ax"
        .align  5
        .globl  rp_vectors
rp_vectors:
        b       rp_park                   @ reset
        b       rp_park                   @ undefined
        b       rp_park                   @ svc
        b       rp_park                   @ prefetch abort
        b       rp_dabt                   @ data abort (precise load fault, or async: same vector)
        b       rp_park                   @ reserved
        b       rp_park                   @ irq
        b       rp_park                   @ fiq
rp_park:
        b       rp_park
rp_dabt:
        push    {r0, r1}
        mrc     p15, 0, r0, c5, c0, 0     @ DFSR
        orr     r0, r0, #0x10000          @ non-zero flag even for status 0
        ldr     r1, =rp_abt
        str     r0, [r1]
        pop     {r0, r1}
        subs    pc, lr, #4                @ resume after the faulting load (async abort: next instruction)
        .ltorg
