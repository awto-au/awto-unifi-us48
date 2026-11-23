// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_irq: CMIC CMC0 IRQ_STAT0/1 + UC0_IRQ_MASK0/1 as an interrupt controller behind GIC SPI 162 (#158, #170).
 * hwirq 0..31 = STAT1 bits (0 = I2C_INTR), hwirq 32..63 = STAT0 bits (44 = ch1 RX DESC_DONE). Routing bench-verified
 * 2026-11-23: UC0_IRQ_MASK1 raises GIC ID 194 (SPI 162), PCIE_IRQ_MASK1 ID 196, UC1_IRQ_MASK1 ID 195 (docs/driver-internals.md).
 * Only this driver's mask bits are touched. Stray guard: a pending+unmasked bit with no handler is masked and counted.
 */
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/irq.h>
#include <linux/irqchip/chained_irq.h>
#include <linux/irqdomain.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_irq.h>
#include <linux/platform_device.h>
#include <linux/sysfs.h>

#define STAT0	0x00	/* CMIC_CMC0_IRQ_STAT0 (0x48031400) */
#define STAT1	0x04	/* CMIC_CMC0_IRQ_STAT1 (0x48031404): read-only mirror of the block status */
#define MASK0	0x28	/* CMIC_CMC0_UC0_IRQ_MASK0 (0x48031428) */
#define MASK1	0x2c	/* CMIC_CMC0_UC0_IRQ_MASK1 (0x4803142c) */
#define NBITS	64
#define BANK(hw)	((hw) < 32 ? 1 : 0)	/* 0 = STAT0/MASK0 (hwirq 32..63), 1 = STAT1/MASK1 (hwirq 0..31) */

struct cmic_irq {
	void __iomem *base;
	struct irq_domain *dom;
	raw_spinlock_t lock;
	struct irq_chip chip;
	atomic_t stray[NBITS];		/* pending + unmasked with no handler: bit masked, counted */
	atomic_t nopend;		/* GIC interrupt with nothing pending and unmasked */
};

static const unsigned int stat_off[2] = { STAT0, STAT1 };
static const unsigned int mask_off[2] = { MASK0, MASK1 };

/* lock held */
static void cmic_irq_rmw(struct cmic_irq *c, irq_hw_number_t hw, bool on)
{
	void __iomem *m = c->base + mask_off[BANK(hw)];
	u32 v = readl(m), b = BIT(hw % 32);

	writel(on ? v | b : v & ~b, m);
}

static void cmic_irq_mask(struct irq_data *d)
{
	struct cmic_irq *c = irq_data_get_irq_chip_data(d);
	unsigned long flags;

	raw_spin_lock_irqsave(&c->lock, flags);
	cmic_irq_rmw(c, d->hwirq, false);
	raw_spin_unlock_irqrestore(&c->lock, flags);
}

static void cmic_irq_unmask(struct irq_data *d)
{
	struct cmic_irq *c = irq_data_get_irq_chip_data(d);
	unsigned long flags;

	raw_spin_lock_irqsave(&c->lock, flags);
	cmic_irq_rmw(c, d->hwirq, true);
	raw_spin_unlock_irqrestore(&c->lock, flags);
}

static void cmic_irq_handler(struct irq_desc *desc)
{
	struct cmic_irq *c = irq_desc_get_handler_data(desc);
	struct irq_chip *parent = irq_desc_get_chip(desc);
	unsigned long pend, flags;
	unsigned int bit, bank, hw;
	bool any = false;

	chained_irq_enter(parent, desc);
	for (bank = 0; bank < 2; bank++) {
		pend = readl(c->base + stat_off[bank]) & readl(c->base + mask_off[bank]);
		for_each_set_bit(bit, &pend, 32) {
			hw = bank ? bit : 32 + bit;
			any = true;
			if (!generic_handle_domain_irq(c->dom, hw))
				continue;
			raw_spin_lock_irqsave(&c->lock, flags);
			cmic_irq_rmw(c, hw, false);
			raw_spin_unlock_irqrestore(&c->lock, flags);
			if (atomic_inc_return(&c->stray[hw]) == 1)
				pr_err("us48_cmic_irq: stray bit %s%u pending with no handler, masked\n", bank ? "STAT1." : "STAT0.", bit);
		}
	}
	if (!any && atomic_inc_return(&c->nopend) == 1)
		pr_err("us48_cmic_irq: GIC interrupt with no pending unmasked bit (STAT0 %08x STAT1 %08x)\n",
		       readl(c->base + STAT0), readl(c->base + STAT1));
	chained_irq_exit(parent, desc);
}

static ssize_t stray_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct cmic_irq *c = dev_get_drvdata(dev);
	int n = 0, i;

	n += sysfs_emit_at(buf, n, "nopend %d\n", atomic_read(&c->nopend));
	for (i = 0; i < NBITS; i++)
		if (atomic_read(&c->stray[i]))
			n += sysfs_emit_at(buf, n, "%s.%d %d\n", BANK(i) ? "STAT1" : "STAT0", i % 32, atomic_read(&c->stray[i]));
	n += sysfs_emit_at(buf, n, "mask0 %08x mask1 %08x\n", readl(c->base + MASK0), readl(c->base + MASK1));
	return n;
}
static DEVICE_ATTR_RO(stray);

static int cmic_irq_map(struct irq_domain *dom, unsigned int virq, irq_hw_number_t hw)
{
	struct cmic_irq *c = dom->host_data;

	irq_set_chip_data(virq, c);
	irq_set_chip_and_handler(virq, &c->chip, handle_level_irq);
	irq_set_noprobe(virq);
	return 0;
}

static const struct irq_domain_ops cmic_irq_ops = {
	.map = cmic_irq_map,
	.xlate = irq_domain_xlate_onecell,
};

static int cmic_irq_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct resource *res;
	struct cmic_irq *c;
	int parent;

	c = devm_kzalloc(dev, sizeof(*c), GFP_KERNEL);
	if (!c)
		return -ENOMEM;
	res = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	if (!res)
		return -EINVAL;
	/* shares the CMIC window with us48_cmic: map without claiming the region */
	c->base = devm_ioremap(dev, res->start, resource_size(res));
	if (!c->base)
		return -ENOMEM;
	parent = platform_get_irq(pdev, 0);
	if (parent < 0)
		return parent;
	raw_spin_lock_init(&c->lock);
	c->chip.name = "us48-cmic-irq";
	c->chip.irq_mask = cmic_irq_mask;
	c->chip.irq_unmask = cmic_irq_unmask;
	c->dom = irq_domain_create_linear(of_fwnode_handle(dev->of_node), NBITS, &cmic_irq_ops, c);
	if (!c->dom)
		return -ENOMEM;
	irq_set_chained_handler_and_data(parent, cmic_irq_handler, c);
	platform_set_drvdata(pdev, c);
	device_create_file(dev, &dev_attr_stray);
	dev_info(dev, "CMC0 IRQ_STAT0/1 behind GIC irq %d, UC0_IRQ_MASK0/1 0x%08x/0x%08x\n", parent, readl(c->base + MASK0),
		 readl(c->base + MASK1));
	return 0;
}

static const struct of_device_id cmic_irq_of[] = {
	{ .compatible = "ubnt,us48-cmic-irq" },
	{ }
};
MODULE_DEVICE_TABLE(of, cmic_irq_of);

static struct platform_driver cmic_irq_driver = {
	.driver = { .name = "us48-cmic-irq", .of_match_table = cmic_irq_of, .suppress_bind_attrs = true },
	.probe = cmic_irq_probe,
};

/* no module_exit: the SMBus driver keeps our interrupt for its lifetime, so the module is never unloaded */
static int __init cmic_irq_init(void)
{
	return platform_driver_register(&cmic_irq_driver);
}
module_init(cmic_irq_init);

MODULE_DESCRIPTION("UniFi US-48-500W CMIC CMC0 interrupt controller (SMBus interrupt)");
MODULE_LICENSE("GPL");
