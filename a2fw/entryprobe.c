// SPDX-License-Identifier: GPL-2.0
/* a2 entry-state probe (#79 H1): prints SCTLR/ACTLR/Diagnostic/MIDR as found at reset (start.S ENTRY_PROBE), MMU and D-cache off. */
#include "rsc.h"

char trace[TRACE_SZ] __attribute__((aligned(4096), section(".trace")));
extern uint32_t entry_regs[4];

static void hex32(volatile char *t, uint32_t v)
{
	int i;

	for (i = 7; i >= 0; i--, v >>= 4)
		t[i] = "0123456789abcdef"[v & 15];
}

int main(void)
{
	static const char lab[4][12] = { "sctlr=", "actlr=", "diag=", "midr=" };
	volatile char *t = trace;
	uint32_t i, j, o = 0, now;

	for (i = 0; i < 4; i++) {
		for (j = 0; lab[i][j]; j++)
			t[o++] = lab[i][j];
		hex32(t + o, entry_regs[i]);
		o += 8;
		t[o++] = '\n';
	}
	__asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(now));
	for (j = 0; "now="[j]; j++)
		t[o++] = "now="[j];
	hex32(t + o, now);
	o += 8;
	t[o++] = '\n';
	for (j = 0; "pass=00000001\n"[j]; j++)
		t[o++] = "pass=00000001\n"[j];
	for (;;)
		;
}
