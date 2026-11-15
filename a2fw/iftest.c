// SPDX-License-Identifier: GPL-2.0
/*
 * a2 I-fetch / D-fill probes over PCIe (#79). One image per placement (build_a2fw.py):
 *   ifa: loop code in a2 SRAM, data across PCIe; ifb: 64 KiB code across PCIe, data in SRAM (4 KiB window);
 *   ifd: 64 KiB code and 64 KiB data both across PCIe (every code line fill coincides with a data line fill).
 * Both L1 caches on. Result text like rawtest: m = wrong sums, r = wrong words in dst (PCIe data only).
 */
#include "rsc.h"
#include "a2_diag.h"

#define BASE		HW_A2_CARVEOUT_BASE
#define PT_OFF		(HW_A2_SHARED_BASE - BASE + 0x4000U)
#define SRC_OFF		0x00900000U
#define DST_OFF		(SRC_OFF + 0x10000U)
#define NLINES		2048U	/* 64 KiB of src, 2x the 32 KiB L1 D */
#define SRAM_CODE	(HW_A2_SRAM_BASE + 0x1000U)
#define SRAM_SRC	(HW_A2_SRAM_BASE + 0x2000U)
#define SRAM_DST	(HW_A2_SRAM_BASE + 0x3000U)
#define VA_NC(pa)	((pa) - BASE + 0x20000000U)
#define SEC_WB		0x1c0eU
#define SEC_NC		0x1c02U
#define SEC_DEV		0x0c16U

#if defined(IF_A)
#define MODE_NAME	"ifa"
#elif defined(IF_B)
#define MODE_NAME	"ifb"
#else
#define MODE_NAME	"ifd"
#endif

char trace[TRACE_SZ] __attribute__((aligned(4096), section(".trace")));
static volatile char *const TR = (volatile char *)VA_NC((uint32_t)trace);
extern char dloop_start[], dloop_end[];
extern uint32_t bigcode(const uint32_t *src, uint32_t *dst);

static inline void dsb(void)
{
	__asm__ volatile("dsb" ::: "memory");
}

static void dcache_setway(int op)	/* 0 invalidate, 2 clean+invalidate */
{
	uint32_t ccsidr, sets, ways, lshift, wshift, s, w;

	__asm__ volatile("mcr p15, 2, %0, c0, c0, 0\n\tisb" : : "r"(0));
	__asm__ volatile("mrc p15, 1, %0, c0, c0, 0" : "=r"(ccsidr));
	lshift = (ccsidr & 7) + 4;
	ways = ((ccsidr >> 3) & 0x3ff) + 1;
	sets = ((ccsidr >> 13) & 0x7fff) + 1;
	wshift = __builtin_clz(ways - 1);
	for (w = 0; w < ways; w++)
		for (s = 0; s < sets; s++) {
			uint32_t v = (w << wshift) | (s << lshift);

			if (op == 0)
				__asm__ volatile("mcr p15, 0, %0, c7, c6, 2" : : "r"(v));
			else
				__asm__ volatile("mcr p15, 0, %0, c7, c14, 2" : : "r"(v));
		}
	dsb();
}

static void mmu_on(void)
{
	volatile uint32_t *pt = (volatile uint32_t *)(BASE + PT_OFF);	/* MMU still off: direct physical writes */
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
	pt[HW_A2_SRAM_BASE >> 20] = HW_A2_SRAM_BASE | SEC_WB;	/* SRAM cacheable; accesses stay inside 0x1b000000..0x1b003fff */
	dsb();
	dcache_setway(0);
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

static inline uint32_t val(uint32_t i)
{
	return (i * 0x9e3779b1U) ^ 0x1234567U;
}

int main(void)
{
	volatile uint32_t *nsrc __attribute__((unused)) = (volatile uint32_t *)VA_NC(BASE + SRC_OFF), *ndst __attribute__((unused)) = (volatile uint32_t *)VA_NC(BASE + DST_OFF);
	const uint32_t *src;
	uint32_t *dst, i, j __attribute__((unused)), pass = 0, m = 0, r = 0, v, off, off_pass, off_m, off_r, e, nl;
	volatile uint32_t *p __attribute__((unused));

#ifdef IF_A
	p = (volatile uint32_t *)HW_A2_SRAM_BASE;	/* MMU off: copy the loop into SRAM */
	{
		const volatile uint32_t *s = (const volatile uint32_t *)dloop_start;

		for (i = 0; i < (uint32_t)(dloop_end - dloop_start) / 4; i++)
			p[0x400 + i] = s[i];
	}
#endif
#ifdef IF_B
	p = (volatile uint32_t *)SRAM_SRC;
	for (i = 0; i < 1024; i++)
		p[i] = val(i);	/* word 0 of line k (word 8k) is what bigcode reads */
#endif
	mmu_on();
#ifdef NIC_ISS
	*(volatile uint32_t *)A2_NIC_PCIE0_S0_ISS = NIC_ISS;
#endif
#ifndef DIAG_SET
#define DIAG_SET 0
#endif
	off = put(0, "a2fw iftest " MODE_NAME " diag:");
	hex32(off, a2_diag_rmw(DIAG_SET));
	off = put(off + 8, "\npass=");
	off_pass = off;
	off = put(off + 8, " m = wrong sums, r = wrong dst words\n" MODE_NAME " m=");
	off_m = off;
	off = put(off + 8, " r=");
	off_r = off;
	off = put(off + 8, "\n");
#ifdef IF_B
	src = (const uint32_t *)SRAM_SRC;
	dst = (uint32_t *)SRAM_DST;
	nl = 128;	/* distinct src lines */
#else
	for (i = 0; i < NLINES * 8; i++)
		nsrc[i] = val(i);
	src = (const uint32_t *)(BASE + SRC_OFF);
	dst = (uint32_t *)(BASE + DST_OFF);
	nl = NLINES;
#endif
	for (;;) {
		for (e = 0, i = 0; i < NLINES; i++)
			e += val((i % nl) * 8);
#ifdef IF_A
		v = ((uint32_t (*)(const uint32_t *, uint32_t *, uint32_t))SRAM_CODE)(src, dst, NLINES);
#else
		v = bigcode(src, dst);
#endif
		if (v != e)
			m++;
#ifndef IF_B
		dcache_setway(2);
		for (e = 0, j = 0; j < NLINES; j++) {
			e += val(j * 8);
			if (ndst[j * 8] != e)
				r++;
		}
#endif
		pass++;
		hex32(off_pass, pass);
		hex32(off_m, m);
		hex32(off_r, r);
	}
}
