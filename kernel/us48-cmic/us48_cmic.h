/* SPDX-License-Identifier: GPL-2.0 */
/* Shared by us48_cmic.ko (core + unit 0) and us48_cmic_xe.ko (unit 1 PCI front-end, loadable on its own). */
#ifndef US48_CMIC_H
#define US48_CMIC_H

#include <linux/bits.h>
#include <linux/hrtimer.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/phylink.h>
#include <linux/spinlock.h>
#include <linux/workqueue.h>

#include "bcm_cmic.h"

#define CMIC_BAR		2
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
/* Internal bus 1, clause 45 (INTERNAL_SEL b25, BUS_ID [24:22], C45 b21). TSC0 answers at 1-4, TSC1 (HiGig) at 5-8 (#57). */
#define MIIM_TSC_C45		(BIT(25) | 1 << 22 | BIT(21))
#define TSC0_PHY		2
#define TSC1_PHY		5
/* C45 op = address + data frame, 2 x 26 us at MDC 2.5 MHz (section 13); 1.25x. Expiry logged. */
#define MIIM_TIMEOUT_US		65
/* Abort completes after one in-flight descriptor (tens of us); 10 ms backstop, logged on expiry. */
#define ABORT_TIMEOUT_US	10000
/* No IRQ yet: poll every 1 ms; NAPI drains the ring. */
#define POLL_NS			(1000 * 1000)
/* Copper carrier once a second: a link change needs no faster reaction. xe ports: phylink's 1 s PCS poll. */
#define LINK_POLL_MS		1000

#define TX_CH			0
#define RX_CH			1
#define BUF_SZ			2048
#define RX_RING			256	/* 1 ms poll x 83 frames/ms at 1G line rate = 83 min */
#define TX_RING			64	/* frames in flight; power of 2 (index mask) */
#define SKB_POOL		128	/* preallocated RX skbs, refilled from process context */
#define SKB_POOL_LEN		1600	/* data bytes per pooled skb: fits a 2 KiB slab with headroom + shinfo */
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
#define EGR_ENABLE(phys)	(0x345c0000 + (phys))	/* physical port index (#124) */
#define L2X			0x1c000000	/* hashed: TABLE_INSERT/DELETE; also readable by index */
/* switchdev (us48_cmic_sw.c): bcm_cmic_hr2_memtab.h / regtab.h (generated from regdb) */
#define L2X_ENTRIES		16384
#define L2_USER_ENTRY(i)	(0x1c100000 + (i))
#define L2U_ENTRIES		128
#define L2_AGE_TIMER		0x02000400	/* IPIPE register */

/* One offloaded Linux bridge on one unit = one ASIC VLAN + STG (docs/switchdev.md). */
#define US48_BR_MAX		8
struct us48_br {
	struct net_device *dev;		/* bridge; NULL = free slot */
	u16 vid;			/* ASIC VLAN, also its FID_ID (L2 key) */
	u8 stg;
	u32 members;			/* logical port bitmap */
	struct list_head hosts;		/* bridge-local MACs (struct us48_host) -> CPU */
};

/* Logical port numbers are the chip's p2l view of the physical SerDes: board.md "Physical -> logical". */
struct port_cfg {
	const char *name;
	u8 front, lport, lane;
	u8 phys;			/* physical port: EGR_ENABLE index (#124); lport for everything else */
	bool copper;
	u16 vid;			/* private VLAN id */
	u8 blk, phy;			/* copper: GPORT block and MIIM PHY address */
	bool external_phy;		/* copper PHY is on external MIIM bus 0 */
};

#define MAXPORTS	26		/* unit 1: 24 copper + 2 xe */

struct us48_cmic;
struct netlink_callback;

/* One S-channel op under the unit owner's lock (unit 1: us48_pci_schan, shared with us48_pvtmon, #125). NULL = raw (unit 0). */
typedef int (*us48_schan_fn)(void *ctx, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack);

struct us48_port {
	struct us48_cmic *u;
	struct net_device *ndev;
	const struct port_cfg *cfg;
	u16 vid;			/* private VLAN id (hwdb_gen ports_c: copper 100 + front, xe front) */
	bool open;
	bool registered;
	bool rx_tuned;			/* xe: one RX restart after the first link up (#87) */
	bool copper_ready;		/* copper: MAC/PHY/VLAN bring-up done (first ndo_open) */
	s8 fw_mode;			/* xe: lane FIRMWARE_MODE set (0 optic, 2 DAC); -1 = TSC not initialised */
	struct phylink *pl;		/* xe: SFP cage (us48_cmic_phylink.c), NULL = no DT port node */
	struct phylink_config plc;
	struct phylink_pcs pcs;		/* the TSC lane */
	struct fwnode_handle *moddef0;	/* xe: GPIO controller of the cage's mod-def0 line, NULL = unknown */
	u32 moddef0_line;
	bool moddef0_low;		/* GPIO_ACTIVE_LOW: raw 0 = present */
	u16 speed;			/* copper: Mb/s of the last MAC link up (sysfs link_speed) */
	struct us48_br *br;		/* offloaded bridge (switchdev), NULL = private VLAN; rtnl */
	u8 stp;				/* BR_STATE_* of the port in br */
	bool learning;			/* BR_LEARNING: PORT_TAB CML 8 while in br */
	u16 rx_vid;			/* VLAN tag stripped on RX: vid, or br->vid while bridged */
};

struct us48_cmic {
	struct device *dev;			/* devres owner, log, netdev parent */
	struct device *dma_dev;		/* DMA mask and mappings (the PCI device for unit 1) */
	const struct port_cfg *cfgs;
	int nports;
	u8 modid;			/* PORT_TAB.MY_MODID: unit 0 = 0, unit 1 = 1 */
	void __iomem *cmic;
	struct bcm_cmic_unit cu;		/* every S-channel op of this unit (driver, shared core, schan param) */
	us48_schan_fn schan_fn;
	void *schan_ctx;
	struct mutex schan_seq;		/* one op, or one schan-param read/write/read-back sequence */
	struct us48_port *port[MAXPORTS];
	struct net_device *napi_dev;	/* NAPI needs a netdev; the rings serve all ports */
	struct napi_struct napi;
	struct hrtimer poll_timer;
	struct delayed_work link_work;
	struct mutex mdio;		/* TSC AER lane select + access must not interleave */
	u8 tsc_phy;			/* MIIM address of us48_tsc_rd/wr: 0 = TSC0_PHY; set only while holding mdio */
	struct mutex link_lock;		/* copper bring-up / MAC up-down vs the link poll */
	u8 blk_ready;			/* bit = GPORT block whose block-wide init (+ octal top reset) ran */
	u8 octal_off;			/* bit g = GPORT g octal powered down by pwrsave (us48_cmic_pwr.c); link_lock */
	bool xe_off;			/* unit 0 XLPORT0 + TSC0 powered down by the gate (#143); link_lock */
	bool xe_poll;			/* xe_pwr_work initialised and running */
	struct delayed_work xe_pwr_work;
	spinlock_t tx_lock;
	int nopen;
	bool quiesced;			/* DMA stopped for good: NAPI must not re-arm RX */
	__le32 *rx_dcb;			/* RX_RING x 16 words */
	dma_addr_t rx_dcb_dma;
	void *rx_buf;			/* RX_RING x BUF_SZ */
	dma_addr_t rx_buf_dma;
	int rx_next;
	struct sk_buff_head skb_pool;	/* ready RX skbs; consumed in NAPI, topped up by pool_work */
	struct work_struct pool_work;
	__le32 *tx_dcb;			/* TX_RING x 2 DCBs x 16 words */
	dma_addr_t tx_dcb_dma;
	void *tx_buf;			/* TX_RING x BUF_SZ */
	dma_addr_t tx_buf_dma;
	u32 tx_meta[TX_RING];		/* port index << 16 | frame length, for the stats at reap */
	unsigned int tx_head;		/* next free slot (free-running) */
	unsigned int tx_run;		/* end of the chain the engine runs; tx_tail..tx_run in flight */
	unsigned int tx_tail;		/* oldest unreaped slot */
	struct us48_br br[US48_BR_MAX];	/* switchdev bridges on this unit; rtnl */
	u8 l2_modid;			/* MODULE_ID of own ports in L2X/L2_USER_ENTRY (PORT_TAB.MY_MODID); rtnl */
	int l2u_bpdu;			/* L2_USER_ENTRY index of the BPDU trap, -1 = none */
	bool l2u_ours;			/* we wrote it (removed with the last bridge) */
	struct delayed_work l2mod;	/* L2_MOD_FIFO drain -> bridge FDB (us48_cmic_sw.c), runs while a bridge exists */
	struct us48_l2rec *l2shadow;	/* L2X_ENTRIES: entries reported to the bridge; rtnl */
	u32 *l2buf;			/* one resync chunk of raw L2X entries */
	u32 *l2pop;			/* popped L2_MOD_FIFO entries */
	unsigned long l2resync_at;	/* jiffies: earliest next overflow resync */
	unsigned long l2check_at;	/* jiffies: next consistency walk */
	u32 l2ev, l2full, l2resyncs, l2mismatch, l2fast;	/* since the FIFO went on (logged when it goes off) */
	bool l2resync;			/* FIFO was full or a pop failed: events may be lost; rtnl */
	bool l2mod_on;			/* rtnl */
	struct us48_ledu *leds;		/* LEDUP0 port LEDs (us48_cmic_led.c), NULL = none */
};

/* Second chip (unit 1, PCIe): SFP+ 1/2 plus copper test ports. */
extern const struct port_cfg us48_unit1_cfgs[];
extern const int us48_unit1_nports;

struct firmware;
/* Optional SerDes bring-up (unit 1: TSC microcode), run before the ports are enabled; failure is logged, not fatal. */
typedef int (*us48_serdes_init_t)(struct us48_cmic *u, const struct firmware *fw);

int us48_probe_common(struct device *dev, struct device *dma_dev, void __iomem *cmic, us48_schan_fn schan_fn,
		      void *schan_ctx, const struct port_cfg *cfgs, int nports, u8 modid, const struct firmware *fw,
		      us48_serdes_init_t serdes_init, struct us48_cmic **out);
/* Reset + full cold init of one chip (every load, whatever state it is in), DMA stopped first. unit 0 or 1. */
int us48_chip_cold_init(struct device *dev, void __iomem *cmic, us48_schan_fn schan_fn, void *schan_ctx, int unit);
void us48_mdio_scan(struct us48_cmic *u);
/* Unbind: stop link polling, unregister the netdevs (ndo_stop), stop DMA for good and self-check. */
void us48_remove_common(struct us48_cmic *u);
/* Stop link polling and DMA, park the ports in VLAN 1 (shutdown). */
void us48_quiesce(struct us48_cmic *u);
/* TSC MIIM access (clause 45, AER via 0xffde), caller holds u->mdio. */
int us48_tsc_wr(struct us48_cmic *u, u32 reg, u16 val);
int us48_tsc_rd(struct us48_cmic *u, u32 reg, u16 *val);
int us48_tsc_uc_ram_wr(struct us48_cmic *u, u8 lane, u16 ram, u16 data);
/* TSC stage 0 + microcode + stage 2 for every xe port (us48_tsc_fw.c, linked into us48_cmic_xe.ko). */
int us48_tsc_init(struct us48_cmic *u, const struct firmware *fw);
/* Read-only TSC / XLMAC status of every xe port, logged (write-only 'tsc' param of the front-end). */
void us48_tsc_dump(struct us48_cmic *u);

/* Per-lane RX restart without firmware download (13.3b): signal present but PCS unlocked. */
int us48_tsc_rx_restart(struct us48_cmic *u, const struct port_cfg *c);
/* Lane FIRMWARE_MODE (0 optic/SFI, 2 DAC/CR) inside a uC stop/resume + RX restart; from pcs_config (#105, #21). */
int us48_tsc_fw_mode(struct us48_port *p, u16 mode);
/* xe port on phylink + sfp (us48_cmic_phylink.c): create before register_netdev, destroy after unregister, no rtnl. */
int us48_xe_phylink_create(struct us48_port *p);
void us48_xe_phylink_destroy(struct us48_port *p);

void us48_tsc_range(struct us48_cmic *u, const struct port_cfg *c, u32 lo, u32 hi, int aer);

/* S-channel op on that unit, under its S-channel lock (schan_seq + the owner's lock). */
int us48_sbus(struct us48_cmic *u, u32 op, u32 blk, u32 addr, u32 *data, int words);
/* Unit 0's driver state while us48_cmic_u0 is bound, else NULL. */
struct us48_cmic *us48_unit0(void);
/* Opt-in HiGig link bring-up of logical 28/29 (XLPORT1 + TSC1, 12.73G HiGig2) on one unit; no table writes (#57). */
int us48_higig_up(struct us48_cmic *u, const struct firmware *fw);
void us48_higig_status(struct us48_cmic *u);

bool us48_is_port(const struct net_device *nd);
/* Copper PHY MII_CTRL power (#133). */
int us48_copper_power(struct us48_port *p, bool on);
/* Power save of unused blocks (us48_cmic_pwr.c, #25): restore before the port setup, save after it, octal up on first open. */
void us48_pwr_restore(struct us48_cmic *u);
void us48_pwr_save(struct us48_cmic *u);
int us48_pwr_octal_up(struct us48_port *p);
/* Unit 0 XLPORT0 gate (#143): 300 ms poll off while both cages are empty and both xe ports down; up on open, link_lock held. */
void us48_pwr_xe_start(struct us48_cmic *u);
void us48_pwr_xe_stop(struct us48_cmic *u);
int us48_pwr_xe_up(struct us48_cmic *u);
void us48_xe_port_enable(struct us48_port *p);
/* SFP cage presence from the sfp node's mod-def0 GPIO (read only): 1 present, 0 empty, < 0 unknown. */
int us48_xe_cage_present(struct us48_port *p);
/* Port LEDs (us48_cmic_led.c, docs/leds.md): module init/exit, per unit after register_netdev / before unregister. */
int us48_led_init(void);
void us48_led_exit(void);
void us48_led_unit_init(struct us48_cmic *u);
void us48_led_unit_exit(struct us48_cmic *u);
/* switchdev L2 offload, stage 1 (us48_cmic_sw.c, docs/switchdev.md); rtnl held unless noted. */
#if IS_ENABLED(CONFIG_NET_SWITCHDEV)
int us48_sw_init(void);			/* module init, no rtnl */
void us48_sw_exit(void);		/* module exit, no rtnl */
void us48_sw_unit_init(struct us48_cmic *u);	/* probe, before the netdevs register */
void us48_sw_unit_exit(struct us48_cmic *u);	/* remove, after the netdevs are gone, no rtnl */
void us48_sw_port_reapply(struct us48_port *p);	/* after copper bring-up rewrote PORT_TAB */
int us48_sw_parent_id(struct net_device *nd, struct netdev_phys_item_id *ppid);
int us48_sw_fdb_dump(struct sk_buff *skb, struct netlink_callback *cb, struct net_device *dev,
		     struct net_device *filter_dev, int *idx);
#else
static inline int us48_sw_init(void) { return 0; }
static inline void us48_sw_exit(void) { }
static inline void us48_sw_unit_init(struct us48_cmic *u) { }
static inline void us48_sw_unit_exit(struct us48_cmic *u) { }
static inline void us48_sw_port_reapply(struct us48_port *p) { }
#endif

#endif
