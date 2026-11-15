// SPDX-License-Identifier: GPL-2.0
/*
 * a1 read-only register probe of unit 0 (#86), run from awto-uboot: tftpboot 0x01000000 + `go 0x01000000`.
 * Reads every address of the generated list once; a data abort is flagged by rp_dabt and the load skipped.
 * Output on UART1 (console, polled): header, then one 8-hex token per read ("Axxxxxxx" = abort, DFSR),
 * a line start "iiii:" every 32 tokens. A hang = output stops; token count = index of the stalled read.
 */
#include <stdint.h>
#include "regprobe_list.h"

#define UART_THR	(*(volatile uint8_t *)0x18000400)
#define UART_LSR	(*(volatile uint8_t *)0x18000405)
#define GT_CNT_LO	(*(volatile uint32_t *)0x19020200)

volatile uint32_t rp_abt;			/* set by rp_dabt: DFSR | 0x10000 */
extern char rp_vectors[];

/* LSR THRE poll: 16 B FIFO drains at 115200 baud (87 us per char); 2e6 polls = well over one char time, expiry drops the char */
static void putc_(char c)
{
	uint32_t n = 2000000;

	while (!(UART_LSR & 0x20) && --n)
		;
	UART_THR = c;
}

static void hex(uint32_t v, int n)
{
	int i;

	for (i = n - 1; i >= 0; i--)
		putc_("0123456789abcdef"[(v >> (4 * i)) & 15]);
}

static void puts_(const char *s)
{
	while (*s)
		putc_(*s++);
}

int probe_main(void)
{
	uint32_t i, naborts = 0, v, t0;

	t0 = GT_CNT_LO;
	puts_("\nU0P1 n=");
	hex(RP_N, 4);
	puts_("\n");
	for (i = 0; i < RP_N; i++) {
		if (!(i & 31)) {
			puts_("\n");
			hex(i, 4);
			puts_(":");
		}
		rp_abt = 0;
		v = 0xdeadbeef;
		if (rp_w[i] == 1)
			__asm__ volatile("dsb\n\tldrb %0, [%1]\n\tdsb\n\tisb" : "+r"(v) : "r"(rp_addr[i]) : "memory");
		else
			__asm__ volatile("dsb\n\tldr %0, [%1]\n\tdsb\n\tisb" : "+r"(v) : "r"(rp_addr[i]) : "memory");
		if (rp_abt) {
			naborts++;
			putc_('A');
			hex(rp_abt & 0xffff, 7);
		} else {
			hex(v, 8);
		}
		putc_(' ');
	}
	puts_("\nDONE aborts=");
	hex(naborts, 4);
	puts_(" ticks=");
	hex(GT_CNT_LO - t0, 8);
	puts_("\n");
	return 0;
}
