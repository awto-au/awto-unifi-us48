// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_irq: CMIC CMC0 IRQ_STAT1 / UC0_IRQ_MASK1 as an interrupt controller behind GIC SPI 162 (#158).
 * Bit 0 = I2C_INTR (SMBus master). Routing bench-verified 2026-11-23: UC0_IRQ_MASK1 raises GIC ID 194 (SPI 162),
 * PCIE_IRQ_MASK1 ID 196, UC1_IRQ_MASK1 ID 195 (docs/board.md). Only this driver's mask bits are touched.
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

#define STAT1	0x00	/* CMIC_CMC0_IRQ_STAT1 (0x48031404): read-only mirror of the block status */
#define MASK1	0x28	/* CMIC_CMC0_UC0_IRQ_MASK1 (0x4803142c) */
#define NBITS	32

struct cmic_irq {
	void __iomem *base;
	struct irq_domain *dom;
	raw_spinlock_t lock;
	struct irq_chip chip;
};

static void cmic_irq_mask(struct irq_data *d)
{
	struct cmic_irq *c = irq_data_get_irq_chip_data(d);
	unsigned long flags;

	raw_spin_lock_irqsave(&c->lock, flags);
	writel(readl(c->base + MASK1) & ~BIT(d->hwirq), c->base + MASK1);
	raw_spin_unlock_irqrestore(&c->lock, flags);
}

static void cmic_irq_unmask(struct irq_data *d)
{
	struct cmic_irq *c = irq_data_get_irq_chip_data(d);
	unsigned long flags;

	raw_spin_lock_irqsave(&c->lock, flags);
	writel(readl(c->base + MASK1) | BIT(d->hwirq), c->base + MASK1);
	raw_spin_unlock_irqrestore(&c->lock, flags);
}

static void cmic_irq_handler(struct irq_desc *desc)
{
	struct cmic_irq *c = irq_desc_get_handler_data(desc);
	struct irq_chip *parent = irq_desc_get_chip(desc);
	unsigned long pend;
	unsigned int bit;

	chained_irq_enter(parent, desc);
	pend = readl(c->base + STAT1) & readl(c->base + MASK1);
	for_each_set_bit(bit, &pend, NBITS)
		generic_handle_domain_irq(c->dom, bit);
	chained_irq_exit(parent, desc);
}

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
	dev_info(dev, "CMC0 IRQ_STAT1 behind GIC irq %d, UC0_IRQ_MASK1 0x%08x\n", parent, readl(c->base + MASK1));
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
