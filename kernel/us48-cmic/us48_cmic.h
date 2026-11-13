/* SPDX-License-Identifier: GPL-2.0 */
/* Shared by us48_cmic.ko (core + unit 0) and us48_cmic_xe.ko (unit 1 PCI front-end, loadable on its own). */
#ifndef US48_CMIC_H
#define US48_CMIC_H

#include <linux/bits.h>
#include <linux/hrtimer.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/spinlock.h>
#include <linux/workqueue.h>

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
#define  DMA_ACTIVE(ch)		BIT(8 + (ch))
#define DMA_DESC(ch)		(0x31158 + 4 * (ch))
#define COS_CTRL_RX_0(ch)	(0x31168 + 8 * (ch))
#define COS_CTRL_RX_1(ch)	(0x3116c + 8 * (ch))
#define DMA_STAT_CLR		0x311a4
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
/* No IRQ yet: poll every 1 ms; NAPI drains the ring. */
#define POLL_NS			(1000 * 1000)
/* Carrier from the TSC PCS once a second: a link change needs no faster reaction. */
#define LINK_POLL_MS		1000

#define TX_CH			0
#define RX_CH			1
#define BUF_SZ			2048
#define RX_RING			256	/* 1 ms poll x 83 frames/ms at 1G line rate = 83 min */
#define DCB_W1_CHAIN		BIT(16)
#define DCB_W1_RELOAD		BIT(18)
#define DCB_W1_SG		BIT(17)
#define DCB_W1_HG		BIT(19)
#define DCB_W1_STAT		BIT(20)
#define DCB_W15_DONE		BIT(31)
#define DCB_W15_ERR		BIT(18)

#define BLK_XLPORT		5
#define BLK_IPIPE		10
#define BLK_EPIPE		11
#define EPC_LINK_BMAP_64	0x46002700
#define PORT_TAB(p)		(0x04000000 + (p))
#define VLAN_TAB(i)		(0x14000000 + (i))
#define STG_TAB(i)		(0x14040000 + (i))
#define EGR_VLAN(i)		(0x100c0000 + (i))
#define EGR_VLAN_STG(i)		(0x10100000 + (i))
#define EGR_ENABLE(p)		(0x345c0000 + (p))
#define L2X			0x1c000000	/* hashed: TABLE_INSERT/DELETE, no index */

/* Logical port numbers are the chip's p2l view of the physical SerDes: board.md "Physical -> logical". */
struct port_cfg {
	const char *name;
	u8 front, lport, lane;
	bool copper;
	u16 vid;			/* private VLAN id */
	u8 blk, phy;			/* copper: GPORT block, integrated GPHY address on internal MIIM bus 0 */
};

#define MAXPORTS	3

struct us48_cmic;

struct us48_port {
	struct us48_cmic *u;
	struct net_device *ndev;
	const struct port_cfg *cfg;
	u16 vid;			/* private VLAN id = front port number */
	bool open;
};

struct us48_cmic {
	struct device *dev;
	const struct port_cfg *cfgs;
	int nports;
	u8 modid;			/* PORT_TAB.MY_MODID: unit 0 = 0, unit 1 = 1 */
	void __iomem *cmic;
	struct us48_port *port[MAXPORTS];
	struct net_device *napi_dev;	/* NAPI needs a netdev; the rings serve all ports */
	struct napi_struct napi;
	struct hrtimer poll_timer;
	struct delayed_work link_work;
	struct mutex mdio;		/* TSC AER lane select + access must not interleave */
	spinlock_t tx_lock;
	int nopen;
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

/* Second chip (unit 1, PCIe): SFP+ 1/2 plus copper test ports. */
extern const struct port_cfg us48_unit1_cfgs[];
extern const int us48_unit1_nports;

int us48_probe_common(struct device *dev, void __iomem *cmic, const struct port_cfg *cfgs, int nports,
		      u8 modid, struct us48_cmic **out);
/* Stop link polling and DMA, park the ports in VLAN 1 (shutdown and unbind). */
void us48_quiesce(struct us48_cmic *u);
/* Read-only TSC / XLMAC status of every xe port, logged (write-only 'tsc' param of the front-end). */
void us48_tsc_dump(struct us48_cmic *u);

/* Per-lane RX restart without firmware download (13.3b): signal present but PCS unlocked. */
int us48_tsc_rx_restart(struct us48_cmic *u, const struct port_cfg *c);

#endif
