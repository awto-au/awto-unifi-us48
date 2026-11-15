// SPDX-License-Identifier: GPL-2.0+
/*
 * US-48 fans: 7-byte MSPI board-IO frame per switch chip (docs/fan.md, docs/board.md MSPI section).
 * Presets only (level 0..3 = 50/70/90/100 %), no off, no tach. Quiet rule (docs/fan.md): level 0 at boot, 100 %
 * only for a die >= 110 C or an LM75 >= 80 C; read failures stay at level 0 and log. FAN_QUIET 0 = old policy.
 */

#include <command.h>
#include <linux/errno.h>
#include <linux/delay.h>
#include <time.h>
#include <vsprintf.h>
#include <asm/io.h>
#include "bcm_cmic.h"
#include "us48_fan.h"
#include "us48_pcie.h"

#define MSPI		0x1500			/* CMIC MSPI block, unit 0 only */
#define GP		0x2000			/* CMIC GP block: DATA_OUT +4, OUT_EN +8 */
#define BSPI_MAST_N_BOOT 0x1680
#define RING_MAP_16_23	0x100a0			/* nibble 0 = ring of block 16 (TOP); stock value 0x00005004 */
#define TOP_BLK		16
#define PVT_CTRL0	0x02050000
#define PVT_CTRL1	0x02050400
#define PVT_RESULT	0x02050800
#define PVT_CTRL0_STOCK	0x001d2003		/* stock switchdrvr write (docs/clocks.md PVTMON) */
#define FAN_QUIET	1			/* 0 = pre-2026-11-15 policy: die >= 100 C or any read failure = 100 % */
#define BOARD_HOT	800			/* LM75 >= 80 C: 100 % */
#define DIE_HOT		(FAN_QUIET ? 1100 : 1000)	/* PVTMON steady-state rating 110 C (docs/clock-audit.md) */

/* iProc SMBus master in the unit 0 CMIC (docs/board.md I2C); register layout = i2c-bcm-iproc.c */
#define SMB_CFG		0x00
#define SMB_TIM		0x04
#define SMB_FIFO	0x0c
#define SMB_CMD		0x30
#define SMB_TX		0x40
#define SMB_RX		0x44
#define PCA9548		0x70
#define MUX_LM75_CH	6
/* Process call = 5 bytes + start/stop at 100 kHz = ~0.5 ms (I); 70 polls x 10 us = 0.7 ms = 1.4x, expiry = read fail */
#define SMB_POLLS	70

/* SPIF: 7 bytes at SPBR 8 on the APB clock = ~10 us (I); 1000 STATUS reads (~0.5 us each) = 50x, expiry = fail + log */
#define SPIF_POLLS	1000

static struct bcm_cmic_unit unit0 = { .base = (void __iomem *)CONFIG_BCM_CMIC_UNIT0_BASE };
static struct bcm_cmic_unit unit1 = { .base = (void __iomem *)CONFIG_BCM_CMIC_UNIT1_BASE };

static bool unit1_ok;

/* One frame to the chain selected by A = 1 (unit 0 GP0) and B = group (unit 1 GP0); bytes 1-6 stay 0 (PoE bits) */
static int fan_frame(int group, u8 level, struct us48_fan_rb *rb)
{
	void __iomem *m = unit0.base + MSPI;
	void __iomem *g0 = unit0.base + GP, *g1 = unit1.base + GP;
	u32 boot = readl(unit0.base + BSPI_MAST_N_BOOT);
	int i, n;

	writel(group, g1 + 4);			/* B */
	writel(1, g1 + 8);
	writel(1, g0 + 4);			/* A */
	writel(1, g0 + 8);
	writel(1, unit0.base + BSPI_MAST_N_BOOT);	/* MSPI owns the bus */
	writel(0x8d, m);			/* mode 3, SPBR 8 */
	writel(0x83, m + 4);
	writel(1, m + 8);
	writel(1, m + 12);
	for (i = 0; i < 7; i++) {
		writel(i ? 0 : level << 4, m + 0x40 + 8 * i);
		writel(i < 6 ? 0x80 : 0, m + 0x140 + 4 * i);
	}
	writel(0, m + 0x10);
	writel(6, m + 0x14);
	writel(0, m + 0x20);
	writel(0x40, m + 0x18);
	for (n = 0; n < SPIF_POLLS && !(readl(m + 0x20) & 1); n++)
		;
	rb->spif = n < SPIF_POLLS;
	rb->polls = n;
	rb->status = readl(m + 0x20);
	rb->endqp = readl(m + 0x14);
	for (i = 0; i < 7; i++)
		rb->tx[i] = readl(m + 0x40 + 8 * i);
	rb->a_out = readl(g0 + 4);
	rb->a_oe = readl(g0 + 8);
	rb->b_out = readl(g1 + 4);
	rb->b_oe = readl(g1 + 8);
	writel(boot, unit0.base + BSPI_MAST_N_BOOT);
	return rb->spif ? 0 : -ETIMEDOUT;
}

static int fan_group(int group, u8 level, bool quiet)
{
	struct us48_fan_rb rb;
	int ret = fan_frame(group, level, &rb);
	int ok = !ret && rb.tx[0] == (u32)level << 4 && !rb.tx[1] && !rb.tx[2] && !rb.tx[3] && !rb.tx[4] &&
		 !rb.tx[5] && !rb.tx[6] && rb.endqp == 6 && (rb.a_out & 1) == 1 && (rb.b_out & 1) == (u32)group &&
		 (rb.a_oe & 1) && (rb.b_oe & 1);

	printf("fan: group %d (unit %d) level %d (%d %%) %s: SPIF %d polls %d status 0x%x tx %02x %02x %02x %02x %02x %02x %02x A out/oe %x/%x B out/oe %x/%x\n",
	       group, group, level, 50 + 20 * level > 100 ? 100 : 50 + 20 * level, ok ? "ok" : "FAILED", rb.spif, rb.polls,
	       rb.status, rb.tx[0], rb.tx[1], rb.tx[2], rb.tx[3], rb.tx[4], rb.tx[5], rb.tx[6], rb.a_out, rb.a_oe,
	       rb.b_out, rb.b_oe);
	(void)quiet;
	return ok ? 0 : -EIO;
}

/* Die temperature in 0.1 C, or negative errno. PVTMON via S-channel; stock init only when the TOP ring/PVT is down. */
static int pvt_read(struct bcm_cmic_unit *u, int *tenths, int *peak)
{
	u32 ring = readl(u->base + RING_MAP_16_23), v = 0, code;
	int ret, ms;

	if (ring == 0xffffffff)
		return -ENODEV;
	if ((ring & 0xf) != 4)
		writel(0x00005004, u->base + RING_MAP_16_23);
	ret = bcm_cmic_reg_read(u, TOP_BLK, PVT_CTRL1, &v, 1, NULL);
	if (ret)
		return ret;
	if (!(v & BIT(31))) {
		u32 w;

		w = PVT_CTRL0_STOCK;
		bcm_cmic_reg_write(u, TOP_BLK, PVT_CTRL0, &w, 1);
		w = 0;
		bcm_cmic_reg_write(u, TOP_BLK, PVT_CTRL1, &w, 1);
		w = BIT(31);
		bcm_cmic_reg_write(u, TOP_BLK, PVT_CTRL1, &w, 1);
	}
	/* First conversion after PVT init: time unmeasured (I); poll 1 ms steps, 20 ms limit, expiry = read fail */
	for (ms = 0; ms < 20; ms++) {
		ret = bcm_cmic_reg_read(u, TOP_BLK, PVT_RESULT, &v, 1, NULL);
		if (ret)
			return ret;
		code = v & 0x3ff;
		if (code) {
			int t = (4180000 - 5556 * (int)code) / 1000;

			if (t < -400 || t > 1500)
				return -ERANGE;
			*tenths = t;
			*peak = (4180000 - 5556 * (int)((v >> 22) & 0x3ff)) / 1000;
			return 0;
		}
		mdelay(1);
	}
	return -ETIMEDOUT;
}

static void pvt_show(const char *name, struct bcm_cmic_unit *u, int *hot)
{
	int t = 0, pk = 0, r = pvt_read(u, &t, &pk);

	if (r) {
		printf("fan: %s PVTMON read failed or bogus (%d)%s\n", name, r, FAN_QUIET ? ": staying at level 0" : "");
		*hot |= !FAN_QUIET;
		return;
	}
	printf("fan: %s PVTMON %d.%d C (peak %d.%d C)\n", name, t / 10, (int)abs(t % 10), pk / 10, (int)abs(pk % 10));
	if (t >= DIE_HOT)
		*hot = 1;
}

/* Queue is TX FIFO bytes (bit 31 = last), then start; wait for START_BUSY clear. 0 = ok, else errno */
static int smb_xfer(const u32 *tx, int ntx, u32 cmd)
{
	void __iomem *b = unit0.base;
	int i, n;

	if (readl(b + SMB_CMD) & BIT(31))
		return -EBUSY;
	writel(BIT(31) | BIT(30), b + SMB_FIFO);
	for (i = 0; i < ntx; i++)
		writel(tx[i], b + SMB_TX);
	writel(BIT(31) | cmd, b + SMB_CMD);
	for (n = 0; n < SMB_POLLS && (readl(b + SMB_CMD) & BIT(31)); n++)
		udelay(10);
	if (n == SMB_POLLS) {
		writel(BIT(31) | BIT(30), b + SMB_FIFO);
		return -ETIMEDOUT;
	}
	return ((readl(b + SMB_CMD) >> 25) & 7) ? -EIO : 0;
}

static void smb_init(void)
{
	void __iomem *b = unit0.base;

	if (readl(b + SMB_CFG) & BIT(30))	/* stock/earlier stage left it enabled */
		return;
	writel(BIT(31), b + SMB_CFG);		/* reset >= 100 us (i2c-bcm-iproc.c) */
	udelay(150);
	writel(0x400f0c00, b + SMB_CFG);	/* stock value: enabled, 100 kHz */
	writel(0x19003200, b + SMB_TIM);
}

/* LM75 on mux channel 6, 0.1 C or negative errno; mux select + pointer write are the only I2C writes */
static int lm75_read(int addr, int *tenths)
{
	u32 sel[2] = { PCA9548 << 1, BIT(31) | BIT(MUX_LM75_CH) };
	u32 off[2] = { PCA9548 << 1, BIT(31) };
	u32 pc[3] = { addr << 1, 0, BIT(31) | (addr << 1 | 1) };
	int ret, msb, lsb;

	ret = smb_xfer(sel, 2, 7 << 9);
	if (!ret) {
		ret = smb_xfer(pc, 3, 0xa << 9 | 2);
		if (!ret) {
			msb = readl(unit0.base + SMB_RX) & 0xff;
			lsb = readl(unit0.base + SMB_RX) & 0xff;
			*tenths = ((s8)msb * 2 + (lsb >> 7)) * 5;
		}
		smb_xfer(off, 2, 7 << 9);
	}
	return ret;
}

/* All four LM75s (0x48..0x4b); *hot = any >= 80 C or any unreadable */
static void lm75_show(int *hot)
{
	int a, t = 0, r;

	smb_init();
	for (a = 0x48; a <= 0x4b; a++) {
		r = lm75_read(a, &t);
		if (r) {
			printf("fan: LM75 0x%02x read failed (%d)%s\n", a, r, FAN_QUIET ? ": staying at level 0" : "");
			*hot |= !FAN_QUIET;
			continue;
		}
		printf("fan: LM75 0x%02x %d.%d C\n", a, t / 10, (int)abs(t % 10));
		if (t >= BOARD_HOT)
			*hot = 1;
	}
}

/* Both groups, in the order unit 1 (needs its BAR) then unit 0 */
static int fan_all(u8 level)
{
	int r = 0;

	if (!unit1_ok || !us48_pcie_up()) {
		printf("fan: unit 1 not reachable, no frame sent (select line B undriven)\n");
		return -ENODEV;
	}
	r |= fan_group(1, level, false);
	r |= fan_group(0, level, false);
	return r;
}

/* Called once from board_init (console up): probe unit 1, level 0 on both groups, temps, 100 % only on an emergency */
void us48_fan_boot(void)
{
	int hot = 0;

	if (us48_pcie_up()) {
		printf("fan: probing unit 1 BAR at 0x%08x\n", CONFIG_BCM_CMIC_UNIT1_BASE);
		unit1_ok = readl(unit1.base + RING_MAP_16_23) != 0xffffffff;
	} else {
		printf("fan: PCIe link to unit 1 down, no unit 1 access; fans left at their power-on state (default speed unknown, docs/fan.md)\n");
	}
	fan_all(0);
	lm75_show(&hot);
	pvt_show("unit 0", &unit0, &hot);
	if (unit1_ok)
		pvt_show("unit 1", &unit1, &hot);
	else
		hot |= !FAN_QUIET;
	if (hot) {
		printf("fan: emergency (LM75 >= 80 C or die >= %d C): 100 %%\n", DIE_HOT / 10);
		fan_all(3);
	} else {
		printf("fan: no emergency, staying at level 0 (50 %%)\n");
	}
}

static int do_fan(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	int hot = 0;
	ulong level;
	char *end;

	if (argc == 1) {
		lm75_show(&hot);
		pvt_show("unit 0", &unit0, &hot);
		if (unit1_ok)
			pvt_show("unit 1", &unit1, &hot);
		return CMD_RET_SUCCESS;
	}
	level = simple_strtoul(argv[1], &end, 0);
	if (argc != 2 || !*argv[1] || *end || level > 3)
		return CMD_RET_USAGE;
	return fan_all(level) ? CMD_RET_FAILURE : CMD_RET_SUCCESS;
}

U_BOOT_CMD(fan, 2, 0, do_fan, "fan preset (both switch chips) / temperatures",
	   "<0-3>  - 0 = 50 %, 1 = 70 %, 2 = 90 %, 3 = 100 % (no off); prints the frame readback\n"
	   "fan      - print LM75 and PVTMON temperatures");
