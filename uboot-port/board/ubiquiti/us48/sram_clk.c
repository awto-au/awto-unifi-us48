// SPDX-License-Identifier: GPL-2.0+
/*
 * SRAM-resident clock switch with rollback (docs/sram-clock-switch.md, #17). UNTESTED.
 * us48_sram_exec() is position independent: no calls, no globals, no jump tables (Makefile flags);
 * everything it touches comes through its area pointer.
 */

#include <cpu_func.h>
#include <errno.h>
#include <asm/io.h>
#include "us48_sram_clk.h"

#define SRAM_TEXT __attribute__((section(".text.us48_sram"), __noinline__, aligned(16)))

/* 1 = ok, 0 = poll expired, -1 = check mismatch or bad op */
static inline __attribute__((always_inline)) int sram_step(const struct us48_clk_op *o, u32 *lastv)
{
	volatile u32 *p = (volatile u32 *)o->addr;
	u32 v, n;

	if (o->op == US48_OP_WR) {
		*p = o->a;
		return 1;
	}
	if (o->op == US48_OP_RMW) {
		*p = (*p & ~o->a) | o->b;
		return 1;
	}
	if (o->op == US48_OP_CHECK) {
		v = *p;
		*lastv = v;
		return (v & o->a) == o->b ? 1 : -1;
	}
	if (o->op == US48_OP_POLL_SET || o->op == US48_OP_POLL_CLR) {
		if (!o->b || o->b > US48_POLL_MAX)
			return -1;
		for (n = o->b; n; n--) {
			v = *p;
			*lastv = v;
			if (o->op == US48_OP_POLL_SET ? (v & o->a) == o->a : !(v & o->a))
				return 1;
		}
		return 0;
	}
	return -1;
}

SRAM_TEXT int us48_sram_exec(struct us48_clk_area *ar)
{
	const struct us48_clk_op *ops = ar->cmd.ops;
	u32 nf = ar->cmd.nfwd, nr = ar->cmd.nrb, i, lastv = 0;
	int r = 1, rbfail = 0;

	if (ar->cmd.magic != US48_CLK_MAGIC || nf > US48_CLK_MAXFWD || nr > US48_CLK_MAXRB) {
		ar->status = US48_ST_BADCMD;
		return US48_ST_BADCMD;
	}
	ar->status = US48_ST_RUNNING;
	ar->fail_idx = 0;
	for (i = 0; i < nf; i++) {
		r = sram_step(&ops[i], &lastv);
		if (r != 1)
			break;
	}
	if (r != 1) {
		ar->fail_idx = i + 1;
		ar->fail_val = lastv;
		for (i = 0; i < nr; i++)
			if (sram_step(&ops[nf + i], &lastv) != 1)
				rbfail = 1;
	}
	__asm__ volatile("dsb sy" ::: "memory");
	i = !rbfail ? (r == 1 ? US48_ST_OK : r == 0 ? US48_ST_ROLLED_POLL : US48_ST_ROLLED_CHECK) :
		      US48_ST_RBFAIL;
	ar->status = i;
	return i;
}

/* Must stay last in the section: marks the end of the code to copy */
SRAM_TEXT void us48_sram_end(void)
{
}

/* DDR side from here */

int us48_sram_clk_run(const struct us48_clk_cmd *cmd)
{
	volatile u32 *dst = (volatile u32 *)US48_SRAM_BASE;
	const u32 *src = (const u32 *)((uintptr_t)us48_sram_exec & ~1UL);
	struct us48_clk_area *ar = (struct us48_clk_area *)(US48_SRAM_BASE + US48_SRAM_AREA_OFF);
	volatile u32 *adst = (volatile u32 *)ar;
	const u32 *asrc = (const u32 *)cmd;
	uintptr_t size = ((uintptr_t)us48_sram_end & ~1UL) - (uintptr_t)src;
	int (*fn)(struct us48_clk_area *);
	u32 cpsr, i, st;

	if (size > US48_SRAM_CODE_MAX || cmd->magic != US48_CLK_MAGIC || cmd->nfwd > US48_CLK_MAXFWD ||
	    cmd->nrb > US48_CLK_MAXRB)
		return -EINVAL;

	/* word copies only: SRAM is mapped device memory (sram.md), no unaligned access */
	for (i = 0; i < size / 4; i++)
		dst[i] = src[i];
	adst[0] = US48_ST_PENDING;
	adst[1] = adst[2] = adst[3] = 0;
	for (i = 0; i < sizeof(*cmd) / 4; i++)
		adst[4 + i] = asrc[i];
	flush_dcache_range(US48_SRAM_BASE, US48_SRAM_BASE + 0x1000);
	invalidate_icache_all();

	fn = (int (*)(struct us48_clk_area *))(US48_SRAM_BASE | ((uintptr_t)us48_sram_exec & 1));
	__asm__ volatile("mrs %0, cpsr\n cpsid if" : "=r"(cpsr)::"memory");
	fn(ar);
	__asm__ volatile("msr cpsr_c, %0" ::"r"(cpsr) : "memory");

	st = ar->status;
	if (st == US48_ST_OK)
		return 0;
	if (st == US48_ST_ROLLED_POLL)
		return -ETIMEDOUT;
	if (st == US48_ST_ROLLED_CHECK)
		return -EPROTO;
	if (st == US48_ST_BADCMD)
		return -EINVAL;
	return -EIO;
}

void us48_sram_clk_fail_info(u32 *idx, u32 *val)
{
	struct us48_clk_area *ar = (struct us48_clk_area *)(US48_SRAM_BASE + US48_SRAM_AREA_OFF);

	*idx = ar->fail_idx;
	*val = ar->fail_val;
}

/* Builders */

static void op(struct us48_clk_cmd *c, u32 *n, u32 code, u32 addr, u32 a, u32 b)
{
	struct us48_clk_op *o = &c->ops[(*n)++];

	o->op = code;
	o->addr = addr;
	o->a = a;
	o->b = b;
}

/* GO: write policy, GO|GO_AC, poll GO clear. 4096 reads: expected ~10 us (few hundred 25 MHz cycles, I), ~100x at ~100 MHz CPU; expiry = rollback */
static void go(struct us48_clk_cmd *c, u32 *n, u32 policy)
{
	op(c, n, US48_OP_WR, CCU_POLICY_FREQ, policy, 0);
	op(c, n, US48_OP_WR, CCU_POLICY_CTL, 3, 0);
	op(c, n, US48_OP_POLL_CLR, CCU_POLICY_CTL, 1, 4096);
}

int us48_sram_clk_cpu(int mhz)
{
	struct us48_clk_cmd c = { .magic = US48_CLK_MAGIC };
	u32 n = 0, b = (readl(CCU_PLLARMB) & 0xfff00000) | 1;
	u32 savb = readl(CCU_PLLARMB);

	if (mhz != 800 && !US48_RELOCK_OK(mhz))
		return -EINVAL;
	op(&c, &n, US48_OP_WR, CCU_WR_ACCESS, 0xa5a501, 0);
	if (mhz == 800) {
		go(&c, &n, 0x87070707);			/* E1 */
	} else {
		go(&c, &n, 0x82020202);			/* E2: sys clk while relocking */
		op(&c, &n, US48_OP_WR, CCU_PLLARMB, b, 0);
		op(&c, &n, US48_OP_WR, CCU_PLLARMA, US48_PLLARMA(mhz, 0), 0);	/* ndiv, pdiv 1, soft_resetb */
		/* lock: expected tens of us (I, unmeasured), 2000 reads; expiry = rollback */
		op(&c, &n, US48_OP_POLL_SET, CCU_PLLARMA, CCU_PLLARMA_LOCKED, 2000);
		op(&c, &n, US48_OP_WR, CCU_PLLARMA, US48_PLLARMA(mhz, 1), 0);	/* soft_post_resetb */
		go(&c, &n, 0x87070707);
	}
	op(&c, &n, US48_OP_CHECK, CCU_POLICY_DBG, 7 << 12, 7 << 12);	/* fid 7 active */
	op(&c, &n, US48_OP_WR, CCU_WR_ACCESS, 0, 0);
	c.nfwd = n;

	/* rollback: unlock, sys clk, VCO + PLLARMB back, wait lock, fid 6, verify, lock */
	op(&c, &n, US48_OP_WR, CCU_WR_ACCESS, 0xa5a501, 0);
	go(&c, &n, 0x82020202);
	if (mhz != 800) {
		op(&c, &n, US48_OP_WR, CCU_PLLARMB, savb, 0);
		op(&c, &n, US48_OP_WR, CCU_PLLARMA, 0x11004003, 0);	/* 400 row: ndiv 64, VCO 1600 */
		op(&c, &n, US48_OP_POLL_SET, CCU_PLLARMA, CCU_PLLARMA_LOCKED, 2000);
	}
	go(&c, &n, 0x86060606);
	op(&c, &n, US48_OP_CHECK, CCU_POLICY_DBG, 7 << 12, 6 << 12);
	op(&c, &n, US48_OP_WR, CCU_WR_ACCESS, 0, 0);
	c.nrb = n - c.nfwd;

	return us48_sram_clk_run(&c);
}

int us48_sram_clk_ddrpll(u32 mhz)
{
	/* Needs shmoo/retrain from SRAM, not available: docs/ddr.md experiments 1-2 */
	return -ENOSYS;
}

#ifdef CONFIG_US48_SRAM_CLK_GENPLL
/* DISABLED by default. Sequence: docs/clocks.md E5 ([DL] genpll.c). ch3 = AXI, ch4 = see E5. UNTESTED. */
int us48_sram_clk_axi(u32 ch3_mdiv, u32 ch4_mdiv)
{
	struct us48_clk_cmd c = { .magic = US48_CLK_MAGIC };
	u32 n = 0, mask = 0xff << 8, val = ch3_mdiv << 8, le = 0x08;
	u32 s0 = readl(GENPLL_CTRL0), s1 = readl(GENPLL_CTRL1), s2 = readl(GENPLL_CTRL2);

	/* E5 table values only: 5 = 400, 7 = 285, 8 = 250, 10 = 200 (now), 20 = 100 MHz */
	if (ch3_mdiv != 5 && ch3_mdiv != 7 && ch3_mdiv != 8 && ch3_mdiv != 10 && ch3_mdiv != 20)
		return -EINVAL;
	if (ch4_mdiv) {
		mask |= 0xff << 16;
		val |= ch4_mdiv << 16;
		le = 0x18;
	}
	/* lock: expected <= ~100 us with FAST_LOCK (I), 4000 reads x ~0.1-0.2 us ~ 4-8x; expiry = rollback */
#define GP_LOAD(l) \
	op(&c, &n, US48_OP_RMW, GENPLL_CTRL3, 0x3f << 16, 0); \
	op(&c, &n, US48_OP_RMW, GENPLL_CTRL3, 0, BIT(28)); \
	op(&c, &n, US48_OP_RMW, GENPLL_CTRL3, 0, (l) << 16); \
	op(&c, &n, US48_OP_POLL_SET, GENPLL_STATUS, 1, 4000)
	op(&c, &n, US48_OP_RMW, GENPLL_CTRL0, 0, BIT(28));	/* FAST_LOCK */
	op(&c, &n, US48_OP_RMW, GENPLL_CTRL2, mask, val);
	GP_LOAD(le);
	op(&c, &n, US48_OP_CHECK, GENPLL_CTRL2, mask, val);
	c.nfwd = n;

	op(&c, &n, US48_OP_WR, GENPLL_CTRL0, s0, 0);
	op(&c, &n, US48_OP_WR, GENPLL_CTRL1, s1, 0);
	op(&c, &n, US48_OP_WR, GENPLL_CTRL2, s2, 0);
	GP_LOAD(le);
	c.nrb = n - c.nfwd;
#undef GP_LOAD
	return us48_sram_clk_run(&c);
}
#else
int us48_sram_clk_axi(u32 ch3_mdiv, u32 ch4_mdiv)
{
	return -EPERM;
}
#endif
