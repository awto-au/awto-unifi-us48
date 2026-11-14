// SPDX-License-Identifier: GPL-2.0
/*
 * bcm-cmic HR2 unit-0 memory image: dump every IPIPE/EPIPE/MMU memory (bcm_cmic_hr2_memtab.h) by S-channel reads into a
 * sparse RAM image, then diff/sync a live chip against it (#46 option A). awto-uboot only.
 *
 * Image (LE u32): { magic, version, ntab, bytes }, then per table { idx, nrec, crc32 of its records, words } followed by nrec
 * records { entry index, words[] }. All-zero entries are omitted (absent = zero).
 */
#include <linux/errno.h>
#include <linux/kernel.h>
#include <linux/printk.h>
#include <string.h>
#include <u-boot/crc.h>
#include <vsprintf.h>
#include "bcm_cmic.h"

#define MEM_MAGIC	0x4d533455	/* "U4SM" */
#define MEM_VERSION	1
#define MEM_MAX_WORDS	16
#define MEM_GIVEUP	3		/* consecutive failed reads: table unreadable, skip it (each failure costs a 10 ms S-channel timeout) */

static const struct { const char *name; u8 blk; u32 base; u16 count; u8 words; } memtab[] = {
#include "bcm_cmic_hr2_memtab.h"
};

int bcm_hr2_mem_ntab(void)
{
	return ARRAY_SIZE(memtab);
}

const char *bcm_hr2_mem_name(int t)
{
	return memtab[t].name;
}

static bool is_zero(const u32 *d, int n)
{
	while (n--)
		if (*d++)
			return false;
	return true;
}

u32 bcm_hr2_mem_dump(struct bcm_cmic_unit *u, u32 *img, int first, int last, int *errs)
{
	u32 *p = img + 4;
	int t, i, k, bad = 0;

	for (t = first; t <= last && t < ARRAY_SIZE(memtab); t++) {
		u32 *head = p, *rec;
		u32 nrec = 0, w = memtab[t].words, data[MEM_MAX_WORDS];
		int fails = 0, terr = 0;

		p += 4;
		rec = p;
		for (i = 0; i < memtab[t].count && fails < MEM_GIVEUP; i++) {
			if (bcm_cmic_mem_read(u, memtab[t].blk, memtab[t].base + i, data, w, NULL)) {
				fails++;
				terr++;
				continue;
			}
			fails = 0;
			if (is_zero(data, w))
				continue;
			*p++ = i;
			for (k = 0; k < w; k++)
				*p++ = data[k];
			nrec++;
		}
		head[0] = t;
		head[1] = nrec;
		head[2] = crc32(0, (const u8 *)rec, (p - rec) * 4);
		head[3] = w;
		bad += terr;
		printf("%3d %-40s blk %2u base 0x%08x %5u entries x %2u w: %5u nonzero, %d read errors%s\n", t, memtab[t].name,
		       memtab[t].blk, memtab[t].base, memtab[t].count, w, nrec, terr, fails >= MEM_GIVEUP ? " (given up)" : "");
	}
	img[0] = MEM_MAGIC;
	img[1] = MEM_VERSION;
	img[2] = 0;
	img[3] = (p - img) * 4;
	for (t = 0, p = img + 4; p < img + img[3] / 4; t++)
		p += 4 + p[1] * (1 + p[3]);
	img[2] = t;
	*errs = bad;
	return img[3];
}

/* Diff (write = false) or sync (write = true) the live chip against the image, per table in [first, last]. */
int bcm_hr2_mem_cmp(struct bcm_cmic_unit *u, const u32 *img, int first, int last, bool write, int *total, int *errs)
{
	const u32 *p = img + 4;
	u32 n;
	int bad = 0, sum = 0;

	if (img[0] != MEM_MAGIC || img[1] != MEM_VERSION)
		return -EINVAL;
	for (n = 0; n < img[2]; n++) {
		u32 t = p[0], nrec = p[1], w = p[3], crc = p[2];
		const u32 *rec = p + 4, *r = rec;
		u32 data[MEM_MAX_WORDS], zero[MEM_MAX_WORDS] = { 0 };
		int diff = 0, terr = 0, fails = 0, i, k;

		p = rec + nrec * (1 + w);
		if (t >= ARRAY_SIZE(memtab) || w != memtab[t].words)
			return -EINVAL;
		if (crc32(0, (const u8 *)rec, (p - rec) * 4) != crc) {
			printf("table %u %s: image crc mismatch\n", t, memtab[t].name);
			return -EILSEQ;
		}
		if ((int)t < first || (int)t > last)
			continue;
		for (i = 0; i < memtab[t].count && fails < MEM_GIVEUP; i++) {
			const u32 *want = zero;

			if (r < p && r[0] == i) {
				want = r + 1;
				r += 1 + w;
			}
			if (bcm_cmic_mem_read(u, memtab[t].blk, memtab[t].base + i, data, w, NULL)) {
				fails++;
				terr++;
				continue;
			}
			fails = 0;
			for (k = 0; k < w && data[k] == want[k]; k++)
				;
			if (k == w)
				continue;
			diff++;
			if (write)
				memcpy(data, want, w * 4), terr += !!bcm_cmic_mem_write(u, memtab[t].blk, memtab[t].base + i, data, w);
		}
		if (diff || terr)
			printf("%3u %-40s %5d differ, %d errors%s\n", t, memtab[t].name, diff, terr, fails >= MEM_GIVEUP ? " (given up)" : "");
		sum += diff;
		bad += terr;
	}
	*total = sum;
	*errs = bad;
	return 0;
}
