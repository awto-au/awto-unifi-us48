// SPDX-License-Identifier: GPL-2.0
/*
 * a2 self-test firmware (unit 1 Cortex-A9), runs from the host-DDR carveout with the I-cache on.
 * Trace text at the "trace0" resource: bench result once, heartbeat counter updated in place.
 */
#include "rsc.h"

#define BENCH_ITERS	20000		/* same loop count as tools/a2_bench.S */
/* Heartbeat period 10 ms = 2,000,000 ticks of the global timer (PERIPHCLK 200 MHz, hwdb gtimer desc) */
#define HB_TICKS	2000000
#define GT(r)		(*(volatile uint32_t *)(HW_GTIMER_BASE + HW_GTIMER_##r))

char trace[TRACE_SZ] __attribute__((aligned(4096)));

static unsigned int put(unsigned int off, const char *s)
{
	while (*s)
		trace[off++] = *s++;
	return off;
}

/* 8 hex digits at off; fixed width so the heartbeat can be rewritten in place */
static void hex32(unsigned int off, uint32_t v)
{
	int i;

	for (i = 7; i >= 0; i--, v >>= 4)
		trace[off + i] = "0123456789abcdef"[v & 15];
}

static uint32_t bench(void)
{
	uint32_t t0 = GT(COUNTER_LO), n = BENCH_ITERS;

	__asm__ volatile("1: subs %0, %0, #1\n\tbne 1b" : "+r"(n) : : "cc");
	return GT(COUNTER_LO) - t0;
}

int main(void)
{
	uint32_t sctlr, ticks, hb = 0, next;
	unsigned int off = 0, hb_off;

	GT(CONTROL) = 1;	/* TIMER_ENABLE */
	__asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(sctlr));
	ticks = bench();
	off = put(off, "a2fw selftest v1\nsctlr=");
	hex32(off, sctlr);
	off = put(off + 8, "\nbench iters=");
	hex32(off, BENCH_ITERS);
	off = put(off + 8, " ticks=");
	hex32(off, ticks);
	off = put(off + 8, "\nhb=");
	hb_off = off;
	hex32(hb_off, 0);
	trace[hb_off + 8] = '\n';

	next = GT(COUNTER_LO);
	for (;;) {
		next += HB_TICKS;
		while ((int32_t)(GT(COUNTER_LO) - next) < 0)
			;
		hex32(hb_off, ++hb);
	}
}
