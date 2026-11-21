; Image skeleton, hook wrappers, start-up, dispatch. Layout identical to stock v21 (docs/poe-fw.md section 3), CPU base 0x1A00.
; Hook table: 15 records [0xFF hi lo] (Keil generic code pointers, tag 0xFF: the ROM reads them). Written explicitly high byte first.
	.module startup
	.globl _fw_main
	.globl _isr_i2c_body, _isr2_body, _periodic_body, _tick_body
	.globl _hook_chan0, _hook_chan1, _hook_chan2, _hook_chan3
	.globl _host_write_table, _host_read_table

	.area HOOKS (ABS,CODE)
	.org 0x1A00
	.db 0xFF, >_slot0, _slot0             ; 0 start-up (stock main 0x3CE3)
	.db 0xFF, >_hook_i2c, _hook_i2c       ; 1 I2C slave ISR
	.db 0xFF, >_hook_isr2, _hook_isr2     ; 2 event ISR
	.db 0xFF, >_hook_periodic, _hook_periodic ; 3 periodic ISR
	.db 0xFF, >_hook_chan0, _hook_chan0   ; 4..7 channel ISRs
	.db 0xFF, >_hook_chan1, _hook_chan1
	.db 0xFF, >_hook_chan2, _hook_chan2
	.db 0xFF, >_hook_chan3, _hook_chan3
	.db 0xFF, >_hook_timer1, _hook_timer1 ; 8 timer 1 tick
	.db 0xFF, 0x1A, 0xFD                  ; 9..11 RET stubs
	.db 0xFF, 0x1A, 0xFE
	.db 0xFF, 0x1A, 0xFF
	.db 0xFF, 0x52, 0x20                  ; 12..14 RET stubs
	.db 0xFF, 0x52, 0x21
	.db 0xFF, 0x52, 0x22

; stock 0x1AFC user_init_stub, 0x1AFD..0x1AFF unused slots, 0x1B00 ROM entry: LJMP start-up
	.area ENTRY (ABS,CODE)
	.org 0x1AFC
	ret
	ret
	ret
	ret
	ljmp _rom_init

	.area RETSTUB (ABS,CODE)
	.org 0x5220
	ret
	ret
	ret

	.area TAGSTR (ABS,CODE)
	.org 0x5300
	.ascii "POETEC_IMAGE+ICUT_ILIM_ACCESS_ENABLED+LEGACY_POWERUP+AUTO_FAULT_CLEAR+FIXED_TLIM+REJECT_AC_NOISE+DEFAULT_AF_AUTO_MODE+AWTO_OSS"
	.db 0

	.area CSEG (CODE)
; stock 0x50EA STARTUP.A51 IDATALOOP + init_i2c_status 0x50F3, verbatim (SP and PSW untouched)
_rom_init:
	mov r0,#0xFF
	clr a
00001$:	mov @r0,a
	djnz r0,00001$
	lcall 0x1AFC
	mov 0xDD,#0xB0
	clr a
	mov dptr,#0x804A
	movx @dptr,a
	ret

; slot 0: PSW bank 0 is required by the C code (stock Keil code assumes it too)
_slot0:
	mov psw,#0x00
	ljmp _fw_main

; hooks 1, 2, 3, 8: stock prologue PUSH PSW; MOV PSW,#bank ... POP PSW; RET (no RETI, ROM saves ACC/B/DPTR, I)
_hook_i2c:
	push psw
	mov psw,#0x08
	lcall _isr_i2c_body
	pop psw
	ret
_hook_isr2:
	push psw
	mov psw,#0x18
	lcall _isr2_body
	pop psw
	ret
_hook_periodic:
	push psw
	mov psw,#0x10
	clr 0xB9                              ; IP.1 off while running (stock 0x4EBF)
	lcall _periodic_body
	setb 0xB9
	pop psw
	ret
_hook_timer1:
	push psw
	mov psw,#0x18
	clr 0x8E                              ; TCON.6 (TR1) off, reload, tick, on (stock 0x4FD2..0x4FDD)
	mov 0x8B,#0xF4
	mov 0x8D,#0xE5
	lcall _tick_body
	setb 0x8E
	pop psw
	ret

; Table dispatch: DPL = host register. Deviation from stock 0x47AB: index masked to 0..0x7F (stock reads past the 128-entry
; table for pointers >= 0x80). Tail-jumps into the handler via push/ret, the handler returns to the C caller.
	.globl _host_wr_dispatch
	.globl _host_rd_dispatch
_host_wr_dispatch:
	mov a,dpl
	anl a,#0x7F
	mov b,#0x03
	mul ab
	add a,#<_host_write_table
	mov dpl,a
	mov a,b
	addc a,#>_host_write_table
	mov dph,a
	sjmp _dispatch_go
_host_rd_dispatch:
	mov a,dpl
	anl a,#0x7F
	mov b,#0x03
	mul ab
	add a,#<_host_read_table
	mov dpl,a
	mov a,b
	addc a,#>_host_read_table
	mov dph,a
_dispatch_go:
	mov a,#0x01
	movc a,@a+dptr                        ; hi
	mov b,a
	mov a,#0x02
	movc a,@a+dptr                        ; lo
	push acc
	push b
	ret

; stock 0x3CF1..0x3CF7 / 0x3CFF..0x3D05: LDGPTR_CODE + ICALL through the pointer stored in ROM at 0x1903 (strap 1) / 0x1900 (U)
	.globl _rom_strap_call
_rom_strap_call:
	mov a,dpl
	jz 00001$
	mov dptr,#0x1903
	sjmp 00002$
00001$:	mov dptr,#0x1900
00002$:	sjmp _dispatch_go
