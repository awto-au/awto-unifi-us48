// SPDX-License-Identifier: GPL-2.0
/*
 * bcm59111: Broadcom BCM59111 PoE PSE controller, read-only hwmon (die temperature) for now; the PSE part is #26.
 * Only register 0x2c is read (docs/poe.md section 9): no event/clear-on-read registers (0x00, 0x03, 0x07), no writes.
 * A chip without running firmware NAKs: temp1_input then returns the I2C error.
 */
#include <linux/hwmon.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/property.h>

#define REG_TEMP	0x2c
/* stock hpcUbntPoeMonitorTask 0x610b78: T = 367 - 2 * raw degC, valid only when raw > 0x53 */
#define TEMP_RAW_MIN	0x54
#define TEMP_OFS_C	367

struct bcm59111 {
	struct i2c_client *client;
	const char *label;
};

static umode_t bcm_is_visible(const void *data, enum hwmon_sensor_types type, u32 attr, int ch)
{
	const struct bcm59111 *b = data;

	if (type != hwmon_temp)
		return 0;
	if (attr == hwmon_temp_input || (attr == hwmon_temp_label && b->label))
		return 0444;
	return 0;
}

static int bcm_read(struct device *dev, enum hwmon_sensor_types type, u32 attr, int ch, long *val)
{
	struct bcm59111 *b = dev_get_drvdata(dev);
	int raw;

	if (type != hwmon_temp || attr != hwmon_temp_input)
		return -EOPNOTSUPP;
	raw = i2c_smbus_read_byte_data(b->client, REG_TEMP);
	if (raw < 0)
		return raw;
	if (raw < TEMP_RAW_MIN)
		return -ENODATA;
	*val = (TEMP_OFS_C - 2 * raw) * 1000L;
	return 0;
}

static int bcm_read_string(struct device *dev, enum hwmon_sensor_types type, u32 attr, int ch, const char **str)
{
	struct bcm59111 *b = dev_get_drvdata(dev);

	*str = b->label;
	return 0;
}

static const struct hwmon_ops bcm_hwmon_ops = {
	.is_visible = bcm_is_visible,
	.read = bcm_read,
	.read_string = bcm_read_string,
};

static const struct hwmon_channel_info * const bcm_info[] = {
	HWMON_CHANNEL_INFO(temp, HWMON_T_INPUT | HWMON_T_LABEL),
	NULL
};

static const struct hwmon_chip_info bcm_chip = { .ops = &bcm_hwmon_ops, .info = bcm_info };

/* Probe touches no register: the chips may be in reset or without firmware at boot (PoE off, docs/poe.md). */
static int bcm_probe(struct i2c_client *client)
{
	struct device *dev = &client->dev;
	struct bcm59111 *b;
	struct device *hw;

	if (!i2c_check_functionality(client->adapter, I2C_FUNC_SMBUS_READ_BYTE_DATA))
		return -EOPNOTSUPP;
	b = devm_kzalloc(dev, sizeof(*b), GFP_KERNEL);
	if (!b)
		return -ENOMEM;
	b->client = client;
	device_property_read_string(dev, "label", &b->label);
	hw = devm_hwmon_device_register_with_info(dev, "bcm59111", b, &bcm_chip, NULL);
	return PTR_ERR_OR_ZERO(hw);
}

static const struct of_device_id bcm_of_match[] = {
	{ .compatible = "brcm,bcm59111" },
	{ }
};
MODULE_DEVICE_TABLE(of, bcm_of_match);

static const struct i2c_device_id bcm_ids[] = {
	{ "bcm59111" },
	{ }
};
MODULE_DEVICE_TABLE(i2c, bcm_ids);

static struct i2c_driver bcm_driver = {
	.driver = { .name = "bcm59111", .of_match_table = bcm_of_match },
	.probe = bcm_probe,
	.id_table = bcm_ids,
};
module_i2c_driver(bcm_driver);

MODULE_DESCRIPTION("Broadcom BCM59111 PoE PSE controller: die temperature (read-only hwmon)");
MODULE_LICENSE("GPL");
