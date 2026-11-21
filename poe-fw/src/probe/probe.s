; POE_FW_PROBE only: slots 4..7 count each entry in a 16-bit host-register pair (lo, hi), then run the real hook.
; Uses A, DPTR and carry only, like the hooks themselves (the ROM saves ACC / B / DPTR, I). Register map: poe-fw/inc/common.h.
	.module probe
	.globl _probe_chan0, _probe_chan1, _probe_chan2, _probe_chan3
	.globl _hook_chan0, _hook_chan1, _hook_chan2, _hook_chan3
	.area CSEG (CODE)

_probe_chan0:
	mov	dptr,#0x8054
	movx	a,@dptr
	add	a,#1
	movx	@dptr,a
	mov	dptr,#0x8059
	movx	a,@dptr
	addc	a,#0
	movx	@dptr,a
	ljmp	_hook_chan0
_probe_chan1:
	mov	dptr,#0x805A
	movx	a,@dptr
	add	a,#1
	movx	@dptr,a
	mov	dptr,#0x805B
	movx	a,@dptr
	addc	a,#0
	movx	@dptr,a
	ljmp	_hook_chan1
_probe_chan2:
	mov	dptr,#0x805C
	movx	a,@dptr
	add	a,#1
	movx	@dptr,a
	mov	dptr,#0x805D
	movx	a,@dptr
	addc	a,#0
	movx	@dptr,a
	ljmp	_hook_chan2
_probe_chan3:
	mov	dptr,#0x801C
	movx	a,@dptr
	add	a,#1
	movx	@dptr,a
	mov	dptr,#0x801D
	movx	a,@dptr
	addc	a,#0
	movx	@dptr,a
	ljmp	_hook_chan3
