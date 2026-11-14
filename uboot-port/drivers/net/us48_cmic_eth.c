// SPDX-License-Identifier: GPL-2.0+
/*
 * US-48 second stage, polled. Two DM_ETH drivers over the shared core (lib/bcm-cmic/bcm_cmic_hr2.c):
 * - us48_cmic_eth: BCM53346 (unit 1, PCIe) xe1 = front 50 = logical port 27 (needs stock's TSC state).
 * - us48_cmic_ge: BCM53344 (unit 0, AXI) ge1 = front 1 = logical port 2, GPORT0 lane 7, GPHY 9.
 * Select with `setenv ethact <node name>`; ge1 is probed first and is the default. M3 in docs/uboot-second-stage.md.
 */
#include <dm.h>
#include <env.h>
#include <net.h>
#include <malloc.h>
#include <asm/io.h>
#include <asm/system.h>
#include <cpu_func.h>
#include <linux/sizes.h>
#include <linux/delay.h>
#include "bcm_cmic.h"

/* Low RAM only: the chip's inbound window ends at 0x08000000, relocated U-Boot is far above. */
#define DMA_REGION	0x03000000
#define RX_DCB_OFF	0x0000
#define TX_DCB_OFF	0x1000
#define TX_BUF_OFF	0x2000
#define RX_BUF_OFF	0x10000
/* A frame (<= 1.5 KiB) at 10 Gb/s is ~1.2 us + DMA setup; 2 ms backstop. Expiry = abort TX + log. */
#define TX_WAIT_US	2000
#define TX_POLL_US	5
/* PORT_TAB CML_FLAGS: 0 = no action (stock 8 drops all unicast), docs/driver-internals.md 14.4 */
#define CML_FLAGS	0

/* AN of 1000BASE-T takes 1.5-3 s (#46 plan, x1.25 = 4 s); expiry = log "no link", start fails and the net retry loop calls again. */
#define GE_LINK_WAIT_MS	4000
#define GE_LINK_POLL_MS	20

struct us48_eth_priv {
	struct bcm_cmic_unit unit;
	struct bcm_hr2_dma dma;
	bool up;		/* bring-up done (once, first start) */
	const struct bcm_hr2_ge *ge;	/* copper port, NULL for xe1 */
	bool linkup;
};

/* Unit 0 (host SoC, AXI CMIC): front 1 = logical 2, GPORT0 (block 2) lane 7, GPHY 9; VLAN 101 as the Linux driver. */
static const struct bcm_hr2_ge ge1 = { .blk = 2, .lane = 7, .phy = 9, .lport = 2, .modid = 0, .vid = 101 };

static int us48_ge_wait_link(struct us48_eth_priv *p)
{
	int speed = 0, ms;

	for (ms = 0; ms < GE_LINK_WAIT_MS; ms += GE_LINK_POLL_MS) {
		if (bcm_hr2_ge_link(&p->unit, p->ge, &speed)) {
			bcm_hr2_ge_mac_up(&p->unit, p->ge, speed);
			p->linkup = true;
			printf("us48-ge: link up %d M after %d ms\n", speed, ms);
			return 0;
		}
		mdelay(GE_LINK_POLL_MS);
	}
	printf("us48-ge: no link after %d ms\n", GE_LINK_WAIT_MS);
	return -ENETDOWN;
}

static int us48_eth_start(struct udevice *dev)
{
	struct us48_eth_priv *p = dev_get_priv(dev);
	struct eth_pdata *pdata = dev_get_plat(dev);
	int ret;

	bcm_hr2_dma_init(&p->unit);
	if (!p->up && p->ge) {
		int ops, errs;
		u32 v[2] = { 0, 0 };

		bcm_hr2_u0_cold_init(&p->unit, env_get_hex("us48_init_mask", BCM_HR2_INIT_ALL), &ops, &errs);
		if (ops) {
			bcm_cmic_reg_read(&p->unit, 2, 0x12207, v, 1, NULL);
			printf("us48-ge: cold init %d ops, %d failed; UMAC_REV_ID %08x\n", ops, errs, v[0]);
		}
	}
	if (!p->up) {
		ret = p->ge ? bcm_hr2_ge_bringup(&p->unit, p->ge) : bcm_hr2_xe1_bringup(&p->unit, CML_FLAGS);
		if (ret)
			return ret;
		p->up = true;
	}
	if (p->ge) {
		ret = bcm_hr2_ge_l2_cpu(&p->unit, p->ge, pdata->enetaddr, true);
		if (!ret && !p->linkup)
			ret = us48_ge_wait_link(p);
	} else {
		ret = bcm_hr2_l2_cpu(&p->unit, pdata->enetaddr, true);
	}
	if (ret)
		return ret;
	p->dma.tx_len = 0;
	bcm_hr2_rx_arm(&p->unit, &p->dma);
	return 0;
}

static int us48_eth_send(struct udevice *dev, void *packet, int length)
{
	struct us48_eth_priv *p = dev_get_priv(dev);
	int ret, i;

	ret = bcm_hr2_tx_start(&p->unit, &p->dma, packet, length);
	if (ret)
		return ret;
	for (i = 0; i < TX_WAIT_US; i += TX_POLL_US) {
		if (bcm_hr2_tx_reap(&p->unit, &p->dma))
			return 0;
		udelay(TX_POLL_US);
	}
	printf("us48-eth: TX not done after %u us, aborting\n", TX_WAIT_US);
	bcm_hr2_dma_abort(&p->unit, 0);
	p->dma.tx_len = 0;
	return -ETIMEDOUT;
}

static int us48_eth_recv(struct udevice *dev, int flags, uchar **packetp)
{
	struct us48_eth_priv *p = dev_get_priv(dev);
	int len = bcm_hr2_rx_next(&p->unit, &p->dma, packetp);

	return len > 0 ? len : -EAGAIN;
}

static int us48_eth_free_pkt(struct udevice *dev, uchar *packet, int length)
{
	struct us48_eth_priv *p = dev_get_priv(dev);

	bcm_hr2_rx_done(&p->unit, &p->dma);
	return 0;
}

/* Runs before bootm too: no DMA may run into the next stage. */
static void us48_eth_stop(struct udevice *dev)
{
	struct us48_eth_priv *p = dev_get_priv(dev);
	struct eth_pdata *pdata = dev_get_plat(dev);

	if (!p->up)
		return;
	bcm_hr2_dma_abort(&p->unit, 1);
	bcm_hr2_dma_abort(&p->unit, 0);
	p->dma.tx_len = 0;
	if (p->ge)
		bcm_hr2_ge_l2_cpu(&p->unit, p->ge, pdata->enetaddr, false);
	else
		bcm_hr2_l2_cpu(&p->unit, pdata->enetaddr, false);
}

static int us48_eth_of_to_plat(struct udevice *dev)
{
	struct eth_pdata *pdata = dev_get_plat(dev);

	pdata->iobase = dev_read_addr(dev);
	return pdata->iobase == FDT_ADDR_T_NONE ? -EINVAL : 0;
}

static int us48_eth_probe(struct udevice *dev)
{
	struct us48_eth_priv *p = dev_get_priv(dev);
	struct eth_pdata *pdata = dev_get_plat(dev);
	struct bcm_hr2_dma *d = &p->dma;

	/*
	 * DCBs, RX ring and TX buffers are written by the CMIC behind the CPU: they must not sit in the D-cache
	 * (stale DONE bits, dirty lines evicted over DMA data lost most received frames). Linux uses coherent memory.
	 */
	mmu_set_region_dcache_behaviour(DMA_REGION, SZ_1M, DCACHE_OFF);
	/* Registers are touched on the first start only: probe runs at boot on every net init. */
	p->unit.base = (void __iomem *)(uintptr_t)pdata->iobase;
	d->rx_dcb = (__le32 *)(DMA_REGION + RX_DCB_OFF);
	d->rx_dcb_bus = DMA_REGION + RX_DCB_OFF;
	d->tx_dcb = (__le32 *)(DMA_REGION + TX_DCB_OFF);
	d->tx_dcb_bus = DMA_REGION + TX_DCB_OFF;
	d->tx_buf = (u8 *)(DMA_REGION + TX_BUF_OFF);
	d->tx_buf_bus = DMA_REGION + TX_BUF_OFF;
	d->rx_buf = (u8 *)(DMA_REGION + RX_BUF_OFF);
	d->rx_buf_bus = DMA_REGION + RX_BUF_OFF;
	d->tx_port = BCM_HR2_XE1_PORT;
	d->vid = BCM_HR2_PRIVATE_VID;
	return 0;
}

/* Own 1 MiB DMA region (uncached) so xe1 and ge1 never share DCBs. */
#define GE_DMA_REGION	0x03100000

static int us48_ge_probe(struct udevice *dev)
{
	struct us48_eth_priv *p = dev_get_priv(dev);
	struct eth_pdata *pdata = dev_get_plat(dev);
	struct bcm_hr2_dma *d = &p->dma;

	mmu_set_region_dcache_behaviour(GE_DMA_REGION, SZ_1M, DCACHE_OFF);
	p->unit.base = (void __iomem *)(uintptr_t)pdata->iobase;
	p->ge = &ge1;
	d->rx_dcb = (__le32 *)(GE_DMA_REGION + RX_DCB_OFF);
	d->rx_dcb_bus = GE_DMA_REGION + RX_DCB_OFF;
	d->tx_dcb = (__le32 *)(GE_DMA_REGION + TX_DCB_OFF);
	d->tx_dcb_bus = GE_DMA_REGION + TX_DCB_OFF;
	d->tx_buf = (u8 *)(GE_DMA_REGION + TX_BUF_OFF);
	d->tx_buf_bus = GE_DMA_REGION + TX_BUF_OFF;
	d->rx_buf = (u8 *)(GE_DMA_REGION + RX_BUF_OFF);
	d->rx_buf_bus = GE_DMA_REGION + RX_BUF_OFF;
	d->tx_port = ge1.lport;
	d->vid = ge1.vid;
	return 0;
}

static const struct eth_ops us48_eth_ops = {
	.start		= us48_eth_start,
	.send		= us48_eth_send,
	.recv		= us48_eth_recv,
	.free_pkt	= us48_eth_free_pkt,
	.stop		= us48_eth_stop,
};

static const struct udevice_id us48_eth_ids[] = {
	{ .compatible = "ubnt,us48-cmic-eth" },
	{ }
};

U_BOOT_DRIVER(us48_cmic_eth) = {
	.name		= "us48_cmic_eth",
	.id		= UCLASS_ETH,
	.of_match	= us48_eth_ids,
	.of_to_plat	= us48_eth_of_to_plat,
	.probe		= us48_eth_probe,
	.ops		= &us48_eth_ops,
	.priv_auto	= sizeof(struct us48_eth_priv),
	.plat_auto	= sizeof(struct eth_pdata),
	.flags		= DM_FLAG_ALLOC_PRIV_DMA,
};

static const struct udevice_id us48_ge_ids[] = {
	{ .compatible = "ubnt,us48-cmic-ge" },
	{ }
};

U_BOOT_DRIVER(us48_cmic_ge) = {
	.name		= "us48_cmic_ge",
	.id		= UCLASS_ETH,
	.of_match	= us48_ge_ids,
	.of_to_plat	= us48_eth_of_to_plat,
	.probe		= us48_ge_probe,
	.ops		= &us48_eth_ops,
	.priv_auto	= sizeof(struct us48_eth_priv),
	.plat_auto	= sizeof(struct eth_pdata),
	.flags		= DM_FLAG_ALLOC_PRIV_DMA,
};
