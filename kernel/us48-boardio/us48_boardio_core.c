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
int us48_mspi_frame(enum us48_mspi_chain ch, const u8 *buf, size_t len)
{
	int ret;

	if (ch >= US48_MSPI_NCHAIN || len != chains[ch].len)
		return -EINVAL;
	mutex_lock(&bio_lock);
	if (!bio) {
		ret = -ENODEV;
	} else {
		ret = sel_set(bio, 0, chains[ch].a);
		ret = ret ?: sel_set(bio, 1, chains[ch].b);
		if (ret)
			dev_err_ratelimited(&bio->spi->dev, "chain (%u,%u): select pins not set (%d), frame not sent\n",
					    chains[ch].a, chains[ch].b, ret);
		else
			ret = spi_write(bio->spi, buf, len);
	}
	mutex_unlock(&bio_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_mspi_frame);

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
	if (bio)
		ret = -EBUSY;
	else
		bio = b;
	mutex_unlock(&bio_lock);
	if (ret)
		return dev_err_probe(dev, ret, "board IO already attached\n");
	return devm_add_action_or_reset(dev, bio_detach, NULL);
}
EXPORT_SYMBOL_GPL(us48_boardio_attach);

MODULE_DESCRIPTION("UniFi US-48-500W MSPI board-IO chains: frame API (fans, PoE LEDs) + PoE LED class devices");
MODULE_LICENSE("GPL");
