; SPDX-License-Identifier: GPL-2.0
; US-48-500W port LED program for the CMICd LEDUP0 processor (both units). docs/leds.md.
; Our own program; ISA (opcodes, operand forms) from OpenBCM tools/led/tools/ledasm.h, assembled by scripts/ledasm.py.
; No stock bytes. The CPU owns all policy: per slot it writes CTL (value, blink, activity) and SPORT; this only
; serialises. Ends with `send (TOTAL)`; the processor restarts at 0 on the next refresh.
;
; Data RAM (driver writes everything except TICK/SLOT/CUR/VV; 0x00-0x7f = hardware scan-in, 0x80-0x9f = pack area):
;   CTL+s   [1:0] value bits, [3:2] blink mask (bit n blinks), [5:4] blink speed 0 none 1 slow 2 mid 3 fast,
;           [6] ACT: blank the slot for 2 of 5 refreshes while RX or TX is set on SPORT+s (stock's activity look)
;   SPORT+s scan-chain port for `port` = physical port (stock; needs the SDK PORT_ORDER_REMAP, driver)
;   NSLOT slots, NBITS bits per slot (1..4, LSB first = stock; bits above 1 are 0), POL xor mask, TOTAL = NSLOT * NBITS

CTL     equ 0xA0
SPORT   equ 0xC0
TICK    equ 0xE0
NSLOT   equ 0xE1
NBITS   equ 0xE2
POL     equ 0xE3
TOTAL   equ 0xE4
SLOT    equ 0xE5
CUR     equ 0xE6
VV      equ 0xE7
PH      equ 0xE8    ; activity phase 0..4 (stock data RAM 0xe1)

RX      equ 0x0     ; scan-chain status bits (stock program and OpenBCM hurricane3 examples)
TX      equ 0x1

start:
        inc     (TICK)
        ld      a, (PH)
        inc     a
        cmp     a, 5
        jc      ph_ok
        ld      a, 0
ph_ok:
        ld      (PH), a
        ld      a, 0
        ld      (SLOT), a
slot:
        ld      a, (SLOT)
        cmp     a, (NSLOT)
        jnc     done
        ld      b, SPORT
        add     b, (SLOT)
        ld      a, (b)
        port    a
        ld      b, CTL
        add     b, (SLOT)
        ld      a, (b)
        ld      (CUR), a
        and     a, 0x03
        ld      (VV), a
; blink: phase bit = 5 - speed of TICK (speed 1: bit 4, 2: bit 3, 3: bit 2); speed 0 = no blink
        ld      b, 5
        ld      a, (CUR)
        tst     a, 4
        jnc     sp1
        dec     b
sp1:
        tst     a, 5
        jnc     sp2
        dec     b
        dec     b
sp2:
        cmp     b, 5
        jz      act
        ld      a, (TICK)
        tst     a, b
        jnc     act
        ld      a, (CUR)
        tst     a, 2
        jnc     bm1
        ld      a, (VV)
        and     a, 0xFE
        ld      (VV), a
        ld      a, (CUR)
bm1:
        tst     a, 3
        jnc     act
        ld      a, (VV)
        and     a, 0xFD
        ld      (VV), a
; activity: RX | TX of the slot's port blanks it while PH < 2 (stock 0x44-0x50)
act:
        ld      a, (CUR)
        tst     a, 6
        jnc     emit
        pushst  RX
        pushst  TX
        tor
        pop
        jnc     emit
        ld      a, (PH)
        cmp     a, 2
        jnc     emit
        ld      a, 0
        ld      (VV), a
; shift out NBITS bits of VV ^ POL, LSB first (stock order: docs/ledup-stock.md)
emit:
        ld      a, (VV)
        xor     a, (POL)
        ld      b, 0
bit:
        cmp     b, (NBITS)
        jnc     next
        tst     a, b
        push    cy
        pack
        inc     b
        jmp     bit
next:
        inc     (SLOT)
        jmp     slot
done:
        send    (TOTAL)
