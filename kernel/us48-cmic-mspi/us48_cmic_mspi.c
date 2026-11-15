// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_mspi: write-only SPI controller on the CMIC MSPI (unit 0, 0x48001500): board IO chains (fan, PoE/port LEDs).
 * Same register layout as the iProc MSPI of spi-bcm-qspi, which is not usable here: it needs an IRQ (none known for the
 * CMIC) and has no CMIC bus claim (MAST_N_BOOT). Evidence + frame facts: docs/fan.md, docs/board.md MSPI.
 */
#include <linux/clk.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/math.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/spi/spi.h>
#include "us48_hwdb.h"

#define M(r)		HW_CMIC_MSPI_##r
#define NSLOT		16		/* TXRAM/CDRAM slots = bytes per chain */
#define SPBR_MIN	8		/* hwdb block mspi: SPBR >= 8 */
#define SPBR_MAX	255
#define SPCR1_DTL	1		/* stock value (docs/fan.md capture) */
#define SPCR1_RDSCLK	1
#define CDRAM_CONT	HW_CMIC_MSPI_CDRAM_CONT_MASK

struct mspi {
	void __iomem *base;
	unsigned long rate;
};

static size_t mspi_max_len(struct spi_device *spi)
{
	return NSLOT;
}

static int mspi_transfer_one(struct spi_controller *host, struct spi_device *spi, struct spi_transfer *t)
{
	struct mspi *m = spi_controller_get_devdata(host);
	const u8 *tx = t->tx_buf;
	u32 hz = t->speed_hz ?: spi->max_speed_hz, spbr, status, msb, expect_us;
	unsigned int i;
	int ret;

	spbr = clamp_t(u32, DIV_ROUND_UP(m->rate, 2 * hz), SPBR_MIN, SPBR_MAX);
	hz = m->rate / (2 * spbr);
	msb = HW_CMIC_MSPI_SPCR0_MSB_MSTR_MASK;
	if (spi->mode & SPI_CPOL)
		msb |= HW_CMIC_MSPI_SPCR0_MSB_CPOL_MASK;
	if (spi->mode & SPI_CPHA)
		msb |= HW_CMIC_MSPI_SPCR0_MSB_CPHA_MASK;
	writel(spbr, m->base + M(SPCR0_LSB));
	writel(msb, m->base + M(SPCR0_MSB));
	writel(SPCR1_DTL, m->base + M(SPCR1_LSB));
	writel(SPCR1_RDSCLK, m->base + M(SPCR1_MSB));
	for (i = 0; i < t->len; i++) {
		/* 8-bit slots: data in the low word of each 8-byte TXRAM entry; PCS = 0 as stock; CONT until the last */
		writel(tx[i], m->base + M(TXRAM) + 8 * i);
		writel(i + 1 < t->len ? CDRAM_CONT : 0, m->base + M(CDRAM) + 4 * i);
	}
	writel(0, m->base + M(NEWQP));
	writel(t->len - 1, m->base + M(ENDQP));
	writel(0, m->base + M(STATUS));
	writel(HW_CMIC_MSPI_SPCR2_SPE_MASK, m->base + M(SPCR2));

	/* Waits for SPIF: len x 8 bits at the programmed rate, x1.25 + 10 us for the register writes. Expiry = -ETIMEDOUT. */
	expect_us = DIV_ROUND_UP(t->len * 8 * USEC_PER_SEC, hz);
	ret = readl_poll_timeout_atomic(m->base + M(STATUS), status, status & HW_CMIC_MSPI_STATUS_SPIF_MASK, 5,
				 expect_us + expect_us / 4 + 10);
	if (ret)
		dev_err(&host->dev, "no SPIF: %u bytes at %u Hz, limit %u us, STATUS %08x\n", t->len, hz,
			expect_us + expect_us / 4 + 10, status);
	return ret;
}

static int mspi_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct spi_controller *host;
	struct clk *clk;
	struct mspi *m;
	int ret;

	host = devm_spi_alloc_host(dev, sizeof(*m));
	if (!host)
		return -ENOMEM;
	m = spi_controller_get_devdata(host);
	m->base = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(m->base))
		return PTR_ERR(m->base);
	clk = devm_clk_get_enabled(dev, NULL);
	if (IS_ERR(clk))
		return dev_err_probe(dev, PTR_ERR(clk), "clock\n");
	m->rate = clk_get_rate(clk);
	if (!m->rate)
		return dev_err_probe(dev, -EINVAL, "clock rate is 0\n");

	host->dev.of_node = dev->of_node;
	host->bus_num = -1;
	host->num_chipselect = 1;
	host->mode_bits = SPI_CPOL | SPI_CPHA;
	host->bits_per_word_mask = SPI_BPW_MASK(8);
	host->flags = SPI_CONTROLLER_NO_RX;	/* the chains are write-only shift registers */
	host->max_transfer_size = mspi_max_len;
	host->max_message_size = mspi_max_len;
	host->max_speed_hz = m->rate / (2 * SPBR_MIN);
	host->min_speed_hz = m->rate / (2 * SPBR_MAX);
	host->transfer_one = mspi_transfer_one;
	/* MSPI owns the bus (CMICM_BSPI_MAST_N_BOOT = 1, as the stock SDK) */
	writel(1, m->base + M(WRITE_LOCK));
	ret = devm_spi_register_controller(dev, host);
	if (ret)
		return dev_err_probe(dev, ret, "register\n");
	dev_info(dev, "CMIC MSPI, base clock %lu Hz, SPBR %d..%d\n", m->rate, SPBR_MIN, SPBR_MAX);
	return 0;
}

static const struct of_device_id mspi_of_match[] = {
	{ .compatible = "ubnt,us48-cmic-mspi" },
	{ }
};
MODULE_DEVICE_TABLE(of, mspi_of_match);

static struct platform_driver mspi_driver = {
	.driver = { .name = "us48_cmic_mspi", .of_match_table = mspi_of_match },
	.probe = mspi_probe,
};
module_platform_driver(mspi_driver);

MODULE_DESCRIPTION("UniFi US-48-500W CMIC MSPI: write-only board IO SPI controller");
MODULE_LICENSE("GPL");
