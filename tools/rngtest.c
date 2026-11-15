// SPDX-License-Identifier: GPL-2.0-or-later OR MIT
/*
 * rngtest: FIPS 140-2 (2002 text, no continuous test) on stdin, 20000-bit blocks, as rng-tools rngtest.
 * Replaces rng-tools (autotools + libgcrypt) in the static image; same four tests and limits.
 * Usage: head -c 20000 /dev/hwrng | rngtest      prints "rngtest: blocks=N fail=F monobit=a poker=b runs=c longrun=d"
 */
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define BLK 2500	/* 20000 bits */

static const int runlo[6] = { 2315, 1114, 527, 240, 103, 103 };
static const int runhi[6] = { 2685, 1386, 723, 384, 209, 209 };

static int test_block(const unsigned char *b, int *f)
{
	int ones = 0, i, bit, prev = -1, run = 0, ok = 1;
	int nib[16] = { 0 }, runs[2][6] = { { 0 } };
	long poker = 0;
	double x;

	for (i = 0; i < BLK; i++) {
		nib[b[i] & 15]++;
		nib[b[i] >> 4]++;
	}
	for (i = 0; i < 16; i++)
		poker += (long)nib[i] * nib[i];
	x = 16.0 / 5000.0 * poker - 5000.0;
	for (i = 0; i < BLK * 8; i++) {
		bit = (b[i / 8] >> (i % 8)) & 1;
		ones += bit;
		if (bit == prev) {
			run++;
		} else {
			if (prev >= 0) {
				runs[prev][(run > 6 ? 6 : run) - 1]++;
				if (run >= 26)
					f[3] = 1;
			}
			prev = bit;
			run = 1;
		}
	}
	runs[prev][(run > 6 ? 6 : run) - 1]++;
	if (run >= 26)
		f[3] = 1;
	if (ones <= 9725 || ones >= 10275)
		f[0] = 1;
	if (x <= 2.16 || x >= 46.17)
		f[1] = 1;
	for (bit = 0; bit < 2; bit++)
		for (i = 0; i < 6; i++)
			if (runs[bit][i] < runlo[i] || runs[bit][i] > runhi[i])
				f[2] = 1;
	for (i = 0; i < 4; i++)
		ok &= !f[i];
	return ok;
}

int main(void)
{
	unsigned char b[BLK];
	int got, n, blocks = 0, fail = 0, cnt[4] = { 0 }, f[4], i;

	for (;;) {
		for (got = 0; got < BLK; got += n) {
			n = read(0, b + got, BLK - got);
			if (n <= 0)
				goto out;
		}
		memset(f, 0, sizeof(f));
		blocks++;
		if (!test_block(b, f))
			fail++;
		for (i = 0; i < 4; i++)
			cnt[i] += f[i];
	}
out:
	printf("rngtest: blocks=%d fail=%d monobit=%d poker=%d runs=%d longrun=%d\n", blocks, fail, cnt[0], cnt[1], cnt[2], cnt[3]);
	return blocks ? 0 : 1;
}
