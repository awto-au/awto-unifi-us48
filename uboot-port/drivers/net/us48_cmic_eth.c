// SPDX-License-Identifier: GPL-2.0+
/*
 * US-48 second stage, polled. Two DM_ETH drivers over the shared core (lib/bcm-cmic/bcm_cmic_hr2.c):
 * - us48_cmic_eth: BCM53346 (unit 1, PCIe) xe1 = front 50 = logical port 27 (needs stock's TSC state).
 * - us48_cmic_ge: BCM53344 (unit 0, AXI) ge1 = front 1 = logical port 2, GPORT0 lane 7, GPHY 9.
 * Select with `setenv ethact <node name>`; ge1 is probed first and is the default. M3 in docs/uboot-second-stage.md.
 */
#include <dm.h>
#include <command.h>
#include <env.h>
#include <net.h>
#include <time.h>
#include <malloc.h>
#include <asm/io.h>
#include <asm/system.h>
#include <cpu_func.h>
#include <linux/sizes.h>
#include <linux/delay.h>
#include "bcm_cmic.h"

/* board pcie.c: unit 1 reachable (link up, BARs in place); any access to 0x08xxxxxx with a dead link hangs (#76) */
bool us48_pcie_up(void);

#define EP_BAR0			0x08000000	/* board pcie.c: unit 1 BAR0 (PAXB regs at +0x2000 + off) */
#define EP_PAXB(off)		((void __iomem *)(EP_BAR0 + (off)))
#define BCM_HR2_INIT_U1		0x1f

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

/*
 * Flush before the remap: lines cached earlier (D-cache is on since enable_caches) must not be evicted over DMA data.
 * DCBs, RX ring and TX buffers are written by the CMIC behind the CPU: uncached, so no per-frame maintenance (#75).
 */
static void us48_dma_uncached(ulong base)
{
	flush_dcache_range(base, base + SZ_1M);
	mmu_set_region_dcache_behaviour(base, SZ_1M, DCACHE_OFF);
}

struct us48_eth_priv {
	struct bcm_cmic_unit unit;
	struct bcm_hr2_dma dma;
	bool up;		/* bring-up done (once, first start) */
	const struct bcm_hr2_ge *ge;	/* copper port, NULL for xe1 */
	bool linkup;
	bool quiesced;		/* CPU removed from the VLAN by us48_net_quiesce (#104) */
};

/* Started devices, for the sf hook and `us48quiesce` (one per unit). */
static struct us48_eth_priv *us48_privs[2];
static unsigned int us48_flush_n, us48_flush_us;	/* last flush: frames taken, time */

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

/*
 * Frames queued in the chip while the DMA was off arrive in one burst at arm, 3 us apart (58 in 180 us).
 * A burst past the 32-slot ring while the CPU is elsewhere stalls the engine for seconds: take them first (#104).
 * Quiet 50 us = 8x the widest gap seen (6 us); backstop 2 ms = 10x the 58-frame drain. Expiry = log, go on.
 */
#define FLUSH_QUIET_US	50
#define FLUSH_MAX_US	2000

static void us48_rx_flush(struct us48_eth_priv *p)
{
	ulong t0 = timer_get_us(), last = t0, now;
	unsigned int n = 0;
	u8 *pkt;

	if (!env_get_ulong("us48_flush", 10, 1)) {	/* test knob: 0 = arm only */
		us48_flush_n = us48_flush_us = 0;
		return;
	}
	while ((now = timer_get_us()) - last < FLUSH_QUIET_US) {
		if (now - t0 > FLUSH_MAX_US) {
			printf("us48-eth: rx flush not quiet after %u us, %u frames taken\n", FLUSH_MAX_US, n);
			break;
		}
		if (bcm_hr2_rx_next(&p->unit, &p->dma, &pkt) > 0) {
			bcm_hr2_rx_done(&p->unit, &p->dma);
			n++;
			last = timer_get_us();
		}
	}
	us48_flush_n = n;
	us48_flush_us = last - t0;
	bcm_hr2_rx_arm(&p->unit, &p->dma);	/* clean ring and engine for the caller */
}

/*
 * Quiesce (#104): CPU removed from the private VLAN (bcm_hr2_cpu_vlan) while the DMA is off, so a long flash erase/write
 * leaves almost no backlog in the chip CPU queue. Resume (start, remove) restores exactly what was changed. Idempotent.
 * CPU egress off (EGR_ENABLE[0]) was tried: the backlog stays (docs/uboot-second-stage.md #104).
 */
static void us48_quiesce(struct us48_eth_priv *p, bool on)
{
	u32 vid;

	if (!p || !p->up || p->quiesced == on)
		return;
	vid = p->ge ? p->ge->vid : BCM_HR2_PRIVATE_VID;
	if (on) {
		p->quiesced = bcm_hr2_cpu_vlan(&p->unit, vid, false) == 1;
	} else if (bcm_hr2_cpu_vlan(&p->unit, vid, true) >= 0) {
		p->quiesced = false;
	} else {
		printf("us48-eth: CPU VLAN membership resume failed\n");
	}
}

/* Called from `sf erase|write` (patch 0004): weak-linked, so builds without this driver still link. */
void us48_net_quiesce(void)
{
	int i;

	if (!env_get_ulong("us48_quiesce", 10, 1))	/* test knob */
		return;
	for (i = 0; i < ARRAY_SIZE(us48_privs); i++)
		us48_quiesce(us48_privs[i], true);
}

static int us48_eth_start(struct udevice *dev)
{
	struct us48_eth_priv *p = dev_get_priv(dev);
	struct eth_pdata *pdata = dev_get_plat(dev);
	int ret;

	if (!p->ge && !us48_pcie_up()) {
		printf("us48_cmic_eth: PCIe link to unit 1 down, xe1 not started\n");
		return -ENODEV;
	}
	if (!p->up && !p->ge) {
		int ops, errs;

		/* Endpoint PAXB outbound window for DMA to host RAM (docs/driver-internals.md 14.2), BAR0 = EP_BAR0 */
		writel(0x0, EP_PAXB(0x2104));
		writel(0x1, EP_PAXB(0x2d60));
		writel(0x1, EP_PAXB(0x2d64));
		bcm_hr2_u1_cold_init(&p->unit, env_get_hex("us48_u1_init_mask", BCM_HR2_INIT_U1), &ops, &errs);
		if (ops)
			printf("us48-xe1: cold init %d ops, %d failed\n", ops, errs);
	}
	bcm_hr2_dma_init(&p->unit);
	if (!p->up && p->ge) {
		int ops, errs;
		u32 v[2] = { 0, 0 };

		bcm_hr2_u0_cold_init(&p->unit, env_get_hex("us48_init_mask", BCM_HR2_INIT_ALL), &ops, &errs);
		if (ops) {
			bcm_cmic_reg_read(&p->unit, 2, 0x12207, v, 1, NULL);
			printf("us48-ge: cold init %d ops, %d failed; UMAC_REV_ID %08x\n", ops, errs, v[0]);
			bcm_hr2_dma_init(&p->unit);	/* credits released before the EP pipe reset are lost */
		}
	}
	if (!p->up) {
		ret = p->ge ? bcm_hr2_ge_bringup(&p->unit, p->ge) : bcm_hr2_xe1_bringup(&p->unit, CML_FLAGS);
		if (ret) {
			printf("us48-%s: bring-up FAILED (%d), port not started\n", p->ge ? "ge1" : "xe1", ret);
			return ret;
		}
		p->up = true;
	}
	us48_privs[p->ge ? 0 : 1] = p;
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
	us48_quiesce(p, false);
	bcm_hr2_rx_arm(&p->unit, &p->dma);
	us48_rx_flush(p);
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

/* Both channels off and DESC cleared: no DMA may run into the next stage. */
static void us48_eth_dma_off(struct us48_eth_priv *p)
{
	bcm_hr2_dma_abort(&p->unit, 1);
	bcm_hr2_dma_abort(&p->unit, 0);
}

/* bootm calls remove on DM_FLAG_ACTIVE_DMA devices (eth_halt only reaches the current device, and only with NETCONSOLE). */
static int us48_eth_remove(struct udevice *dev)
{
	struct us48_eth_priv *p = dev_get_priv(dev);

	us48_quiesce(p, false);		/* the next stage (Linux) must find the CPU port as init left it */
	if (p->up)
		us48_eth_dma_off(p);
	return 0;
}

static void us48_eth_stop(struct udevice *dev)
{
	struct us48_eth_priv *p = dev_get_priv(dev);
	struct eth_pdata *pdata = dev_get_plat(dev);

	if (!p->up)
		return;
	us48_eth_dma_off(p);
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

	us48_dma_uncached(DMA_REGION);
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

	us48_dma_uncached(GE_DMA_REGION);
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
	.remove		= us48_eth_remove,
	.ops		= &us48_eth_ops,
	.priv_auto	= sizeof(struct us48_eth_priv),
	.plat_auto	= sizeof(struct eth_pdata),
	.flags		= DM_FLAG_ALLOC_PRIV_DMA | DM_FLAG_ACTIVE_DMA,
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
	.remove		= us48_eth_remove,
	.ops		= &us48_eth_ops,
	.priv_auto	= sizeof(struct us48_eth_priv),
	.plat_auto	= sizeof(struct eth_pdata),
	.flags		= DM_FLAG_ALLOC_PRIV_DMA | DM_FLAG_ACTIVE_DMA,
};

/* us48quiesce on|off|stat: manual quiesce/resume of the started units (tests, #104). */
static int do_us48quiesce(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	int i, n = 0;
	ulong t0;

	if (argc != 2)
		return CMD_RET_USAGE;
	if (!strcmp(argv[1], "stat")) {
		printf("us48quiesce: last rx flush %u frames in %u us\n", us48_flush_n, us48_flush_us);
		for (i = 0; i < ARRAY_SIZE(us48_privs); i++)
			if (us48_privs[i])
				printf("us48quiesce: %s quiesced=%d\n", i ? "xe1" : "ge1", us48_privs[i]->quiesced);
		return CMD_RET_SUCCESS;
	}
	if (strcmp(argv[1], "on") && strcmp(argv[1], "off"))
		return CMD_RET_USAGE;
	t0 = timer_get_us();
	for (i = 0; i < ARRAY_SIZE(us48_privs); i++) {
		us48_quiesce(us48_privs[i], argv[1][1] == 'n');
		n += us48_privs[i] && us48_privs[i]->quiesced == (argv[1][1] == 'n');
	}
	printf("us48quiesce: %s, %d unit(s) in that state, %lu us\n", argv[1], n, timer_get_us() - t0);
	return CMD_RET_SUCCESS;
}

U_BOOT_CMD(us48quiesce, 2, 0, do_us48quiesce, "CPU out of/in the VLAN while the DMA is off",
	   "on|off|stat - on: no frames queue in the chip for the CPU (resumed at the next net start and before bootm)");
