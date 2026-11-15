// SPDX-License-Identifier: GPL-2.0-or-later OR MIT
/*
 * us48bench: 1-2 s CPU/memory probes for scripts/core_test.py (#84). Static musl, ARMv7, no VFP.
 *   us48bench lat [max_kib]   pointer-chase latency per working-set size (L1/L2/DRAM), ns per load
 *   us48bench bw              read / write / memcpy MB/s at 16 KiB (L1), 256 KiB (L2), 8 MiB (DRAM)
 *   us48bench spin            dependent-add loop, indicative core MHz (1 add/cycle assumed)
 *   us48bench tick [secs]     monotonic timestamps around a nanosleep, host compares against its own clock
 *   us48bench rd FILE BYTES   sequential read throughput of a file/device, 64 KiB reads
 * Build: tools/Makefile-free, see scripts/build_rootfs.py build_tools().
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define LINE 32		/* A9 L1 and PL310 line size */
#define SLICE_NS 40000000LL	/* time per measurement point: long enough to dominate clock_gettime cost */

static int64_t now_ns(clockid_t c)
{
	struct timespec t;

	clock_gettime(c, &t);
	return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}

static double lat_point(size_t bytes)
{
	size_t n = bytes / LINE, i, j;
	char *buf = aligned_alloc(LINE, bytes);
	size_t *perm = malloc(n * sizeof(*perm));
	void **p;
	int64_t t0, t1;
	long iters = 0;

	if (!buf || !perm)
		return -1;
	for (i = 0; i < n; i++)
		perm[i] = i;
	srand(1);
	for (i = n - 1; i > 0; i--) {	/* Sattolo: one cycle over all lines */
		j = rand() % i;
		size_t t = perm[i]; perm[i] = perm[j]; perm[j] = t;
	}
	for (i = 0; i < n; i++)
		*(void **)(buf + perm[i] * LINE) = buf + perm[(i + 1) % n] * LINE;
	p = (void **)buf;
	for (i = 0; i < n * 2; i++)	/* warm: fill the caches that fit */
		p = *p;
	t0 = now_ns(CLOCK_MONOTONIC);
	do {
		for (i = 0; i < 4096; i++)
			p = *p;
		iters += 4096;
		t1 = now_ns(CLOCK_MONOTONIC);
	} while (t1 - t0 < SLICE_NS);
	__asm__ volatile("" : : "r"(p));
	free(buf);
	free(perm);
	return (double)(t1 - t0) / iters;
}

static int cmd_lat(size_t max_kib)
{
	size_t kib;

	printf("lat_ns");
	for (kib = 4; kib <= max_kib; kib *= 2)
		printf(" %zuK=%.1f", kib, lat_point(kib * 1024));
	printf("\n");
	return 0;
}

static double bw_run(size_t bytes, int mode)
{
	uint32_t *a = aligned_alloc(64, bytes), *b = aligned_alloc(64, bytes);
	size_t w = bytes / 4, i;
	uint32_t sum = 0;
	int64_t t0, t1;
	double total = 0;

	if (!a || !b)
		return -1;
	memset(a, 1, bytes);
	memset(b, 2, bytes);
	t0 = now_ns(CLOCK_MONOTONIC);
	do {
		switch (mode) {
		case 0:
			for (i = 0; i < w; i += 8)
				sum += a[i] + a[i + 1] + a[i + 2] + a[i + 3] + a[i + 4] + a[i + 5] + a[i + 6] + a[i + 7];
			break;
		case 1:
			for (i = 0; i < w; i += 8) {
				a[i] = sum; a[i + 1] = sum; a[i + 2] = sum; a[i + 3] = sum;
				a[i + 4] = sum; a[i + 5] = sum; a[i + 6] = sum; a[i + 7] = sum;
			}
			break;
		default:
			memcpy(b, a, bytes);
			__asm__ volatile("" : : "r"(b) : "memory");
		}
		total += bytes;
		t1 = now_ns(CLOCK_MONOTONIC);
	} while (t1 - t0 < SLICE_NS);
	__asm__ volatile("" : : "r"(sum));
	free(a);
	free(b);
	return total / ((double)(t1 - t0) / 1e9) / 1e6;
}

static int cmd_bw(void)
{
	static const size_t sz[] = { 16 * 1024, 256 * 1024, 8 * 1024 * 1024 };
	static const char *nm[] = { "16K", "256K", "8M" };
	static const char *mode[] = { "rd", "wr", "cp" };
	int s, m;

	printf("bw_MBps");
	for (s = 0; s < 3; s++)
		for (m = 0; m < 3; m++)
			printf(" %s_%s=%.0f", mode[m], nm[s], bw_run(sz[s], m));
	printf("\n");
	return 0;
}

#define ADD8 "add %0,%0,#1\n add %0,%0,#1\n add %0,%0,#1\n add %0,%0,#1\n" \
	     "add %0,%0,#1\n add %0,%0,#1\n add %0,%0,#1\n add %0,%0,#1\n"

static int cmd_spin(void)
{
	uint32_t acc = 0, i, iters = 0;
	int64_t t0 = now_ns(CLOCK_MONOTONIC), t1;

	do {
		for (i = 0; i < 100000; i++)
			__asm__ volatile(ADD8 ADD8 ADD8 ADD8 ADD8 ADD8 ADD8 ADD8 : "+r"(acc));	/* 64 dependent adds */
		iters += 100000;
		t1 = now_ns(CLOCK_MONOTONIC);
	} while (t1 - t0 < 400000000LL);
	printf("spin_mhz=%.0f\n", (double)iters * 64 / ((double)(t1 - t0) / 1e3));
	return 0;
}

static int cmd_tick(int secs)
{
	struct timespec d = { secs, 0 };

	printf("tick start mono_ns=%lld raw_ns=%lld\n", (long long)now_ns(CLOCK_MONOTONIC), (long long)now_ns(CLOCK_MONOTONIC_RAW));
	fflush(stdout);
	nanosleep(&d, NULL);
	printf("tick end mono_ns=%lld raw_ns=%lld\n", (long long)now_ns(CLOCK_MONOTONIC), (long long)now_ns(CLOCK_MONOTONIC_RAW));
	return 0;
}

static int cmd_rd(const char *path, size_t bytes)
{
	static char buf[65536];
	int fd = open(path, O_RDONLY);
	size_t done = 0;
	int64_t t0, t1;
	ssize_t r;

	if (fd < 0) {
		perror(path);
		return 1;
	}
	t0 = now_ns(CLOCK_MONOTONIC);
	while (done < bytes && (r = read(fd, buf, sizeof(buf))) > 0)
		done += r;
	t1 = now_ns(CLOCK_MONOTONIC);
	printf("rd %s bytes=%zu MBps=%.2f\n", path, done, done / ((double)(t1 - t0) / 1e9) / 1e6);
	return done == bytes ? 0 : 1;
}

int main(int argc, char **argv)
{
	if (argc >= 2 && !strcmp(argv[1], "lat"))
		return cmd_lat(argc > 2 ? strtoul(argv[2], NULL, 0) : 16384);
	if (argc >= 2 && !strcmp(argv[1], "bw"))
		return cmd_bw();
	if (argc >= 2 && !strcmp(argv[1], "spin"))
		return cmd_spin();
	if (argc >= 2 && !strcmp(argv[1], "tick"))
		return cmd_tick(argc > 2 ? atoi(argv[2]) : 2);
	if (argc >= 4 && !strcmp(argv[1], "rd"))
		return cmd_rd(argv[2], strtoul(argv[3], NULL, 0));
	fprintf(stderr, "usage: us48bench lat [max_kib] | bw | spin | tick [secs] | rd FILE BYTES\n");
	return 2;
}
