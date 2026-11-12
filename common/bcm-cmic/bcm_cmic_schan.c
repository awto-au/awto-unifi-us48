// SPDX-License-Identifier: GPL-2.0
/* bcm-cmic S-channel: handshake of docs/driver-internals.md section 1, reference us48_cmic.c sbus(). */
#include <linux/bitops.h>
#include <linux/delay.h>
#include <linux/errno.h>
#include <linux/io.h>
#include <linux/printk.h>
#include "bcm_cmic.h"

#define SCHAN_START	BIT(0)
#define SCHAN_DONE	BIT(1)
#define SCHAN_ABORT	BIT(2)
#define SCHAN_ERRS	(BIT(20) | BIT(21) | BIT(22))
/* One SBUS round trip is a few us; the SBUS timeout reg is 2000 cycles. 1 ms cap = abort + log. */
#define SCHAN_WAIT_US	1000

int bcm_cmic_schan_op(struct bcm_cmic_unit *u, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	bool write = op == BCM_SCHAN_WRITE_REG || op == BCM_SCHAN_WRITE_MEM;
	void __iomem *ctrl_reg = u->base + BCM_CMIC_SCHAN_CTRL;
	u32 ctrl = 0;
	int i;

	if (words < 1 || words > BCM_CMIC_SCHAN_MSG_WORDS - 2)
		return -EINVAL;

	writel(0, ctrl_reg);
	writel(op << 26 | blk << 20 | (4 * words) << 7, u->base + BCM_CMIC_SCHAN_MSG(0));
	writel(addr, u->base + BCM_CMIC_SCHAN_MSG(1));
	for (i = 0; write && i < words; i++)
		writel(data[i], u->base + BCM_CMIC_SCHAN_MSG(2 + i));
	writel(SCHAN_START, ctrl_reg);

	for (i = 0; i < SCHAN_WAIT_US; i++) {
		ctrl = readl(ctrl_reg);
		if (ctrl & (SCHAN_DONE | SCHAN_ERRS))
			break;
		udelay(1);
	}
	if (!(ctrl & (SCHAN_DONE | SCHAN_ERRS)) || (ctrl & SCHAN_ERRS)) {
		pr_err("bcm-cmic: schan op %02x blk %u addr %08x: ctrl %08x after >= %d us\n",
		       op, blk, addr, ctrl, i);
		/* abort pulse recovers a stuck op (driver-internals 1) */
		writel(SCHAN_ABORT, ctrl_reg);
		writel(0, ctrl_reg);
		return (ctrl & SCHAN_ERRS) ? -EIO : -ETIMEDOUT;
	}
	if (ack)
		*ack = readl(u->base + BCM_CMIC_SCHAN_MSG(0));
	for (i = 0; !write && i < words; i++)
		data[i] = readl(u->base + BCM_CMIC_SCHAN_MSG(1 + i));
	writel(0, ctrl_reg);
	return 0;
}
