@ a2 CPU + L1 stress (#79 H2); copied to SRAM, runs with code, data and counters in SRAM only (no PCIe traffic).
@ r0 = data base (save area +0, counters +0x30 iters / +0x34 errA / +0x38 errB, array at +0x800), r1/r2 = target cycles lo/hi.
@ Per iteration: A = ALU chain (mul, eor, ror, add) vs EXP_A; B = MP passes of 8 KiB store + dependent load checksum vs EXP_B.
@ Duration = PMCCNTR delta summed until >= target.
        .arm
        .section .text.stress, "ax"
        .globl  stress_start
        .globl  stress_end
stress_start:
        stmia   r0, {r4-r11, sp, lr}
        mov     r10, r0
        add     r11, r0, #0x800
        mov     r3, #0
        str     r3, [r10, #0x30]
        str     r3, [r10, #0x34]
        str     r3, [r10, #0x38]
        mov     r8, #0
        mov     r9, #0
        mrc     p15, 0, r7, c9, c13, 0
outer:  ldr     r0, =0x12345678
        ldr     r12, =0x9e3779b1
        ldr     lr, =0x7f4a7c15
        ldr     r6, =STRESS_K
        mov     r3, #0
a_loop: mul     r5, r0, r12
        add     r0, r5, lr
        eor     r0, r0, r0, lsr #15
        mov     r0, r0, ror #25
        add     r0, r0, r3
        add     r3, r3, #1
        cmp     r3, r6
        bne     a_loop
        ldr     r5, =EXP_A
        cmp     r0, r5
        ldrne   r5, [r10, #0x34]
        addne   r5, r5, #1
        strne   r5, [r10, #0x34]
        mov     r6, #STRESS_MP
b_pass: mov     r3, r0
        mov     r4, r11
        add     r12, r11, #8192
        ldr     lr, =0x01000193
w_loop: str     r3, [r4], #4
        add     r3, r3, lr
        cmp     r4, r12
        bne     w_loop
        mov     r3, #0
        mov     r4, r12
r_loop: ldr     r5, [r4, #-4]!
        add     r3, r3, r3, lsl #1
        add     r3, r3, r5
        cmp     r4, r11
        bne     r_loop
        add     r0, r0, r3
        subs    r6, r6, #1
        bne     b_pass
        ldr     r5, =EXP_B
        cmp     r0, r5
        ldrne   r5, [r10, #0x38]
        addne   r5, r5, #1
        strne   r5, [r10, #0x38]
        mrc     p15, 0, r5, c9, c13, 0
        sub     r3, r5, r7
        mov     r7, r5
        adds    r8, r8, r3
        adc     r9, r9, #0
        ldr     r3, [r10, #0x30]
        add     r3, r3, #1
        str     r3, [r10, #0x30]
        cmp     r9, r2
        cmpeq   r8, r1
        bcc     outer
        mov     r0, r10
        ldmia   r0, {r4-r11, sp, lr}
        bx      lr
        .ltorg
stress_end:
