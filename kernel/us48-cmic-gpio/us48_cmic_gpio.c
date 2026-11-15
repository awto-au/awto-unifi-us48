// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_gpio: gpio-chip for the 4 CMIC GP pins of each chip. Unit 0: platform device (DT, MMIO). Unit 1: aux device
 * of us48_pci (BAR2, link-checked). GP0 of both units = select lines of the MSPI board IO chains (docs/board.md).
 * Own chip, not gpio-regmap/gpio-mmio: GPIO_REGMAP has no user prompt (out-of-tree cannot enable it) and unit 1 is not MMIO-safe.
 */
#include <linux/auxiliary_bus.h>
#include <linux/bits.h>
#include <linux/gpio/driver.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>
#include "us48_hwdb.h"
#include "us48_pci.h"

#define GP_NUM		4	/* DATA_OUT / OUT_EN fields are 4 bits wide */
#define GP_BASE		HW_CMIC0_GP_DATA_IN
#define GP_IN		0
#define GP_OUT		(HW_CMIC0_GP_DATA_OUT - GP_BASE)
#define GP_EN		(HW_CMIC0_GP_OUT_EN - GP_BASE)
#define GP_LAST		GP_EN

struct gp {
	struct gpio_chip gc;
	struct regmap *map;
};

static int gp_get_direction(struct gpio_chip *gc, unsigned int off)
{
	struct gp *g = gpiochip_get_data(gc);
	unsigned int v;
	int ret = regmap_read(g->map, GP_EN, &v);

	if (ret)
		return ret;
	return v & BIT(off) ? GPIO_LINE_DIRECTION_OUT : GPIO_LINE_DIRECTION_IN;
}

static int gp_direction_input(struct gpio_chip *gc, unsigned int off)
{
	struct gp *g = gpiochip_get_data(gc);

	return regmap_update_bits(g->map, GP_EN, BIT(off), 0);
}

static int gp_set(struct gpio_chip *gc, unsigned int off, int val)
{
	struct gp *g = gpiochip_get_data(gc);

	return regmap_update_bits(g->map, GP_OUT, BIT(off), val ? BIT(off) : 0);
}

static int gp_direction_output(struct gpio_chip *gc, unsigned int off, int val)
{
	struct gp *g = gpiochip_get_data(gc);
	int ret = gp_set(gc, off, val);	/* level first: no glitch when the pad turns to output */

	return ret ?: regmap_update_bits(g->map, GP_EN, BIT(off), BIT(off));
}

static int gp_get(struct gpio_chip *gc, unsigned int off)
{
	struct gp *g = gpiochip_get_data(gc);
	unsigned int v, dir;
	int ret = regmap_read(g->map, GP_EN, &dir);

	if (ret)
		return ret;
	ret = regmap_read(g->map, dir & BIT(off) ? GP_OUT : GP_IN, &v);
	return ret ?: !!(v & BIT(off));
}

static int gp_register(struct device *dev, struct regmap *map)
{
	struct gp *g = devm_kzalloc(dev, sizeof(*g), GFP_KERNEL);

	if (!g)
		return -ENOMEM;
	g->map = map;
	g->gc.label = dev_name(dev);
	g->gc.parent = dev;
	g->gc.owner = THIS_MODULE;
	g->gc.base = -1;
	g->gc.ngpio = GP_NUM;
	g->gc.can_sleep = true;
	g->gc.get_direction = gp_get_direction;
	g->gc.direction_input = gp_direction_input;
	g->gc.direction_output = gp_direction_output;
	g->gc.get = gp_get;
	g->gc.set = gp_set;
	return devm_gpiochip_add_data(dev, &g->gc, g);
}

static const struct regmap_config gp_mmio_cfg = {
	.reg_bits = 32, .val_bits = 32, .reg_stride = 4, .max_register = GP_LAST,
};

static int gp_plat_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	void __iomem *base = devm_platform_ioremap_resource(pdev, 0);
	struct regmap *map;

	if (IS_ERR(base))
		return PTR_ERR(base);
	map = devm_regmap_init_mmio(dev, base, &gp_mmio_cfg);
	if (IS_ERR(map))
		return PTR_ERR(map);
	return gp_register(dev, map);
}

static const struct of_device_id gp_of_match[] = {
	{ .compatible = "ubnt,us48-cmic-gpio" },
	{ }
};
MODULE_DEVICE_TABLE(of, gp_of_match);

static struct platform_driver gp_plat_driver = {
	.driver = { .name = "us48_cmic_gpio", .of_match_table = gp_of_match },
	.probe = gp_plat_probe,
};

/* Unit 1: BAR2 behind PCIe; every access first checks the link (dead link = -ENODEV, no read). */
struct gp1 {
	struct us48_pci *p;
	void __iomem *base;
};

static int gp1_read(void *ctx, unsigned int reg, unsigned int *val)
{
	struct gp1 *g = ctx;
	int ret = us48_pci_link_ok(g->p);

	if (!ret)
		*val = readl(g->base + reg);
	return ret;
}

static int gp1_write(void *ctx, unsigned int reg, unsigned int val)
{
	struct gp1 *g = ctx;
	int ret = us48_pci_link_ok(g->p);

	if (!ret)
		writel(val, g->base + reg);
	return ret;
}

static const struct regmap_config gp1_cfg = {
	.reg_bits = 32, .val_bits = 32, .reg_stride = 4, .max_register = GP_LAST,
	.reg_read = gp1_read, .reg_write = gp1_write,
};

static int gp_aux_probe(struct auxiliary_device *adev, const struct auxiliary_device_id *id)
{
	struct device *dev = &adev->dev;
	struct gp1 *g = devm_kzalloc(dev, sizeof(*g), GFP_KERNEL);
	struct regmap *map;

	if (!g)
		return -ENOMEM;
	g->p = us48_pci_from_adev(adev);
	g->base = us48_pci_cmic_base(g->p) + GP_BASE;
	map = devm_regmap_init(dev, NULL, g, &gp1_cfg);
	if (IS_ERR(map))
		return PTR_ERR(map);
	return gp_register(dev, map);
}

static const struct auxiliary_device_id gp_aux_ids[] = {
	{ .name = US48_PCI_AUX_ID(US48_PCI_AUX_GPIO) },
	{ }
};
MODULE_DEVICE_TABLE(auxiliary, gp_aux_ids);

static struct auxiliary_driver gp_aux_driver = {
	.name = "gp",
	.id_table = gp_aux_ids,
	.probe = gp_aux_probe,
};

static int __init gp_init(void)
{
	int ret = platform_driver_register(&gp_plat_driver);

	if (ret)
		return ret;
	ret = auxiliary_driver_register(&gp_aux_driver);
	if (ret)
		platform_driver_unregister(&gp_plat_driver);
	return ret;
}

static void __exit gp_exit(void)
{
	auxiliary_driver_unregister(&gp_aux_driver);
	platform_driver_unregister(&gp_plat_driver);
}

module_init(gp_init);
module_exit(gp_exit);

MODULE_DESCRIPTION("UniFi US-48-500W CMIC GP pins (both chips) as gpio-chip");
MODULE_LICENSE("GPL");
