// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic: the two 10G uplinks of the UniFi US-48-500W (front 49 = xe49 = logical port 26,
 * front 50 = xe50 = logical port 27) on the BCM53346 (unit 1) behind PCIe.
 * One CMIC DMA ring pair serves both ports: RX is demuxed on the source port, TX goes straight to a
 * port through the module header. Each port sits in its own private VLAN with the CPU only, so the
 * chip never forwards between them or into the HiGig links. Polled. docs/driver-internals.md 1, 5, 13.5, 14.
 */
#include <linux/delay.h>
#include <linux/etherdevice.h>
#include <linux/hrtimer.h>
#include <linux/dma-mapping.h>
#include <linux/if_vlan.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/pci.h>
#include <linux/platform_device.h>
#include <linux/spinlock.h>
#include <linux/unaligned.h>
#include <linux/workqueue.h>

#include "us48_cmic.h"
#include "bcm_cmic.h"

const struct port_cfg us48_unit1_cfgs[] = {
	{ .name = "xe49", .vid = 49, .front = 49, .lport = 26, .lane = 0 },
	{ .name = "xe50", .vid = 50, .front = 50, .lport = 27, .lane = 2 },
	/* Front 40 = logical 17, GPORT1 (block 3) lane 7, GPHY addr 19 (hardware.md, stock S-channel trace #31). */
	{ .name = "ge40", .vid = 40, .front = 40, .lport = 17, .lane = 7, .copper = true, .blk = 3, .phy = 19 },
};
/* Unit 0 (host SoC, AXI CMIC): front 1 = logical 2, GPORT0 (block 2) lane 7, GPHY addr 9; VLAN 101 (VLAN 1 = stock's). */
static const struct port_cfg unit0_cfgs[] = {
	{ .name = "ge1", .vid = 101, .front = 1, .lport = 2, .lane = 7, .copper = true, .blk = 2, .phy = 9 },
};
const int us48_unit1_nports = ARRAY_SIZE(us48_unit1_cfgs);
EXPORT_SYMBOL_GPL(us48_unit1_cfgs);
EXPORT_SYMBOL_GPL(us48_unit1_nports);

/* Log the module header + status words of the next N received frames (RX bring-up, #4). */
static uint rx_dump;
module_param(rx_dump, uint, 0644);

static u32 rd(struct us48_cmic *u, u32 off) { return ioread32(u->cmic + off); }
static void wr(struct us48_cmic *u, u32 off, u32 v) { iowrite32(v, u->cmic + off); }

/* SBUS op: reg read 0x0b / write 0x0d, mem read 0x07 / write 0x09, insert 0x24, delete 0x26. Section 1. */
static int sbus(struct us48_cmic *u, u32 op, u32 blk, u32 addr, u32 *data, int words)
{
	bool write = op == 0x0d || op == 0x09 || op == 0x24 || op == 0x26;
	ktime_t t0 = ktime_get();
	u32 ctrl;
	int i, ret;

	wr(u, SCHAN_CTRL, 0);
	wr(u, SCHAN_MSG(0), op << 26 | blk << 20 | (4 * words) << 7);
	wr(u, SCHAN_MSG(1), addr);
	for (i = 0; write && i < words; i++)
		wr(u, SCHAN_MSG(2 + i), data[i]);
	wr(u, SCHAN_CTRL, SCHAN_START);
	/* DONE only: an error bit alone can be stale while the op is still in flight (seen as ctrl 00200001). */
	ret = readl_poll_timeout_atomic(u->cmic + SCHAN_CTRL, ctrl, ctrl & SCHAN_DONE, 1, SCHAN_TIMEOUT_US);
	if (ret || (ctrl & SCHAN_ERRS)) {
		dev_err(u->dev, "sbus op %02x blk %u addr %08x: ctrl %08x after %lld us (limit %u)\n",
			op, blk, addr, ctrl, ktime_us_delta(ktime_get(), t0), SCHAN_TIMEOUT_US);
		wr(u, SCHAN_CTRL, 0);
		return ret ?: -EIO;
	}
	for (i = 0; !write && i < words; i++)
		data[i] = rd(u, SCHAN_MSG(1 + i));
	wr(u, SCHAN_CTRL, 0);
	return 0;
}

static void ebit(u32 *e, int n) { e[n / 32] |= BIT(n % 32); }

static void set_even_parity(u32 *e, int par)
{
	int i, ones = 0;

	e[par / 32] &= ~BIT(par % 32);
	for (i = 0; i <= par / 32; i++)
		ones += hweight32(e[i]);
	if (ones & 1)
		e[par / 32] |= BIT(par % 32);
}

/* Set bits (bit numbers into the entry) and keep EVEN_PARITY at bit par consistent. */
static void smem_set_bits(struct us48_cmic *u, u32 blk, u32 addr, int words, int par, const int *bits, int n)
{
	u32 e[4];
	int i;

	if (sbus(u, 0x07, blk, addr, e, words))
		return;
	for (i = 0; i < n; i++)
		ebit(e, bits[i]);
	set_even_parity(e, par);
	sbus(u, 0x09, blk, addr, e, words);
}

/* ---- TSC SerDes over MIIM (clause 45, devad 0) ---- */

static int tsc_wr(struct us48_cmic *u, u32 reg, u16 val)
{
	u32 st;
	int ret;

	wr(u, MIIM_CTRL, 0);
	wr(u, MIIM_PARAM, MIIM_TSC0 | val);
	wr(u, MIIM_ADDRESS, reg);
	wr(u, MIIM_CTRL, BIT(0));			/* WR_START */
	ret = readl_poll_timeout_atomic(u->cmic + MIIM_STAT, st, st & BIT(0), 1, MIIM_TIMEOUT_US);
	wr(u, MIIM_CTRL, 0);
	if (ret)
		dev_err(u->dev, "tsc write %04x <- %04x: no OPN_DONE in %u us\n", reg, val, MIIM_TIMEOUT_US);
	return ret;
}

static int tsc_rd(struct us48_cmic *u, u32 reg, u16 *val)
{
	u32 st;
	int ret;

	wr(u, MIIM_CTRL, 0);
	wr(u, MIIM_PARAM, MIIM_TSC0);
	wr(u, MIIM_ADDRESS, reg);
	wr(u, MIIM_CTRL, BIT(1));			/* RD_START */
	ret = readl_poll_timeout_atomic(u->cmic + MIIM_STAT, st, st & BIT(0), 1, MIIM_TIMEOUT_US);
	if (!ret)
		*val = rd(u, MIIM_READ_DATA) & 0xffff;
	wr(u, MIIM_CTRL, 0);
	return ret;
}

static uint phy_dbg;
module_param(phy_dbg, uint, 0644);

/* Log the read-only TSC / XLMAC status of every xe port at probe (cheap: ~30 MIIM + 8 SBUS reads per port). */
static uint tsc_log = 1;
module_param(tsc_log, uint, 0644);

struct tsc_reg {
	const char *n;
	u16 reg;
};
/* Core registers, AER 0 (driver-internals 13.2 checks 1-2). */
static const struct tsc_reg tsc_core[] = {
	{ "SERDESID", 0x900e }, { "ANAPLL", 0x9010 }, { "VERSION", 0xf010 }, { "CRC", 0xf015 },
	{ "DLSTAT", 0xffc5 }, { "UCCMD", 0xffc2 },
};
/* Per-lane registers, AER = lane (checks 3-8). PMD_LATCH / PCS_LATCH are latched: this read clears them. */
static const struct tsc_reg tsc_lane[] = {
	{ "UC_CTRL", 0xc20e }, { "PMD_LIVE", 0xc150 }, { "PMD_LATCH", 0xc151 }, { "ANARX1", 0xc02b },
	{ "SIGDET", 0xc138 }, { "PMACTL0", 0xc137 }, { "PCS_LIVE", 0xc154 }, { "PCS_LATCH0", 0xc153 },
	{ "CL49FSM", 0xc145 }, { "ANATX_DRV", 0xc017 }, { "ANATX6", 0xc01a }, { "TXMISC", 0xc113 },
	{ "CRED0", 0xc100 }, { "CRED1", 0xc101 }, { "TXPCS", 0xc121 },
};

static void tsc_dump_one(struct us48_cmic *u, const struct port_cfg *c)
{
	char b[200];
	u16 v;
	u32 d[2];
	int i, n = 0;

	mutex_lock(&u->mdio);
	if (tsc_wr(u, 0xffde, 0))
		goto out;
	for (i = 0; i < ARRAY_SIZE(tsc_core); i++)
		n += scnprintf(b + n, sizeof(b) - n, " %s=%04x", tsc_core[i].n, tsc_rd(u, tsc_core[i].reg, &v) ? 0xffff : v);
	dev_info(u->dev, "tsc %s core:%s\n", c->name, b);
	if (tsc_wr(u, 0xffde, c->lane))
		goto out;
	n = 0;
	for (i = 0; i < ARRAY_SIZE(tsc_lane); i++) {
		n += scnprintf(b + n, sizeof(b) - n, " %s=%04x", tsc_lane[i].n, tsc_rd(u, tsc_lane[i].reg, &v) ? 0xffff : v);
		if (i == 6 || i == 11) {
			dev_info(u->dev, "tsc %s lane %d:%s\n", c->name, c->lane, b);
			n = 0;
		}
	}
	dev_info(u->dev, "tsc %s lane %d:%s\n", c->name, c->lane, b);
out:
	mutex_unlock(&u->mdio);
	/* XLMAC / XLPORT, block 5, lane in the low address byte. */
	n = 0;
	for (i = 0; i < 4; i++) {
		static const struct { const char *n; u32 a; int w; } m[] = {
			{ "XLMAC_CTRL", 0x60000, 2 }, { "RX_LSS", 0x60b00, 2 }, { "MODE", 0x60100, 2 }, { "TX_CTRL", 0x60400, 2 },
		};

		d[0] = d[1] = 0;
		n += scnprintf(b + n, sizeof(b) - n, " %s=%08x", m[i].n,
			       sbus(u, 0x0b, BLK_XLPORT, m[i].a | c->lane, d, m[i].w) ? 0xffffffff : d[0]);
	}
	d[0] = 0;
	n += scnprintf(b + n, sizeof(b) - n, " ENABLE=%x", sbus(u, 0x0b, BLK_XLPORT, 0x2020b00, d, 1) ? 0xf : d[0]);
	d[0] = 0;
	n += scnprintf(b + n, sizeof(b) - n, " FAULT=%x", sbus(u, 0x0b, BLK_XLPORT, 0x20600 | c->lane, d, 1) ? 0xf : d[0]);
	dev_info(u->dev, "tsc %s mac:%s\n", c->name, b);
	n = 0;
	for (i = 0; i < 3; i++) {
		static const struct { const char *n; u32 a; } m[] = { { "RPKT", 0xb00 }, { "TPKT", 0x4c00 }, { "RFCS", 0xf00 } };

		d[0] = d[1] = 0;
		n += scnprintf(b + n, sizeof(b) - n, " %s=%u", m[i].n,
			       sbus(u, 0x0b, BLK_XLPORT, m[i].a | c->lane, d, 2) ? 0 : d[0]);
	}
	dev_info(u->dev, "tsc %s mib:%s\n", c->name, b);
}

void us48_tsc_dump(struct us48_cmic *u)
{
	int i;

	for (i = 0; i < u->nports; i++)
		if (!u->cfgs[i].copper)
			tsc_dump_one(u, &u->cfgs[i]);
}
EXPORT_SYMBOL_GPL(us48_tsc_dump);

/* Log TSC registers lo..hi (AER = lane) of one port: lane A/B comparison (#46). */
void us48_tsc_range(struct us48_cmic *u, const struct port_cfg *c, u16 lo, u16 hi)
{
	char b[160];
	u16 v, r;
	int n = 0;

	mutex_lock(&u->mdio);
	tsc_wr(u, 0xffde, c->lane);
	for (r = lo; r <= hi; r++) {
		n += scnprintf(b + n, sizeof(b) - n, " %04x=%04x", r, tsc_rd(u, r, &v) ? 0xffff : v);
		if (n > 100 || r == hi) {
			dev_info(u->dev, "tsc %s range:%s\n", c->name, b);
			n = 0;
		}
	}
	mutex_unlock(&u->mdio);
}
EXPORT_SYMBOL_GPL(us48_tsc_range);

/* RMW of a lane register: AER first (it is sticky per PHY address). Caller holds u->mdio. */
static int tsc_rmw(struct us48_cmic *u, u8 lane, u16 reg, u16 clr, u16 set)
{
	u16 v;

	if (tsc_wr(u, 0xffde, lane) || tsc_rd(u, reg, &v))
		return -EIO;
	return tsc_wr(u, reg, (v & ~clr) | set);
}

/* uC mailbox proxy write of one RAM word (OM phy_tsc_iblk_proxy_write, driver-internals 13.3b). Caller holds u->mdio. */
static int tsc_uc_ram_wr(struct us48_cmic *u, u8 lane, u16 ram, u16 data)
{
	ktime_t t0 = ktime_get();
	u16 v = 0;

	if (tsc_wr(u, 0xffde, 0) || tsc_wr(u, 0xffc8, 0) || tsc_wr(u, 0xffc0, ram) || tsc_wr(u, 0xffc1, lane) ||
	    tsc_wr(u, 0xffc3, data) || tsc_wr(u, 0xffc8, 0x0019))	/* REQ | WR | RAM */
		return -EIO;
	/* uC services a mailbox request in its main loop (not measured): 10 ms first-run bound, elapsed logged. */
	while (ktime_us_delta(ktime_get(), t0) < 10000) {
		if (tsc_rd(u, 0xffc8, &v))
			return -EIO;
		if (v & BIT(15))
			break;
		usleep_range(100, 200);
	}
	dev_info(u->dev, "tsc uC mailbox ram %04x <- %04x lane %d: %s after %lld us (mailbox %04x)\n", ram, data, lane,
		 (v & BIT(15)) ? "done" : "TIMEOUT", ktime_us_delta(ktime_get(), t0), v);
	tsc_wr(u, 0xffc8, 0);
	return (v & BIT(15)) ? 0 : -ETIMEDOUT;
}

/*
 * Per-lane RX restart without a firmware download (driver-internals 13.3b, from OM _tsc_serdes_stop /
 * _tsc_uc_rx_lane_control; not verified on hardware): use when signal is present (PMD_LIVE b1) but the PCS never locks.
 */
int us48_tsc_rx_restart(struct us48_cmic *u, const struct port_cfg *c)
{
	u16 ram = 0x04d0 + 0x100 * c->lane, pmd = 0, pcs = 0;
	ktime_t t0;
	int ret;

	mutex_lock(&u->mdio);
	ret = tsc_rmw(u, c->lane, 0xc138, BIT(2) | BIT(1), 0);
	ret = ret ?: tsc_uc_ram_wr(u, c->lane, ram, 0x0d00);		/* uC lane stop */
	ret = ret ?: tsc_rmw(u, c->lane, 0xc137, BIT(0), 0);		/* RSTB_LANE = 0 */
	ret = ret ?: tsc_rmw(u, c->lane, 0xc21e, 0, BIT(15));		/* RXSEQSTART */
	ret = ret ?: tsc_uc_ram_wr(u, c->lane, ram, 0x0e00);		/* uC lane start (forced SFI, non-AN) */
	if (ret) {
		mutex_unlock(&u->mdio);
		dev_err(u->dev, "%s: rx restart failed: %d\n", c->name, ret);
		return ret;
	}
	/* RX adaptation has no OM bound: 1 s first-run backstop, first-lock time logged for tightening. */
	t0 = ktime_get();
	do {
		tsc_wr(u, 0xffde, c->lane);
		if (!tsc_rd(u, 0xc150, &pmd) && !tsc_rd(u, 0xc154, &pcs) && (pmd & BIT(3)) && (pcs & BIT(1)))
			break;
		mutex_unlock(&u->mdio);
		msleep(10);
		mutex_lock(&u->mdio);
	} while (ktime_ms_delta(ktime_get(), t0) < 1000);
	if (!(pmd & BIT(3)))
		tsc_rmw(u, c->lane, 0xc137, 0, BIT(0));			/* RSTB_LANE by host if the uC did not */
	mutex_unlock(&u->mdio);
	dev_info(u->dev, "%s: rx restart: PMD_LIVE %04x PCS_LIVE %04x after %lld ms\n", c->name, pmd, pcs,
		 ktime_ms_delta(ktime_get(), t0));
	return 0;
}
EXPORT_SYMBOL_GPL(us48_tsc_rx_restart);

/* ---- integrated GPHY: clause 22 on internal bus 0 ---- */

static int gphy_rd(struct us48_cmic *u, u8 phy, u8 reg, u16 *val)
{
	u32 st;
	int ret;

	wr(u, MIIM_CTRL, 0);
	wr(u, MIIM_PARAM, BIT(25) | phy << 16);
	wr(u, MIIM_ADDRESS, reg);
	wr(u, MIIM_CTRL, BIT(1));
	ret = readl_poll_timeout_atomic(u->cmic + MIIM_STAT, st, st & BIT(0), 1, MIIM_TIMEOUT_US);
	if (!ret)
		*val = rd(u, MIIM_READ_DATA) & 0xffff;
	wr(u, MIIM_CTRL, 0);
	return ret;
}

static struct us48_cmic *dbg_u;
static const struct port_cfg *dbg_cfg;

/* echo 1 > /sys/module/us48_cmic/parameters/mib: log the GPORT RX counters of the first copper port (#56). */
static int mib_set(const char *val, const struct kernel_param *kp)
{
	static const struct { const char *n; u32 a; } r[] = {
		{ "GRPKT", 0x1b00 }, { "GRBYT", 0x1c00 }, { "GRFCS", 0x0 }, { "GRUND", 0xc00 },
		{ "GROVR", 0x800 }, { "GRFRG", 0xd00 }, { "GRXPF", 0x200 }, { "GTPKT", 0x3800 },
	};
	u32 v[2];
	int i;

	if (!dbg_u || !dbg_cfg)
		return -ENODEV;
	for (i = 0; i < ARRAY_SIZE(r); i++) {
		v[0] = v[1] = 0;
		if (sbus(dbg_u, 0x0b, dbg_cfg->blk, r[i].a | dbg_cfg->lane, v, 2))
			dev_info(dbg_u->dev, "mib %s: read failed\n", r[i].n);
		else
			dev_info(dbg_u->dev, "mib %s = %u (hi %u)\n", r[i].n, v[0], v[1]);
	}
	/* CPU port (MMU port 0): addr = base + (cos << 8) + port, validated vs stock trace by scripts/mmu_addr.py (#56) */
	for (i = 0; i < 8; i++) {
		u32 d[2] = { 0, 0 }, h[2] = { 0, 0 }, c[2] = { 0, 0 };

		sbus(dbg_u, 0x0b, 0xc, 0x101000 + (i << 8), d, 1);
		sbus(dbg_u, 0x0b, 0xc, 0x800 + (i << 8), h, 1);
		sbus(dbg_u, 0x0b, 0xc, 0xed00 + (i << 8), c, 1);
		dev_info(dbg_u->dev, "mib cpu cos%d: EGRDROPPKT %u HOLPKTSETLIMIT %u CELLMAXLIMIT %08x\n", i, d[0],
			 h[0] & 0x7ff, c[0]);
	}
	return 0;
}

static const struct kernel_param_ops mib_ops = { .set = mib_set };
module_param_cb(mib, &mib_ops, NULL, 0200);

static struct bcm_hr2_ge ge_of(const struct us48_port *p)
{
	return (struct bcm_hr2_ge){ .blk = p->cfg->blk, .lane = p->cfg->lane, .phy = p->cfg->phy, .lport = p->cfg->lport,
				    .modid = p->u->modid, .vid = p->vid };
}

/* Shared core (common/bcm-cmic): stock's per-lane copper MAC init (docs/driver-internals.md 12), GPHY AN, private VLAN, PORT_TAB, link bitmap. */
static void copper_enable(struct us48_port *p)
{
	struct us48_cmic *u = p->u;
	struct bcm_cmic_unit cu = { .base = u->cmic };
	struct bcm_hr2_ge ge = ge_of(p);
	int ret;

	mutex_lock(&u->mdio);
	ret = bcm_hr2_ge_bringup(&cu, &ge);
	mutex_unlock(&u->mdio);
	if (p->cfg->front == 1) {
		dbg_u = u;
		dbg_cfg = p->cfg;
	}
	dev_info(u->dev, "%s: copper bring-up %s (block %d lane %d)\n", p->cfg->name, ret ? "FAILED" : "done",
		 p->cfg->blk, p->cfg->lane);
}

/* Link up at 100M/1G full duplex: stock's CC sequence (reset pulse, speed, RX|TX on). Speed from the PHY. */
static void copper_mac_set(struct us48_port *p, int speed)
{
	struct bcm_cmic_unit cu = { .base = p->u->cmic };
	struct bcm_hr2_ge ge = ge_of(p);

	bcm_hr2_ge_mac_up(&cu, &ge, speed);
}

/* BMSR link (latched low: read twice) and negotiated speed from the 1000BASE-T / LPA registers. */
static bool copper_link(struct us48_port *p, int *speed)
{
	struct us48_cmic *u = p->u;
	struct bcm_cmic_unit cu = { .base = u->cmic };
	struct bcm_hr2_ge ge = ge_of(p);
	bool up;

	mutex_lock(&u->mdio);
	up = bcm_hr2_ge_link(&cu, &ge, speed);
	if (phy_dbg) {
		u16 bmcr = 0, aux = 0;

		gphy_rd(u, p->cfg->phy, 0, &bmcr);
		gphy_rd(u, p->cfg->phy, 0x19, &aux);
		netdev_info(p->ndev, "phy %d: BMCR %04x AUX19 %04x speed %d\n", p->cfg->phy, bmcr, aux, *speed);
	}
	mutex_unlock(&u->mdio);
	return up;
}

/* PCS live status (RX_X4_PCS_LIVE_STATUS 0xc154): bit0 sync, bit1 link. */
static void link_work_fn(struct work_struct *w)
{
	struct us48_cmic *u = container_of(to_delayed_work(w), struct us48_cmic, link_work);
	int i;

	for (i = 0; i < u->nports; i++) {
		struct us48_port *p = u->port[i];
		u16 v = 0;
		bool up;
		int speed = 0;

		if (p->cfg->copper) {
			up = copper_link(p, &speed);
			if (up && !netif_carrier_ok(p->ndev))
				copper_mac_set(p, speed);
			else if (!up && netif_carrier_ok(p->ndev)) {
				struct bcm_cmic_unit cu = { .base = u->cmic };
				struct bcm_hr2_ge ge = ge_of(p);

				bcm_hr2_ge_mac_down(&cu, &ge);
			}
		} else {
			mutex_lock(&u->mdio);
			up = !tsc_wr(u, 0xffde, p->cfg->lane) && !tsc_rd(u, 0xc154, &v) && (v & BIT(1));
			mutex_unlock(&u->mdio);
		}
		if (up != netif_carrier_ok(p->ndev)) {
			if (up)
				netif_carrier_on(p->ndev);
			else
				netif_carrier_off(p->ndev);
			netdev_info(p->ndev, "link %s%s\n", up ? "up" : "down", up && p->cfg->copper ? (speed == 1000 ? " 1000M" : speed == 100 ? " 100M" : " 10M") : "");
		}
	}
	schedule_delayed_work(&u->link_work, msecs_to_jiffies(LINK_POLL_MS));
}

/* Undo stock's port disable on one xe lane: verified order, driver-internals 13.5. */
static void port_enable(struct us48_port *p)
{
	static const u16 seq[][2] = {
		{ 0xc138, 0x0009 },			/* RX_SIGDET: drop the forced signal-detect bits */
		{ 0xc101, 0x0084 }, { 0xc100, 0x4021 },	/* TX credits, CREDITENABLE */
		{ 0xc017, 0x2020 },			/* ANATX_DRIVER: clear ELEC_IDLE (stock's xe value) */
		{ 0xc01a, 0x0000 },			/* ANATX_CONTROL6: clear TX disable */
		{ 0xc113, 0xc1c8 }, { 0xc113, 0xc1cb },	/* TX lane reset pulse, after credits */
		{ 0xc137, 0x0001 },			/* RX lane reset release */
	};
	struct us48_cmic *u = p->u;
	u32 a = p->cfg->lane, v[2];
	int i, ret = 0;

	if (p->cfg->copper) {
		copper_enable(p);
		return;
	}
	/* Only from stock's parked state: re-crediting a running port is not known safe. */
	if (!sbus(u, 0x0b, BLK_XLPORT, 0x60000 | a, v, 2) && (v[0] & 0x43) == 0x03) {
		dev_info(u->dev, "%s: MAC already enabled (XLMAC_CTRL %08x): skip enable\n", p->cfg->name, v[0]);
		return;
	}
	mutex_lock(&u->mdio);
	ret = tsc_wr(u, 0xffde, a);			/* AER: lane select */
	for (i = 0; !ret && i < ARRAY_SIZE(seq); i++)
		ret = tsc_wr(u, seq[i][0], seq[i][1]);
	mutex_unlock(&u->mdio);
	if (ret)
		return;
	v[0] = 7;
	sbus(u, 0x0d, BLK_XLPORT, 0x20300 | a, v, 1);	/* XLPORT_TXFIFO_CTRL clear counts */
	v[0] = 4;
	sbus(u, 0x0d, BLK_XLPORT, 0x20300 | a, v, 1);
	v[0] = 0x1003; v[1] = 0;			/* XLMAC_CTRL: TX_EN, RX_EN, SOFT_RESET 0 */
	sbus(u, 0x0d, BLK_XLPORT, 0x60000 | a, v, 2);
	v[0] = 7;					/* XLMAC_CLEAR_RX_LSS_STATUS pulse */
	sbus(u, 0x0d, BLK_XLPORT, 0x60c00 | a, v, 2);
	v[0] = 0;
	sbus(u, 0x0d, BLK_XLPORT, 0x60c00 | a, v, 2);
	dev_info(u->dev, "%s: enable sequence done\n", p->cfg->name);
}

/*
 * Private VLAN (id = front number) with only the CPU and this port: VLAN 1 also holds the HiGig links
 * to unit 0, and two ports in one VLAN would be bridged by the chip. Flood pointers, STG and profile are
 * cloned from VLAN 1; only the bitmaps differ.
 */
static void port_vlan_init(struct us48_port *p)
{
	static const int cpu_stg[] = { 0, 1 };
	struct us48_cmic *u = p->u;
	int lp = p->cfg->lport, stg_bits[] = { 2 * lp, 2 * lp + 1 };
	u32 e[4], g[3], one = 1;
	u32 stg;

	if (sbus(u, 0x07, BLK_IPIPE, VLAN_TAB(p->vid), e, 4) || (e[1] & BIT(6))) {	/* VALID b38 */
		dev_warn(u->dev, "%s: VLAN %d not free (or unreadable): not touching\n", p->cfg->name, p->vid);
		return;
	}
	if (sbus(u, 0x07, BLK_IPIPE, VLAN_TAB(1), e, 4) || sbus(u, 0x07, BLK_EPIPE, EGR_VLAN(1), g, 3))
		return;
	stg = (e[1] >> 7) & 0x1ff;			/* STG b39..47 */
	e[0] &= ~0x3fffffffu;				/* PORT_BITMAP 0:30 */
	ebit(e, 0);
	ebit(e, lp);
	set_even_parity(e, 126);
	sbus(u, 0x09, BLK_IPIPE, VLAN_TAB(p->vid), e, 4);
	g[0] = 0;					/* UT_BITMAP 0:30 + PORT_BITMAP 30:30 */
	g[1] &= ~0x0fffffffu;
	ebit(g, 0);
	ebit(g, lp);
	ebit(g, 30);
	ebit(g, 30 + lp);
	set_even_parity(g, 76);
	sbus(u, 0x09, BLK_EPIPE, EGR_VLAN(p->vid), g, 3);
	/* SP_TREE state 3 = forwarding, for the CPU and this port, in the cloned STG. */
	smem_set_bits(u, BLK_IPIPE, STG_TAB(stg), 2, 60, cpu_stg, 2);
	smem_set_bits(u, BLK_IPIPE, STG_TAB(stg), 2, 60, stg_bits, 2);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN_STG(stg), 2, 60, cpu_stg, 2);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN_STG(stg), 2, 60, stg_bits, 2);
	/* Stock's port disable leaves EGR_ENABLE = 0: every frame to the port dies before the MAC. */
	sbus(u, 0x09, BLK_EPIPE, EGR_ENABLE(lp), &one, 1);
}

/*
 * PORT_TAB[port]: PORT_VID (24:12) puts untagged ingress into our VLAN. CML_FLAGS_NEW/MOVE (121:4,
 * 125:4) stay 0: stock's 8 ('do HW learn') drops every unicast frame here (ING_EVENT_DEBUG CML+RDROP).
 */
static void port_tab_set(struct us48_cmic *u, int lport, u32 vid, bool cml_off)
{
	u32 e[8];

	if (sbus(u, 0x07, BLK_IPIPE, PORT_TAB(lport), e, 8))
		return;
	e[0] = (e[0] & 0x00ffffff) | (vid << 24);
	e[1] = (e[1] & ~0xfu) | (vid >> 8);
	if (cml_off) {
		e[3] &= ~(0xfu << 25 | 0x7u << 29);
		e[4] &= ~1u;
	}
	sbus(u, 0x09, BLK_IPIPE, PORT_TAB(lport), e, 8);
}

static void set_field(u32 *e, int lsb, int width, u64 v)
{
	int i;

	for (i = 0; i < width; i++)
		if (v & BIT_ULL(i))
			e[(lsb + i) / 32] |= BIT((lsb + i) % 32);
}

/* Static L2X entry: the port's MAC in its VLAN -> CPU (modid 1, port 0). TX bypasses learning. */
static int l2_op_cpu(struct us48_port *p, u32 op)
{
	u32 e[4] = { };
	int i, ones = 0;

	if (p->cfg->copper) {
		struct bcm_cmic_unit cu = { .base = p->u->cmic };
		struct bcm_hr2_ge ge = ge_of(p);

		return bcm_hr2_ge_l2_cpu(&cu, &ge, p->ndev->dev_addr, op == 0x24);
	}

	set_field(e, 2, 12, p->vid);			/* VLAN_ID */
	set_field(e, 14, 48, ether_addr_to_u64(p->ndev->dev_addr));	/* MAC_ADDR */
	set_field(e, 68, 7, p->u->modid);			/* MODULE_ID, PORT_NUM 0 = CPU */
	set_field(e, 91, 1, 1);				/* CPU: copy to CPU */
	set_field(e, 97, 1, 1);				/* STATIC_BIT */
	set_field(e, 99, 1, 1);				/* VALID */
	for (i = 0; i < 4; i++)
		ones += hweight32(e[i]);
	set_field(e, 100, 1, ones & 1);			/* EVEN_PARITY */
	return sbus(p->u, op, BLK_IPIPE, L2X, e, 4);
}

static void dma_abort(struct us48_cmic *u, int ch)
{
	u32 ctrl = rd(u, DMA_CTRL(ch)), stat;

	if (rd(u, DMA_STAT) & DMA_ACTIVE(ch)) {
		wr(u, DMA_CTRL(ch), ctrl | DMA_EN | DMA_ABORT);
		if (readl_poll_timeout_atomic(u->cmic + DMA_STAT, stat, !(stat & DMA_ACTIVE(ch)),
					      1, ABORT_TIMEOUT_US))
			dev_err(u->dev, "ch%d abort: still active after %u us, DMA_STAT %08x\n",
				ch, ABORT_TIMEOUT_US, stat);
	}
	wr(u, DMA_CTRL(ch), rd(u, DMA_CTRL(ch)) & ~(DMA_EN | DMA_ABORT));
	wr(u, DMA_STAT_CLR, BIT(ch));
}

struct us48_buf {
	struct device *dev;
	size_t size;
	void *cpu;
	dma_addr_t dma;
	enum dma_data_direction dir;
};

static void us48_free_buf(void *data)
{
	struct us48_buf *b = data;

	dma_free_pages(b->dev, b->size, virt_to_page(b->cpu), b->dma, b->dir);
}

/* Cached DMA pages below 128 MiB (the CMIC inbound window); freed with the device. */
static void *us48_alloc_buf(struct device *dev, size_t size, dma_addr_t *dma, enum dma_data_direction dir)
{
	struct us48_buf *b = devm_kzalloc(dev, sizeof(*b), GFP_KERNEL);
	struct page *pg;

	if (!b)
		return NULL;
	size = PAGE_ALIGN(size);
	pg = dma_alloc_pages(dev, size, dma, dir, GFP_KERNEL);
	if (!pg)
		return NULL;
	b->dev = dev;
	b->size = size;
	b->cpu = page_address(pg);
	b->dma = *dma;
	b->dir = dir;
	if (devm_add_action_or_reset(dev, us48_free_buf, b))
		return NULL;
	return b->cpu;
}

static void rx_arm(struct us48_cmic *u)
{
	int i;

	/* RX_RING data DCBs + a RELOAD DCB back to DCB 0: the engine never stops (shared code: bcm_hr2_rx_arm). */
	memset(u->rx_dcb, 0, (RX_RING + 1) * 64);
	for (i = 0; i < RX_RING; i++) {
		u->rx_dcb[16 * i] = cpu_to_le32(u->rx_buf_dma + i * BUF_SZ);
		u->rx_dcb[16 * i + 1] = cpu_to_le32(BUF_SZ | DCB_W1_CHAIN);
	}
	u->rx_dcb[16 * RX_RING] = cpu_to_le32(u->rx_dcb_dma);
	u->rx_dcb[16 * RX_RING + 1] = cpu_to_le32(DCB_W1_CHAIN | DCB_W1_RELOAD);
	u->rx_next = 0;
	dma_sync_single_for_device(u->dev, u->rx_buf_dma, RX_RING * BUF_SZ, DMA_FROM_DEVICE);
	wmb();
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & ~DMA_EN);
	wr(u, DMA_STAT_CLR, BIT(RX_CH));
	wr(u, DMA_DESC(RX_CH), u->rx_dcb_dma);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) | DMA_EN);
}

static struct us48_port *port_by_lport(struct us48_cmic *u, unsigned int lport)
{
	int i;

	for (i = 0; i < u->nports; i++)
		if (u->port[i]->cfg->lport == lport)
			return u->port[i];
	return NULL;
}

/* One received DCB -> skb on the port it came from: drop the 4 CRC bytes and our VLAN tag if present. */
static void rx_one(struct us48_cmic *u, int i)
{
	u32 w15 = le32_to_cpu(u->rx_dcb[16 * i + 15]);
	u32 w12 = le32_to_cpu(u->rx_dcb[16 * i + 12]);
	u8 *d = u->rx_buf + i * BUF_SZ;
	unsigned int len = w15 & 0xffff;
	struct us48_port *p = port_by_lport(u, w12 & 0xff);
	struct net_device *nd;
	struct sk_buff *skb;

	dma_sync_single_for_cpu(u->dev, u->rx_buf_dma + i * BUF_SZ, BUF_SZ, DMA_FROM_DEVICE);
	if (rx_dump) {
		rx_dump--;
		dev_info(u->dev, "RX dcb%d len %u src %u w15 %08x da %pM et %04x\n", i, len, w12 & 0xff, w15, d,
			 get_unaligned_be16(d + 12));
	}
	if (!p || !p->open)
		return;
	nd = p->ndev;
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
	if (get_unaligned_be16(d + 12) == ETH_P_8021Q && (get_unaligned_be16(d + 14) & VLAN_VID_MASK) == p->vid) {
		skb_put_data(skb, d, 12);
		skb_put_data(skb, d + 16, len - 16);
	} else {
		skb_put_data(skb, d, len);
	}
	skb->protocol = eth_type_trans(skb, nd);
	nd->stats.rx_packets++;
	nd->stats.rx_bytes += skb->len;
	napi_gro_receive(&u->napi, skb);
}

static void tx_wake_all(struct us48_cmic *u)
{
	int i;

	for (i = 0; i < u->nports; i++)
		netif_wake_queue(u->port[i]->ndev);
}

static void tx_reap(struct us48_cmic *u)
{
	spin_lock(&u->tx_lock);
	if (u->tx_len && (le32_to_cpu(READ_ONCE(u->tx_dcb[16 + 15])) & DCB_W15_DONE)) {
		struct net_device *nd = u->port[u->tx_len >> 16]->ndev;

		wr(u, DMA_CTRL(TX_CH), rd(u, DMA_CTRL(TX_CH)) & ~DMA_EN);
		wr(u, DMA_STAT_CLR, BIT(TX_CH));
		nd->stats.tx_packets++;
		nd->stats.tx_bytes += u->tx_len & 0xffff;
		u->tx_len = 0;
		tx_wake_all(u);
	}
	spin_unlock(&u->tx_lock);
}

static int us48_poll(struct napi_struct *napi, int budget)
{
	struct us48_cmic *u = container_of(napi, struct us48_cmic, napi);
	int done = 0;

	tx_reap(u);
	while (done < budget) {
		int i = u->rx_next;

		if (!(le32_to_cpu(READ_ONCE(u->rx_dcb[16 * i + 15])) & DCB_W15_DONE))
			break;
		dma_rmb();
		rx_one(u, i);
		dma_sync_single_for_device(u->dev, u->rx_buf_dma + i * BUF_SZ, BUF_SZ, DMA_FROM_DEVICE);
		u->rx_dcb[16 * i + 15] = 0;	/* hand the slot back */
		if (i == RX_RING - 1)		/* the RELOAD DCB also gets DONE; a stale DONE halts the engine (#46) */
			u->rx_dcb[16 * RX_RING + 15] = 0;
		wmb();
		u->rx_next = (i + 1) % RX_RING;
		done++;
	}
	/* Engine halted with every filled slot consumed: restart it from DCB 0. */
	if (!done && !(le32_to_cpu(READ_ONCE(u->rx_dcb[16 * u->rx_next + 15])) & DCB_W15_DONE) &&
	    !(rd(u, DMA_STAT) & DMA_ACTIVE(RX_CH)))
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

/* TX to a port with the module header (OM bcm56150_a0_bmd_tx): DCB0 = DA+SA (12 B), DCB1 = rest at +16, HG on both. */
static netdev_tx_t us48_xmit(struct sk_buff *skb, struct net_device *nd)
{
	struct us48_port *p = netdev_priv(nd);
	struct us48_cmic *u = p->u;
	unsigned int len = max_t(unsigned int, skb->len, ETH_ZLEN);
	int i;

	spin_lock(&u->tx_lock);
	if (u->tx_len || len + 16 > BUF_SZ) {
		spin_unlock(&u->tx_lock);
		nd->stats.tx_dropped++;
		dev_kfree_skb_any(skb);
		return NETDEV_TX_OK;
	}
	memset(u->tx_buf, 0, len + 16);		/* DCB1 reads buf[16, len + 8) */
	skb_copy_bits(skb, 0, u->tx_buf, 12);
	skb_copy_bits(skb, 12, u->tx_buf + 16, skb->len - 12);
	memset(u->tx_dcb, 0, 128);
	u->tx_dcb[0] = cpu_to_le32(u->tx_buf_dma);
	u->tx_dcb[1] = cpu_to_le32(12 | DCB_W1_CHAIN | DCB_W1_SG | DCB_W1_STAT | DCB_W1_HG);
	u->tx_dcb[2] = cpu_to_le32(0x81000000);
	u->tx_dcb[3] = cpu_to_le32(p->cfg->lport);
	u->tx_dcb[4] = cpu_to_le32(0x00040000);		/* txq 0 */
	u->tx_dcb[16] = cpu_to_le32(u->tx_buf_dma + 16);
	/* +4: XLMAC_TX_CTRL CRC_MODE is 'replace' (stock): the last 4 bytes are overwritten with the FCS, not added. */
	u->tx_dcb[17] = cpu_to_le32((len + ETH_FCS_LEN - 12) | DCB_W1_STAT | DCB_W1_HG);
	memcpy(&u->tx_dcb[18], &u->tx_dcb[2], 16);	/* OM _dcb_init: header on both DCBs */
	for (i = 0; i < u->nports; i++)
		if (u->port[i] == p)
			u->tx_len = i << 16 | len;
	for (i = 0; i < u->nports; i++)
		netif_stop_queue(u->port[i]->ndev);
	dma_sync_single_for_device(u->dev, u->tx_buf_dma, len + 16, DMA_TO_DEVICE);
	wmb();
	wr(u, DMA_DESC(TX_CH), u->tx_dcb_dma);
	wr(u, DMA_CTRL(TX_CH), rd(u, DMA_CTRL(TX_CH)) | DMA_EN);
	spin_unlock(&u->tx_lock);
	dev_consume_skb_any(skb);
	return NETDEV_TX_OK;
}

static void us48_tx_timeout(struct net_device *nd, unsigned int txq)
{
	struct us48_port *p = netdev_priv(nd);
	struct us48_cmic *u = p->u;

	netdev_err(nd, "TX stuck: DMA_STAT %08x CTRL %08x dcb1 w15 %08x\n", rd(u, DMA_STAT),
		   rd(u, DMA_CTRL(TX_CH)), le32_to_cpu(u->tx_dcb[31]));
	spin_lock_bh(&u->tx_lock);
	dma_abort(u, TX_CH);
	nd->stats.tx_errors++;
	u->tx_len = 0;
	tx_wake_all(u);
	spin_unlock_bh(&u->tx_lock);
}

static int us48_set_mac(struct net_device *nd, void *a)
{
	struct us48_port *p = netdev_priv(nd);
	int ret;

	if (p->open)
		l2_op_cpu(p, 0x26);
	ret = eth_mac_addr(nd, a);
	if (!ret && p->open && l2_op_cpu(p, 0x24))
		netdev_warn(nd, "L2 insert for %pM failed\n", nd->dev_addr);
	return ret;
}

static int us48_open(struct net_device *nd)
{
	struct us48_port *p = netdev_priv(nd);
	struct us48_cmic *u = p->u;

	if (l2_op_cpu(p, 0x24))
		netdev_warn(nd, "L2 insert for %pM failed: unicast to us will not arrive\n", nd->dev_addr);
	p->open = true;
	if (u->nopen++ == 0) {
		rx_arm(u);
		napi_enable(&u->napi);
		hrtimer_start(&u->poll_timer, ns_to_ktime(POLL_NS), HRTIMER_MODE_REL);
	}
	netif_start_queue(nd);
	return 0;
}

static int us48_stop(struct net_device *nd)
{
	struct us48_port *p = netdev_priv(nd);
	struct us48_cmic *u = p->u;

	netif_stop_queue(nd);
	p->open = false;
	l2_op_cpu(p, 0x26);
	if (--u->nopen == 0) {
		hrtimer_cancel(&u->poll_timer);
		napi_disable(&u->napi);
		dma_abort(u, RX_CH);
		spin_lock_bh(&u->tx_lock);
		dma_abort(u, TX_CH);
		u->tx_len = 0;
		spin_unlock_bh(&u->tx_lock);
	}
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

static void us48_free_napi_dev(void *data)
{
	struct us48_cmic *u = data;

	cancel_delayed_work_sync(&u->link_work);
	netif_napi_del(&u->napi);
	free_netdev(u->napi_dev);
}

int us48_probe_common(struct device *dev, void __iomem *cmic, const struct port_cfg *cfgs, int nports,
			     u8 modid, struct us48_cmic **out)
{
	struct us48_cmic *u;
	u32 link[2];
	int i, ch, ret;

	u = devm_kzalloc(dev, sizeof(*u), GFP_KERNEL);
	if (!u)
		return -ENOMEM;
	u->dev = dev;
	u->cfgs = cfgs;
	u->nports = nports;
	u->modid = modid;
	u->cmic = cmic;
	mutex_init(&u->mdio);
	spin_lock_init(&u->tx_lock);
	INIT_DELAYED_WORK(&u->link_work, link_work_fn);
	ret = dma_set_mask_and_coherent(dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;
	*out = u;

	/* Stock's DCBs live in RAM the kernel has reused: stop both channels before anything else. */
	for (ch = 0; ch < 2; ch++) {
		dev_info(dev, "stock left ch%d CTRL %08x DESC %08x\n", ch, rd(u, DMA_CTRL(ch)),
			 rd(u, DMA_DESC(ch)));
		dma_abort(u, ch);
	}
	/* Init (section 5): keep stock's endian bits, ch0 TX / ch1 RX, all CPU COS to ch1. */
	wr(u, DMA_CTRL(TX_CH), (rd(u, DMA_CTRL(TX_CH)) & DMA_ENDIAN) | DMA_DIR_TX);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & DMA_ENDIAN);
	wr(u, COS_CTRL_RX_0(RX_CH), 0xffffffff);
	wr(u, COS_CTRL_RX_1(RX_CH), 0xffff);

	u->rx_dcb = dmam_alloc_coherent(dev, (RX_RING + 1) * 64, &u->rx_dcb_dma, GFP_KERNEL);
	/* Data buffers are cached pages + streaming syncs: CPU copies out of uncached coherent memory cost ~1.4 us/byte (#46). */
	u->rx_buf = us48_alloc_buf(dev, RX_RING * BUF_SZ, &u->rx_buf_dma, DMA_FROM_DEVICE);
	u->tx_dcb = dmam_alloc_coherent(dev, 128, &u->tx_dcb_dma, GFP_KERNEL);
	u->tx_buf = us48_alloc_buf(dev, BUF_SZ, &u->tx_buf_dma, DMA_TO_DEVICE);
	if (!u->rx_dcb || !u->rx_buf || !u->tx_dcb || !u->tx_buf)
		return -ENOMEM;
	/* Endpoint inbound window covers bus < 0x08000000 only (section 14.3). */
	if (u->rx_buf_dma + RX_RING * BUF_SZ > SZ_128M || u->tx_buf_dma + BUF_SZ > SZ_128M ||
	    u->rx_dcb_dma > SZ_128M || u->tx_dcb_dma > SZ_128M) {
		dev_err(dev, "DMA memory above 128 MiB (rx %pad tx %pad)\n", &u->rx_buf_dma, &u->tx_buf_dma);
		return -ENOMEM;
	}

	u->napi_dev = alloc_netdev_dummy(0);
	if (!u->napi_dev)
		return -ENOMEM;
	netif_napi_add(u->napi_dev, &u->napi, us48_poll);
	ret = devm_add_action_or_reset(dev, us48_free_napi_dev, u);
	if (ret)
		return ret;
	hrtimer_setup(&u->poll_timer, us48_poll_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);

	for (i = 0; i < u->nports; i++) {
		struct net_device *nd = devm_alloc_etherdev(dev, sizeof(struct us48_port));
		struct us48_port *p;

		if (!nd)
			return -ENOMEM;
		p = netdev_priv(nd);
		p->u = u;
		p->ndev = nd;
		p->cfg = &u->cfgs[i];
		p->vid = u->cfgs[i].vid;
		u->port[i] = p;
		SET_NETDEV_DEV(nd, dev);
		nd->netdev_ops = &us48_netdev_ops;
		nd->watchdog_timeo = HZ;	/* one frame completes in us; 1 s = stuck, logged + aborted */
		nd->min_mtu = ETH_MIN_MTU;
		nd->max_mtu = BUF_SZ - ETH_HLEN - VLAN_HLEN - ETH_FCS_LEN - 4;
		/* Fixed locally administered MACs: one L2 entry per port, no growth across loads. */
		eth_hw_addr_set(nd, (const u8[ETH_ALEN]){ 0x02, 0x75, 0x73, 0x34, 0x38, p->cfg->front });
		strscpy(nd->name, p->cfg->name, IFNAMSIZ);
		netif_carrier_off(nd);
	}

	for (i = 0; i < u->nports; i++) {
		struct us48_port *p = u->port[i];

		port_enable(p);
		if (p->cfg->copper)		/* the core did VLAN + PORT_TAB */
			continue;
		port_vlan_init(p);
		port_tab_set(u, p->cfg->lport, p->vid, true);
	}
	if (!sbus(u, 0x0b, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2)) {
		for (i = 0; i < u->nports; i++)
			link[0] |= BIT(u->port[i]->cfg->lport);
		link[0] |= BIT(0);
		sbus(u, 0x0d, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2);
	}

	for (i = 0; i < u->nports; i++) {
		/* devm: unregister (ndo_stop aborts both channels) runs before the DMA memory is freed. */
		ret = devm_register_netdev(dev, u->port[i]->ndev);
		if (ret)
			return ret;
		netdev_info(u->port[i]->ndev, "front %d (logical port %d), MAC %pM, VLAN %d\n", u->port[i]->cfg->front,
			    u->port[i]->cfg->lport, u->port[i]->ndev->dev_addr, u->port[i]->vid);
	}
	schedule_delayed_work(&u->link_work, msecs_to_jiffies(LINK_POLL_MS));
	if (tsc_log)
		us48_tsc_dump(u);
	return 0;
}
EXPORT_SYMBOL_GPL(us48_probe_common);

void us48_quiesce(struct us48_cmic *u)
{
	int i;

	cancel_delayed_work_sync(&u->link_work);
	dma_abort(u, RX_CH);
	dma_abort(u, TX_CH);
	for (i = 0; i < u->nports; i++)
		port_tab_set(u, u->cfgs[i].lport, 1, false);
}
EXPORT_SYMBOL_GPL(us48_quiesce);

/* Unit 0: the host SoC's own CMIC on the AXI bus (0x48000000), management port front 1. */
static uint unit0 = 1;
module_param(unit0, uint, 0444);
MODULE_PARM_DESC(unit0, "1 (default) = drive unit 0 front 1 (ge1) over AXI; 0 = core only (us48_cmic_xe.ko needs the core loaded)");

#define UNIT0_CMIC_BASE	0x48000000
#define UNIT0_CMIC_LEN	0x40000

static struct platform_device *unit0_pdev;

static int us48_cmic_plat_probe(struct platform_device *pdev)
{
	struct us48_cmic *u = NULL;
	void __iomem *cmic = devm_ioremap(&pdev->dev, UNIT0_CMIC_BASE, UNIT0_CMIC_LEN);

	if (!cmic)
		return -ENOMEM;
	return us48_probe_common(&pdev->dev, cmic, unit0_cfgs, ARRAY_SIZE(unit0_cfgs), 0, &u);
}

static struct platform_driver us48_cmic_plat_driver = {
	.driver		= { .name = "us48_cmic_u0" },
	.probe		= us48_cmic_plat_probe,
};

static int __init us48_cmic_init(void)
{
	int ret;

	if (!unit0)
		return 0;
	ret = platform_driver_register(&us48_cmic_plat_driver);
	if (ret)
		return ret;
	unit0_pdev = platform_device_register_simple("us48_cmic_u0", -1, NULL, 0);
	if (IS_ERR(unit0_pdev)) {
		ret = PTR_ERR(unit0_pdev);
		platform_driver_unregister(&us48_cmic_plat_driver);
	}
	return ret;
}

static void __exit us48_cmic_exit(void)
{
	dbg_u = NULL;
	if (unit0_pdev && !IS_ERR(unit0_pdev)) {
		platform_device_unregister(unit0_pdev);
		platform_driver_unregister(&us48_cmic_plat_driver);
	}
}

module_init(us48_cmic_init);
module_exit(us48_cmic_exit);

MODULE_DESCRIPTION("UniFi US-48-500W BCM5334x switch port core + unit 0 front 1 (ge1, polled DMA)");
MODULE_LICENSE("GPL");
