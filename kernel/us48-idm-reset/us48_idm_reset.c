// SPDX-License-Identifier: GPL-2.0
/*
 * us48_idm_reset: reset controller for the IDM wrapper of the PL330 DMAC (hwdb dmac_idm, #84).
 * RESET_CONTROL bit 0 = 1 holds the DMAC in reset (reset value); pl330 deasserts it through DT `resets`.
 * Sequence and sources: docs/core-bringup.md "PL330 release sequence".
 */
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/reset-controller.h>
#include <linux/seq_file.h>
#include "us48_hwdb.h"

#define RST_REG		HW_DMAC_IDM_RESET_CONTROL
#define RST_BIT		HW_DMAC_IDM_RESET_CONTROL_RESET_MASK
/* IO_CONTROL_DIRECT bit 0: CLK_GATING_EN in the regdb, "clk enable" in the D-Link SATA/NSP code */
#define IOC_REG		HW_DMAC_IDM_IO_CONTROL_DIRECT
#define IOC_CLK_BIT	BIT(0)
/* D-Link GPL board_bu.c config_AHCI: udelay(1000) after each IDM write; -10 % */
#define SETTLE_US	900
/* Read-back of a posted IDM write: one AXI round trip, expected under 1 us; 2 us = 1.25x rounded up. On expiry -ETIMEDOUT */
#define READBACK_US	2

static bool clk_en;
module_param(clk_en, bool, 0444);
MODULE_PARM_DESC(clk_en, "set IO_CONTROL_DIRECT bit 0 (+ settle) before releasing the reset (default 0: BISDN/D-Link SDIO sequence)");

struct idm_rst {
	void __iomem *base;
	struct reset_controller_dev rcdev;
};

static struct idm_rst *to_idm(struct reset_controller_dev *r)
{
	return container_of(r, struct idm_rst, rcdev);
}

static int idm_assert(struct reset_controller_dev *r, unsigned long id)
{
	struct idm_rst *p = to_idm(r);

	writel(readl(p->base + RST_REG) | RST_BIT, p->base + RST_REG);
	return 0;
}

static int idm_deassert(struct reset_controller_dev *r, unsigned long id)
{
	struct idm_rst *p = to_idm(r);
	u32 v;
	int ret;

	dev_info(r->dev, "release: IO_CONTROL_DIRECT=0x%08x RESET_CONTROL=0x%08x clk_en=%d\n",
		 readl(p->base + IOC_REG), readl(p->base + RST_REG), clk_en);
	if (clk_en) {
		writel(readl(p->base + IOC_REG) | IOC_CLK_BIT, p->base + IOC_REG);
		readl(p->base + IOC_REG);
		fsleep(SETTLE_US);
	}
	writel(readl(p->base + RST_REG) & ~RST_BIT, p->base + RST_REG);
	ret = readl_poll_timeout_atomic(p->base + RST_REG, v, !(v & RST_BIT), 1, READBACK_US);
	if (ret) {
		dev_err(r->dev, "RESET_CONTROL still 0x%08x after %u us read-back\n", v, READBACK_US);
		return ret;
	}
	fsleep(SETTLE_US);
	dev_info(r->dev, "released: RESET_CONTROL=0x%08x\n", readl(p->base + RST_REG));
	return 0;
}

static int idm_status(struct reset_controller_dev *r, unsigned long id)
{
	return !!(readl(to_idm(r)->base + RST_REG) & RST_BIT);
}

static const struct reset_control_ops idm_ops = {
	.assert = idm_assert,
	.deassert = idm_deassert,
	.status = idm_status,
};

/* Read-only register view (/sys/kernel/debug/us48_idm_reset/regs): no /dev/mem needed */
static int idm_regs_show(struct seq_file *m, void *unused)
{
	struct idm_rst *p = m->private;

	seq_printf(m, "RESET_CONTROL=0x%08x IO_CONTROL_DIRECT=0x%08x\n", readl(p->base + RST_REG), readl(p->base + IOC_REG));
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(idm_regs);

static void idm_dbg_remove(void *dir)
{
	debugfs_remove(dir);
}

static int idm_probe(struct platform_device *pdev)
{
	struct idm_rst *p = devm_kzalloc(&pdev->dev, sizeof(*p), GFP_KERNEL);
	struct dentry *dir;

	if (!p)
		return -ENOMEM;
	p->base = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(p->base))
		return PTR_ERR(p->base);
	p->rcdev.owner = THIS_MODULE;
	p->rcdev.dev = &pdev->dev;
	p->rcdev.of_node = pdev->dev.of_node;
	p->rcdev.ops = &idm_ops;
	p->rcdev.nr_resets = 1;
	dev_info(&pdev->dev, "RESET_CONTROL=0x%08x IO_CONTROL_DIRECT=0x%08x (no write at probe)\n",
		 readl(p->base + RST_REG), readl(p->base + IOC_REG));
	dir = debugfs_create_dir("us48_idm_reset", NULL);
	debugfs_create_file("regs", 0444, dir, p, &idm_regs_fops);
	devm_add_action_or_reset(&pdev->dev, idm_dbg_remove, dir);
	return devm_reset_controller_register(&pdev->dev, &p->rcdev);
}

static const struct of_device_id idm_of_match[] = {
	{ .compatible = "awto,hr2-dmac-idm-reset" },
	{ }
};
MODULE_DEVICE_TABLE(of, idm_of_match);

static struct platform_driver idm_driver = {
	.probe = idm_probe,
	.driver = { .name = "us48_idm_reset", .of_match_table = idm_of_match },
};
module_platform_driver(idm_driver);

MODULE_DESCRIPTION("HR2 PL330 DMAC IDM wrapper reset controller");
MODULE_LICENSE("GPL");
