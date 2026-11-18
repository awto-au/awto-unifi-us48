// SPDX-License-Identifier: GPL-2.0
/* bcm-cmic MIIM, clause 22 read. Register use as scripts/miim.py (verified on unit 0). */
#include <linux/bitops.h>
#include <linux/delay.h>
#include <linux/errno.h>
#include <linux/io.h>
#include <linux/printk.h>
#include "bcm_cmic.h"

#define MIIM_RD_START	BIT(1)
#define MIIM_OPN_DONE	BIT(0)
/* C22 frame = 64 bits at ~2.5 MHz MDC = 26 us (miim.py); the Linux driver needed 65 us in practice (32 us timed out
 * on ge1 under Linux, 2026-11-14), so use its bound. Expiry = log + -ETIMEDOUT. */
#define MIIM_WAIT_US	65

int bcm_cmic_miim_read(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 reg, u16 *val)
{
	u32 stat = 0;
	int i;

	writel(0, u->base + BCM_CMIC_MIIM_CTRL);
	writel((u32)internal << 25 | bus << 22 | phy << 16, u->base + BCM_CMIC_MIIM_PARAM);
	writel(reg & 0x1f, u->base + BCM_CMIC_MIIM_ADDR);
	writel(MIIM_RD_START, u->base + BCM_CMIC_MIIM_CTRL);
	for (i = 0; i < MIIM_WAIT_US; i++) {
		stat = readl(u->base + BCM_CMIC_MIIM_STAT);
		if (stat & MIIM_OPN_DONE)
			break;
		udelay(1);
	}
	if (!(stat & MIIM_OPN_DONE)) {
		pr_err("bcm-cmic: miim %s bus %u phy %u reg %u: no OPN_DONE after >= %d us\n",
		       internal ? "int" : "ext", bus, phy, reg, i);
		writel(0, u->base + BCM_CMIC_MIIM_CTRL);
		return -ETIMEDOUT;
	}
	*val = readl(u->base + BCM_CMIC_MIIM_RDATA) & 0xffff;
	writel(0, u->base + BCM_CMIC_MIIM_CTRL);
	return 0;
}

#define MIIM_WR_START	BIT(0)

int bcm_cmic_miim_write(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 reg, u16 val)
{
	u32 stat = 0;
	int i;

	writel(0, u->base + BCM_CMIC_MIIM_CTRL);
	writel((u32)internal << 25 | bus << 22 | phy << 16 | val, u->base + BCM_CMIC_MIIM_PARAM);
	writel(reg & 0x1f, u->base + BCM_CMIC_MIIM_ADDR);
	writel(MIIM_WR_START, u->base + BCM_CMIC_MIIM_CTRL);
	for (i = 0; i < MIIM_WAIT_US; i++) {
		stat = readl(u->base + BCM_CMIC_MIIM_STAT);
		if (stat & MIIM_OPN_DONE)
			break;
		udelay(1);
	}
	writel(0, u->base + BCM_CMIC_MIIM_CTRL);
	if (!(stat & MIIM_OPN_DONE)) {
		pr_err("bcm-cmic: miim %s bus %u phy %u reg %u write: no OPN_DONE after >= %d us\n",
		       internal ? "int" : "ext", bus, phy, reg, i);
		return -ETIMEDOUT;
	}
	return 0;
}

/*
 * Broadcom shadow (reg 0x1c) and RDB accesses, sequence in docs/driver-internals.md 12.4 (stock 0x10c9be4).
 * Shadow read = selector write; RDB enter (exp 0xf7e <- 0) and exit (RDB 0x87 <- 0x8000) are mode writes.
 */
int bcm_cmic_miim_shadow_read(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 shadow, u16 *val)
{
	int ret = bcm_cmic_miim_write(u, internal, bus, phy, 0x1c, (shadow & 0x1f) << 10);

	return ret ?: bcm_cmic_miim_read(u, internal, bus, phy, 0x1c, val);
}

int bcm_cmic_miim_shadow_write(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 shadow, u16 val)
{
	return bcm_cmic_miim_write(u, internal, bus, phy, 0x1c,
				   BIT(15) | (shadow & 0x1f) << 10 | (val & 0x03ff));
}

/* One RDB access: enter, 0x1e <- reg, 0x1f data. Exit runs whenever the first enter write landed (no half-entered PHY). */
static int miim_rdb(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 reg, u16 *rd, u16 wr)
{
	int ret = bcm_cmic_miim_write(u, internal, bus, phy, 0x17, 0x0f7e), exit_ret;

	if (ret)
		return ret;
	ret = bcm_cmic_miim_write(u, internal, bus, phy, 0x15, 0x0000);
	ret = ret ?: bcm_cmic_miim_write(u, internal, bus, phy, 0x1e, reg);
	ret = ret ?: (rd ? bcm_cmic_miim_read(u, internal, bus, phy, 0x1f, rd) :
		      bcm_cmic_miim_write(u, internal, bus, phy, 0x1f, wr));
	exit_ret = bcm_cmic_miim_write(u, internal, bus, phy, 0x1e, 0x0087);
	exit_ret = exit_ret ?: bcm_cmic_miim_write(u, internal, bus, phy, 0x1f, 0x8000);
	return ret ?: exit_ret;
}

int bcm_cmic_miim_rdb_read(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 reg, u16 *val)
{
	return miim_rdb(u, internal, bus, phy, reg, val, 0);
}

int bcm_cmic_miim_rdb_write(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 reg, u16 val)
{
	return miim_rdb(u, internal, bus, phy, reg, NULL, val);
}
