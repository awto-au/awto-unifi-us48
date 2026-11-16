// SPDX-License-Identifier: GPL-2.0+
/*
 * Read-only TSC register reader for STOCK (unit-1 CMIC through /dev/mem, MIIM clause 45 on internal bus 1, #78).
 * Usage: tsc_read <cmic phys base> < lines "A aer reg"   (reg = devad<<16 | reg16; A = MIIM phy id: 2 lane 0, 4 lane 2)
 * Output: "A aer reg val" (val = ffffffff on MIIM timeout). Method = docs/driver-internals.md 13.1; AER written before every read.
 * Line "S words addr" = S-channel register read (block 5, XLPORT/XLMAC; words 1 or 2): prints "S words addr ack lo hi"
 * (docs/driver-internals.md 13.3: SCHAN_CTRL 0x31000, MSG0 0x3100c, MSG1 0x31010..). Only MIIM_PARAM/ADDRESS/CTRL writes and READ_DATA/STAT reads; no TSC register is written except the AER.
 * Poll bound: one C45 frame ~64 bit x 0.4 us = 26 us; 2000 polls of a mapped read (~1 us each) = 2 ms = ~75x, log-and-skip on expiry.
 */
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

#define PARAM 0x31080
#define RDATA 0x31084
#define ADDR  0x31088
#define CTRL  0x3108c
#define STAT  0x31090
#define POLLS 2000

static volatile uint32_t *map;

static uint32_t rd(uint32_t o) { return map[o >> 2]; }
static void wr(uint32_t o, uint32_t v) { map[o >> 2] = v; }

static int xfer(uint32_t ctrl)
{
	int i;

	wr(CTRL, 0);
	wr(CTRL, ctrl);
	for (i = 0; i < POLLS; i++)
		if (rd(STAT) & 1)
			break;
	wr(CTRL, 0);
	return i == POLLS ? -1 : 0;
}

static int c45_write(unsigned a, unsigned devad, unsigned reg, unsigned val)
{
	wr(PARAM, 0x02600000 | a << 16 | (val & 0xffff));
	wr(ADDR, devad << 16 | reg);
	return xfer(1);
}

static int c45_read(unsigned a, unsigned devad, unsigned reg, uint32_t *val)
{
	wr(PARAM, 0x02600000 | a << 16);
	wr(ADDR, devad << 16 | reg);
	if (xfer(2))
		return -1;
	*val = rd(RDATA) & 0xffff;
	return 0;
}

#define SCTRL 0x31000
#define SMSG  0x3100c
#define SPOLLS 20000	/* a register read ~1 us on the chip; 20000 mapped reads ~ 20 ms >> expected, log-and-skip on expiry */

/* "W words addr lo hi": S-channel register write, EXPERIMENTS ON OUR LINUX ONLY (never run on stock); waits like sread */
static int swrite(unsigned words, unsigned addr, uint32_t lo, uint32_t hi)
{
	int i;

	wr(SCTRL, 0);
	wr(SMSG, 0x34500000 | words * 4 << 7);
	wr(SMSG + 4, addr);
	wr(SMSG + 8, lo);
	wr(SMSG + 12, hi);
	wr(SCTRL, 1);
	for (i = 0; i < SPOLLS; i++)
		if (rd(SCTRL) & 2)
			break;
	i = (i == SPOLLS || (rd(SCTRL) & 0x700000)) ? -1 : 0;
	wr(SCTRL, 0);
	return i;
}

static int sread(unsigned words, unsigned addr, uint32_t *ack, uint32_t *lo, uint32_t *hi)
{
	int i;

	wr(SCTRL, 0);
	wr(SMSG, 0x2c500000 | words * 4 << 7);
	wr(SMSG + 4, addr);
	wr(SCTRL, 1);
	for (i = 0; i < SPOLLS; i++)
		if (rd(SCTRL) & 2)
			break;
	*ack = rd(SMSG);
	*lo = rd(SMSG + 4);
	*hi = rd(SMSG + 8);
	i = (i == SPOLLS || (rd(SCTRL) & 0x700000)) ? -1 : 0;
	wr(SCTRL, 0);
	return i;
}

int main(int argc, char **argv)
{
	unsigned a, aer, reg;
	char kind[4];
	uint32_t val;
	unsigned long base;
	int fd;

	if (argc != 2)
		return 2;
	base = strtoul(argv[1], NULL, 0);
	fd = open("/dev/mem", O_RDWR | O_SYNC);
	if (fd < 0) {
		perror("open /dev/mem");
		return 1;
	}
	map = mmap(NULL, 0x40000, PROT_READ | PROT_WRITE, MAP_SHARED, fd, base);
	if (map == MAP_FAILED) {
		perror("mmap");
		return 1;
	}
	while (scanf(" %3s", kind) == 1) {
		unsigned devad, r16;
		uint32_t ack, lo, hi;

		if (kind[0] == 'R') {	/* "R hexoff": raw CMIC register read, prints "R off val" */
			if (scanf("%x", &reg) != 1)
				break;
			printf("R %x %x\n", reg, rd(reg));
			continue;
		}
		if (kind[0] == 'W') {
			unsigned wlo, whi;

			if (scanf("%u %x %x %x", &a, &reg, &wlo, &whi) != 4)
				break;
			printf("W %u %x %x %x rc=%d\n", a, reg, wlo, whi, swrite(a, reg, wlo, whi));
			continue;
		}
		if (kind[0] == 'S') {
			if (scanf("%u %x", &a, &reg) != 2)
				break;
			if (sread(a, reg, &ack, &lo, &hi))
				ack = lo = hi = 0xffffffff;
			printf("S %u %x %x %x %x\n", a, reg, ack, lo, hi);
			continue;
		}
		if (scanf("%u %x", &aer, &reg) != 2)
			break;
		a = strtoul(kind, NULL, 10);
		devad = (reg >> 16) & 0xf;
		r16 = reg & 0xffff;
		if (c45_write(a, devad, 0xffde, aer) || c45_read(a, devad, r16, &val))
			val = 0xffffffff;
		printf("%u %u %x %x\n", a, aer, reg, val);
	}
	return 0;
}
