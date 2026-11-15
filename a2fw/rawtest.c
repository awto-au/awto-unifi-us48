// SPDX-License-Identifier: GPL-2.0
/*
 * a2 read-after-write ordering test over PCIe (#79). Stores to host DDR (carveout, across PAXB) followed at once by loads of the
 * same word/line, under the memory types Linux (UP: WB no-write-allocate) and bustest (WBWA, NC) use. Doc: docs/a2-linux.md "Ordering test".
 * Results: trace0 text, fixed-width hex fields rewritten in place; m = loads that returned the wrong value, r = words wrong in memory
 * (re-read through the non-cacheable alias after clean+invalidate). m > 0, r = 0: stale read. r > 0: write lost or late.
 */
#include "rsc.h"
#include "a2_diag.h"

#define BASE		HW_A2_CARVEOUT_BASE
#define PT_OFF		(HW_A2_SHARED_BASE - BASE + 0x4000U)	/* 16 KiB aligned first-level table, a2_shared */
#define TBUF_OFF	0x00900000U	/* 1 MiB section, unused by the other a2 firmwares */
#define TBYTES		0x40000U	/* 256 KiB: 8x the 32 KiB L1 D */
#define LINE		32U
#define NVA		4
#define VA_WB(pa)	(pa)				/* TEX=001 C=1 B=1: write-back write-allocate (bustest) */
#ifdef NO_ALIAS	/* one mapping per physical page: carveout WB, a2_shared MiB (trace, stack, table) NC; no aliases */
#define VA_NC(pa)	(pa)
#else
#define VA_NC(pa)	((pa) - BASE + 0x20000000U)	/* TEX=001 C=0 B=0: normal non-cacheable */
#endif
#define VA_NWA(pa)	((pa) - BASE + 0x28000000U)	/* TEX=000 C=1 B=1: write-back no write-allocate (Linux UP default) */
#define VA_WT(pa)	((pa) - BASE + 0x30000000U)	/* TEX=000 C=1 B=0: write-through */
#define SEC_WB		0x1c0eU
#define SEC_NC		0x1c02U
#define SEC_NWA		0x0c0eU
#define SEC_WT		0x0c0aU
#define SEC_DEV		0x0c16U

#ifndef TESTMASK
#define TESTMASK 0x3ffU	/* bit t = run test t (bisect, #79); 0 = setup only */
#endif

char trace[TRACE_SZ] __attribute__((aligned(4096), section(".trace")));
static volatile char *const TR = (volatile char *)VA_NC((uint32_t)trace);

enum { A_WB, A_NWA, A_WT, A_NC, B_WB, B_NWA, B_WT, B_NC, C_EVICT, D_CONF, NT };
static const char *const names[NT] = { "A_wb ", "A_nwa", "A_wt ", "A_nc ", "B_wb ", "B_nwa", "B_wt ", "B_nc ", "C_evi", "D_con" };
static uint32_t off_m[NT], off_r[NT], off_pass, off_first;
static volatile uint32_t sink;
static uint32_t pt_bad, off_rfirst, rfirst_n;
static uint32_t cnt_m[NT], cnt_r[NT], way_bytes, first_n;

static inline void dsb(void)
{
	__asm__ volatile("dsb" ::: "memory");
}

static void dcache_setway(int op)	/* 0 invalidate, 2 clean+invalidate */
{
	uint32_t ccsidr, sets, ways, lshift, wshift, s, w;

	__asm__ volatile("mcr p15, 2, %0, c0, c0, 0\n\tisb" : : "r"(0));	/* CSSELR = L1 D */
	__asm__ volatile("mrc p15, 1, %0, c0, c0, 0" : "=r"(ccsidr));
	lshift = (ccsidr & 7) + 4;
	ways = ((ccsidr >> 3) & 0x3ff) + 1;
	sets = ((ccsidr >> 13) & 0x7fff) + 1;
	wshift = __builtin_clz(ways - 1);
	way_bytes = sets << lshift;
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
#ifdef PT_SRAM	/* table walks stay on-chip: TTBCR.N = 2 -> 1 GiB of VA, 1024 entries, 4 KiB aligned */
	volatile uint32_t *pt = (volatile uint32_t *)(HW_A2_SRAM_BASE + 0x2000U);
#define PT_PA		(HW_A2_SRAM_BASE + 0x2000U)
#define PT_N		2
#define PT_ENTRIES	1024
#else
	volatile uint32_t *pt = (volatile uint32_t *)(BASE + PT_OFF);	/* MMU still off: direct physical writes */
#define PT_PA		(BASE + PT_OFF)
#define PT_N		0
#define PT_ENTRIES	4096
#endif
	uint32_t i, sctlr;

	for (i = 0; i < PT_ENTRIES; i++)
		pt[i] = 0;
	for (i = 0; i < HW_A2_CARVEOUT_SIZE >> 20; i++) {
		uint32_t pa = BASE + (i << 20);

#ifdef NO_ALIAS
		pt[pa >> 20] = pa | (pa >= HW_A2_SHARED_BASE ? SEC_NC : SEC_WB);
#else
		pt[pa >> 20] = pa | SEC_WB;
		pt[VA_NC(pa) >> 20] = pa | SEC_NC;
		pt[VA_NWA(pa) >> 20] = pa | SEC_NWA;
		pt[VA_WT(pa) >> 20] = pa | SEC_WT;
#endif
	}
	for (i = 0x180; i < 0x1c0; i++)
		pt[i] = (i << 20) | SEC_DEV;
	dsb();
#if defined(PT_RB) && !defined(NO_ALIAS)
	/* MMU off: every load crosses PCIe, so it cannot pass the posted table writes (read-after-write, as 3dd7608) */
	{
		uint32_t bad = 0;

		for (i = 0; i < HW_A2_CARVEOUT_SIZE >> 20; i++) {
			uint32_t pa = BASE + (i << 20);

			bad += pt[pa >> 20] != (pa | SEC_WB);
			bad += pt[VA_NC(pa) >> 20] != (pa | SEC_NC);
			bad += pt[VA_NWA(pa) >> 20] != (pa | SEC_NWA);
			bad += pt[VA_WT(pa) >> 20] != (pa | SEC_WT);
		}
		pt_bad = bad;
		dsb();
	}
#endif
	dcache_setway(0);
	__asm__ volatile("mcr p15, 0, %0, c8, c7, 0" : : "r"(0));		/* TLBIALL */
	__asm__ volatile("mcr p15, 0, %0, c7, c5, 6" : : "r"(0));		/* BPIALL */
	__asm__ volatile("mcr p15, 0, %0, c2, c0, 2" : : "r"(PT_N));	/* TTBCR.N */
	__asm__ volatile("mcr p15, 0, %0, c2, c0, 0" : : "r"(PT_PA));	/* TTBR0, walks non-cacheable */
	__asm__ volatile("mcr p15, 0, %0, c3, c0, 0" : : "r"(3));		/* DACR: domain 0 manager */
	__asm__ volatile("isb");
	__asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(sctlr));
	sctlr |= 1U;	/* M */
#ifndef NO_DCACHE
	sctlr |= 4U;	/* C */
#endif
#ifndef NO_ICACHE
	sctlr |= 0x1000U;	/* I */
#endif
	__asm__ volatile("mcr p15, 0, %0, c1, c0, 0\n\tisb" : : "r"(sctlr));
}

/* --- trace text --- */

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

static void miss(int t, uint32_t addr, uint32_t exp, uint32_t got)
{
	cnt_m[t]++;
	if (first_n < 4) {
		uint32_t o = off_first + first_n * 36;

		hex32(o, t);
		hex32(o + 9, addr);
		hex32(o + 18, exp);
		hex32(o + 27, got);
		first_n++;
	}
}

static inline uint32_t val(uint32_t pass, uint32_t i)
{
	return (i * 0x9e3779b1U) ^ (pass * 0x85ebca6bU + 0x1234567U);
}

/* memory truth: write back everything, then read the stored words through the non-cacheable alias */
static void check_mem(int t, uint32_t pass, uint32_t step)
{
	volatile uint32_t *m = (volatile uint32_t *)VA_NC(BASE + TBUF_OFF);	/* NO_ALIAS: WB view, lines were just invalidated */
	uint32_t i;

	dcache_setway(2);
	for (i = 0; i < TBYTES / 4; i += step) {
		uint32_t g = m[i];

		if (g != val(pass, i)) {
			cnt_r[t]++;
			if (rfirst_n < 4) {	/* re-read: transient (return-data mix-up) or stable (wrong page / lost write)? */
				uint32_t o = off_rfirst + rfirst_n * 54, g2;
				volatile uint32_t *pte = (volatile uint32_t *)VA_NC(BASE + PT_OFF);

				dsb();
				g2 = m[i];
				hex32(o, t);
				hex32(o + 9, i);
				hex32(o + 18, val(pass, i));
				hex32(o + 27, g);
				hex32(o + 36, g2);
				hex32(o + 45, pte[VA_NC(BASE + TBUF_OFF) >> 20]);
				rfirst_n++;
			}
		}
	}
}

/* A: store word, load it back at once */
static void test_a(int t, volatile uint32_t *p, uint32_t pass)
{
	uint32_t i, v, x;

	for (i = 0; i < TBYTES / 4; i++) {
		v = val(pass, i);
		p[i] = v;
		x = p[i];
		if (x != v)
			miss(t, (uint32_t)&p[i], v, x);
	}
}

/* B: store the 8 words of a line, then load them in reverse (a line fill after narrow stores) */
static void test_b(int t, volatile uint32_t *p, uint32_t pass)
{
	uint32_t l, j, v, x;

	for (l = 0; l < TBYTES / LINE; l++) {
		for (j = 0; j < 8; j++)
			p[l * 8 + j] = val(pass, l * 8 + j);
		for (j = 8; j-- > 0;) {
			v = val(pass, l * 8 + j);
			x = p[l * 8 + j];
			if (x != v)
				miss(t, (uint32_t)&p[l * 8 + j], v, x);
		}
	}
}

/* C: store, clean+invalidate that line, DSB, reload (write-back and refill of the same line back to back) */
static void test_c(int t, volatile uint32_t *p, uint32_t pass)
{
	uint32_t i, v, x;

	for (i = 0; i < TBYTES / 4; i++) {
		v = val(pass, i);
		p[i] = v;
		__asm__ volatile("mcr p15, 0, %0, c7, c14, 1\n\tdsb" : : "r"(&p[i]) : "memory");	/* DCCIMVAC */
		x = p[i];
		if (x != v)
			miss(t, (uint32_t)&p[i], v, x);
	}
}

/* D: store a word, evict its line by loading 9 lines of the same L1 set, reload it (capacity write-back then refill) */
static void test_d(int t, volatile uint32_t *p, uint32_t pass)
{
	uint32_t l, k, v, x, nl = TBYTES / LINE;

	for (l = 0; l < nl; l++) {
		uint32_t w = l * 8;

		v = val(pass, w);
		p[w] = v;
		for (k = 1; k <= 9; k++)
			sink += p[(w + k * (way_bytes / 4)) % (TBYTES / 4)];
		x = p[w];
		if (x != v)
			miss(t, (uint32_t)&p[w], v, x);
	}
}

static void run(int t, uint32_t pass)
{
	uint32_t pa = BASE + TBUF_OFF;

	switch (t) {
	case A_WB: test_a(t, (volatile uint32_t *)VA_WB(pa), pass); break;
	case A_NWA: test_a(t, (volatile uint32_t *)VA_NWA(pa), pass); break;
	case A_WT: test_a(t, (volatile uint32_t *)VA_WT(pa), pass); break;
	case A_NC: test_a(t, (volatile uint32_t *)VA_NC(pa), pass); break;
	case B_WB: test_b(t, (volatile uint32_t *)VA_WB(pa), pass); break;
	case B_NWA: test_b(t, (volatile uint32_t *)VA_NWA(pa), pass); break;
	case B_WT: test_b(t, (volatile uint32_t *)VA_WT(pa), pass); break;
	case B_NC: test_b(t, (volatile uint32_t *)VA_NC(pa), pass); break;
	case C_EVICT: test_c(t, (volatile uint32_t *)VA_WB(pa), pass); break;
	default: test_d(t, (volatile uint32_t *)VA_WB(pa), pass); break;
	}
	check_mem(t, pass, t == D_CONF ? 8 : 1);
	dcache_setway(2);
}

int main(void)
{
	uint32_t t, off, pass = 0, v;

	mmu_on();
#ifdef NIC_ISS
	*(volatile uint32_t *)A2_NIC_PCIE0_S0_ISS = NIC_ISS;
#endif
#ifndef DIAG_SET
#define DIAG_SET 0
#endif
	dcache_setway(2);	/* sets way_bytes */
	off = put(0, "a2fw rawtest v1 actlr=");
	__asm__ volatile("mrc p15, 0, %0, c1, c0, 1" : "=r"(v));
	hex32(off, v);
	off = put(off + 8, " way=");
	hex32(off, way_bytes);
	off = put(off + 8, " diag:");
	hex32(off, a2_diag_rmw(DIAG_SET));
	off = put(off + 8, " nic:");
#ifdef NIC_ISS
	hex32(off, *(volatile uint32_t *)A2_NIC_PCIE0_S0_ISS);
#else
	hex32(off, 0);
#endif
	off = put(off + 8, " ptbad:");
	hex32(off, pt_bad);
	off = put(off + 8, "\npass=");
	off_pass = off;
	off = put(off + 8, " (m = wrong loads, r = wrong words in memory)\n");
	for (t = 0; t < NT; t++) {
		off = put(off, names[t]);
		off = put(off, " m=");
		off_m[t] = off;
		off = put(off + 8, " r=");
		off_r[t] = off;
		off = put(off + 8, "\n");
	}
	off = put(off, "first (test addr exp got):\n");
	off_first = off;
	for (t = 0; t < 4; t++)
		off = put(off, "00000000 00000000 00000000 00000000\n");
	off = put(off, "rfirst (test idx exp got reread pte_nc):\n");
	off_rfirst = off;
	for (t = 0; t < 4; t++)
		off = put(off, "00000000 00000000 00000000 00000000 00000000 00000000\n");
	for (;;) {
		for (t = 0; t < NT; t++) {
			if (!(TESTMASK >> t & 1))
				continue;
			run(t, pass);
			hex32(off_m[t], cnt_m[t]);
			hex32(off_r[t], cnt_r[t]);
		}
		pass++;
		hex32(off_pass, pass);
	}
}
