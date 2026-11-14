// SPDX-License-Identifier: GPL-2.0
/*
 * bcm-cmic HR2 unit-0 cold init: ring map + MIIM rate, then the filtered stock write list (generated).
 * Not built by the Linux module (stock has initialised the chip there); awto-uboot only.
 */
#include <linux/bitops.h>
#include <linux/delay.h>
#include <linux/errno.h>
#include <linux/io.h>
#include <linux/kernel.h>
#include <linux/printk.h>
#include <env.h>
#include "bcm_cmic.h"

static u32 rd(struct bcm_cmic_unit *u, u32 off) { return readl(u->base + off); }
static void wr(struct bcm_cmic_unit *u, u32 off, u32 v) { writel(v, u->base + off); }

#define CMIC_SBUS_TIMEOUT	0x10094
#define CMIC_SBUS_RING_MAP(n)	(0x10098 + 4 * (n))	/* 0_7, 8_15, 16_23, 24_31 */
#define CMIC_RATE_ADJUST	0x11000
#define CMIC_RATE_ADJUST_INT	0x11004
static const u32 hr2_init[] = {
#include "bcm_cmic_hr2_init_data.h"
};

static const u32 hr2_init_x[] = {
#include "bcm_cmic_hr2_init_x.h"
};

int bcm_hr2_u0_extra(struct bcm_cmic_unit *u, unsigned int xmask, int *ops, int *errs)
{
	size_t i = 0;
	int n = 0, bad = 0, k;

	while (i < ARRAY_SIZE(hr2_init_x)) {
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

int bcm_hr2_u0_cold_init(struct bcm_cmic_unit *u, unsigned int mask, int *ops, int *errs)
{
	static const u32 ring[4] = { 0x11122200, 0x00430000, 0x00005004, 0 };	/* driver-internals 1: rings per SBUS block */
	size_t i = 0;
	int n = 0, bad = 0, k;

	*ops = *errs = 0;
	if (rd(u, CMIC_SBUS_RING_MAP(0)) == ring[0])
		return 0;			/* already initialised (stock or an earlier run) */
	for (k = 0; k < 4; k++)
		wr(u, CMIC_SBUS_RING_MAP(k), ring[k]);
	wr(u, CMIC_SBUS_TIMEOUT, 0x7d0);
	wr(u, CMIC_RATE_ADJUST, 1 << 16 | 40);		/* MIIM MDC 5 MHz from 200 MHz (driver-internals 2 init 5) */
	wr(u, CMIC_RATE_ADJUST_INT, 1 << 16 | 40);
	while (i < ARRAY_SIZE(hr2_init)) {
		u32 h = hr2_init[i];

		if ((h >> 24) == 0xff) {
			mdelay(h & 0xffff);
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
			if (bcm_cmic_schan_op(u, op, blk, hr2_init[i + 1], data, words, NULL))
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
