// SPDX-License-Identifier: GPL-2.0
/*
 * bcm-cmic HR2 unit-1 port 50 (xe1): chip programming shared by Linux and U-Boot.
 * Ported 1:1 from kernel/us48-cmic/us48_cmic.c; facts in docs/driver-internals.md 1, 5, 13.5, 14.
 */
#include <linux/bitops.h>
#include <linux/delay.h>
#include <linux/errno.h>
#include <linux/io.h>
#include <linux/printk.h>
#include <linux/string.h>
#include <asm/byteorder.h>
#include "bcm_cmic.h"

#define DMA_CTRL(ch)		(0x31140 + 4 * (ch))
#define  DMA_DIR_TX		BIT(0)
#define  DMA_EN			BIT(1)
#define  DMA_ABORT		BIT(2)
#define  DMA_ENDIAN		(BIT(4) | BIT(5))
#define DMA_STAT		0x31150
#define  DMA_ACTIVE(ch)		BIT(8 + (ch))
#define DMA_DESC(ch)		(0x31158 + 4 * (ch))
#define COS_CTRL_RX_0(ch)	(0x31168 + 8 * (ch))
#define COS_CTRL_RX_1(ch)	(0x3116c + 8 * (ch))
#define DMA_STAT_CLR		0x311a4
#define TX_CH			0
#define RX_CH			1
#define DCB_W1_CHAIN		BIT(16)
#define DCB_W1_SG		BIT(17)
#define DCB_W1_RELOAD		BIT(18)
#define DCB_W1_HG		BIT(19)
#define DCB_W1_STAT		BIT(20)
#define DCB_W15_DONE		BIT(31)
#define DCB_W15_ERR		BIT(18)

#define BLK_IPIPE		10
#define BLK_EPIPE		11
#define BLK_XLPORT		5
#define EPC_LINK_BMAP_64	0x46002700
#define PORT_TAB(p)		(0x04000000 + (p))
#define VLAN_TAB(i)		(0x14000000 + (i))
#define STG_TAB(i)		(0x14040000 + (i))
#define EGR_VLAN(i)		(0x100c0000 + (i))
#define EGR_VLAN_STG(i)		(0x10100000 + (i))
#define EGR_ENABLE(p)		(0x345c0000 + (p))
#define L2X			0x1c000000	/* hashed: TABLE_INSERT, no index */
#define UNIT1_MODID		1
#define XE1_PORT		BCM_HR2_XE1_PORT
#define XE1_LANE		BCM_HR2_XE1_LANE	/* TSC0 lane, XLPORT low address byte */
#define OUR_VID			BCM_HR2_PRIVATE_VID
#define BUF_SZ			BCM_HR2_BUF_SZ
#define RX_RING			BCM_HR2_RX_RING
#define ETH_ZLEN		60
#define ETH_FCS_LEN		4
/* Internal bus 1, clause 45 (INTERNAL_SEL b25, BUS_ID [24:22], C45 b21); TSC0 at address 2. */
#define MIIM_TSC0		(BIT(25) | 1 << 22 | BIT(21) | 2 << 16)
/* C45 op = address + data frame, 2 x 26 us at MDC 2.5 MHz; x1.25. Expiry logged. */
#define MIIM_WAIT_US		65
/* Abort completes after one in-flight descriptor (tens of us); 10 ms backstop, logged on expiry. */
#define ABORT_WAIT_US		10000

static u32 rd(struct bcm_cmic_unit *u, u32 off) { return readl(u->base + off); }
static void wr(struct bcm_cmic_unit *u, u32 off, u32 v) { writel(v, u->base + off); }

static int sbus(struct bcm_cmic_unit *u, u32 op, u32 blk, u32 addr, u32 *data, int words)
{
	return bcm_cmic_schan_op(u, op, blk, addr, data, words, NULL);
}

/* Set bits (bit numbers into the entry) and keep EVEN_PARITY at bit par consistent. */
static void smem_set_bits(struct bcm_cmic_unit *u, u32 blk, u32 addr, int words, int par,
			  const int *bits, int n)
{
	u32 e[4];
	int i, flips = 0;

	if (bcm_cmic_mem_read(u, blk, addr, e, words, NULL))
		return;
	for (i = 0; i < n; i++) {
		if (!(e[bits[i] / 32] & BIT(bits[i] % 32)))
			flips++;
		e[bits[i] / 32] |= BIT(bits[i] % 32);
	}
	if (flips & 1)
		e[par / 32] ^= BIT(par % 32);
	if (flips)
		bcm_cmic_mem_write(u, blk, addr, e, words);
}

static void set_even_parity(u32 *e, int par)
{
	int i, ones = 0;

	e[par / 32] &= ~BIT(par % 32);
	for (i = 0; i <= par / 32; i++)
		ones += hweight32(e[i]);
	if (ones & 1)
		e[par / 32] |= BIT(par % 32);
}

static void set_field(u32 *e, int lsb, int width, u64 v)
{
	int i;

	for (i = 0; i < width; i++)
		if (v & BIT_ULL(i))
			e[(lsb + i) / 32] |= BIT((lsb + i) % 32);
}

/*
 * Private VLAN OUR_VID with only the CPU and xe1: VLAN 1 also holds the HiGig links to unit 0.
 * Fields cloned from VLAN 1 (STG, profile, flood pointers), bitmaps replaced.
 */
static void private_vlan_init(struct bcm_cmic_unit *u)
{
	static const int stg[] = { 0, 1, 2 * XE1_PORT, 2 * XE1_PORT + 1 };	/* SP_TREE 3 = forward */
	u32 e[4], g[3], one = 1;

	if (bcm_cmic_mem_read(u, BLK_IPIPE, VLAN_TAB(1), e, 4, NULL) ||
	    bcm_cmic_mem_read(u, BLK_EPIPE, EGR_VLAN(1), g, 3, NULL))
		return;
	e[0] = BIT(0) | BIT(XE1_PORT);			/* PORT_BITMAP 0:30 */
	set_even_parity(e, 126);
	bcm_cmic_mem_write(u, BLK_IPIPE, VLAN_TAB(OUR_VID), e, 4);
	g[0] = BIT(0) | BIT(XE1_PORT) | BIT(30);	/* UT_BITMAP 0:30, PORT_BITMAP 30:30 (low bits) */
	/* PORT_BITMAP bit 30 + 27 = 57 = w1 bit 25; keep VLAN 1's EGR STG (w1 b31:28), else STG 0 = unconfigured */
	g[1] = (g[1] & 0xf0000000) | BIT(XE1_PORT - 2);
	set_even_parity(g, 76);
	bcm_cmic_mem_write(u, BLK_EPIPE, EGR_VLAN(OUR_VID), g, 3);
	smem_set_bits(u, BLK_IPIPE, STG_TAB(1), 2, 60, stg, 4);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN_STG(1), 2, 60, stg, 4);
	/* Stock's port disable leaves EGR_ENABLE[27] = 0: every frame to xe1 dies before the MAC. */
	bcm_cmic_mem_write(u, BLK_EPIPE, EGR_ENABLE(XE1_PORT), &one, 1);
}

/*
 * PORT_TAB[27].PORT_VID (24:12) and, for our VLAN, CML_FLAGS_NEW/MOVE (121:4, 125:4); stock's 8 drops
 * every unicast here. cml < 0 keeps stock.
 */
static void port_vid_set(struct bcm_cmic_unit *u, u32 vid, int cml)
{
	u32 e[8];

	if (bcm_cmic_mem_read(u, BLK_IPIPE, PORT_TAB(XE1_PORT), e, 8, NULL))
		return;
	e[0] = (e[0] & 0x00ffffff) | (vid << 24);
	e[1] = (e[1] & ~0xfu) | (vid >> 8);
	if (cml >= 0 && vid == OUR_VID) {
		e[3] = (e[3] & ~(0xfu << 25 | 0x7u << 29)) | (cml & 0xf) << 25 | (cml & 0x7) << 29;
		e[4] = (e[4] & ~1u) | ((cml >> 3) & 1);
	}
	bcm_cmic_mem_write(u, BLK_IPIPE, PORT_TAB(XE1_PORT), e, 8);
}

void bcm_hr2_port_vid_restore(struct bcm_cmic_unit *u)
{
	port_vid_set(u, 1, -1);
}

static int tsc_wr(struct bcm_cmic_unit *u, u32 reg, u16 val)
{
	u32 st = 0;
	int i;

	wr(u, BCM_CMIC_MIIM_CTRL, 0);
	wr(u, BCM_CMIC_MIIM_PARAM, MIIM_TSC0 | val);
	wr(u, BCM_CMIC_MIIM_ADDR, reg);			/* devad 0 */
	wr(u, BCM_CMIC_MIIM_CTRL, BIT(0));		/* WR_START */
	for (i = 0; i < MIIM_WAIT_US; i++) {
		st = rd(u, BCM_CMIC_MIIM_STAT);
		if (st & BIT(0))
			break;
		udelay(1);
	}
	wr(u, BCM_CMIC_MIIM_CTRL, 0);
	if (!(st & BIT(0))) {
		pr_err("bcm-cmic: tsc write %04x <- %04x: no OPN_DONE in %u us\n", reg, val, MIIM_WAIT_US);
		return -ETIMEDOUT;
	}
	return 0;
}

/* Undo stock's port disable on xe1 (front 50): verified order, driver-internals 13.5. */
static int xe1_enable(struct bcm_cmic_unit *u)
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
	int i, ret;

	/* Only from stock's parked state: re-crediting a running port is not known safe. */
	if (!bcm_cmic_reg_read(u, BLK_XLPORT, 0x60000 | XE1_LANE, v, 2, NULL) && (v[0] & 0x43) == 0x03)
		return 0;
	for (i = 0; i < ARRAY_SIZE(seq); i++) {
		ret = tsc_wr(u, seq[i][0], seq[i][1]);
		if (ret)
			return ret;
	}
	v[0] = 7;
	bcm_cmic_reg_write(u, BLK_XLPORT, 0x20300 | XE1_LANE, v, 1);	/* XLPORT_TXFIFO_CTRL clear counts */
	v[0] = 4;
	bcm_cmic_reg_write(u, BLK_XLPORT, 0x20300 | XE1_LANE, v, 1);
	v[0] = 0x1003; v[1] = 0;				/* XLMAC_CTRL: TX_EN, RX_EN, SOFT_RESET 0 */
	bcm_cmic_reg_write(u, BLK_XLPORT, 0x60000 | XE1_LANE, v, 2);
	v[0] = 7;						/* XLMAC_CLEAR_RX_LSS_STATUS pulse */
	bcm_cmic_reg_write(u, BLK_XLPORT, 0x60c00 | XE1_LANE, v, 2);
	v[0] = 0;
	bcm_cmic_reg_write(u, BLK_XLPORT, 0x60c00 | XE1_LANE, v, 2);
	return 0;
}

int bcm_hr2_xe1_bringup(struct bcm_cmic_unit *u, int cml)
{
	u32 link[2];
	int ret = xe1_enable(u);

	if (ret)
		return ret;
	if (!bcm_cmic_reg_read(u, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2, NULL) &&
	    (link[0] & (BIT(0) | BIT(XE1_PORT))) != (BIT(0) | BIT(XE1_PORT))) {
		link[0] |= BIT(0) | BIT(XE1_PORT);
		bcm_cmic_reg_write(u, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2);
	}
	private_vlan_init(u);
	port_vid_set(u, OUR_VID, cml);
	return 0;
}

/* Static L2X entry: mac in the private VLAN -> CPU (modid 1, port 0). TX bypasses learning. */
int bcm_hr2_l2_cpu(struct bcm_cmic_unit *u, const u8 *mac, bool add)
{
	u32 e[4] = { };
	u64 m = 0;
	int i, ones = 0;

	for (i = 0; i < 6; i++)
		m = m << 8 | mac[i];
	set_field(e, 2, 12, OUR_VID);			/* VLAN_ID */
	set_field(e, 14, 48, m);			/* MAC_ADDR */
	set_field(e, 62, 6, 0);				/* PORT_NUM: CPU */
	set_field(e, 68, 7, UNIT1_MODID);		/* MODULE_ID */
	set_field(e, 91, 1, 1);				/* CPU: copy to CPU */
	set_field(e, 97, 1, 1);				/* STATIC_BIT */
	set_field(e, 99, 1, 1);				/* VALID */
	for (i = 0; i < 4; i++)
		ones += hweight32(e[i]);
	set_field(e, 100, 1, ones & 1);			/* EVEN_PARITY */
	return sbus(u, add ? BCM_SCHAN_TABLE_INSERT : BCM_SCHAN_TABLE_DELETE, BLK_IPIPE, L2X, e, 4);
}

void bcm_hr2_dma_abort(struct bcm_cmic_unit *u, int ch)
{
	u32 ctrl = rd(u, DMA_CTRL(ch)), stat = 0;
	int i;

	if (rd(u, DMA_STAT) & DMA_ACTIVE(ch)) {
		wr(u, DMA_CTRL(ch), ctrl | DMA_EN | DMA_ABORT);
		for (i = 0; i < ABORT_WAIT_US; i++) {
			stat = rd(u, DMA_STAT);
			if (!(stat & DMA_ACTIVE(ch)))
				break;
			udelay(1);
		}
		if (stat & DMA_ACTIVE(ch))
			pr_err("bcm-cmic: ch%d abort: still active after %u us, DMA_STAT %08x\n",
			       ch, ABORT_WAIT_US, stat);
	}
	wr(u, DMA_CTRL(ch), rd(u, DMA_CTRL(ch)) & ~(DMA_EN | DMA_ABORT));
	wr(u, DMA_STAT_CLR, BIT(ch));
}

void bcm_hr2_dma_init(struct bcm_cmic_unit *u)
{
	/* Stock's DCBs live in RAM that may be reused: stop both channels before anything else. */
	bcm_hr2_dma_abort(u, RX_CH);
	bcm_hr2_dma_abort(u, TX_CH);
	/* Keep stock's endian bits. */
	wr(u, DMA_CTRL(TX_CH), (rd(u, DMA_CTRL(TX_CH)) & DMA_ENDIAN) | DMA_DIR_TX);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & DMA_ENDIAN);
	wr(u, COS_CTRL_RX_0(RX_CH), 0xffffffff);
	wr(u, COS_CTRL_RX_1(RX_CH), 0xffff);
}

/* Ring: RX_RING data DCBs (CHAIN) + one RELOAD DCB pointing back at DCB 0: the engine never stops (docs/driver-internals.md 4). */
void bcm_hr2_rx_arm(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d)
{
	int i;

	memset(d->rx_dcb, 0, (RX_RING + 1) * BCM_HR2_DCB_BYTES);
	for (i = 0; i < RX_RING; i++) {
		d->rx_dcb[16 * i] = cpu_to_le32(d->rx_buf_bus + i * BUF_SZ);
		d->rx_dcb[16 * i + 1] = cpu_to_le32(BUF_SZ | DCB_W1_CHAIN);
	}
	d->rx_dcb[16 * RX_RING] = cpu_to_le32(d->rx_dcb_bus);
	d->rx_dcb[16 * RX_RING + 1] = cpu_to_le32(DCB_W1_CHAIN | DCB_W1_RELOAD);
	d->rx_next = 0;
	wmb();
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & ~DMA_EN);
	wr(u, DMA_STAT_CLR, BIT(RX_CH));
	wr(u, DMA_DESC(RX_CH), d->rx_dcb_bus);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) | DMA_EN);
}

void bcm_hr2_rx_done(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d)
{
	/* Hand the slot back: clear its status; the engine reaches it again after RX_RING-1 other frames. */
	d->rx_dcb[16 * d->rx_next + 15] = 0;
	wmb();
	d->rx_next = (d->rx_next + 1) % RX_RING;
}

int bcm_hr2_rx_next(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d, u8 **pkt)
{
	{
		int i = d->rx_next;
		u32 w15 = le32_to_cpu(*(volatile __le32 *)&d->rx_dcb[16 * i + 15]);
		u8 *p = d->rx_buf + i * BUF_SZ;
		unsigned int len = w15 & 0xffff;

		if (!(w15 & DCB_W15_DONE))
			return 0;
		rmb();
		if ((w15 & DCB_W15_ERR) || len < ETH_ZLEN + ETH_FCS_LEN || len > BUF_SZ) {
			bcm_hr2_rx_done(u, d);
			return bcm_hr2_rx_next(u, d, pkt);
		}
		len -= ETH_FCS_LEN;
		/* CPU copy may carry our VLAN tag: drop it, keeping DA+SA. */
		if (p[12] == 0x81 && p[13] == 0x00 && (((p[14] << 8) | p[15]) & 0xfff) == OUR_VID) {
			memmove(p + 4, p, 12);
			p += 4;
			len -= 4;
		}
		*pkt = p;
		return len;
	}
	return 0;
}

/* TX to xe1 with the module header: DCB0 = DA+SA (12 B), DCB1 = rest at +16, HG on both. */
int bcm_hr2_tx_start(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d, const u8 *frame, unsigned int len)
{
	unsigned int n = len < ETH_ZLEN ? ETH_ZLEN : len;

	if (d->tx_len)
		return -EBUSY;
	if (len < 12 || n + 16 > BUF_SZ)
		return -EMSGSIZE;
	memset(d->tx_buf, 0, n + 16);		/* DCB1 reads buf[16, n + 8) */
	memcpy(d->tx_buf, frame, 12);
	memcpy(d->tx_buf + 16, frame + 12, len - 12);
	memset(d->tx_dcb, 0, 128);
	d->tx_dcb[0] = cpu_to_le32(d->tx_buf_bus);
	d->tx_dcb[1] = cpu_to_le32(12 | DCB_W1_CHAIN | DCB_W1_SG | DCB_W1_STAT | DCB_W1_HG);
	d->tx_dcb[2] = cpu_to_le32(0x81000000);
	d->tx_dcb[3] = cpu_to_le32(XE1_PORT);
	d->tx_dcb[4] = cpu_to_le32(0x00040000);	/* txq 0 */
	d->tx_dcb[16] = cpu_to_le32(d->tx_buf_bus + 16);
	/* +4: XLMAC CRC mode is 'replace': the last 4 bytes are overwritten with the FCS, not added. */
	d->tx_dcb[17] = cpu_to_le32((n + ETH_FCS_LEN - 12) | DCB_W1_STAT | DCB_W1_HG);
	memcpy(&d->tx_dcb[18], &d->tx_dcb[2], 16);	/* header on both DCBs */
	d->tx_len = n;
	wmb();
	wr(u, DMA_DESC(TX_CH), d->tx_dcb_bus);
	wr(u, DMA_CTRL(TX_CH), rd(u, DMA_CTRL(TX_CH)) | DMA_EN);
	return 0;
}

bool bcm_hr2_tx_reap(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d)
{
	if (!d->tx_len)
		return true;
	if (!(le32_to_cpu(*(volatile __le32 *)&d->tx_dcb[16 + 15]) & DCB_W15_DONE))
		return false;
	wr(u, DMA_CTRL(TX_CH), rd(u, DMA_CTRL(TX_CH)) & ~DMA_EN);
	wr(u, DMA_STAT_CLR, BIT(TX_CH));
	d->tx_len = 0;
	return true;
}
