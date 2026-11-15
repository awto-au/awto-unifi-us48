// SPDX-License-Identifier: GPL-2.0
/*
 * a2 PCIe bus test firmware (#80): measures a2 -> host DDR (carveout, across PCIe) and checks data integrity.
 * Loops forever; results struct at carveout + BT_RES_OFF (a2fw/bustest.h), text in trace0. Doc: docs/pcie-harness.md.
 * MMU on (default): carveout identity = write-back cached, alias ALIAS = same memory non-cacheable, peripherals = device.
 * -DNO_MMU: strongly-ordered accesses only (cached columns stay 0); fallback if the MMU bring-up misbehaves.
 */
#include "rsc.h"
#include "bustest.h"

#define BASE		HW_A2_CARVEOUT_BASE
#define ALIAS		0x20000000U
#define GT(r)		(*(volatile uint32_t *)(HW_GTIMER_BASE + HW_GTIMER_##r))
#define LINE		32U

#ifdef NO_MMU
#define VA_NC(pa)	(pa)
#else
#define VA_NC(pa)	((pa) - BASE + ALIAS)
#endif

static volatile struct bt_results *const R = (volatile struct bt_results *)VA_NC(BASE + BT_RES_OFF);
static volatile char *const TR = (volatile char *)VA_NC((uint32_t)trace);	/* written via the alias only */
char trace[TRACE_SZ] __attribute__((aligned(4096), section(".trace")));	/* a2_shared, see build_a2fw.py */
static uint32_t sink;

static inline uint32_t now(void)
{
	return GT(COUNTER_LO);
}

/* --- cache and MMU --- */

static inline void dsb(void)
{
	__asm__ volatile("dsb" ::: "memory");
}

static void dcache_setway(int op)	/* 0 invalidate, 1 clean, 2 clean+invalidate */
{
	uint32_t ccsidr, sets, ways, lshift, wshift, s, w;

	__asm__ volatile("mcr p15, 2, %0, c0, c0, 0\n\tisb" : : "r"(0));	/* CSSELR = L1 D */
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
			else if (op == 1)
				__asm__ volatile("mcr p15, 0, %0, c7, c10, 2" : : "r"(v));
			else
				__asm__ volatile("mcr p15, 0, %0, c7, c14, 2" : : "r"(v));
		}
	dsb();
}

static void range_op(uint32_t va, uint32_t n, int inval)
{
	uint32_t a;

	for (a = va & ~(LINE - 1); a < va + n; a += LINE) {
		if (inval)
			__asm__ volatile("mcr p15, 0, %0, c7, c6, 1" : : "r"(a));	/* DCIMVAC */
		else
			__asm__ volatile("mcr p15, 0, %0, c7, c10, 1" : : "r"(a));	/* DCCMVAC */
	}
	dsb();
}

#ifndef NO_MMU
#define SEC_WB		0x1c0eU	/* section, AP=3, TEX=001 C=1 B=1: normal write-back write-allocate */
#define SEC_NC		0x1c02U	/* TEX=001 C=0 B=0: normal non-cacheable */
#define SEC_DEV		0x0c16U	/* TEX=000 C=0 B=1, XN: shared device */

static void mmu_on(void)
{
	volatile uint32_t *pt = (volatile uint32_t *)(BASE + BT_PT_OFF);	/* MMU still off: direct physical writes */
	uint32_t i, sctlr;

	for (i = 0; i < 4096; i++)
		pt[i] = 0;
	for (i = 0; i < HW_A2_CARVEOUT_SIZE >> 20; i++) {
		pt[(BASE >> 20) + i] = (BASE + (i << 20)) | SEC_WB;
		pt[(ALIAS >> 20) + i] = (BASE + (i << 20)) | SEC_NC;
	}
	for (i = 0x180; i < 0x1c0; i++)		/* 0x18000000-0x1bffffff: CCB, timers, SRAM */
		pt[i] = (i << 20) | SEC_DEV;
	dsb();
	dcache_setway(0);
	__asm__ volatile("mcr p15, 0, %0, c8, c7, 0" : : "r"(0));		/* TLBIALL */
	__asm__ volatile("mcr p15, 0, %0, c7, c5, 6" : : "r"(0));		/* BPIALL */
	__asm__ volatile("mcr p15, 0, %0, c2, c0, 2" : : "r"(0));		/* TTBCR = 0 */
	__asm__ volatile("mcr p15, 0, %0, c2, c0, 0" : : "r"(BASE + BT_PT_OFF));	/* TTBR0, walks non-cacheable */
	__asm__ volatile("mcr p15, 0, %0, c3, c0, 0" : : "r"(3));		/* DACR: domain 0 manager */
	__asm__ volatile("isb");
	__asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(sctlr));
	sctlr |= 1U | 4U | 0x1000U;	/* M, C, I */
	__asm__ volatile("mcr p15, 0, %0, c1, c0, 0\n\tisb" : : "r"(sctlr));
}
#endif

/* --- measurements: each returns timer ticks --- */

static uint32_t rd_burst(uint32_t va, uint32_t n)
{
	uint32_t t0 = now();

	__asm__ volatile("1: ldmia %0!, {r4-r11}\n\tsubs %1, %1, #32\n\tbne 1b"
			 : "+r"(va), "+r"(n) : : "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "cc", "memory");
	return now() - t0;
}

static uint32_t wr_burst(uint32_t va, uint32_t n)
{
	uint32_t t0 = now();

	__asm__ volatile("1: stmia %0!, {r4-r11}\n\tsubs %1, %1, #32\n\tbne 1b"
			 : "+r"(va), "+r"(n) : : "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "cc", "memory");
	return now() - t0;
}

static uint32_t cpy_burst(uint32_t dst, uint32_t src, uint32_t n)
{
	uint32_t t0 = now();

	__asm__ volatile("1: ldmia %1!, {r4-r11}\n\tstmia %0!, {r4-r11}\n\tsubs %2, %2, #32\n\tbne 1b"
			 : "+r"(dst), "+r"(src), "+r"(n) : : "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "cc", "memory");
	return now() - t0;
}

/* word-wide, one bus transaction per access (non-cacheable) */
static uint32_t rd_words(uint32_t va, uint32_t n)
{
	volatile uint32_t *p = (volatile uint32_t *)va;
	uint32_t i, s = 0, t0 = now();

	for (i = 0; i < n / 4; i++)
		s += p[i];
	sink += s;
	return now() - t0;
}

static uint32_t wr_words(uint32_t va, uint32_t n)
{
	volatile uint32_t *p = (volatile uint32_t *)va;
	uint32_t i, t0 = now();

	for (i = 0; i < n / 4; i++)
		p[i] = i;
	dsb();
	return now() - t0;
}

static uint32_t cpy_words(uint32_t dst, uint32_t src, uint32_t n)
{
	volatile uint32_t *d = (volatile uint32_t *)dst, *s = (volatile uint32_t *)src;
	uint32_t i, t0 = now();

	for (i = 0; i < n / 4; i++)
		d[i] = s[i];
	dsb();
	return now() - t0;
}

/* BT_LAT_N reads, each address depends on the previous value (and 0), timer read around each */
static void latency(uint32_t va, uint32_t stride, uint32_t ovh, uint32_t *sum, uint32_t *min, uint32_t *max)
{
	volatile uint32_t *p = (volatile uint32_t *)va;
	uint32_t i, t0, t1, v, dt;

	*sum = 0;
	*min = 0xffffffffU;
	*max = 0;
	for (i = 0; i < BT_LAT_N; i++) {
		t0 = now();
		v = *p;
		__asm__ volatile("and %0, %0, #0" : "+r"(v));	/* data dependency: the timer read waits for the load */
		t1 = now() + v;
		dt = t1 - t0 - ovh;
		*sum += dt;
		if (dt < *min)
			*min = dt;
		if (dt > *max)
			*max = dt;
		p = (volatile uint32_t *)(va + (i + 1) * stride);
	}
}

__asm__(".section .text.ifetch, \"ax\"\n\t.balign 32\n\t.globl ifetch_blk\n\t.type ifetch_blk, %function\n"
	"ifetch_blk:\n\t.rept 2047\n\tnop\n\t.endr\n\tbx lr\n\t.text\n");
extern void ifetch_blk(void);

static uint32_t ifetch_run(void)
{
	uint32_t t0 = now();

	ifetch_blk();
	return now() - t0;
}

/* --- integrity --- */

static uint32_t pat_next(int kind, uint32_t i, uint32_t pa, uint32_t *s)
{
	switch (kind) {
	case 0:
		return 1U << (i & 31);		/* walking 1 */
	case 1:
		return ~(1U << (i & 31));	/* walking 0 */
	case 2:
		return pa + i * 4;		/* address in data */
	default:
		*s = ((*s << 1) | (((*s >> 30) ^ (*s >> 27)) & 1)) & 0x7fffffffU;	/* PRBS31 x^31 + x^28 + 1 */
		return *s ^ (*s << 31);
	}
}

static void miss(uint32_t addr, uint32_t exp, uint32_t got)
{
	uint32_t n = R->miss_n;

	R->mismatch++;
	if (n < BT_MISS_MAX) {
		volatile uint32_t *e = &R->miss0_addr + 5 * n;

		e[0] = addr;
		e[1] = exp;
		e[2] = got;
		e[3] = R->pass;
		e[4] = now();
		R->miss_n = n + 1;
	}
}

/* write + read-verify BT_UBYTES (non-cacheable word accesses) or BT_CBYTES (cached bursts: clean, invalidate, read) */
static void sweep(uint32_t k)
{
	int cached = (k & 1) && R->mmu, kind = (k >> 1) & 3;
	uint32_t n = cached ? BT_CBYTES : BT_UBYTES, pa = BASE + BT_SBUF_OFF, i, s = 0x1234567U, v, t0 = now();
	volatile uint32_t *w = (volatile uint32_t *)(cached ? pa : VA_NC(pa));

	for (i = 0; i < n / 4; i++)
		w[i] = pat_next(kind, i, pa, &s);
	dsb();
	if (cached) {
		range_op(pa, n, 0);
		range_op(pa, n, 1);
	}
	s = 0x1234567U;
	for (i = 0; i < n / 4; i++) {
		uint32_t e = pat_next(kind, i, pa, &s);

		v = w[i];
		if (v != e)
			miss(pa + i * 4, e, v);
	}
	if (cached)
		range_op(pa, n, 1);	/* leave no lines behind that the alias view would not see */
	R->sweep_pass = k;
	R->sweep_words = n / 4;
	R->sweep_ticks = now() - t0;
}

/* a2 fills XBUF (a1 verifies); a2 verifies a1's YBUF when y_seq moved, in both views */
static void xy(void)
{
	uint32_t xpa = BASE + BT_XBUF_OFF, ypa = BASE + BT_YBUF_OFF, i, seq = R->y_seq, seed, v;
	volatile uint32_t *x = (volatile uint32_t *)VA_NC(xpa), *y = (volatile uint32_t *)VA_NC(ypa);

	for (i = 0; i < BT_XY_SIZE / 4; i++)
		x[i] = bt_xpat(i);
	dsb();
	R->x_ready = 1;
	if (seq == R->y_ack)
		return;
	seed = R->y_seed;
	for (i = 0; i < BT_XY_SIZE / 4; i++) {
		v = y[i];
		if (v != bt_ypat(seed, i)) {
			R->y_mismatch++;
			miss(ypa + i * 4, bt_ypat(seed, i), v);
		}
	}
	if (R->mmu) {
		volatile uint32_t *yc = (volatile uint32_t *)ypa;

		range_op(ypa, BT_XY_SIZE, 1);
		for (i = 0; i < BT_XY_SIZE / 4; i++) {
			v = yc[i];
			if (v != bt_ypat(seed, i)) {
				R->y_mismatch++;
				miss(ypa + i * 4, bt_ypat(seed, i), v);
			}
		}
		range_op(ypa, BT_XY_SIZE, 1);
	}
	R->y_ack = seq;
}

/* --- trace text: fixed-width hex fields rewritten in place --- */

static unsigned int put(unsigned int off, const char *s)
{
	while (*s)
		TR[off++] = *s++;
	return off;
}

static void hex32(unsigned int off, uint32_t v)
{
	int i;

	for (i = 7; i >= 0; i--, v >>= 4)
		TR[off + i] = "0123456789abcdef"[v & 15];
}

int main(void)
{
	uint32_t i, off, o_pass, o_hb, o_mis, o_ymis, ovh, t, rb = BASE + BT_RBUF_OFF;

	GT(CONTROL) = 1;
	for (i = 0; i < sizeof(struct bt_results) / 4; i++)
		((volatile uint32_t *)R)[i] = 0;
#ifndef NO_MMU
	mmu_on();
	R->mmu = 1;
#endif
	__asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(i));
	R->sctlr = i;
	R->version = BT_VERSION;
	off = put(0, "a2fw bustest v1 ");
	off = put(off, R->mmu ? "mmu\nsctlr=" : "nommu\nsctlr=");
	hex32(off, i);
	off = put(off + 8, "\npass=");
	o_pass = off;
	off = put(off + 8, " hb=");
	o_hb = off;
	off = put(off + 8, " mism=");
	o_mis = off;
	off = put(off + 8, " ymis=");
	o_ymis = off;
	TR[o_ymis + 8] = '\n';
	R->magic = BT_MAGIC;	/* last: a1 trusts the struct from here */

	t = now();
	ovh = now() - t;	/* one timer read (device access) */
	R->timer_ovh = ovh;

	for (;;) {
		R->phase = 1;
		if (R->mmu) {
			dcache_setway(2);
			R->rd_c = rd_burst(rb, BT_RBUF_SIZE);
			R->hb++;
			R->wr_c = wr_burst(rb, BT_RBUF_SIZE);
			dcache_setway(2);
			R->cpy_c = cpy_burst(rb + BT_CBYTES, rb, BT_CBYTES);
			dcache_setway(2);
			R->hb++;
		}
		R->phase = 2;
		R->rd_u = rd_words(VA_NC(rb), BT_UBYTES);
		R->hb++;
		R->wr_u = wr_words(VA_NC(rb), BT_UBYTES);
		R->hb++;
		R->cpy_u = cpy_words(VA_NC(rb + BT_UBYTES), VA_NC(rb), BT_UBYTES);
		R->hb++;
		R->phase = 3;
		{
			uint32_t sum, mn, mx;

			latency(VA_NC(rb), 4, ovh, &sum, &mn, &mx);	/* non-cacheable word read round trip */
			R->lat_u_sum = sum;
			R->lat_u_min = mn;
			R->lat_u_max = mx;
			if (R->mmu) {
				dcache_setway(2);
				latency(rb, LINE, ovh, &sum, &mn, &mx);	/* cold cached line fill */
				R->lat_line_sum = sum;
			}
		}
		R->hb++;
		R->phase = 4;
		__asm__ volatile("mcr p15, 0, %0, c7, c5, 0\n\tmcr p15, 0, %0, c7, c5, 6\n\tdsb\n\tisb" : : "r"(0));
		R->ifetch_cold = ifetch_run();
		R->ifetch_warm = ifetch_run();
		R->hb++;
		R->phase = 5;
		xy();
		R->hb++;
		R->phase = 6;
		sweep(R->pass & 7);
		R->pass++;
		R->hb++;
		R->now = now();
		hex32(o_pass, R->pass);
		hex32(o_hb, R->hb);
		hex32(o_mis, R->mismatch);
		hex32(o_ymis, R->y_mismatch);
	}
}
