// SPDX-License-Identifier: GPL-2.0
/*
 * bcm-cmic HR2 unit-0 cold init: ring map + MIIM rate, then the filtered stock write list (generated).
 * Shared by awto-uboot and the Linux module (us48_cmic unit 0 cold init, #34).
 */
#include <linux/bitops.h>
#include <linux/delay.h>
#include <linux/errno.h>
#include <linux/io.h>
#include <linux/kernel.h>
#include <linux/printk.h>
#ifdef __UBOOT__
#include <env.h>
#else
#define env_get_hex(name, dflt)	(dflt)		/* Linux: no env, defaults */
#endif
#include "bcm_cmic.h"

static u32 rd(struct bcm_cmic_unit *u, u32 off) { return readl(u->base + off); }
static void wr(struct bcm_cmic_unit *u, u32 off, u32 v) { writel(v, u->base + off); }

#define CMIC_SBUS_TIMEOUT	0x10094
#define CMIC_SBUS_RING_MAP(n)	(0x10098 + 4 * (n))	/* 0_7, 8_15, 16_23, 24_31 */
#define CMIC_RATE_ADJUST	0x11000
#define CMIC_RATE_ADJUST_INT	0x11004
/*
 * Delay entries in the tables: cat 0xff = hardware wait (always), 0xfe = stock trace gap (software pacing in stock,
 * skipped unless gaps=1: env us48_init_gaps in awto-uboot, module param gaps in Linux). Profile and rationale: #34.
 */
unsigned int bcm_hr2_init_gaps;
#define ENTRY_IS_DELAY(h)	(((h) >> 24) >= 0xfe)
static void init_delay(u32 h)
{
	if ((h >> 24) == 0xff || bcm_hr2_init_gaps)
		mdelay(h & 0xffff);
}

static const u32 hr2_init[] = {
#include "bcm_cmic_hr2_init_data.h"
};

/*
 * Unit 1: awto-uboot uses the full replay (gen_bcm_hr2_init.py --unit 1, untracked: 720 KB does not fit the flash image,
 * autochain.md, #78); Linux uses the minimum list (gen_bcm_hr2_init1_min.py, docs/init-phases-u1.md).
 */
static const u32 hr2_init1[] = {
#if defined(__UBOOT__)
#if __has_include("bcm_cmic_hr2_init1_data.h")
#include "bcm_cmic_hr2_init1_data.h"
#else
	0xff000000,
#endif
#else
#include "bcm_cmic_hr2_init1_min_data.h"
#endif
};

static const u32 hr2_init_x[] = {
#ifdef __UBOOT__			/* dropped-write groups are an awto-uboot experiment (xmask env); not in the module */
#include "bcm_cmic_hr2_init_x.h"
#else
	0,
#endif
};

int bcm_hr2_u0_extra(struct bcm_cmic_unit *u, unsigned int xmask, int *ops, int *errs)
{
	size_t i = 0;
	int n = 0, bad = 0, k;

#ifndef __UBOOT__
	xmask = 0;
#endif
	while (xmask && i < ARRAY_SIZE(hr2_init_x)) {
		u32 h = hr2_init_x[i], words = h & 0xff, data[BCM_CMIC_SCHAN_MSG_WORDS];

		if ((xmask >> (h >> 24)) & 1) {
			for (k = 0; k < words && k < BCM_CMIC_SCHAN_MSG_WORDS - 2; k++)
				data[k] = hr2_init_x[i + 2 + k];
			if (bcm_cmic_schan_op(u, (h >> 16) & 0xff, (h >> 8) & 0xff, hr2_init_x[i + 1], data, words, NULL))
				bad++;
			n++;
		}
		i += 2 + words;
	}
	*ops = n;
	*errs = bad;
	return 0;
}

/*
 * Pipe table clear: ING_HW_RESET_CONTROL_2 (blk 10) / EGR_HW_RESET_CONTROL_1 (blk 11) take {RESET_ALL, VALID, COUNT} and
 * set DONE (bit 18) when the range is cleared. The stock trace has the DONE polls dropped: starting the next range or
 * writing 0 early leaves tables uninitialised (L2X half unreadable, L2_ENTRY parity errors, L2 insert NAK; #34).
 * 5 ms = OpenMDK's limit per range (docs/uboot-second-stage.md); expiry = log + counted as a failed op.
 */
#define HWRST_ING2_ADDR		0x02000300
#define HWRST_EGR1_ADDR		0x02000100
#define HWRST_DONE		BIT(18)
#define HWRST_POLL_US		20
#define HWRST_POLLS		250

static int hwreset_wait(struct bcm_cmic_unit *u, u32 op, u32 blk, u32 addr, const u32 *data)
{
	u32 v = 0;
	int i;

	if (op != BCM_SCHAN_WRITE_REG || !data[0] ||
	    !((blk == 10 && addr == HWRST_ING2_ADDR) || (blk == 11 && addr == HWRST_EGR1_ADDR)))
		return 0;
	for (i = 0; i < HWRST_POLLS; i++) {
		if (!bcm_cmic_reg_read(u, blk, addr, &v, 1, NULL) && (v & HWRST_DONE))
			return 0;
		udelay(HWRST_POLL_US);
	}
	pr_err("bcm-cmic: pipe reset blk %u addr %08x: no DONE after %d us (%08x)\n", blk, addr, i * HWRST_POLL_US, v);
	return -ETIMEDOUT;
}

/* One stock write-list op plus the DONE wait it needs. Returns nonzero on failure. */
static int init_op(struct bcm_cmic_unit *u, u32 op, u32 blk, u32 addr, u32 *data, int words)
{
	return bcm_cmic_schan_op(u, op, blk, addr, data, words, NULL) || hwreset_wait(u, op, blk, addr, data);
}

static size_t replay_entries(const u32 *tab, size_t len)
{
	size_t i = 0, n = 0;

	while (i < len) {
		i += ENTRY_IS_DELAY(tab[i]) ? 1 : 2 + (tab[i] & 0xff);
		n++;
	}
	return n;
}

size_t bcm_hr2_u0_replay_entries(void) { return replay_entries(hr2_init, ARRAY_SIZE(hr2_init)); }
size_t bcm_hr2_u1_replay_entries(void) { return replay_entries(hr2_init1, ARRAY_SIZE(hr2_init1)); }

#define TOP_SOFT_RESET_REG	0x02030400
#define TOP_RESET_ALL_RELEASED	0x000fffff

/* Replay entries [first, last] (ordinal over all entries incl. delays) of the categories in mask; no init guard, no CMIC setup. */
static int replay(struct bcm_cmic_unit *u, const u32 *tab, size_t len, unsigned int mask, unsigned int first,
		  unsigned int last, int *ops, int *errs)
{
	size_t i = 0, ord = 0;
	int n = 0, bad = 0, k;
	bool cps = (mask & BCM_HR2_INIT_CPS) && u->pre_top_release;

	while (i < len && ord <= last) {
		u32 h = tab[i];
		u32 cat = h >> 24, words = h & 0xff, data[BCM_CMIC_SCHAN_MSG_WORDS];
		size_t cur = ord++;

		if (ENTRY_IS_DELAY(h)) {
			if (cur >= first)
				init_delay(h);
			i++;
			continue;
		}
		if (cur >= first && (cat & mask)) {
			for (k = 0; k < words && k < BCM_CMIC_SCHAN_MSG_WORDS - 2; k++)
				data[k] = tab[i + 2 + k];
			if (cps && ((h >> 8) & 0xff) == 16 && tab[i + 1] == TOP_SOFT_RESET_REG && data[0] == TOP_RESET_ALL_RELEASED) {
				u->pre_top_release(u);	/* stock's place for CMIC_CPS_RESET, same as unit 0 (#78, #106) */
				cps = false;
			}
			if (init_op(u, (h >> 16) & 0xff, (h >> 8) & 0xff, tab[i + 1], data, words))
				bad++;
			n++;
		}
		i += 2 + words;
	}
	*ops = n;
	*errs = bad;
	return 0;
}

int bcm_hr2_u0_replay(struct bcm_cmic_unit *u, unsigned int mask, unsigned int first, unsigned int last, int *ops, int *errs)
{
	return replay(u, hr2_init, ARRAY_SIZE(hr2_init), mask, first, last, ops, errs);
}

int bcm_hr2_u1_replay(struct bcm_cmic_unit *u, unsigned int mask, unsigned int first, unsigned int last, int *ops, int *errs)
{
	return replay(u, hr2_init1, ARRAY_SIZE(hr2_init1), mask, first, last, ops, errs);
}

static const u32 sbus_ring[4] = { 0x11122200, 0x00430000, 0x00005004, 0 };	/* driver-internals 1: rings per SBUS block */

/* SBUS ring map, timeout and MIIM rate: CMIC state that CPS_RESET returns to default. */
static void cmic_sbus_setup(struct bcm_cmic_unit *u)
{
	int k;

	for (k = 0; k < 4; k++)
		wr(u, CMIC_SBUS_RING_MAP(k), sbus_ring[k]);
	wr(u, CMIC_SBUS_TIMEOUT, 0x7d0);
	wr(u, CMIC_RATE_ADJUST, 1 << 16 | 40);		/* MIIM MDC 5 MHz from 200 MHz (driver-internals 2 init 5) */
	wr(u, CMIC_RATE_ADJUST_INT, 1 << 16 | 40);
}

/* Ring map, timeout, MIIM rate: for a caller's CPS_RESET hook, which must redo them after the reset. */
void bcm_hr2_cmic_setup(struct bcm_cmic_unit *u) { cmic_sbus_setup(u); }

int bcm_hr2_u0_cold_init(struct bcm_cmic_unit *u, unsigned int mask, int *ops, int *errs)
{
	size_t i = 0;
	int n = 0, bad = 0, k;
	bool cps = (mask & BCM_HR2_INIT_CPS) && u->pre_top_release;

	*ops = *errs = 0;
	if (!(mask & BCM_HR2_INIT_FORCE) && rd(u, CMIC_SBUS_RING_MAP(0)) == sbus_ring[0])
		return 0;			/* already initialised (stock or an earlier run) */
	bcm_hr2_init_gaps = env_get_hex("us48_init_gaps", bcm_hr2_init_gaps);
	cmic_sbus_setup(u);
	while (i < ARRAY_SIZE(hr2_init)) {
		u32 h = hr2_init[i];

		if (ENTRY_IS_DELAY(h)) {
			init_delay(h);
			i++;
			continue;
		}
		{
			u32 cat = h >> 24, op = (h >> 16) & 0xff, blk = (h >> 8) & 0xff, words = h & 0xff;

			if (!(cat & mask)) {
				i += 2 + words;
				continue;
			}
			u32 data[BCM_CMIC_SCHAN_MSG_WORDS];

			for (k = 0; k < words && k < BCM_CMIC_SCHAN_MSG_WORDS - 2; k++)
				data[k] = hr2_init[i + 2 + k];
			if (cps && blk == 16 && hr2_init[i + 1] == TOP_SOFT_RESET_REG && data[0] == TOP_RESET_ALL_RELEASED) {
				/* stock's place for CMIC_CPS_RESET: after REG_2 0x3f00 + wait, before TOP_SOFT_RESET_REG 0xfffff (#78) */
				u->pre_top_release(u);
				cps = false;
			}
			if (init_op(u, op, blk, hr2_init[i + 1], data, words))
				bad++;
			n++;
			i += 2 + words;
		}
	}
	/*
	 * Final stock values (live read of the stock-initialised chip, 2026-11-14) where the trace's last write differs:
	 * the trace has later runtime reconfiguration. Applied last, masked by bit 0x100.
	 */
	if (mask & 0x100) {
		static const struct { u8 blk; u32 addr, w0, w1; u8 n; } fin[] = {
			{ 11, 0x02000100, 0x00000000, 0, 1 },
			{ 12, 0x02025a00, 0x00001fed, 0, 1 },
			{ 10, 0x1e000100, 0x00000013, 0, 1 },
			{ 10, 0x46003400, 0x0000000f, 0, 1 },
			{ 10, 0x06000000, 0x2080f042, 0x00004000, 2 },
			{ 11, 0x04000b00, 0x00004000, 0, 1 },
			{ 11, 0x04000b02, 0x00004000, 0, 1 },
			{ 10, 0x02000400, 0x0010012c, 0, 1 },
		};
		u32 d[2];

		for (k = 0; k < ARRAY_SIZE(fin); k++) {
			d[0] = fin[k].w0;
			d[1] = fin[k].w1;
			if (bcm_cmic_schan_op(u, 0x0d, fin[k].blk, fin[k].addr, d, fin[k].n, NULL))
				bad++;
			n++;
		}
	}
	{
		int xo, xe;

		bcm_hr2_u0_extra(u, env_get_hex("us48_init_xmask", 0), &xo, &xe);
		n += xo;
		bad += xe;
	}
	*ops = n;
	*errs = bad;
	return 0;
}

/* Unit 1 (BCM53346 behind PCIe): same CMIC setup as unit 0, then the generated unit-1 write list (init1_data.h). */
int bcm_hr2_u1_cold_init(struct bcm_cmic_unit *u, unsigned int mask, int *ops, int *errs)
{
	static const u32 ring[4] = { 0x11122200, 0x00430000, 0x00005004, 0 };
	int k;

	*ops = *errs = 0;
	if (!(mask & BCM_HR2_INIT_FORCE) && rd(u, CMIC_SBUS_RING_MAP(0)) == ring[0])
		return 0;			/* already initialised */
	for (k = 0; k < 4; k++)
		wr(u, CMIC_SBUS_RING_MAP(k), ring[k]);
	wr(u, CMIC_SBUS_TIMEOUT, 0x7d0);
	wr(u, CMIC_RATE_ADJUST, 1 << 16 | 40);
	wr(u, CMIC_RATE_ADJUST_INT, 1 << 16 | 40);
	replay(u, hr2_init1, ARRAY_SIZE(hr2_init1), mask, 0, ~0U, ops, errs);
	/* DMA to host RAM over PCIe: remap case A (docs/driver-internals.md 14.1): AXI bit 32 for every host address; after the CPS_RESET of the replay */
	wr(u, 0x31474, 0x144d2450);
	wr(u, 0x31478, 0x19617595);
	wr(u, 0x3147c, 0x1e75c6da);
	wr(u, 0x314ac, 0x1f);
	return 0;
}
