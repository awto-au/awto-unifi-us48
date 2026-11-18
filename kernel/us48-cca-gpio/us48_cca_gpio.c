// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cca_gpio: ChipcommonA GPIO of both chips as a read-only gpio-chip (SFP presence, #21). Unit 0: platform device
 * (DT, MMIO DIN/DOUT/OUTEN). Unit 1: aux device of us48_pci (unit 1 AXI regmap). Never writes: a line that is an
 * output (OUTEN set) cannot be requested as input; outputs and IRQs (PoE reset, LEDs, PSE IRQs) belong to their owners.
 */
#include <linux/auxiliary_bus.h>
#include <linux/bits.h>
#include <linux/gpio/driver.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>
#include "us48_hwdb.h"
#include "us48_pci.h"

#define CCA_NGPIO	12	/* stock GPIOA chip: 12 lines (docs/board.md GPIO map) */
#define CCA_DIN		0
#define CCA_OUTEN	(HW_CCA_GPIO_OUTEN - HW_CCA_GPIO_DIN)

struct cca {
	struct gpio_chip gc;
	struct regmap *map;
	unsigned int base;	/* regmap address of DIN */
};

static int cca_get_direction(struct gpio_chip *gc, unsigned int off)
{
	struct cca *c = gpiochip_get_data(gc);
	unsigned int v;
	int ret = regmap_read(c->map, c->base + CCA_OUTEN, &v);

	if (ret)
		return ret;
	return v & BIT(off) ? GPIO_LINE_DIRECTION_OUT : GPIO_LINE_DIRECTION_IN;
}

static int cca_get(struct gpio_chip *gc, unsigned int off)
{
	struct cca *c = gpiochip_get_data(gc);
	unsigned int v;
	int ret = regmap_read(c->map, c->base + CCA_DIN, &v);

	return ret ?: !!(v & BIT(off));
}

/* No direction_input/output, no set: gpiolib accepts an input request only while get_direction says IN. */
static int cca_register(struct device *dev, struct regmap *map, unsigned int base, bool can_sleep)
{
	struct cca *c = devm_kzalloc(dev, sizeof(*c), GFP_KERNEL);

	if (!c)
		return -ENOMEM;
	c->map = map;
	c->base = base;
	c->gc.label = dev_name(dev);
	c->gc.parent = dev;
	c->gc.owner = THIS_MODULE;
	c->gc.base = -1;
	c->gc.ngpio = CCA_NGPIO;
	c->gc.can_sleep = can_sleep;
	c->gc.get_direction = cca_get_direction;
	c->gc.get = cca_get;
	return devm_gpiochip_add_data(dev, &c->gc, c);
}

static const struct regmap_config cca_mmio_cfg = {
	.reg_bits = 32, .val_bits = 32, .reg_stride = 4, .max_register = CCA_OUTEN,
};

static int cca_plat_probe(struct platform_device *pdev)
{
	void __iomem *base = devm_platform_ioremap_resource(pdev, 0);
	struct regmap *map;

	if (IS_ERR(base))
		return PTR_ERR(base);
	map = devm_regmap_init_mmio(&pdev->dev, base, &cca_mmio_cfg);
	if (IS_ERR(map))
		return PTR_ERR(map);
	return cca_register(&pdev->dev, map, 0, false);
}

static const struct of_device_id cca_of_match[] = {
	{ .compatible = "ubnt,us48-cca-gpio" },
	{ }
};
MODULE_DEVICE_TABLE(of, cca_of_match);

static struct platform_driver cca_plat_driver = {
	.driver = { .name = "us48_cca_gpio", .of_match_table = cca_of_match },
	.probe = cca_plat_probe,
};

/* Unit 1: the us48_pci AXI regmap (window lock, link check, access table) */
static int cca_aux_probe(struct auxiliary_device *adev, const struct auxiliary_device_id *id)
{
	struct regmap *map = us48_pci_regmap(us48_pci_from_adev(adev));

	if (IS_ERR_OR_NULL(map))
		return map ? PTR_ERR(map) : -ENODEV;
	return cca_register(&adev->dev, map, HW_CCA_BASE + HW_CCA_GPIO_DIN, true);
}

static const struct auxiliary_device_id cca_aux_ids[] = {
	{ .name = US48_PCI_AUX_ID(US48_PCI_AUX_CCA) },
	{ }
};
MODULE_DEVICE_TABLE(auxiliary, cca_aux_ids);

static struct auxiliary_driver cca_aux_driver = {
	.name = "cca",
	.id_table = cca_aux_ids,
	.probe = cca_aux_probe,
};

static int __init cca_init(void)
{
	int ret = platform_driver_register(&cca_plat_driver);

	if (ret)
		return ret;
	ret = auxiliary_driver_register(&cca_aux_driver);
	if (ret)
		platform_driver_unregister(&cca_plat_driver);
	return ret;
}

static void __exit cca_exit(void)
{
	auxiliary_driver_unregister(&cca_aux_driver);
	platform_driver_unregister(&cca_plat_driver);
}

module_init(cca_init);
module_exit(cca_exit);

MODULE_DESCRIPTION("UniFi US-48-500W ChipcommonA GPIO inputs (both chips), read-only gpio-chip");
MODULE_LICENSE("GPL");
