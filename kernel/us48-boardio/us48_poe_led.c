// SPDX-License-Identifier: GPL-2.0
/*
 * PoE LEDs front 1-48: 2-bit field per port on chains (0,0) / (0,1), MSB first (docs/board.md "Frame layout").
 * LED class `ge<N>::poe` (brightness 1 = field 1, stock 802.3 power-good) + /sys/devices/platform/us48-board-io/
 * poe_pattern (fields 0-2 for tests). Field 3 is never written. Design + board check: docs/leds.md "PoE LEDs".
 */
#include <linux/ctype.h>
#include <linux/leds.h>
#include <linux/lockdep.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/string.h>
#include "us48_boardio.h"

#define NCARD		2
#define PER_CARD	24
#define NPORT		(NCARD * PER_CARD)
#define FRAME_LEN	6
/* Stock writes 0, 1 (802.3 power-good) or 2 (passive mode), never 3 (docs/poe.md 6, 0x5f6a40 / 0x60615c) */
#define VAL_MAX		2

struct poe_led {
	struct led_classdev cdev;
	u8 idx;				/* front - 1 */
	bool registered;
	char name[16];			/* ge<N>::poe */
	char trig[24];			/* ge<N>-poe-good: provided later by the PSE driver (#26) */
};

static DEFINE_MUTEX(poe_lock);
static u8 val[NPORT];			/* wanted field per front - 1 */
static u8 sent[NCARD][FRAME_LEN];	/* last frame acked per card */
static bool sent_ok[NCARD];		/* false = unknown (probe, failed write): next flush sends */
static unsigned int frame_errs;
static struct poe_led *leds;
static struct platform_device *pdev;

/* port i of the card -> byte i>>2, bits 7-2(i&3) : 6-2(i&3) (docs/board.md, masks fc f3 cf 3f at 0x15a3818) */
static void frame_build(int card, u8 *buf)
{
	int i;

	memset(buf, 0, FRAME_LEN);
	for (i = 0; i < PER_CARD; i++)
		buf[i >> 2] |= (val[card * PER_CARD + i] & 3) << (6 - 2 * (i & 3));
}

/* Send a card's frame only when it differs from the last acked one */
static int flush(int card)
{
	u8 buf[FRAME_LEN];
	int ret;

	lockdep_assert_held(&poe_lock);
	frame_build(card, buf);
	if (sent_ok[card] && !memcmp(buf, sent[card], FRAME_LEN))
		return 0;
	ret = us48_mspi_frame(US48_MSPI_POE_LED0 + card, buf, FRAME_LEN);
	if (ret) {
		sent_ok[card] = false;
		frame_errs++;
		pr_err_ratelimited("us48_boardio: PoE LED frame card %d failed: %d\n", card, ret);
		return ret;
	}
	memcpy(sent[card], buf, FRAME_LEN);
	sent_ok[card] = true;
	return 0;
}

static int poe_led_set(struct led_classdev *c, enum led_brightness b)
{
	struct poe_led *l = container_of(c, struct poe_led, cdev);
	int ret;

	mutex_lock(&poe_lock);
	val[l->idx] = b ? 1 : 0;
	ret = flush(l->idx / PER_CARD);
	mutex_unlock(&poe_lock);
	return ret;
}

static enum led_brightness poe_led_get(struct led_classdev *c)
{
	struct poe_led *l = container_of(c, struct poe_led, cdev);

	return READ_ONCE(val[l->idx]) ? 1 : 0;
}

static ssize_t poe_pattern_show(struct device *dev, struct device_attribute *a, char *buf)
{
	int i, len = 0;

	mutex_lock(&poe_lock);
	for (i = 0; i < NPORT; i++)
		len += sysfs_emit_at(buf, len, "%u", val[i]);
	mutex_unlock(&poe_lock);
	return len + sysfs_emit_at(buf, len, "\n");
}

/* "off", or 48 digits 0-2 in front order 1..48 (whitespace ignored) */
static ssize_t poe_pattern_store(struct device *dev, struct device_attribute *a, const char *buf, size_t len)
{
	u8 next[NPORT] = {};
	int i, k = 0, ret;

	if (!sysfs_streq(buf, "off")) {
		for (i = 0; i < len; i++) {
			if (isspace(buf[i]))
				continue;
			if (k >= NPORT || buf[i] < '0' || buf[i] > '0' + VAL_MAX)
				return -EINVAL;
			next[k++] = buf[i] - '0';
		}
		if (k != NPORT)
			return -EINVAL;
	}
	mutex_lock(&poe_lock);
	memcpy(val, next, sizeof(val));
	ret = flush(0);
	i = flush(1);
	mutex_unlock(&poe_lock);
	return ret ?: i ?: len;
}
static DEVICE_ATTR_RW(poe_pattern);

static ssize_t poe_frame_errors_show(struct device *dev, struct device_attribute *a, char *buf)
{
	unsigned int n;

	mutex_lock(&poe_lock);
	n = frame_errs;
	mutex_unlock(&poe_lock);
	return sysfs_emit(buf, "%u\n", n);
}
static DEVICE_ATTR_RO(poe_frame_errors);

static struct attribute *poe_attrs[] = { &dev_attr_poe_pattern.attr, &dev_attr_poe_frame_errors.attr, NULL };
ATTRIBUTE_GROUPS(poe);

static void poe_leds_detach(void *data)
{
	int i;

	for (i = 0; i < NPORT; i++)
		if (leds[i].registered)
			led_classdev_unregister(&leds[i].cdev);	/* off: field 0, one frame per card at most */
	kfree(leds);
	leds = NULL;
	device_remove_groups(&pdev->dev, poe_groups);
	platform_device_unregister(pdev);
	pdev = NULL;
}

/* devm on the attaching device (us48_fan's spi device): all fields 0 on both cards, then 48 LED class devices */
int us48_poe_leds_attach(struct device *dev)
{
	int i, ret;

	mutex_lock(&poe_lock);
	memset(val, 0, sizeof(val));
	memset(sent_ok, 0, sizeof(sent_ok));
	ret = flush(0);
	i = flush(1);
	mutex_unlock(&poe_lock);
	if (ret || i)
		dev_warn(dev, "PoE LED zero frames failed (%d, %d), retried on the next change\n", ret, i);

	leds = kcalloc(NPORT, sizeof(*leds), GFP_KERNEL);
	if (!leds)
		return -ENOMEM;
	pdev = platform_device_register_data(dev, "us48-board-io", PLATFORM_DEVID_NONE, NULL, 0);
	ret = IS_ERR(pdev) ? PTR_ERR(pdev) : device_add_groups(&pdev->dev, poe_groups);
	if (ret) {
		if (!IS_ERR(pdev))
			platform_device_unregister(pdev);
		pdev = NULL;
		kfree(leds);
		leds = NULL;
		return dev_err_probe(dev, ret, "us48-board-io device\n");
	}
	for (i = 0; i < NPORT; i++) {
		struct poe_led *l = &leds[i];

		l->idx = i;
		snprintf(l->name, sizeof(l->name), "ge%d::poe", i + 1);
		snprintf(l->trig, sizeof(l->trig), "ge%d-poe-good", i + 1);
		l->cdev.name = l->name;
		l->cdev.max_brightness = 1;
		l->cdev.brightness_set_blocking = poe_led_set;
		l->cdev.brightness_get = poe_led_get;
		/* TODO(#26): bcm59111 registers this trigger per PI and fires 1 on power-good (needs its IRQ path) */
		l->cdev.default_trigger = l->trig;
		if (led_classdev_register(&pdev->dev, &l->cdev))
			dev_warn(dev, "%s: LED class register failed\n", l->name);
		else
			l->registered = true;
	}
	return devm_add_action_or_reset(dev, poe_leds_detach, NULL);
}
EXPORT_SYMBOL_GPL(us48_poe_leds_attach);
