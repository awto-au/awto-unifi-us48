// SPDX-License-Identifier: GPL-2.0+
/*
 * memtest_arm: DDR + on-chip SRAM pattern test and bandwidth/latency bench for the US-48 A9 (#17, #55).
 * Build: scripts/clock_sweep.py --build (static, OpenWrt musl toolchain). Run on the board as root.
 * Output: one "KEY value" line per result; exit code = number of failed checks (capped at 100).
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define SRAM_PHYS 0x1b000000u
#define SRAM_LEN 0x4000u /* 16 KiB; a read at +0x4000 bus-faults and resets the board (docs/sram.md) */

static uint32_t crc_tab[256];
static int fails;

static void crc_init(void)
{
	for (uint32_t i = 0; i < 256; i++) {
		uint32_t c = i;
		for (int k = 0; k < 8; k++)
			c = c & 1 ? 0xedb88320u ^ (c >> 1) : c >> 1;
		crc_tab[i] = c;
	}
}

/* zlib-compatible CRC32, so the host can recompute expected values */
static uint32_t crc32_buf(const volatile uint32_t *p, size_t words)
{
	uint32_t c = 0xffffffffu;

	for (size_t i = 0; i < words; i++) {
		uint32_t w = p[i];
		for (int b = 0; b < 4; b++, w >>= 8)
			c = crc_tab[(c ^ w) & 0xff] ^ (c >> 8);
	}
	return c ^ 0xffffffffu;
}

static double now(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec * 1e-9;
}

/* Fill with f(i), verify, report errors and the first bad word. Works on device memory (32-bit accesses only). */
static void pattern(const char *where, const char *name, volatile uint32_t *p, size_t words,
		    uint32_t (*f)(size_t, uint32_t), uint32_t arg)
{
	size_t i, bad = 0, first = 0;
	uint32_t got = 0, exp = 0;

	for (i = 0; i < words; i++)
		p[i] = f(i, arg);
	for (i = 0; i < words; i++) {
		uint32_t v = p[i], e = f(i, arg);
		if (v != e) {
			if (!bad++) {
				first = i;
				got = v;
				exp = e;
			}
		}
	}
	printf("PAT %s_%s errors=%zu", where, name, bad);
	if (bad)
		printf(" first_off=0x%zx got=0x%08x exp=0x%08x", first * 4, got, exp);
	printf(" crc=%08x\n", crc32_buf(p, words));
	if (bad)
		fails++;
}

static uint32_t f_const(size_t i, uint32_t a) { (void)i; return a; }
static uint32_t f_addr(size_t i, uint32_t a) { return (uint32_t)(i * 4) ^ a; }
static uint32_t f_walk(size_t i, uint32_t a) { return 1u << ((i + a) & 31); }
static uint32_t f_walk0(size_t i, uint32_t a) { return ~(1u << ((i + a) & 31)); }

static void patterns(const char *where, volatile uint32_t *p, size_t words, int walk_passes)
{
	static const uint32_t k[] = { 0x00000000u, 0xffffffffu, 0x55555555u, 0xaaaaaaaau };
	char n[32];

	for (unsigned i = 0; i < sizeof(k) / sizeof(k[0]); i++) {
		snprintf(n, sizeof(n), "const_%08x", k[i]);
		pattern(where, n, p, words, f_const, k[i]);
	}
	pattern(where, "addr", p, words, f_addr, 0);
	pattern(where, "addr_inv", p, words, f_addr, 0xffffffffu);
	for (int s = 0; s < walk_passes; s++) {
		snprintf(n, sizeof(n), "walk1_%d", s);
		pattern(where, n, p, words, f_walk, s);
		snprintf(n, sizeof(n), "walk0_%d", s);
		pattern(where, n, p, words, f_walk0, s);
	}
}

/* 32-bit volatile loops: valid on device memory, and comparable between SRAM and DDR */
static void bw_words(const char *where, volatile uint32_t *p, size_t words, int reps)
{
	double t;
	uint32_t acc = 0;
	size_t i;
	int r;

	t = now();
	for (r = 0; r < reps; r++)
		for (i = 0; i < words; i++)
			p[i] = (uint32_t)i;
	t = now() - t;
	printf("BW %s_write32_MBps %.1f\n", where, (double)words * 4 * reps / t / 1e6);
	t = now();
	for (r = 0; r < reps; r++)
		for (i = 0; i < words; i++)
			acc += p[i];
	t = now() - t;
	printf("BW %s_read32_MBps %.1f (acc %08x)\n", where, (double)words * 4 * reps / t / 1e6, acc);
}

static void bw_ddr(uint32_t *a, uint32_t *b, size_t bytes, int reps)
{
	double t;
	int r;

	t = now();
	for (r = 0; r < reps; r++)
		memset(a, r, bytes);
	t = now() - t;
	printf("BW ddr_memset_MBps %.1f\n", (double)bytes * reps / t / 1e6);
	t = now();
	for (r = 0; r < reps; r++)
		memcpy(b, a, bytes);
	t = now() - t;
	printf("BW ddr_memcpy_MBps %.1f (bytes copied per second)\n", (double)bytes * reps / t / 1e6);
	bw_words("ddr", a, bytes / 4, reps);
}

/* Random pointer chase over 64-byte lines: ns per dependent load (L1 32 KiB, L2 128 KiB) */
static void latency(uint32_t *buf, size_t bytes, const char *tag)
{
	size_t n = bytes / 64, i, steps = 2000000;
	size_t *idx = malloc(n * sizeof(*idx));
	uint32_t seed = 0x5301beefu;
	volatile uint32_t *p;
	uint32_t cur = 0;
	double t;

	if (!idx) {
		printf("LAT %s skipped (no memory)\n", tag);
		return;
	}
	for (i = 0; i < n; i++)
		idx[i] = i;
	for (i = n - 1; i > 0; i--) {	/* Fisher-Yates, xorshift32 */
		size_t j, tmp;
		seed ^= seed << 13;
		seed ^= seed >> 17;
		seed ^= seed << 5;
		j = seed % (i + 1);
		tmp = idx[i];
		idx[i] = idx[j];
		idx[j] = tmp;
	}
	for (i = 0; i < n; i++)		/* one cycle through every line */
		buf[idx[i] * 16] = (uint32_t)(idx[(i + 1) % n] * 16);
	free(idx);
	p = buf;
	t = now();
	for (i = 0; i < steps; i++)
		cur = p[cur];
	t = now() - t;
	printf("LAT %s_ns %.1f (span %zu KiB, end %u)\n", tag, t / steps * 1e9, bytes >> 10, cur);
}

int main(int argc, char **argv)
{
	size_t mib = 64, bytes;
	int sram = 1, walk = 4, i, fd;
	uint32_t *a, *b;

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--mib") && i + 1 < argc)
			mib = strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--no-sram"))
			sram = 0;
		else if (!strcmp(argv[i], "--walk") && i + 1 < argc)
			walk = atoi(argv[++i]);
		else {
			fprintf(stderr, "usage: %s [--mib N] [--walk N] [--no-sram]\n", argv[0]);
			return 2;
		}
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	crc_init();
	bytes = mib << 20;
	a = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
	b = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
	if (a == MAP_FAILED || b == MAP_FAILED) {
		printf("FAIL ddr_alloc %zu MiB x2\n", mib);
		return 100;
	}
	mlock(a, bytes);	/* best effort: keeps the test on the same physical pages */
	mlock(b, bytes);
	printf("INFO ddr_mib %zu walk_passes %d\n", mib, walk);
	patterns("ddr", a, bytes / 4, walk);
	bw_ddr(a, b, bytes, 4);
	latency(a, 16 << 10, "l1");
	latency(a, 96 << 10, "l2");
	latency(a, bytes < (32u << 20) ? bytes : 32u << 20, "ddr");

	if (sram) {
		volatile uint32_t *s;
		uint32_t save[SRAM_LEN / 4];

		fd = open("/dev/mem", O_RDWR | O_SYNC);
		s = fd < 0 ? MAP_FAILED : mmap(NULL, SRAM_LEN, PROT_READ | PROT_WRITE, MAP_SHARED, fd, SRAM_PHYS);
		if (s == MAP_FAILED) {
			printf("FAIL sram_map /dev/mem 0x%08x\n", SRAM_PHYS);
			fails++;
		} else {
			for (i = 0; i < (int)(SRAM_LEN / 4); i++)
				save[i] = s[i];
			patterns("sram", s, SRAM_LEN / 4, 32);
			bw_words("sram", s, SRAM_LEN / 4, 256);
			for (i = 0; i < (int)(SRAM_LEN / 4); i++)
				s[i] = save[i];
			munmap((void *)s, SRAM_LEN);
		}
		if (fd >= 0)
			close(fd);
	}
	printf("DONE fails %d\n", fails);
	return fails > 100 ? 100 : fails;
}
