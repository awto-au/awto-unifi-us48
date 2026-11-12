/* SPDX-License-Identifier: GPL-2.0+ */

#ifndef __US48_H
#define __US48_H

#define CFG_SYS_SDRAM_BASE		0x00000000

/* ARM global timer = PERIPHCLK = A9 400 MHz / 2 */
#define CFG_SYS_HZ_CLOCK		200000000

/* UART1 on the 50 MHz APB clock */
#define CFG_SYS_NS16550_CLK		50000000

/* Fixed locally administered MAC = the Linux driver's (static L2 entry key) */
#define CFG_EXTRA_ENV_SETTINGS	"ethaddr=02:75:73:34:38:50\0"

#endif /* __US48_H */
