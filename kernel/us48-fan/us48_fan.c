// SPDX-License-Identifier: GPL-2.0
/*
 * us48_fan: US-48-500W fans, a client of the board-IO chains (us48_boardio): hwmon pwm1 (4 discrete levels) + thermal
 * cooling device. Frame and levels: docs/fan.md, docs/board.md MSPI. Policy lives in the DT thermal zones (#19).
 * Its DT node carries the select pins, so its probe attaches the board IO and starts the PoE LED client (docs/leds.md).
 */
#include <linux/hwmon.h>
#include <linux/math.h>
#include <linux/jiffies.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/spi/spi.h>
#include <linux/thermal.h>
#include <linux/workqueue.h>
#include "us48_boardio.h"

#define NLEVEL		4
#define LEVEL_FULL	(NLEVEL - 1)
#define FRAME_LEN	7	/* byte 0 = level << 4; bytes 1..6 are PoE mode bits (poe_field, default 0) */
#define LEVEL_SHIFT	4
#define NGROUP		2	/* chain US48_MSPI_FAN0 (A=1,B=0) = unit 0 fans, FAN1 (A=1,B=1) = unit 1 fans */
/* Sensor check period (WATCH_MS): LM75 and die temperatures move by degrees per minute. */
#define WATCH_MS	2000

/* quiet=0 restores the fail-safe: level 3 on probe, sensor loss and write errors (docs/fan.md) */
static bool quiet = true;
module_param(quiet, bool, 0644);
MODULE_PARM_DESC(quiet, "quiet rule (lab noise): level 0 unless a DT emergency trip fires (default 1)");

static uint level = 0;
module_param(level, uint, 0644);
MODULE_PARM_DESC(level, "level floor from probe on (thermal trips raise it), 0..3 = 50/70/90/100 % (default 0, quiet rule)");

/* Failure log once per minute: a dead sensor is noted, not spammed */
#define LOG_MS		60000

static const u8 duty_pct[NLEVEL] = { 50, 70, 90, 100 };	/* firmware labels, docs/fan.md */

struct fan {
	struct spi_device *spi;
	struct mutex lock;
	unsigned int req;		/* level asked by the thermal core */
	unsigned int manual;		/* level asked through pwm1 */
	bool manual_mode;		/* pwm1_enable == 1 */
	unsigned long last_log;		/* jiffies of the last failure message, 0 = none yet */
	bool sensors_ok;		/* every watched zone read OK on the last check */
	bool wr_err;			/* a frame failed: hold 3 until a write works again */
	int hw;				/* level on the wire, -1 unknown */
	unsigned int frame_errs;	/* failed frames since probe */
	const char **zones;
	int nzones;
	struct delayed_work watch;
};

/* PoE mode-select field of every port in bytes 1..6 of the (1,x) frames: 2 = 802.3 (stock capture: 0xaa on all ports, docs/poe.md 6;
 * field 0 = no detection, bench 2026-11-21 #26). 1 (passive 24 V) is never written. Changes are sent at once. */
static uint poe_field = 2;
static uint test_bits;	/* bench: OR-ed into byte 0 of every frame (bit walk #12); 0 = off */
static struct fan *fan_inst;
static int fan_frame(struct fan *f, unsigned int level);

static int poe_field_set(const char *val, const struct kernel_param *kp)
{
	uint v;
	int ret = kstrtouint(val, 0, &v);

	if (ret)
		return ret;
	if (v != 0 && v != 2)
		return -EINVAL;
	poe_field = v;
	if (fan_inst) {
		mutex_lock(&fan_inst->lock);
		if (fan_inst->hw >= 0)
			ret = fan_frame(fan_inst, fan_inst->hw);
		mutex_unlock(&fan_inst->lock);
	}
	return ret;
}

static const struct kernel_param_ops poe_field_ops = { .set = poe_field_set, .get = param_get_uint };
module_param_cb(poe_field, &poe_field_ops, &poe_field, 0644);

static int test_bits_set(const char *val, const struct kernel_param *kp)
{
	uint v;
	int ret = kstrtouint(val, 0, &v);

	if (ret)
		return ret;
	if (v > 0xff || (v & 0x30))	/* the fan level bits stay owned by the level logic */
		return -EINVAL;
	test_bits = v;
	if (fan_inst) {
		mutex_lock(&fan_inst->lock);
		if (fan_inst->hw >= 0)
			ret = fan_frame(fan_inst, fan_inst->hw);
		mutex_unlock(&fan_inst->lock);
	}
	return ret;
}

static const struct kernel_param_ops test_bits_ops = { .set = test_bits_set, .get = param_get_uint };
module_param_cb(test_bits, &test_bits_ops, &test_bits, 0644);
MODULE_PARM_DESC(test_bits, "bench only: bits OR-ed into byte 0 (not 5:4), 0 = off");
MODULE_PARM_DESC(poe_field, "PoE mode-select field written for all 48 ports: 2 = 802.3 as stock (default; 0 leaves the 802.3 path off, PDs are never detected); 1 (passive 24 V) is refused");

/* floor in automatic mode: the thermal governor requests 0 while no trip is active */
static unsigned int base_level(void)
{
	return min_t(uint, level, LEVEL_FULL);
}

static unsigned int fail_level(void)
{
	return quiet ? 0 : LEVEL_FULL;
}

/* Read-only status for the boot health line (#35): last level acked by spi_write (byte 0 = level << 4). The MSPI has no RX. */
static ssize_t wire_level_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct fan *f = spi_get_drvdata(to_spi_device(dev));
	int hw;

	mutex_lock(&f->lock);
	hw = f->hw;
	mutex_unlock(&f->lock);
	return sysfs_emit(buf, "%d\n", hw);
}
static DEVICE_ATTR_RO(wire_level);

static ssize_t frame_errors_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct fan *f = spi_get_drvdata(to_spi_device(dev));
	unsigned int n;

	mutex_lock(&f->lock);
	n = f->frame_errs;
	mutex_unlock(&f->lock);
	return sysfs_emit(buf, "%u\n", n);
}
static DEVICE_ATTR_RO(frame_errors);

static struct attribute *fan_attrs[] = { &dev_attr_wire_level.attr, &dev_attr_frame_errors.attr, NULL };
ATTRIBUTE_GROUPS(fan);

static int fan_frame(struct fan *f, unsigned int level)
{
	u8 buf[FRAME_LEN] = { (level << LEVEL_SHIFT) | test_bits };
	int g, ret = 0;

	if (poe_field == 2)
		memset(buf + 1, 0xaa, FRAME_LEN - 1);

	for (g = 0; g < NGROUP && !ret; g++)
		ret = us48_mspi_frame(US48_MSPI_FAN0 + g, buf, sizeof(buf));
	return ret;
}

/* Caller holds f->lock. Sends the wanted level if it differs from the wire; a failed write falls back to 3. */
static int fan_apply(struct fan *f)
{
	unsigned int want = f->manual_mode ? f->manual : max(f->req, base_level());
	int ret;

	if (!f->sensors_ok || f->wr_err)
		want = fail_level();
	if (f->hw == want)
		return 0;
	ret = fan_frame(f, want);
	if (!ret) {
		f->hw = want;
		f->wr_err = false;
		return 0;
	}
	dev_err_ratelimited(&f->spi->dev, "frame for level %u failed: %d, holding level %u\n", want, ret,
			    fail_level());
	f->hw = -1;
	f->wr_err = true;
	f->frame_errs++;
	if (want != fail_level() && !fan_frame(f, fail_level()))
		f->hw = fail_level();
	return ret;
}

static void fan_watch(struct work_struct *work)
{
	struct fan *f = container_of(work, struct fan, watch.work);
	bool ok = true;
	int i, t = 0, ret;

	for (i = 0; i < f->nzones; i++) {
		struct thermal_zone_device *tz = thermal_zone_get_zone_by_name(f->zones[i]);

		ret = IS_ERR(tz) ? PTR_ERR(tz) : thermal_zone_get_temp(tz, &t);
		if (ret || t < -40000 || t > 150000) {
			if (!f->last_log || time_after(jiffies, f->last_log + msecs_to_jiffies(LOG_MS))) {
				dev_warn(&f->spi->dev, "zone %s unreadable or bogus (%d, %d mC): level %u\n",
					 f->zones[i], ret, t, fail_level());
				f->last_log = jiffies;
			}
			ok = false;
			break;
		}
	}
	mutex_lock(&f->lock);
	if (ok && !f->sensors_ok)
		dev_info(&f->spi->dev, "all %d zones read OK\n", f->nzones);
	f->sensors_ok = ok;
	fan_apply(f);
	mutex_unlock(&f->lock);
	schedule_delayed_work(&f->watch, msecs_to_jiffies(WATCH_MS));
}

static int fan_get_max_state(struct thermal_cooling_device *cdev, unsigned long *state)
{
	*state = LEVEL_FULL;
	return 0;
}

static int fan_get_cur_state(struct thermal_cooling_device *cdev, unsigned long *state)
{
	struct fan *f = cdev->devdata;

	*state = f->req;
	return 0;
}

static int fan_set_cur_state(struct thermal_cooling_device *cdev, unsigned long state)
{
	struct fan *f = cdev->devdata;
	int ret;

	if (state > LEVEL_FULL)
		return -EINVAL;
	mutex_lock(&f->lock);
	f->req = state;
	ret = fan_apply(f);
	mutex_unlock(&f->lock);
	return ret;
}

static const struct thermal_cooling_device_ops fan_cdev_ops = {
	.get_max_state = fan_get_max_state,
	.get_cur_state = fan_get_cur_state,
	.set_cur_state = fan_set_cur_state,
};

static umode_t fan_is_visible(const void *data, enum hwmon_sensor_types type, u32 attr, int ch)
{
	return 0644;
}

static int fan_read(struct device *dev, enum hwmon_sensor_types type, u32 attr, int ch, long *val)
{
	struct fan *f = dev_get_drvdata(dev);

	mutex_lock(&f->lock);
	if (attr == hwmon_pwm_enable)
		*val = f->manual_mode ? 1 : 2;
	else
		*val = DIV_ROUND_CLOSEST(duty_pct[f->hw < 0 ? LEVEL_FULL : f->hw] * 255, 100);
	mutex_unlock(&f->lock);
	return 0;
}

/* pwm1 value -> nearest of the 4 levels (midpoints 60 / 80 / 95 %) */
static unsigned int pwm_to_level(long v)
{
	unsigned int l, best = 0;
	long d, dbest = 255;

	for (l = 0; l < NLEVEL; l++) {
		d = abs(v - DIV_ROUND_CLOSEST(duty_pct[l] * 255, 100));
		if (d < dbest) {
			dbest = d;
			best = l;
		}
	}
	return best;
}

static int fan_write(struct device *dev, enum hwmon_sensor_types type, u32 attr, int ch, long val)
{
	struct fan *f = dev_get_drvdata(dev);
	int ret = 0;

	mutex_lock(&f->lock);
	if (attr == hwmon_pwm_enable) {
		if (val != 1 && val != 2)
			ret = -EINVAL;
		else
			f->manual_mode = val == 1;
	} else if (!f->manual_mode) {
		ret = -EBUSY;	/* automatic: the thermal zones own the level */
	} else if (val < 0 || val > 255) {
		ret = -EINVAL;
	} else {
		f->manual = pwm_to_level(val);
	}
	if (!ret)
		ret = fan_apply(f);
	mutex_unlock(&f->lock);
	return ret;
}

static const struct hwmon_ops fan_hwmon_ops = {
	.is_visible = fan_is_visible,
	.read = fan_read,
	.write = fan_write,
};

static const struct hwmon_channel_info * const fan_info[] = {
	HWMON_CHANNEL_INFO(pwm, HWMON_PWM_INPUT | HWMON_PWM_ENABLE),
	NULL
};

static const struct hwmon_chip_info fan_chip = { .ops = &fan_hwmon_ops, .info = fan_info };

/* Unbind: stop the watcher, fans to the fail level (0 quiet, else 100 %): nothing watches afterwards. */
static void fan_stop(void *data)
{
	struct fan *f = data;

	fan_inst = NULL;
	cancel_delayed_work_sync(&f->watch);
	mutex_lock(&f->lock);
	f->sensors_ok = false;
	fan_apply(f);
	mutex_unlock(&f->lock);
}

static int fan_probe(struct spi_device *spi)
{
	struct device *dev = &spi->dev;
	struct thermal_cooling_device *cdev;
	struct device *hw;
	struct fan *f;
	int ret;

	f = devm_kzalloc(dev, sizeof(*f), GFP_KERNEL);
	if (!f)
		return -ENOMEM;
	f->spi = spi;
	f->hw = -1;
	f->req = base_level();
	mutex_init(&f->lock);
	INIT_DELAYED_WORK(&f->watch, fan_watch);

	f->nzones = of_property_count_strings(dev->of_node, "us48,watch-zones");
	if (f->nzones < 1)
		return dev_err_probe(dev, -EINVAL, "us48,watch-zones missing: no sensor to fail safe on\n");
	f->zones = devm_kcalloc(dev, f->nzones, sizeof(*f->zones), GFP_KERNEL);
	if (!f->zones)
		return -ENOMEM;
	ret = of_property_read_string_array(dev->of_node, "us48,watch-zones", f->zones, f->nzones);
	if (ret < 0)
		return ret;
	ret = us48_boardio_attach(spi);
	if (ret)
		return ret;

	/* Fail level (0 quiet) until the sensors read; the first watch pass moves to the requested level */
	mutex_lock(&f->lock);
	ret = fan_apply(f);
	mutex_unlock(&f->lock);
	if (ret)
		return dev_err_probe(dev, ret, "first frame\n");
	/* PoE LED frames after the first fan frame (stock order, docs/board.md); a PoE LED failure keeps the fans */
	ret = us48_poe_leds_attach(dev);
	if (ret)
		dev_warn(dev, "PoE LEDs not attached: %d\n", ret);
	cdev = devm_thermal_of_cooling_device_register(dev, 0, "us48-fan", f, &fan_cdev_ops);
	if (IS_ERR(cdev))
		return dev_err_probe(dev, PTR_ERR(cdev), "cooling device\n");
	hw = devm_hwmon_device_register_with_info(dev, "us48_fan", f, &fan_chip, NULL);
	if (IS_ERR(hw))
		return PTR_ERR(hw);
	spi_set_drvdata(spi, f);
	fan_inst = f;
	ret = devm_add_action_or_reset(dev, fan_stop, f);	/* runs first on unbind */
	if (ret)
		return ret;
	schedule_delayed_work(&f->watch, 0);
	return 0;
}

static const struct of_device_id fan_of_match[] = {
	{ .compatible = "ubnt,us48-fan" },
	{ }
};
MODULE_DEVICE_TABLE(of, fan_of_match);

static const struct spi_device_id fan_spi_id[] = {
	{ "us48-fan" },
	{ }
};
MODULE_DEVICE_TABLE(spi, fan_spi_id);

static struct spi_driver fan_driver = {
	.driver = { .name = "us48_fan", .of_match_table = fan_of_match, .dev_groups = fan_groups },
	.id_table = fan_spi_id,
	.probe = fan_probe,
};
module_spi_driver(fan_driver);

MODULE_DESCRIPTION("UniFi US-48-500W fans: hwmon pwm1 + thermal cooling device over the CMIC MSPI");
MODULE_LICENSE("GPL");
