// SPDX-License-Identifier: GPL-2.0+
/* Read-only CMIC examination commands over the bcm-cmic core (docs/uboot-second-stage.md section 10). */

#include <command.h>
#include <string.h>
#include <vsprintf.h>
#include <asm/io.h>
#include "bcm_cmic.h"

/* Unit 0 window: CMC0..2 regs end below +0x34000 (driver-internals 1) */
#define CMIC_WINDOW	0x40000
#define CMIC_MAX_WORDS	64
#define SCHAN_MAX_BLK	63
#define MEM_MAX_WORDS	16

static struct bcm_cmic_unit unit0 = { .base = (void __iomem *)CONFIG_BCM_CMIC_UNIT0_BASE };

static bool num(const char *s, ulong *v)
{
	char *end;

	*v = simple_strtoul(s, &end, 0);
	return *s && !*end;
}

static int do_cmic(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	ulong off, n = 1, i;

	if (argc < 3 || argc > 4 || strcmp(argv[1], "r") || !num(argv[2], &off) || (argc == 4 && !num(argv[3], &n)))
		return CMD_RET_USAGE;
	if ((off & 3) || n < 1 || n > CMIC_MAX_WORDS || off + 4 * n > CMIC_WINDOW) {
		printf("cmic: offset 4-aligned, 1..%d words, below 0x%x\n", CMIC_MAX_WORDS, CMIC_WINDOW);
		return CMD_RET_FAILURE;
	}
	for (i = 0; i < n; i++)
		printf("cmic 0 0x%05lx = 0x%08x\n", off + 4 * i, readl(unit0.base + off + 4 * i));
	return CMD_RET_SUCCESS;
}

U_BOOT_CMD(cmic, 4, 0, do_cmic, "CMIC register read (read-only)",
	   "r <off> [words]  - unit 0 CMIC register(s), offset from base");

static int sbus_read(bool mem, int argc, char *const argv[])
{
	ulong blk, addr, words = mem ? 0 : 1;
	u32 data[MEM_MAX_WORDS], ack;
	ulong max = mem ? MEM_MAX_WORDS : 2;
	int ret;
	ulong i;

	if (argc < (mem ? 5 : 4) || argc > 5 || strcmp(argv[1], "r") || !num(argv[2], &blk) || !num(argv[3], &addr) ||
	    (argc == 5 && !num(argv[4], &words)))
		return CMD_RET_USAGE;
	if (blk > SCHAN_MAX_BLK || words < 1 || words > max) {
		printf("blk 0..%d, words 1..%lu\n", SCHAN_MAX_BLK, max);
		return CMD_RET_FAILURE;
	}
	ret = bcm_cmic_schan_op(&unit0, mem ? BCM_SCHAN_READ_MEM : BCM_SCHAN_READ_REG, blk, addr, data, words, &ack);
	if (ret) {
		printf("%s r blk %lu addr 0x%lx: error %d\n", mem ? "smem" : "schan", blk, addr, ret);
		return CMD_RET_FAILURE;
	}
	printf("%s 0 blk %lu addr 0x%08lx ack 0x%08x\n", mem ? "smem" : "schan", blk, addr, ack);
	for (i = 0; i < words; i++)
		printf("  [%lu] = 0x%08x\n", i, data[i]);
	return CMD_RET_SUCCESS;
}

static int do_schan(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	return sbus_read(false, argc, argv);
}

static int do_smem(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	return sbus_read(true, argc, argv);
}

U_BOOT_CMD(schan, 5, 0, do_schan, "S-channel register read (read-only)",
	   "r <blk> <addr> [words]  - READ_REG (1-2 words); addr = offset | port index (driver-internals 1)");
U_BOOT_CMD(smem, 5, 0, do_smem, "S-channel memory read (read-only)",
	   "r <blk> <addr> <words>  - READ_MEM, entry of 1-16 words");

static int do_miim(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	bool internal = true, scan;
	ulong bus, phy, reg, p;
	u16 id1, id2, v;
	int a = 2, ret;

	if (argc < 2)
		return CMD_RET_USAGE;
	scan = !strcmp(argv[1], "scan");
	if (!scan && strcmp(argv[1], "r"))
		return CMD_RET_USAGE;
	if (argc > a && (!strcmp(argv[a], "int") || !strcmp(argv[a], "ext")))
		internal = !strcmp(argv[a++], "int");
	if (argc - a != (scan ? 1 : 3) || !num(argv[a], &bus) || bus > 7 ||
	    (!scan && (!num(argv[a + 1], &phy) || !num(argv[a + 2], &reg) || phy > 31 || reg > 31)))
		return CMD_RET_USAGE;

	if (!scan) {
		ret = bcm_cmic_miim_read(&unit0, internal, bus, phy, reg, &v);
		if (ret)
			return CMD_RET_FAILURE;
		printf("miim %s bus %lu phy %lu reg %lu = 0x%04x\n", internal ? "int" : "ext", bus, phy, reg, v);
		return CMD_RET_SUCCESS;
	}
	for (p = 0; p < 32; p++) {
		if (bcm_cmic_miim_read(&unit0, internal, bus, p, 2, &id1) ||
		    bcm_cmic_miim_read(&unit0, internal, bus, p, 3, &id2))
			return CMD_RET_FAILURE;
		if ((id1 == 0xffff && id2 == 0xffff) || (!id1 && !id2))
			continue;
		printf("miim %s bus %lu phy %2lu: id1 0x%04x id2 0x%04x\n", internal ? "int" : "ext", bus, p, id1, id2);
	}
	return CMD_RET_SUCCESS;
}

U_BOOT_CMD(miim, 6, 0, do_miim, "CMIC MIIM clause-22 read (read-only)",
	   "r [int|ext] <bus> <phy> <reg>  - one read, default int\n"
	   "miim scan [int|ext] <bus>      - ID1/ID2 of PHY 0-31, silent addresses skipped");
