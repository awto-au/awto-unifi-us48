@ a2 boot stub for Linux on a2 (payload mode). Entered at the a2 reset entry set by us48_a2 (remoteproc start).
@ MMU and D-cache off, L1 D invalidated, I-cache on; r0 = 0, r1 = 0xffffffff (no machine type), r2 = DTB; jumps to the zImage.
@ Optional DCC markers ('a' entry, 'J' jump) prove the console path before the kernel runs (boot args flags).
#include "us48_a2_boot.h"
        .arm
        .section .text.start, "ax"
        .globl  _start
_start:
        cpsid   aif
        mrc     p15, 0, r0, c1, c0, 0
        bic     r0, r0, #0x1000           @ SCTLR.I off while the I-cache is invalidated
        bic     r0, r0, #0x5              @ SCTLR.M, SCTLR.C off
        mcr     p15, 0, r0, c1, c0, 0
        isb
        mov     r1, #0
        mcr     p15, 0, r1, c7, c5, 0     @ ICIALLU
        mcr     p15, 0, r1, c7, c5, 6     @ BPIALL
        mcr     p15, 0, r1, c8, c7, 0     @ TLBIALL
        dsb
        isb
        @ L1 D invalidate by set/way (v7_invalidate_l1): the decompressor turns the D-cache on without it
        mov     r0, #0
        mcr     p15, 2, r0, c0, c0, 0     @ CSSELR = L1 D
        isb
        mrc     p15, 1, r0, c0, c0, 0     @ CCSIDR
        movw    r1, #0x7fff
        and     r2, r1, r0, lsr #13       @ sets - 1
        movw    r1, #0x3ff
        and     r3, r1, r0, lsr #3        @ ways - 1
        add     r2, r2, #1
        and     r0, r0, #7
        add     r0, r0, #4                @ line shift
        clz     r1, r3                    @ way shift
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
        mrc     p15, 0, r0, c1, c0, 0
        orr     r0, r0, #0x1000           @ SCTLR.I: allowed, the kernel manages it from here
        mcr     p15, 0, r0, c1, c0, 0
        isb
        ldr     r4, =boot_args
        ldr     r0, [r4, #0]
        ldr     r1, =US48_A2_BOOT_MAGIC
        cmp     r0, r1
        bne     park                      @ driver did not write the args: never guess an address
        mov     r1, #1
        str     r1, [r4, #24]             @ stage 1: magic ok
        ldr     r7, [r4, #16]             @ flags
        tst     r7, #US48_A2_BOOT_F_DCC
        beq     nodcc1
        mov     r0, #'a'
        bl      dccput
        mov     r1, #2
        str     r1, [r4, #24]             @ stage 2: first marker written (CP14 DTR write did not fault)
nodcc1: tst     r7, #US48_A2_BOOT_F_L2AUX
        beq     nol2
        @ PL310 (hwdb pl310, 0x19022000) resets with way size 0 (reserved); uboot-1 lowlevel_init @0x1c0005b8:
        @ aux = (aux & ~0xf0000) | 0x0a120000 with L2 off. A leftover enable from the previous run is switched off first.
        ldr     r0, =0x19022000
        mov     r2, #0
        str     r2, [r0, #0x100]          @ CTRL: L2 off, the kernel invalidates and enables it
        ldr     r2, [r0, #0x104]
        bic     r2, r2, #0xf0000
        ldr     r3, =0x0a120000
        orr     r2, r2, r3
        str     r2, [r0, #0x104]
        dsb
nol2:   ldr     r5, [r4, #4]              @ zImage
        ldr     r6, [r4, #8]              @ DTB
        mov     r1, #3
        str     r1, [r4, #24]             @ stage 3: at the jump
        mov     r1, #1
        str     r1, [r4, #12]             @ status: reached the jump
        tst     r7, #US48_A2_BOOT_F_DCC
        movne   r0, #'J'
        blne    dccput
        mov     r0, #0
        mvn     r1, #0
        mov     r2, r6
        bx      r5
park:   b       park

@ r0 = character: wait for DSCR.DTRTXfull (bit 29) clear, then DBGDTRTXint = r0. Clobbers r2, r3.
@ Wait: a1 drains within one poll period (5 ms) + work latency, ~7 ms; DCC_SPIN CP14 reads at an unmeasured 5-60 ns = 5-60 ms.
@ Expiry: marker skipped, boot_args.dcc_fail++ (the stub must not hang on a missing reader).
dccput: ldr     r3, =US48_A2_BOOT_DCC_SPIN
3:      mrc     p14, 0, r2, c0, c1, 0
        tst     r2, #0x20000000
        beq     4f
        subs    r3, r3, #1
        bne     3b
        ldr     r3, [r4, #20]
        add     r3, r3, #1
        str     r3, [r4, #20]
        bx      lr
4:      mcr     p14, 0, r0, c0, c5, 0
        bx      lr
        .ltorg
        .section .bootargs, "aw"
        .globl  boot_args
boot_args:
        .space  32
