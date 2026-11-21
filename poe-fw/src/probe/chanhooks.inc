	.globl _probe_chan0, _probe_chan1, _probe_chan2, _probe_chan3
	.db 0xFF, >_probe_chan0, _probe_chan0 ; 4..7 channel ISRs via counting trampolines (probe.s)
	.db 0xFF, >_probe_chan1, _probe_chan1
	.db 0xFF, >_probe_chan2, _probe_chan2
	.db 0xFF, >_probe_chan3, _probe_chan3
