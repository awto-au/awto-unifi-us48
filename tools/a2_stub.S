@ a2 stub: runs on unit 1's Cortex-A9 from SRAM 0x1b000000, serves a mailbox at 0x1b000100.
@ Layout: stub 0x1b000000..0xff, mailbox 0x1b000100, payload area 0x1b000200.., stack top 0x1b003ff0.
@ Mailbox words: +0 cmd (host writes last; stub clears), +4 arg0, +8 arg1, +12 arg2,
@                +16 result, +20 seq (stub increments after each command), +24 heartbeat, +28 ready magic.
@ cmd 1: call arg0(arg1, arg2) -> result r0   cmd 2: read32 [arg0]   cmd 3: write32 [arg0] = arg1
        .arm
        .text
        .globl  _start
_start:
        ldr     sp, =0x1b003ff0
        ldr     r4, =0x1b000100
        mov     r5, #0
        str     r5, [r4, #0]
        str     r5, [r4, #20]
        str     r5, [r4, #24]
        ldr     r0, =0x57b10ade
        str     r0, [r4, #28]           @ ready magic written last
idle:
        ldr     r6, [r4, #24]
        add     r6, r6, #1
        str     r6, [r4, #24]           @ heartbeat
        ldr     r0, [r4, #0]
        cmp     r0, #0
        beq     idle
        ldr     r1, [r4, #4]
        ldr     r2, [r4, #8]
        ldr     r3, [r4, #12]
        cmp     r0, #1
        beq     do_call
        cmp     r0, #2
        beq     do_read
        cmp     r0, #3
        beq     do_write
        mvn     r0, #0                  @ unknown cmd -> 0xffffffff
        b       done
do_call:
        mov     r5, #0
        mcr     p15, 0, r5, c7, c5, 0   @ ICIALLU: payload was just written by the host
        dsb
        isb
        mov     r5, r1                  @ target
        mov     r0, r2                  @ payload arg0
        mov     r1, r3                  @ payload arg1
        blx     r5
        b       done
do_read:
        ldr     r0, [r1]
        b       done
do_write:
        str     r2, [r1]
        mov     r0, #0
done:
        ldr     r4, =0x1b000100         @ payload may have clobbered r4
        str     r0, [r4, #16]
        mov     r5, #0
        str     r5, [r4, #0]            @ ack: cmd = 0
        ldr     r6, [r4, #20]
        add     r6, r6, #1
        str     r6, [r4, #20]           @ seq last: host waits on this
        b       idle
        .ltorg
