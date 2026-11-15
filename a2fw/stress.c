// SPDX-License-Identifier: GPL-2.0
/*
 * a2 CPU + L1 stress (#79 H2): the loop (stress_asm.S) runs from a2 SRAM with data in SRAM, no PCIe traffic while it runs.
 * Known answers (EXP_A, EXP_B from build_a2fw.py) for an ALU chain and an 8 KiB store/load checksum; target cycles from PMCCNTR.
 * Output like rawtest: m = ALU mismatches, r = memory-checksum mismatches, pass = iterations, done=00000001 at the end.
 */
#include "rsc.h"

#define BASE		HW_A2_CARVEOUT_BASE
#define PT_OFF		(HW_A2_SHARED_BASE - BASE + 0x4000U)
#define SRAM_CODE	HW_A2_SRAM_BASE
#define SRAM_DATA	(HW_A2_SRAM_BASE + 0x800U)	/* save area + counters; array at +0x800 more (8 KiB, ends at SRAM end - 0x800) */
#define VA_NC(pa)	((pa) - BASE + 0x20000000U)
#define SEC_WB		0x1c0eU
#define SEC_NC		0x1c02U
#define SEC_DEV		0x0c16U

char trace[TRACE_SZ] __attribute__((aligned(4096), section(".trace")));
static volatile char *const TR = (volatile char *)VA_NC((uint32_t)trace);
extern char stress_start[], stress_end[];

static void dcache_inv(void)
{
	uint32_t ccsidr, sets, ways, lshift, wshift, s, w;

	__asm__ volatile("mcr p15, 2, %0, c0, c0, 0\n\tisb" : : "r"(0));
	__asm__ volatile("mrc p15, 1, %0, c0, c0, 0" : "=r"(ccsidr));
	lshift = (ccsidr & 7) + 4;
	ways = ((ccsidr >> 3) & 0x3ff) + 1;
	sets = ((ccsidr >> 13) & 0x7fff) + 1;
	wshift = __builtin_clz(ways - 1);
	for (w = 0; w < ways; w++)
		for (s = 0; s < sets; s++)
			__asm__ volatile("mcr p15, 0, %0, c7, c6, 2" : : "r"((w << wshift) | (s << lshift)));
	__asm__ volatile("dsb" ::: "memory");
}

static void mmu_on(void)
{
	volatile uint32_t *pt = (volatile uint32_t *)(BASE + PT_OFF);
	uint32_t i, sctlr;

	for (i = 0; i < 4096; i++)
		pt[i] = 0;
	for (i = 0; i < HW_A2_CARVEOUT_SIZE >> 20; i++) {
		uint32_t pa = BASE + (i << 20);

		pt[pa >> 20] = pa | SEC_WB;
		pt[VA_NC(pa) >> 20] = pa | SEC_NC;
	}
	for (i = 0x180; i < 0x1c0; i++)
		pt[i] = (i << 20) | SEC_DEV;
	pt[HW_A2_SRAM_BASE >> 20] = HW_A2_SRAM_BASE | SEC_WB;	/* SRAM cacheable, accesses stay inside 16 KiB */
	__asm__ volatile("dsb" ::: "memory");
	dcache_inv();
	__asm__ volatile("mcr p15, 0, %0, c8, c7, 0" : : "r"(0));
	__asm__ volatile("mcr p15, 0, %0, c7, c5, 6" : : "r"(0));
	__asm__ volatile("mcr p15, 0, %0, c2, c0, 2" : : "r"(0));
	__asm__ volatile("mcr p15, 0, %0, c2, c0, 0" : : "r"(BASE + PT_OFF));
	__asm__ volatile("mcr p15, 0, %0, c3, c0, 0" : : "r"(3));
	__asm__ volatile("isb");
	__asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(sctlr));
	sctlr |= 1U | 4U | 0x1000U;	/* M, C, I */
	__asm__ volatile("mcr p15, 0, %0, c1, c0, 0\n\tisb" : : "r"(sctlr));
}

static uint32_t put(uint32_t off, const char *s)
{
	while (*s)
		TR[off++] = *s++;
	return off;
}

static void hex32(uint32_t off, uint32_t v)
{
	int i;

	for (i = 7; i >= 0; i--, v >>= 4)
		TR[off + i] = "0123456789abcdef"[v & 15];
}

int main(void)
{
	volatile uint32_t *code = (volatile uint32_t *)SRAM_CODE, *d = (volatile uint32_t *)SRAM_DATA;
	const volatile uint32_t *s = (const volatile uint32_t *)stress_start;
	uint32_t i, off, o_pass, o_m, o_r, o_done;

	for (i = 0; i < (uint32_t)(stress_end - stress_start) / 4; i++)
		code[i] = s[i];	/* MMU off: direct writes into SRAM */
	mmu_on();
	__asm__ volatile("dsb\n\tmcr p15, 0, %0, c7, c5, 0\n\tisb" : : "r"(0));	/* ICIALLU: the loop was written after the I-cache was on */
	__asm__ volatile("mcr p15, 0, %0, c9, c12, 0" : : "r"(5));		/* PMCR: E, reset cycle counter */
	__asm__ volatile("mcr p15, 0, %0, c9, c12, 1" : : "r"(0x80000000U));	/* PMCNTENSET: cycle counter */
	off = put(0, "a2fw stress target=");
	hex32(off, (uint32_t)STRESS_TARGET);
	off = put(off + 8, "\npass=");
	o_pass = off;
	off = put(off + 8, " m=");
	o_m = off;
	off = put(off + 8, " r=");
	o_r = off;
	off = put(off + 8, " done=");
	o_done = off;
	hex32(off, 0);
	hex32(o_pass, 0);
	hex32(o_m, 0);
	hex32(o_r, 0);
	((void (*)(uint32_t, uint32_t, uint32_t))SRAM_CODE)(SRAM_DATA, (uint32_t)STRESS_TARGET, (uint32_t)((uint64_t)STRESS_TARGET >> 32));
	hex32(o_pass, d[0x30 / 4]);
	hex32(o_m, d[0x34 / 4]);
	hex32(o_r, d[0x38 / 4]);
	hex32(o_done, 1);
	for (;;)
		;
}
