// SPDX-License-Identifier: GPL-2.0
/*
 * bcm-cmic HR2 unit-0 register image: every IPIPE/EPIPE/MMU/TOP gen/port register (bcm_cmic_hr2_regtab.h) by S-channel reads
 * into a RAM image, then diff/sync a live chip against it (#46). awto-uboot only.
 *
 * Instance = (table row, element, port). Port registers are read at CPU port 0 and front-1 logical port 2; elements step 0x100.
 * Image (LE u32): { magic, version, ninst, bytes }, then records { instance index (bit 31 = unreadable), words[4] }.
 * Zero-valued instances are omitted (absent = zero).
 */
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/printk.h>
#include <string.h>
#include <vsprintf.h>
#include "bcm_cmic.h"

#define REG_MAGIC	0x52473455	/* "U4GR" */
#define REG_VERSION	1
#define REG_W		4
#define REG_BAD		0x80000000u

static const struct { const char *name; u8 blk; u32 addr; u16 nelem; u8 words, port, ctr; } regtab[] = {
#include "bcm_cmic_hr2_regtab.h"
};
static const u8 ports[2] = { 0, 2 };

/* Resolve instance n -> row/element/port/address; returns false past the end. */
static bool inst_at(u32 n, int *row, int *el, int *pt, u32 *addr)
{
	int t;

	for (t = 0; t < ARRAY_SIZE(regtab); t++) {
		u32 per = regtab[t].nelem * (regtab[t].port ? 2 : 1);

		if (n >= per) {
			n -= per;
			continue;
		}
		*row = t;
		*el = regtab[t].port ? n / 2 : n;
		*pt = regtab[t].port ? ports[n & 1] : -1;
		*addr = regtab[t].addr + *el * 0x100 + (*pt > 0 ? *pt : 0);
		return true;
	}
	return false;
}

u32 bcm_hr2_reg_ninst(void)
{
	u32 n = 0;
	int t;

	for (t = 0; t < ARRAY_SIZE(regtab); t++)
		n += regtab[t].nelem * (regtab[t].port ? 2 : 1);
	return n;
}

static bool is_zero(const u32 *d, int n)
{
	while (n--)
		if (*d++)
			return false;
	return true;
}

u32 bcm_hr2_reg_dump(struct bcm_cmic_unit *u, u32 *img, int *errs)
{
	u32 *p = img + 4, n, ninst = bcm_hr2_reg_ninst();
	int row, el, pt, bad = 0;
	u32 addr;

	for (n = 0; n < ninst && inst_at(n, &row, &el, &pt, &addr); n++) {
		u32 d[REG_W] = { 0 };

		if (bcm_cmic_reg_read(u, regtab[row].blk, addr, d, regtab[row].words, NULL)) {
			bad++;
			*p++ = n | REG_BAD;
			memset(p, 0, REG_W * 4);
			p += REG_W;
			continue;
		}
		if (is_zero(d, regtab[row].words))
			continue;
		*p++ = n;
		memcpy(p, d, REG_W * 4);
		p += REG_W;
	}
	img[0] = REG_MAGIC;
	img[1] = REG_VERSION;
	img[2] = ninst;
	img[3] = (p - img) * 4;
	*errs = bad;
	return img[3];
}

/* Diff (write = false) or sync (write = true) instances [first, last] against the image; prints each difference. */
int bcm_hr2_reg_cmp(struct bcm_cmic_unit *u, const u32 *img, u32 first, u32 last, bool write, int *total, int *errs)
{
	const u32 *r = img + 4, *end = img + img[3] / 4;
	u32 n, ninst = bcm_hr2_reg_ninst(), zero[REG_W] = { 0 };
	int row, el, pt, diff = 0, bad = 0, k;
	u32 addr;

	if (img[0] != REG_MAGIC || img[1] != REG_VERSION || img[2] != ninst)
		return -EINVAL;
	for (n = 0; n < ninst && inst_at(n, &row, &el, &pt, &addr); n++) {
		const u32 *want = zero;
		bool unreadable = false;
		u32 d[REG_W] = { 0 };
		int w = regtab[row].words;

		if (r < end && (r[0] & ~REG_BAD) == n) {
			unreadable = r[0] & REG_BAD;
			want = r + 1;
			r += 1 + REG_W;
		}
		if (n < first || n > last || unreadable)
			continue;
		if (bcm_cmic_reg_read(u, regtab[row].blk, addr, d, w, NULL)) {
			bad++;
			continue;
		}
		for (k = 0; k < w && d[k] == want[k]; k++)
			;
		if (k == w)
			continue;
		diff++;
		printf("REG %4u %-36s blk %2u 0x%08x e%d p%d%s: warm", n, regtab[row].name, regtab[row].blk, addr, el, pt,
		       regtab[row].ctr ? " (ctr)" : "");
		for (k = 0; k < w; k++)
			printf(" %08x", want[k]);
		printf(" cold");
		for (k = 0; k < w; k++)
			printf(" %08x", d[k]);
		printf("\n");
		if (write) {
			u32 wv[REG_W];

			memcpy(wv, want, sizeof(wv));
			if (bcm_cmic_reg_write(u, regtab[row].blk, addr, wv, w)) {
				bad++;
				printf("  write failed\n");
			}
		}
	}
	*total = diff;
	*errs = bad;
	return 0;
}
