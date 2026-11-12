// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic: bring-up module for the BCM53346 (unit 1) CMIC on the UniFi US-48-500W.
 * Step 3: netdev for xe1 (front 50): RX ring on CMC0 ch1, TX on ch0 straight to port 27 (module
 * header), polled; xe1 enabled from stock's disabled state. docs/driver-internals.md 1, 5, 13.5, 14.
 */
#include <linux/delay.h>
#include <linux/etherdevice.h>
#include <linux/hrtimer.h>
#include <linux/dma-mapping.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/pci.h>
#include <linux/if_vlan.h>
#include <linux/unaligned.h>
#include <linux/netdevice.h>

#define CMIC_BAR		2
#define SCHAN_CTRL		0x31000
#define SCHAN_MSG(n)		(0x3100c + 4 * (n))
#define  SCHAN_START		BIT(0)
#define  SCHAN_DONE		BIT(1)
#define  SCHAN_ERRS		(BIT(20) | BIT(21) | BIT(22))
#define DMA_CTRL(ch)		(0x31140 + 4 * (ch))
#define  DMA_DIR_TX		BIT(0)
#define  DMA_EN			BIT(1)
#define  DMA_ABORT		BIT(2)
#define  DMA_ENDIAN		(BIT(4) | BIT(5))
#define DMA_STAT		0x31150
#define  DMA_DESC_DONE(ch)	BIT(4 + (ch))
#define  DMA_ACTIVE(ch)		BIT(8 + (ch))
#define DMA_DESC(ch)		(0x31158 + 4 * (ch))
#define COS_CTRL_RX_0(ch)	(0x31168 + 8 * (ch))
#define COS_CTRL_RX_1(ch)	(0x3116c + 8 * (ch))
#define DMA_CURR_DESC(ch)	(0x311a8 + 4 * (ch))
#define DMA_STAT_CLR		0x311a4
#define TX_CH			0
#define RX_CH			1
#define BUF_SZ			2048
#define RX_RING			16
#define DCB_W1_CHAIN		BIT(16)
#define DCB_W1_SG		BIT(17)
#define DCB_W1_HG		BIT(19)
#define DCB_W1_STAT		BIT(20)
#define DCB_W15_DONE		BIT(31)
#define DCB_W15_ERR		BIT(18)
/* EPC_LINK_BMAP_64: IPIPE block 10, 64-bit; bit 0 = CPU port. */
#define BLK_IPIPE		10
#define EPC_LINK_BMAP_64	0x46002700
#define BLK_EPIPE		11
#define VLAN_TAB(i)		(0x14000000 + (i))
#define STG_TAB(i)		(0x14040000 + (i))
#define EGR_VLAN(i)		(0x100c0000 + (i))
#define EGR_VLAN_STG(i)		(0x10100000 + (i))
#define VLAN_PROFILE_TAB(i)	(0x14080000 + (i))
#define EGR_ENABLE(p)		(0x345c0000 + (p))
#define L2X			0x1c000000	/* hashed: TABLE_INSERT, no index */
#define UNIT1_MODID		1		/* PORT_TAB.MY_MODID, read live */
#define XE1_PORT		27	/* front 50 */
#define XE1_LANE		2	/* TSC0 lane, XLPORT low address byte */
#define BLK_XLPORT		5
#define MIIM_PARAM		0x31080
#define MIIM_READ_DATA		0x31084
#define MIIM_ADDRESS		0x31088
#define MIIM_CTRL		0x3108c
#define MIIM_STAT		0x31090
/* Internal bus 1, clause 45 (INTERNAL_SEL b25, BUS_ID [24:22], C45 b21); TSC0 at address 2. */
#define MIIM_TSC0		(BIT(25) | 1 << 22 | BIT(21) | 2 << 16)
/* C45 op = address + data frame, 2 x 26 us at MDC 2.5 MHz (section 13); 1.25x. Expiry logged. */
#define MIIM_TIMEOUT_US		65
/* One SBUS op answers in a few us; 10 ms = 1000x backstop. Expiry = op abandoned, logged. */
#define SCHAN_TIMEOUT_US	10000
/* Abort completes after one in-flight descriptor (tens of us); 10 ms backstop, logged on expiry. */
#define ABORT_TIMEOUT_US	10000
/* No IRQ yet: poll every 1 ms (ping latency target); NAPI drains the ring. */
#define POLL_NS			(1000 * 1000)

struct us48_cmic {
	struct pci_dev *pdev;
	struct net_device *ndev;
	void __iomem *cmic;
	struct napi_struct napi;
	struct hrtimer poll_timer;
	__le32 *rx_dcb;			/* RX_RING x 16 words */
	dma_addr_t rx_dcb_dma;
	void *rx_buf;			/* RX_RING x BUF_SZ */
	dma_addr_t rx_buf_dma;
	int rx_next;
	__le32 *tx_dcb;			/* 2 x 16 words */
	dma_addr_t tx_dcb_dma;
	void *tx_buf;
	dma_addr_t tx_buf_dma;
	unsigned int tx_len;		/* 0 = idle */
};

static u32 rd(struct us48_cmic *u, u32 off) { return ioread32(u->cmic + off); }
static void wr(struct us48_cmic *u, u32 off, u32 v) { iowrite32(v, u->cmic + off); }

/* SBUS op: reg read 0x0b / write 0x0d, mem read 0x07 / write 0x09, table insert 0x24. Section 1. */
static int sbus(struct us48_cmic *u, u32 op, u32 blk, u32 addr, u32 *data, int words)
{
	bool write = op == 0x0d || op == 0x09 || op == 0x24;
	u32 ctrl;
	int i, ret;

	wr(u, SCHAN_CTRL, 0);
	wr(u, SCHAN_MSG(0), op << 26 | blk << 20 | (4 * words) << 7);
	wr(u, SCHAN_MSG(1), addr);
	for (i = 0; write && i < words; i++)
		wr(u, SCHAN_MSG(2 + i), data[i]);
	wr(u, SCHAN_CTRL, SCHAN_START);
	ret = readl_poll_timeout_atomic(u->cmic + SCHAN_CTRL, ctrl, ctrl & (SCHAN_DONE | SCHAN_ERRS),
					1, SCHAN_TIMEOUT_US);
	if (ret || (ctrl & SCHAN_ERRS)) {
		dev_err(&u->pdev->dev, "sbus op %02x blk %u addr %08x: ctrl %08x after %u us\n",
			op, blk, addr, ctrl, SCHAN_TIMEOUT_US);
		wr(u, SCHAN_CTRL, 0);
		return ret ?: -EIO;
	}
	for (i = 0; !write && i < words; i++)
		data[i] = rd(u, SCHAN_MSG(1 + i));
	wr(u, SCHAN_CTRL, 0);
	return 0;
}

/* Set bits (bit numbers into the entry) and keep EVEN_PARITY at bit par consistent. */
static void smem_set_bits(struct us48_cmic *u, u32 blk, u32 addr, int words, int par,
			  const int *bits, int n)
{
	u32 e[4];
	int i, flips = 0;

	if (sbus(u, 0x07, blk, addr, e, words))
		return;
	for (i = 0; i < n; i++) {
		if (!(e[bits[i] / 32] & BIT(bits[i] % 32)))
			flips++;
		e[bits[i] / 32] |= BIT(bits[i] % 32);
	}
	if (flips & 1)
		e[par / 32] ^= BIT(par % 32);
	if (flips)
		sbus(u, 0x09, blk, addr, e, words);
	dev_info(&u->pdev->dev, "mem %u/%08x: %d bit(s) set, w0 %08x w1 %08x\n", blk, addr, flips, e[0], e[1]);
}

/* VLAN 1 + STG 1 lack the CPU and xe1 (27) after stock: add both (driver-internals 14.4). */
static void cpu_path_init(struct us48_cmic *u)
{
	static const int vlan[] = { 0, XE1_PORT };			/* PORT_BITMAP 0:30 */
	static const int egr[] = { 30, 30 + XE1_PORT, 0, XE1_PORT };	/* PORT_BITMAP 30:30, UT_BITMAP 0:30 */
	static const int stg[] = { 0, 1, 2 * XE1_PORT, 2 * XE1_PORT + 1 };	/* SP_TREE 3 = forward */

	smem_set_bits(u, BLK_IPIPE, VLAN_TAB(1), 4, 126, vlan, 2);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN(1), 3, 76, egr, 4);
	smem_set_bits(u, BLK_IPIPE, STG_TAB(1), 2, 60, stg, 4);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN_STG(1), 2, 60, stg, 4);
	/* Stock's port disable leaves EGR_ENABLE[27] = 0: every frame to xe1 dies before the MAC. */
	{
		u32 en = 1;

		if (sbus(u, 0x09, BLK_EPIPE, EGR_ENABLE(XE1_PORT), &en, 1) == 0)
			dev_info(&u->pdev->dev, "EGR_ENABLE[%d] <- 1\n", XE1_PORT);
	}
}

/* VLAN_PROFILE_TAB.L2_MISS_TOCPU: redirects (not copies) L2 misses to the CPU, own TX included. Test knob. */
static bool l2_miss_cpu;
module_param(l2_miss_cpu, bool, 0444);

static void l2_miss_set(struct us48_cmic *u)
{
	u32 e[4], prof;

	if (sbus(u, 0x07, BLK_IPIPE, VLAN_TAB(1), e, 4))
		return;
	prof = VLAN_PROFILE_TAB((e[1] >> 28) & 0xf);	/* VLAN_PROFILE_PTR [63:60] */
	if (sbus(u, 0x07, BLK_IPIPE, prof, e, 1))
		return;
	e[0] = l2_miss_cpu ? e[0] | BIT(18) : e[0] & ~BIT(18);
	sbus(u, 0x09, BLK_IPIPE, prof, e, 1);
	dev_info(&u->pdev->dev, "VLAN_PROFILE %08x: %08x\n", prof, e[0]);
}

/* true: module header straight to xe1 (no L2 learning); false: CPU-port ingress, switched + learnt. */
static bool tx_direct = true;
module_param(tx_direct, bool, 0644);
/* Module-header dest port: logical 27 or physical 28 for xe1 (which one HR2 wants is under test, #4). */
static uint tx_port = XE1_PORT;
module_param(tx_port, uint, 0644);

static int tsc_wr(struct us48_cmic *u, u32 reg, u16 val)
{
	u32 st;
	int ret;

	wr(u, MIIM_CTRL, 0);
	wr(u, MIIM_PARAM, MIIM_TSC0 | val);
	wr(u, MIIM_ADDRESS, reg);			/* devad 0 */
	wr(u, MIIM_CTRL, BIT(0));			/* WR_START */
	ret = readl_poll_timeout_atomic(u->cmic + MIIM_STAT, st, st & BIT(0), 1, MIIM_TIMEOUT_US);
	wr(u, MIIM_CTRL, 0);
	if (ret)
		dev_err(&u->pdev->dev, "tsc write %04x <- %04x: no OPN_DONE in %u us\n", reg, val,
			MIIM_TIMEOUT_US);
	return ret;
}

/* Undo stock's port disable on xe1 (front 50): verified order, driver-internals 13.5. */
static void xe1_enable(struct us48_cmic *u)
{
	static const u16 seq[][2] = {
		{ 0xffde, XE1_LANE },			/* AER: lane select */
		{ 0xc138, 0x0009 },			/* RX_SIGDET: drop the forced signal-detect bits */
		{ 0xc101, 0x0084 }, { 0xc100, 0x4021 },	/* TX credits, CREDITENABLE */
		{ 0xc017, 0x2020 },			/* ANATX_DRIVER: clear ELEC_IDLE */
		{ 0xc01a, 0x0000 },			/* ANATX_CONTROL6: clear TX disable */
		{ 0xc113, 0xc1c8 }, { 0xc113, 0xc1cb },	/* TX lane reset pulse, after credits */
		{ 0xc137, 0x0001 },			/* RX lane reset release */
	};
	u32 v[2];
	int i;

	/* Only from stock's parked state: re-crediting a running port is not known safe. */
	if (!sbus(u, 0x0b, BLK_XLPORT, 0x60000 | XE1_LANE, v, 2) && (v[0] & 0x43) == 0x03) {
		dev_info(&u->pdev->dev, "xe1 MAC already enabled (XLMAC_CTRL %08x): skip enable\n", v[0]);
		return;
	}
	for (i = 0; i < ARRAY_SIZE(seq); i++)
		if (tsc_wr(u, seq[i][0], seq[i][1]))
			return;
	v[0] = 7;
	sbus(u, 0x0d, BLK_XLPORT, 0x20300 | XE1_LANE, v, 1);	/* XLPORT_TXFIFO_CTRL clear counts */
	v[0] = 4;
	sbus(u, 0x0d, BLK_XLPORT, 0x20300 | XE1_LANE, v, 1);
	v[0] = 0x1003; v[1] = 0;				/* XLMAC_CTRL: TX_EN, RX_EN, SOFT_RESET 0 */
	sbus(u, 0x0d, BLK_XLPORT, 0x60000 | XE1_LANE, v, 2);
	v[0] = 7;						/* XLMAC_CLEAR_RX_LSS_STATUS pulse */
	sbus(u, 0x0d, BLK_XLPORT, 0x60c00 | XE1_LANE, v, 2);
	v[0] = 0;
	sbus(u, 0x0d, BLK_XLPORT, 0x60c00 | XE1_LANE, v, 2);
	dev_info(&u->pdev->dev, "xe1 (front 50) enable sequence done\n");
}

static void set_field(u32 *e, int lsb, int width, u64 v)
{
	int i;

	for (i = 0; i < width; i++)
		if (v & BIT_ULL(i))
			e[(lsb + i) / 32] |= BIT((lsb + i) % 32);
}

/* Static L2X entry: our MAC in VLAN 1 -> CPU (modid 1, port 0). TX bypasses learning (module header). */
static int l2_add_cpu(struct us48_cmic *u, const u8 *mac)
{
	u32 e[4] = { };
	int i, ones = 0;

	set_field(e, 2, 12, 1);				/* VLAN_ID */
	set_field(e, 14, 48, ether_addr_to_u64(mac));	/* MAC_ADDR */
	set_field(e, 62, 6, 0);				/* PORT_NUM: CPU */
	set_field(e, 68, 7, UNIT1_MODID);		/* MODULE_ID */
	set_field(e, 97, 1, 1);				/* STATIC_BIT */
	set_field(e, 99, 1, 1);				/* VALID */
	for (i = 0; i < 4; i++)
		ones += hweight32(e[i]);
	set_field(e, 100, 1, ones & 1);			/* EVEN_PARITY */
	return sbus(u, 0x24, BLK_IPIPE, L2X, e, 4);
}

static void dma_abort(struct us48_cmic *u, int ch)
{
	u32 ctrl = rd(u, DMA_CTRL(ch)), stat;

	if (rd(u, DMA_STAT) & DMA_ACTIVE(ch)) {
		wr(u, DMA_CTRL(ch), ctrl | DMA_EN | DMA_ABORT);
		if (readl_poll_timeout_atomic(u->cmic + DMA_STAT, stat, !(stat & DMA_ACTIVE(ch)),
					      1, ABORT_TIMEOUT_US))
			dev_err(&u->pdev->dev, "ch%d abort: still active after %u us, DMA_STAT %08x\n",
				ch, ABORT_TIMEOUT_US, stat);
	}
	wr(u, DMA_CTRL(ch), rd(u, DMA_CTRL(ch)) & ~(DMA_EN | DMA_ABORT));
	wr(u, DMA_STAT_CLR, BIT(ch));
}


static void rx_arm(struct us48_cmic *u)
{
	int i;

	memset(u->rx_dcb, 0, RX_RING * 64);
	for (i = 0; i < RX_RING; i++) {
		u->rx_dcb[16 * i] = cpu_to_le32(u->rx_buf_dma + i * BUF_SZ);
		u->rx_dcb[16 * i + 1] = cpu_to_le32(BUF_SZ | (i < RX_RING - 1 ? DCB_W1_CHAIN : 0));
	}
	u->rx_next = 0;
	wmb();
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & ~DMA_EN);
	wr(u, DMA_STAT_CLR, BIT(RX_CH));
	wr(u, DMA_DESC(RX_CH), u->rx_dcb_dma);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) | DMA_EN);
}

/* One received DCB -> skb: drop the 4 CRC bytes and the VLAN 1 tag the CPU copy carries. */
static void rx_one(struct us48_cmic *u, int i)
{
	struct net_device *nd = u->ndev;
	u32 w15 = le32_to_cpu(u->rx_dcb[16 * i + 15]);
	u8 *p = u->rx_buf + i * BUF_SZ;
	unsigned int len = w15 & 0xffff;
	struct sk_buff *skb;

	if ((w15 & DCB_W15_ERR) || len < ETH_ZLEN + ETH_FCS_LEN || len > BUF_SZ) {
		nd->stats.rx_errors++;
		return;
	}
	len -= ETH_FCS_LEN;
	skb = napi_alloc_skb(&u->napi, len);
	if (!skb) {
		nd->stats.rx_dropped++;
		return;
	}
	if (get_unaligned_be16(p + 12) == ETH_P_8021Q && (get_unaligned_be16(p + 14) & VLAN_VID_MASK) == 1) {
		skb_put_data(skb, p, 12);
		skb_put_data(skb, p + 16, len - 16);
	} else {
		skb_put_data(skb, p, len);
	}
	skb->protocol = eth_type_trans(skb, nd);
	nd->stats.rx_packets++;
	nd->stats.rx_bytes += skb->len;
	napi_gro_receive(&u->napi, skb);
}

static void tx_reap(struct us48_cmic *u)
{
	struct net_device *nd = u->ndev;

	netif_tx_lock(nd);
	if (u->tx_len && (le32_to_cpu(READ_ONCE(u->tx_dcb[16 + 15])) & DCB_W15_DONE)) {
		wr(u, DMA_CTRL(TX_CH), rd(u, DMA_CTRL(TX_CH)) & ~DMA_EN);
		wr(u, DMA_STAT_CLR, BIT(TX_CH));
		nd->stats.tx_packets++;
		nd->stats.tx_bytes += u->tx_len;
		u->tx_len = 0;
		netif_wake_queue(nd);
	}
	netif_tx_unlock(nd);
}

static int us48_poll(struct napi_struct *napi, int budget)
{
	struct us48_cmic *u = container_of(napi, struct us48_cmic, napi);
	int done = 0;

	tx_reap(u);
	while (done < budget && u->rx_next < RX_RING) {
		int i = u->rx_next;

		if (!(le32_to_cpu(READ_ONCE(u->rx_dcb[16 * i + 15])) & DCB_W15_DONE))
			break;
		dma_rmb();
		rx_one(u, i);
		u->rx_next++;
		done++;
	}
	if (u->rx_next == RX_RING)	/* chain ended: engine stopped, re-arm from DCB 0 */
		rx_arm(u);
	if (done < budget)
		napi_complete_done(napi, done);
	return done;
}

static enum hrtimer_restart us48_poll_timer(struct hrtimer *t)
{
	struct us48_cmic *u = container_of(t, struct us48_cmic, poll_timer);

	napi_schedule(&u->napi);
	hrtimer_forward_now(t, ns_to_ktime(POLL_NS));
	return HRTIMER_RESTART;
}

/* TX to xe1 with the module header (OM bcm56150_a0_bmd_tx): DCB0 = DA+SA (12 B), DCB1 = rest at +16, HG on both. */
static netdev_tx_t us48_xmit(struct sk_buff *skb, struct net_device *nd)
{
	struct us48_cmic *u = netdev_priv(nd);
	unsigned int len = max_t(unsigned int, skb->len, ETH_ZLEN);

	if (u->tx_len || len + 4 > BUF_SZ) {
		nd->stats.tx_dropped++;
		dev_kfree_skb_any(skb);
		return NETDEV_TX_OK;
	}
	memset(u->tx_buf, 0, len + 4);
	skb_copy_bits(skb, 0, u->tx_buf, 12);
	skb_copy_bits(skb, 12, u->tx_buf + 16, skb->len - 12);
	memset(u->tx_dcb, 0, 128);
	u->tx_dcb[0] = cpu_to_le32(u->tx_buf_dma);
	u->tx_dcb[1] = cpu_to_le32(12 | DCB_W1_CHAIN | DCB_W1_SG | DCB_W1_STAT |
				   (tx_direct ? DCB_W1_HG : 0));
	if (tx_direct) {
		u->tx_dcb[2] = cpu_to_le32(0x81000000);
		u->tx_dcb[3] = cpu_to_le32(READ_ONCE(tx_port));
		u->tx_dcb[4] = cpu_to_le32(0x00040000);	/* txq 0 */
	}
	u->tx_dcb[16] = cpu_to_le32(u->tx_buf_dma + 16);
	u->tx_dcb[17] = cpu_to_le32((len - 12) | DCB_W1_STAT | (tx_direct ? DCB_W1_HG : 0));
	memcpy(&u->tx_dcb[18], &u->tx_dcb[2], 16);	/* OM _dcb_init: header on both DCBs */
	u->tx_len = len;
	netif_stop_queue(nd);
	wmb();
	wr(u, DMA_DESC(TX_CH), u->tx_dcb_dma);
	wr(u, DMA_CTRL(TX_CH), rd(u, DMA_CTRL(TX_CH)) | DMA_EN);
	dev_consume_skb_any(skb);
	return NETDEV_TX_OK;
}

static void us48_tx_timeout(struct net_device *nd, unsigned int txq)
{
	struct us48_cmic *u = netdev_priv(nd);

	netdev_err(nd, "TX stuck: DMA_STAT %08x CTRL %08x dcb1 w15 %08x\n", rd(u, DMA_STAT),
		   rd(u, DMA_CTRL(TX_CH)), le32_to_cpu(u->tx_dcb[31]));
	dma_abort(u, TX_CH);
	nd->stats.tx_errors++;
	u->tx_len = 0;
	netif_wake_queue(nd);
}

static int us48_set_mac(struct net_device *nd, void *p)
{
	struct us48_cmic *u = netdev_priv(nd);
	int ret = eth_mac_addr(nd, p);

	if (!ret && l2_add_cpu(u, nd->dev_addr))
		netdev_warn(nd, "L2 insert for %pM failed\n", nd->dev_addr);
	return ret;
}

static int us48_open(struct net_device *nd)
{
	struct us48_cmic *u = netdev_priv(nd);

	if (l2_add_cpu(u, nd->dev_addr))
		netdev_warn(nd, "L2 insert for %pM failed: unicast to us will not arrive\n", nd->dev_addr);

	rx_arm(u);
	napi_enable(&u->napi);
	hrtimer_start(&u->poll_timer, ns_to_ktime(POLL_NS), HRTIMER_MODE_REL);
	netif_start_queue(nd);
	return 0;
}

static int us48_stop(struct net_device *nd)
{
	struct us48_cmic *u = netdev_priv(nd);

	netif_stop_queue(nd);
	hrtimer_cancel(&u->poll_timer);
	napi_disable(&u->napi);
	dma_abort(u, RX_CH);
	dma_abort(u, TX_CH);
	u->tx_len = 0;
	return 0;
}

static const struct net_device_ops us48_netdev_ops = {
	.ndo_open		= us48_open,
	.ndo_stop		= us48_stop,
	.ndo_start_xmit		= us48_xmit,
	.ndo_tx_timeout		= us48_tx_timeout,
	.ndo_set_mac_address	= us48_set_mac,
	.ndo_validate_addr	= eth_validate_addr,
};

static int us48_cmic_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	struct net_device *nd;
	struct us48_cmic *u;
	u32 link[2];
	int ch, ret;

	if (PCI_FUNC(pdev->devfn) != 0)
		return -ENODEV;
	nd = devm_alloc_etherdev(&pdev->dev, sizeof(*u));
	if (!nd)
		return -ENOMEM;
	u = netdev_priv(nd);
	u->pdev = pdev;
	u->ndev = nd;
	ret = pcim_enable_device(pdev);
	if (ret)
		return ret;
	pci_set_master(pdev);
	ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;
	u->cmic = pcim_iomap(pdev, CMIC_BAR, 0);
	if (!u->cmic)
		return -ENOMEM;
	pci_set_drvdata(pdev, u);

	/* Stock's DCBs live in RAM the kernel has reused: stop both channels before anything else. */
	for (ch = 0; ch < 2; ch++) {
		dev_info(&pdev->dev, "stock left ch%d CTRL %08x DESC %08x\n", ch, rd(u, DMA_CTRL(ch)),
			 rd(u, DMA_DESC(ch)));
		dma_abort(u, ch);
	}
	/* Init (section 5): keep stock's endian bits, ch0 TX / ch1 RX, all CPU COS to ch1. */
	wr(u, DMA_CTRL(TX_CH), (rd(u, DMA_CTRL(TX_CH)) & DMA_ENDIAN) | DMA_DIR_TX);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & DMA_ENDIAN);
	wr(u, COS_CTRL_RX_0(RX_CH), 0xffffffff);
	wr(u, COS_CTRL_RX_1(RX_CH), 0xffff);

	u->rx_dcb = dmam_alloc_coherent(&pdev->dev, RX_RING * 64, &u->rx_dcb_dma, GFP_KERNEL);
	u->rx_buf = dmam_alloc_coherent(&pdev->dev, RX_RING * BUF_SZ, &u->rx_buf_dma, GFP_KERNEL);
	u->tx_dcb = dmam_alloc_coherent(&pdev->dev, 128, &u->tx_dcb_dma, GFP_KERNEL);
	u->tx_buf = dmam_alloc_coherent(&pdev->dev, BUF_SZ, &u->tx_buf_dma, GFP_KERNEL);
	if (!u->rx_dcb || !u->rx_buf || !u->tx_dcb || !u->tx_buf)
		return -ENOMEM;
	/* Endpoint inbound window covers bus < 0x08000000 only (section 14.3). */
	if (u->rx_buf_dma + RX_RING * BUF_SZ > SZ_128M || u->tx_buf_dma + BUF_SZ > SZ_128M ||
	    u->rx_dcb_dma > SZ_128M || u->tx_dcb_dma > SZ_128M) {
		dev_err(&pdev->dev, "DMA memory above 128 MiB (rx %pad tx %pad)\n", &u->rx_buf_dma, &u->tx_buf_dma);
		return -ENOMEM;
	}

	xe1_enable(u);
	if (!sbus(u, 0x0b, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2) &&
	    (link[0] & (BIT(0) | BIT(XE1_PORT))) != (BIT(0) | BIT(XE1_PORT))) {
		link[0] |= BIT(0) | BIT(XE1_PORT);
		sbus(u, 0x0d, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2);
	}
	cpu_path_init(u);
	l2_miss_set(u);

	SET_NETDEV_DEV(nd, &pdev->dev);
	nd->netdev_ops = &us48_netdev_ops;
	nd->watchdog_timeo = HZ;	/* one frame completes in us; 1 s = stuck, logged + aborted */
	nd->min_mtu = ETH_MIN_MTU;
	nd->max_mtu = BUF_SZ - ETH_HLEN - VLAN_HLEN - ETH_FCS_LEN - 4;
	eth_hw_addr_random(nd);
	netif_napi_add(nd, &u->napi, us48_poll);
	hrtimer_setup(&u->poll_timer, us48_poll_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	strscpy(nd->name, "xe%d", IFNAMSIZ);
	/* devm: unregister (ndo_stop aborts both channels) runs before the DMA memory is freed. */
	ret = devm_register_netdev(&pdev->dev, nd);
	if (ret)
		return ret;
	netdev_info(nd, "front 50 (port %d), MAC %pM\n", XE1_PORT, nd->dev_addr);
	return 0;
}

/* Reboot keeps unit 1 out of reset (PERST# held released, patch 952): a live DMA hangs stock U-Boot's PCIe init. */
static void us48_cmic_shutdown(struct pci_dev *pdev)
{
	struct us48_cmic *u = pci_get_drvdata(pdev);

	if (!u)
		return;
	dma_abort(u, RX_CH);
	dma_abort(u, TX_CH);
	pci_clear_master(pdev);
}

static const struct pci_device_id us48_cmic_ids[] = {
	/* Function 1 (01:00.1) shares the ID; probe rejects it, only function 0 carries the CMIC. */
	{ PCI_DEVICE(PCI_VENDOR_ID_BROADCOM, 0x8346) },
	{ }
};
MODULE_DEVICE_TABLE(pci, us48_cmic_ids);

static struct pci_driver us48_cmic_driver = {
	.name		= "us48_cmic",
	.id_table	= us48_cmic_ids,
	.probe		= us48_cmic_probe,
	.shutdown	= us48_cmic_shutdown,
};
module_pci_driver(us48_cmic_driver);

MODULE_DESCRIPTION("UniFi US-48-500W BCM53346 CMIC bring-up (step 3: xe1 netdev, polled DMA)");
MODULE_LICENSE("GPL");
