/* SPDX-License-Identifier: GPL-2.0+ */

#ifndef __US48_H
#define __US48_H

#define CFG_SYS_SDRAM_BASE		0x00000000

#if CONFIG_US48_CPU_MHZ != 400 && CONFIG_US48_CPU_MHZ != 800 && CONFIG_US48_CPU_MHZ != 1000
#error "US48_CPU_MHZ must be 400, 800 or 1000"
#endif

/* Fallback only: the live rate (ARM/2) comes from periph_clk in board.c */
#define CFG_SYS_HZ_CLOCK		200000000

/* UART1 on the 50 MHz APB clock */
#define CFG_SYS_NS16550_CLK		50000000

/* Fixed locally administered MAC = the Linux driver's (static L2 entry key) */
#define CFG_EXTRA_ENV_SETTINGS	"ethaddr=02:75:73:34:38:50\0"

#endif /* __US48_H */
