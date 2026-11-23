// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cca_gpio: ChipcommonA GPIO of both chips (SFP presence #21, system LED #60). Unit 0: platform device
 * (DT, MMIO DIN/DOUT/OUTEN). Unit 1: aux device of us48_pci (unit 1 AXI regmap). Writes only the unit 0 system LED
 * pins 6/7 (docs/leds.md); every other line is input-only: an output (OUTEN set) cannot be requested as input.
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
#define CCA_DOUT	(HW_CCA_GPIO_DOUT - HW_CCA_GPIO_DIN)
#define CCA_OUTEN	(HW_CCA_GPIO_OUTEN - HW_CCA_GPIO_DIN)
/* Unit 0 system LED: stock gpiodev sets OUTEN 6/7 and writes DOUT 6/7 (kernel-pse.md 3.1); uboot-1 the same */
/* + pin 9: PoE chip reset, shared by all 12 chips, active low (bcm59111 fw_reset) */
#define CCA_U0_OUTPUTS	(BIT(6) | BIT(7) | BIT(9))

struct cca {
	struct gpio_chip gc;
	struct regmap *map;
	unsigned int base;	/* regmap address of DIN */
	u32 outputs;		/* lines this driver may drive */
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

static int cca_set(struct gpio_chip *gc, unsigned int off, int val)
{
	struct cca *c = gpiochip_get_data(gc);

	if (!(c->outputs & BIT(off)))
		return -EPERM;
	return regmap_update_bits(c->map, c->base + CCA_DOUT, BIT(off), val ? BIT(off) : 0);
}

/* Value first, then OUTEN: no glitch to the old DOUT level */
static int cca_direction_output(struct gpio_chip *gc, unsigned int off, int val)
{
	struct cca *c = gpiochip_get_data(gc);
	int ret = cca_set(gc, off, val);

	return ret ?: regmap_update_bits(c->map, c->base + CCA_OUTEN, BIT(off), BIT(off));
}

/* Input request: drivable lines turn their output off; the others are accepted only while already inputs */
static int cca_direction_input(struct gpio_chip *gc, unsigned int off)
{
	struct cca *c = gpiochip_get_data(gc);
	int dir;

	if (c->outputs & BIT(off))
		return regmap_update_bits(c->map, c->base + CCA_OUTEN, BIT(off), 0);
	dir = cca_get_direction(gc, off);
	if (dir < 0)
		return dir;
	return dir == GPIO_LINE_DIRECTION_IN ? 0 : -EPERM;
}

static int cca_register(struct device *dev, struct regmap *map, unsigned int base, bool can_sleep, u32 outputs)
{
	struct cca *c = devm_kzalloc(dev, sizeof(*c), GFP_KERNEL);

	if (!c)
		return -ENOMEM;
	c->map = map;
	c->base = base;
	c->outputs = outputs;
	c->gc.label = dev_name(dev);
	c->gc.parent = dev;
	c->gc.owner = THIS_MODULE;
	c->gc.base = -1;
	c->gc.ngpio = CCA_NGPIO;
	c->gc.can_sleep = can_sleep;
	c->gc.get_direction = cca_get_direction;
	c->gc.get = cca_get;
	c->gc.set = cca_set;
	c->gc.direction_input = cca_direction_input;
	c->gc.direction_output = cca_direction_output;
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
	return cca_register(&pdev->dev, map, 0, false, CCA_U0_OUTPUTS);
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
	return cca_register(&adev->dev, map, HW_CCA_BASE + HW_CCA_GPIO_DIN, true, 0);
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

MODULE_DESCRIPTION("UniFi US-48-500W ChipcommonA GPIO (both chips): inputs, unit 0 system LED outputs");
MODULE_LICENSE("GPL");
