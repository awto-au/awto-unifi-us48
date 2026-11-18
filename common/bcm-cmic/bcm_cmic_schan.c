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
#define SCHAN_NACK	BIT(21)
#define SCHAN_ERRS	(BIT(20) | BIT(21) | BIT(22))
/* One SBUS round trip is a few us; 10 ms backstop. Expiry = abort pulse + log + -ETIMEDOUT. */
#define SCHAN_WAIT_US	10000

int bcm_cmic_schan_raw(struct bcm_cmic_unit *u, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	bool keyed = op == BCM_SCHAN_TABLE_LOOKUP || op == BCM_SCHAN_TABLE_DELETE;
	bool send = op == BCM_SCHAN_WRITE_REG || op == BCM_SCHAN_WRITE_MEM || op == BCM_SCHAN_TABLE_INSERT ||
		    keyed;
	int reply = op == BCM_SCHAN_TABLE_LOOKUP ? words + 1 : send ? 0 : words;
	void __iomem *ctrl_reg = u->base + BCM_CMIC_SCHAN_CTRL;
	u32 ctrl = 0;
	int i;

	if (words < 1 || words + 2 > BCM_CMIC_SCHAN_MSG_WORDS - 1)
		return -EINVAL;

	writel(0, ctrl_reg);
	writel(op << 26 | blk << 20 | (4 * words) << 7, u->base + BCM_CMIC_SCHAN_MSG(0));
	writel(addr, u->base + BCM_CMIC_SCHAN_MSG(1));
	for (i = 0; send && i < words; i++)
		writel(data[i], u->base + BCM_CMIC_SCHAN_MSG(2 + i));
	writel(SCHAN_START, ctrl_reg);

	for (i = 0; i < SCHAN_WAIT_US; i++) {
		ctrl = readl(ctrl_reg);
		if (ctrl & SCHAN_DONE)	/* error bits alone can be stale mid-op: DONE only */
			break;
		udelay(1);
	}
	if (ack)
		*ack = readl(u->base + BCM_CMIC_SCHAN_MSG(0));
	/* key not in the table: an answer, not a fault (no abort, no log) */
	if (keyed && (ctrl & SCHAN_DONE) && (ctrl & SCHAN_ERRS) == SCHAN_NACK) {
		writel(0, ctrl_reg);
		return -ENOENT;
	}
	if (!(ctrl & SCHAN_DONE) || (ctrl & SCHAN_ERRS)) {
		pr_err("bcm-cmic: schan op %02x blk %u addr %08x: ctrl %08x after >= %d us\n",
		       op, blk, addr, ctrl, i);
		/* abort pulse recovers a stuck op (driver-internals 1) */
		writel(SCHAN_ABORT, ctrl_reg);
		writel(0, ctrl_reg);
		return (ctrl & SCHAN_ERRS) ? -EIO : -ETIMEDOUT;
	}
	for (i = 0; i < reply; i++)
		data[i] = readl(u->base + BCM_CMIC_SCHAN_MSG(1 + i));
	writel(0, ctrl_reg);
	return 0;
}

int bcm_cmic_schan_op(struct bcm_cmic_unit *u, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	if (u->schan_op)
		return u->schan_op(u, op, blk, addr, data, words, ack);
	return bcm_cmic_schan_raw(u, op, blk, addr, data, words, ack);
}
