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
/* C22 frame = 64 bits at ~2.5 MHz MDC = 26 us (miim.py); x1.25 = 32 us. Expiry = log + -ETIMEDOUT. */
#define MIIM_WAIT_US	32

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
