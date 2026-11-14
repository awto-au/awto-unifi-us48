// SPDX-License-Identifier: GPL-2.0+
/* Read-only CMIC examination commands over the bcm-cmic core (docs/uboot-second-stage.md section 10). */

#include <command.h>
#include <string.h>
#include <vsprintf.h>
#include <asm/io.h>
#include <asm/global_data.h>
#include <time.h>
#include <u-boot/crc.h>
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

/* schan w <blk> <addr> <w0> [w1]: bring-up experiments on the CHIP (never flash); one WRITE_REG */
static int sbus_write(int argc, char *const argv[])
{
	ulong blk, addr, w0, w1 = 0;
	u32 data[2];

	if (argc < 5 || argc > 6 || !num(argv[2], &blk) || !num(argv[3], &addr) || !num(argv[4], &w0) ||
	    (argc == 6 && !num(argv[5], &w1)))
		return CMD_RET_USAGE;
	data[0] = w0;
	data[1] = w1;
	if (blk > SCHAN_MAX_BLK || bcm_cmic_schan_op(&unit0, BCM_SCHAN_WRITE_REG, blk, addr, data, argc == 6 ? 2 : 1, NULL))
		return CMD_RET_FAILURE;
	return CMD_RET_SUCCESS;
}

static int do_schan(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	if (argc > 1 && !strcmp(argv[1], "w"))
		return sbus_write(argc, argv);
	return sbus_read(false, argc, argv);
}

static int do_smem(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	return sbus_read(true, argc, argv);
}

U_BOOT_CMD(schan, 6, 0, do_schan, "S-channel register read; w = WRITE_REG (chip bring-up experiments)",
	   "r <blk> <addr> [words]  - READ_REG (1-2 words); addr = offset | port index (driver-internals 1)\n"
	   "w <blk> <addr> <w0> [w1] - WRITE_REG (chip only, never flash)");
U_BOOT_CMD(smem, 5, 0, do_smem, "S-channel memory read (read-only)",
	   "r <blk> <addr> <words>  - READ_MEM, entry of 1-16 words");

/* hr2x <xmask>: apply the dropped stock write groups to the live chip (bring-up bisect, never flash) */
static int do_hr2x(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	ulong m;
	int ops, errs;

	if (argc != 2 || !num(argv[1], &m))
		return CMD_RET_USAGE;
	bcm_hr2_u0_extra(&unit0, m, &ops, &errs);
	printf("hr2x mask 0x%lx: %d ops, %d failed\n", m, ops, errs);
	return errs ? CMD_RET_FAILURE : CMD_RET_SUCCESS;
}

U_BOOT_CMD(hr2x, 2, 0, do_hr2x, "apply dropped stock S-channel write groups (chip only)",
	   "<xmask>  - bit per group: 0 cpu_cos_map 1 egr_pri_cng_map 2 source_trunk_map 3 ipipe_mem 4 epipe_mem 5 ser 6 gport12 7 xlport 8 other");


/* hr2mem dump|diff|sync <addr> [first [last]]: memory image of the chip (#46 option A; sync writes the chip only, never flash) */
static int do_hr2mem(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	ulong addr, first = 0, last = bcm_hr2_mem_ntab() - 1;
	int errs, total, ret;
	unsigned long t0 = get_timer(0);
	u32 *img;

	if (argc >= 2 && !strcmp(argv[1], "list")) {
		for (ret = 0; ret < bcm_hr2_mem_ntab(); ret++)
			printf("%3d %s\n", ret, bcm_hr2_mem_name(ret));
		return CMD_RET_SUCCESS;
	}
	if (argc < 3 || argc > 5 || !num(argv[2], &addr) || (argc > 3 && !num(argv[3], &first)) ||
	    (argc > 4 && !num(argv[4], &last)) || last >= bcm_hr2_mem_ntab() || first > last)
		return CMD_RET_USAGE;
	img = (u32 *)addr;
	if (!strcmp(argv[1], "dump")) {
		u32 n = bcm_hr2_mem_dump(&unit0, img, first, last, &errs);

		printf("hr2mem dump: %u bytes at 0x%lx crc32 0x%08x, %d read errors, %lu ms\n", n, addr, crc32(0, (u8 *)img, n), errs,
		       get_timer(t0));
		return CMD_RET_SUCCESS;
	}
	if (strcmp(argv[1], "diff") && strcmp(argv[1], "sync"))
		return CMD_RET_USAGE;
	ret = bcm_hr2_mem_cmp(&unit0, img, first, last, argv[1][0] == 's', &total, &errs);
	if (ret) {
		printf("hr2mem: bad image (%d)\n", ret);
		return CMD_RET_FAILURE;
	}
	printf("hr2mem %s tables %lu..%lu: %d entries differ, %d errors, %lu ms\n", argv[1], first, last, total, errs, get_timer(t0));
	return errs ? CMD_RET_FAILURE : CMD_RET_SUCCESS;
}

U_BOOT_CMD(hr2mem, 5, 0, do_hr2mem, "chip memory image dump / diff / sync (chip only)",
	   "list                          - table indexes\n"
	   "hr2mem dump <addr> [first [last]] - read all memories by S-channel into a sparse RAM image\n"
	   "hr2mem diff <addr> [first [last]] - per-table count of live entries that differ from the image\n"
	   "hr2mem sync <addr> [first [last]] - diff, writing the image values into the differing entries");

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
