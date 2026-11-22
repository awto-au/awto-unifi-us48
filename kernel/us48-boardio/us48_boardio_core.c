// SPDX-License-Identifier: GPL-2.0
/*
 * us48_boardio: the CMIC MSPI board-IO chains (docs/board.md "MSPI board IO"): one lock, both select pins, one frame
 * call. Clients: us48_fan (fan chains), us48_poe_led.c (PoE LED chains). What sits at the chain ends is not identified
 * (I: shift registers). The select-gpios live on the fan DT node (mainline 0013), so us48_fan probe attaches.
 */
#include <linux/gpio/consumer.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/spi/spi.h>
#include "us48_boardio.h"

/* docs/board.md "MSPI board IO" table: pins per chain, frame lengths {6,6,7,7} (stock 0x23e8c38) */
static const struct { u8 a, b, len; } chains[US48_MSPI_NCHAIN] = {
	[US48_MSPI_POE_LED0] = { 0, 0, 6 },
	[US48_MSPI_POE_LED1] = { 0, 1, 6 },
	[US48_MSPI_FAN0] = { 1, 0, 7 },
	[US48_MSPI_FAN1] = { 1, 1, 7 },
};

struct boardio {
	struct spi_device *spi;
	struct gpio_desc *sel[2];	/* A, B */
};

static DEFINE_MUTEX(bio_lock);		/* stock: semaphore around pin set + transfer (0x5e5d60) */
static struct boardio *bio;

#define FAN_FRAME_LEN	7
#define NPORT_CHAIN	24
static u8 fan_shadow[2][FAN_FRAME_LEN];	/* (1,0) and (1,1): byte 0 fan, bytes 1..6 port fields; bio_lock */

/* 2 = 802.3 on every port as stock (docs/poe.md 6, bench M1: with 0 no PD is ever detected); 1 is never a default */
static uint poe_field = US48_POE_FIELD_8023;
module_param(poe_field, uint, 0444);
MODULE_PARM_DESC(poe_field, "initial PoE mode field of all 48 ports: 2 = 802.3 as stock (default), 0 = none; 1 (passive 24 V) is refused");

static uint test_bits;
static int fan_shadow_send(unsigned int g);

/* Applied at once: byte 0 of both fan chains = level bits kept, test bits replaced */
static int test_bits_set(const char *val, const struct kernel_param *kp)
{
	unsigned int g, v;
	int ret = kstrtouint(val, 0, &v);

	if (ret)
		return ret;
	if (v > 0xff || (v & 0x30))	/* the fan level bits stay owned by us48_boardio_fan_level */
		return -EINVAL;
	mutex_lock(&bio_lock);
	test_bits = v;
	for (g = 0; g < 2 && !ret; g++) {
		fan_shadow[g][0] = (fan_shadow[g][0] & 0x30) | v;
		ret = bio ? fan_shadow_send(g) : 0;
	}
	mutex_unlock(&bio_lock);
	return ret;
}

static const struct kernel_param_ops test_bits_ops = { .set = test_bits_set, .get = param_get_uint };
module_param_cb(test_bits, &test_bits_ops, &test_bits, 0644);
MODULE_PARM_DESC(test_bits, "bench only: bits OR-ed into byte 0 of the fan frames (not 5:4), bit walk #12");

/* Drive one select pin and read it back (us48_cmic_gpio returns the DATA_OUT bit of an output) */
static int sel_set(struct boardio *b, int pin, int v)
{
	int ret = gpiod_set_value_cansleep(b->sel[pin], v);

	if (ret)
		return ret;
	ret = gpiod_get_value_cansleep(b->sel[pin]);
	if (ret < 0)
		return ret;
	return ret == v ? 0 : -EIO;
}

/*
 * Pins A then B before every transfer, never restored (stock, docs/board.md). The length must match the chain, and
 * nothing is sent unless both pins read back as wanted: a PoE LED frame never reaches a (1,x) mode-select chain.
 */
static int frame_locked(enum us48_mspi_chain ch, const u8 *buf, size_t len)
{
	int ret;

	if (ch >= US48_MSPI_NCHAIN || len != chains[ch].len)
		return -EINVAL;
	if (!bio)
		return -ENODEV;
	ret = sel_set(bio, 0, chains[ch].a);
	ret = ret ?: sel_set(bio, 1, chains[ch].b);
	if (ret) {
		dev_err_ratelimited(&bio->spi->dev, "chain (%u,%u): select pins not set (%d), frame not sent\n",
				    chains[ch].a, chains[ch].b, ret);
		return ret;
	}
	return spi_write(bio->spi, buf, len);
}

int us48_mspi_frame(enum us48_mspi_chain ch, const u8 *buf, size_t len)
{
	int ret;

	mutex_lock(&bio_lock);
	ret = frame_locked(ch, buf, len);
	mutex_unlock(&bio_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_mspi_frame);

static int fan_shadow_send(unsigned int g)
{
	return frame_locked(US48_MSPI_FAN0 + g, fan_shadow[g], FAN_FRAME_LEN);
}

int us48_boardio_fan_level(unsigned int level)
{
	unsigned int g;
	int ret = 0;

	if (level > 3)
		return -EINVAL;
	mutex_lock(&bio_lock);
	for (g = 0; g < 2 && !ret; g++) {
		fan_shadow[g][0] = (level << 4) | test_bits;
		ret = fan_shadow_send(g);
	}
	mutex_unlock(&bio_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_boardio_fan_level);

int us48_boardio_poe_field(unsigned int port, unsigned int field)
{
	unsigned int g = port / NPORT_CHAIN, p = port % NPORT_CHAIN, shift = 6 - 2 * (p & 3);
	u8 *byte;
	int ret;

	if (port >= 2 * NPORT_CHAIN || field > US48_POE_FIELD_8023)
		return -EINVAL;
	mutex_lock(&bio_lock);
	byte = &fan_shadow[g][1 + (p >> 2)];
	*byte = (*byte & ~(3 << shift)) | (field << shift);
	ret = fan_shadow_send(g);
	mutex_unlock(&bio_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_boardio_poe_field);

int us48_boardio_poe_field_get(unsigned int port)
{
	unsigned int g = port / NPORT_CHAIN, p = port % NPORT_CHAIN;
	int v;

	if (port >= 2 * NPORT_CHAIN)
		return -EINVAL;
	mutex_lock(&bio_lock);
	v = (fan_shadow[g][1 + (p >> 2)] >> (6 - 2 * (p & 3))) & 3;
	mutex_unlock(&bio_lock);
	return v;
}
EXPORT_SYMBOL_GPL(us48_boardio_poe_field_get);

static void bio_detach(void *data)
{
	mutex_lock(&bio_lock);
	bio = NULL;
	mutex_unlock(&bio_lock);
}

/* devm on spi->dev: select pins as outputs, SPI mode, one instance */
int us48_boardio_attach(struct spi_device *spi)
{
	struct device *dev = &spi->dev;
	struct boardio *b;
	int ret = 0;

	if (poe_field != US48_POE_FIELD_NONE && poe_field != US48_POE_FIELD_8023)
		return dev_err_probe(dev, -EINVAL, "poe_field %u refused (0 or 2 only; passive 24 V is per port, bcm59111)\n", poe_field);
	b = devm_kzalloc(dev, sizeof(*b), GFP_KERNEL);
	if (!b)
		return -ENOMEM;
	b->spi = spi;
	/* A = 1 (fan chains), B = 0 at request, as us48_fan since 0013; stock power-on levels are not assumed */
	b->sel[0] = devm_gpiod_get_index(dev, "select", 0, GPIOD_OUT_HIGH);
	if (IS_ERR(b->sel[0]))
		return dev_err_probe(dev, PTR_ERR(b->sel[0]), "select A\n");
	b->sel[1] = devm_gpiod_get_index(dev, "select", 1, GPIOD_OUT_LOW);
	if (IS_ERR(b->sel[1]))
		return dev_err_probe(dev, PTR_ERR(b->sel[1]), "select B\n");
	spi->mode = SPI_MODE_3;	/* stock soc_mspi_config: CPOL 1, CPHA 1, 8 bit */
	spi->bits_per_word = 8;
	ret = spi_setup(spi);
	if (ret)
		return dev_err_probe(dev, ret, "spi_setup\n");
	mutex_lock(&bio_lock);
	if (bio) {
		ret = -EBUSY;
	} else {
		bio = b;
		memset(fan_shadow, poe_field == US48_POE_FIELD_8023 ? 0xaa : 0, sizeof(fan_shadow));
		fan_shadow[0][0] = fan_shadow[1][0] = 0;
	}
	mutex_unlock(&bio_lock);
	if (ret)
		return dev_err_probe(dev, ret, "board IO already attached\n");
	return devm_add_action_or_reset(dev, bio_detach, NULL);
}
EXPORT_SYMBOL_GPL(us48_boardio_attach);

MODULE_DESCRIPTION("UniFi US-48-500W MSPI board-IO chains: frame API (fans, PoE mode select, PoE LEDs) + PoE LED class devices");
MODULE_LICENSE("GPL");
