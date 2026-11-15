@ a2 boot stub for Linux on a2 (payload mode). Entered at the a2 reset entry set by us48_a2 (remoteproc start).
@ MMU and D-cache off, I-cache on; r0 = 0, r1 = 0xffffffff (no machine type), r2 = DTB address; jumps to the zImage.
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
        dsb
        isb
        orr     r0, r0, #0x1000           @ SCTLR.I: allowed, the kernel manages it from here
        mcr     p15, 0, r0, c1, c0, 0
        isb
        ldr     r4, =boot_args
        ldr     r0, [r4, #0]
        ldr     r1, =US48_A2_BOOT_MAGIC
        cmp     r0, r1
        bne     park                      @ driver did not write the args: never guess an address
        ldr     r5, [r4, #4]              @ zImage
        ldr     r6, [r4, #8]              @ DTB
        mov     r1, #1
        str     r1, [r4, #12]             @ status: reached the jump
        mov     r0, #0
        mvn     r1, #0
        mov     r2, r6
        bx      r5
park:   b       park
        .ltorg
        .section .bootargs, "aw"
        .globl  boot_args
boot_args:
        .space  16
