// SPDX-License-Identifier: GPL-2.0
/*
 * a2 read-only register probe (#86): reads every address of regprobe_list.h once (strongly ordered, MMU off), logs per
 * address ok/abort + value in trace0 (16 B records after a 32 B header), parks. A data abort is flagged by rp_dabt and skipped.
 * Header "cur=" = index being read (written before each read): a stalled bus shows as a cur= that stops moving.
 */
#include "rsc.h"
#include "regprobe_list.h"

#define GT(r)	(*(volatile uint32_t *)(HW_GTIMER_BASE + HW_GTIMER_##r))
#define HDR	32
#define REC	16

char trace[TRACE_SZ] __attribute__((aligned(4096), section(".trace")));
volatile uint32_t rp_abt;			/* set by rp_dabt: DFSR | 0x10000 */
extern char rp_vectors[];
static uint32_t abt_stack[64];

static void hex(unsigned int off, uint32_t v, int n)
{
	int i;

	for (i = n - 1; i >= 0; i--, v >>= 4)
		trace[off + i] = "0123456789abcdef"[v & 15];
}

static void puts_at(unsigned int off, const char *s)
{
	while (*s)
		trace[off++] = *s++;
}

int main(void)
{
	uint32_t sctlr, i, t0, naborts = 0, v;
	unsigned int off;

	GT(CONTROL) = 1;
	__asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(sctlr));
	sctlr &= ~(1U << 13);			/* V: low vectors, VBAR below */
	__asm__ volatile("mcr p15, 0, %0, c1, c0, 0\n\tisb" : : "r"(sctlr));
	__asm__ volatile("mcr p15, 0, %0, c12, c0, 0\n\tisb" : : "r"(rp_vectors));
	__asm__ volatile("mrs r0, cpsr\n\tcps #0x17\n\tmov sp, %0\n\tmsr cpsr_c, r0" : : "r"(&abt_stack[64]) : "r0");
	puts_at(0, "regprobe v1 n=");
	hex(14, RP_N, 4);
	puts_at(18, " cur=0000\n   \n");	/* 32 B header, no NUL gap: trace0 is read as a string */
	__asm__ volatile("cpsie a");		/* take asynchronous aborts too */
	t0 = GT(COUNTER_LO);
	for (i = 0; i < RP_N; i++) {
		hex(23, i, 4);
		rp_abt = 0;
		v = 0xdeadbeef;
		if (rp_w[i] == 1)
			__asm__ volatile("dsb\n\tldrb %0, [%1]\n\tdsb\n\tisb" : "+r"(v) : "r"(rp_addr[i]) : "memory");
		else
			__asm__ volatile("dsb\n\tldr %0, [%1]\n\tdsb\n\tisb" : "+r"(v) : "r"(rp_addr[i]) : "memory");
		off = HDR + i * REC;
		hex(off, i, 4);
		if (rp_abt) {
			naborts++;
			trace[off + 4] = ' ';
			trace[off + 5] = 'A';
			trace[off + 6] = ' ';
			hex(off + 7, rp_abt & 0xffff, 8);
		} else {
			puts_at(off + 4, " . ");
			hex(off + 7, v, 8);
		}
		trace[off + 15] = '\n';
	}
	off = HDR + RP_N * REC;
	puts_at(off, "done aborts=");
	hex(off + 12, naborts, 4);
	puts_at(off + 16, " ticks=");
	hex(off + 23, GT(COUNTER_LO) - t0, 8);
	trace[off + 31] = '\n';
	for (;;)
		;
}
