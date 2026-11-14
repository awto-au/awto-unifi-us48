/* SPDX-License-Identifier: GPL-2.0+ */

#ifndef __US48_H
#define __US48_H

#define CFG_SYS_SDRAM_BASE		0x00000000

/* Relock values: 1000..1999 in 12.5 MHz steps (ndiv = MHz x 2 / 25), see US48_RELOCK_OK */
#if CONFIG_US48_CPU_MHZ != 400 && CONFIG_US48_CPU_MHZ != 800 && \
	(CONFIG_US48_CPU_MHZ < 1000 || CONFIG_US48_CPU_MHZ >= 2000 || CONFIG_US48_CPU_MHZ * 2 % 25)
#error "US48_CPU_MHZ must be 400, 800 or 1000..1999 with MHz x 2 a multiple of 25"
#endif

/* Fallback only: the live rate (ARM/2) comes from periph_clk in board.c */
#define CFG_SYS_HZ_CLOCK		200000000

/* UART1 on the 50 MHz APB clock */
#define CFG_SYS_NS16550_CLK		50000000

/* Fixed locally administered MACs = the Linux driver's per port (static L2 entry key): eth0 = ge1 (front 1), eth1 = xe1 (front 50) */
#define CFG_EXTRA_ENV_SETTINGS	"ethaddr=02:75:73:34:38:01\0eth1addr=02:75:73:34:38:50\0"

#endif /* __US48_H */
