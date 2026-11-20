// SPDX-License-Identifier: GPL-2.0
/*
 * Power down what no port uses (#25), at probe unless pwrsave=0: XLPORT/TSC blocks without a port (SDK), QSGMII2X 0/1
 * and GPHY octals none of whose ports was opened (stock IDDQ writes). Sources: docs/driver-internals.md 16.
 */
#include <linux/firmware.h>

#include "us48_cmic.h"

static uint pwrsave = 1;
module_param(pwrsave, uint, 0444);
MODULE_PARM_DESC(pwrsave, "1 (default) = power down unused XLPORT/TSC, QSGMII2X, unopened GPHY octals, unit 0 XLPORT0 while SFP 1/2 are empty and down; 0 = all on (#25, #143)");

#define BLK_TOP			16
#define TOP_QSGMII2X_CTRL	0x02033400
#define  QSGMII2X_IDDQ(i)	BIT(6 + (i))		/* one bit per instance 0-2 */
#define TOP_QGPHY_CTRL_0	0x02033800
#define  QGPHY_EXT_PWRDOWN(g)	(0xffu << (8 * (g)))	/* one bit per PHY; GPORT g = phys 2 + 8g .. 9 + 8g */
#define  QGPHY_IDDQ_BIAS(g)	(3u << (16 + 2 * (g)))	/* one bit per quad */
#define XLPORT_ENABLE_REG	0x02020b00
#define XLPORT_POWER_SAVE	0x02020d00		/* XPORT_CORE0 b0 */
#define XLPORT_XGXS0_CTRL	0x02021400
#define  XGXS_PWRDWN		BIT(6)
#define  XGXS_TSCCLK_EN		BIT(7)
#define  XGXS_IDDQ		BIT(8)
#define GPORT_BLK0		2			/* port_cfg.blk of GPORT0 (hwdb_gen: QSGMII<n> -> 2 + n) */

static int top_rmw(struct us48_cmic *u, u32 addr, u32 clr, u32 set)
{
	u32 v;
	int ret = us48_sbus(u, BCM_SCHAN_READ_REG, BLK_TOP, addr, &v, 1);

	if (ret)
		return ret;
	v = (v & ~clr) | set;
	return us48_sbus(u, BCM_SCHAN_WRITE_REG, BLK_TOP, addr, &v, 1);
}

/* GPORT g (0-2) power: QGPHY quads (g 0/1) or the QSGMII2X core of the external octal (g 2). */
static int octal_power(struct us48_cmic *u, int g, bool on)
{
	if (g == 2)
		return top_rmw(u, TOP_QSGMII2X_CTRL, on ? QSGMII2X_IDDQ(2) : 0, on ? 0 : QSGMII2X_IDDQ(2));
	return top_rmw(u, TOP_QGPHY_CTRL_0, on ? QGPHY_EXT_PWRDOWN(g) | QGPHY_IDDQ_BIAS(g) : 0,
		       on ? 0 : QGPHY_EXT_PWRDOWN(g) | QGPHY_IDDQ_BIAS(g));
}

/* SDK _soc_hu2_power_Optimize for a block with XLPORT_ENABLE_REG = 0; us48_tsc_fw.c xlport_up() undoes it. */
static int xlport_power(struct us48_cmic *u, u32 blk, bool on)
{
	u32 v, en = 0;
	int ret = us48_sbus(u, BCM_SCHAN_READ_REG, blk, XLPORT_ENABLE_REG, &en, 1) ?:
		  us48_sbus(u, BCM_SCHAN_READ_REG, blk, XLPORT_XGXS0_CTRL, &v, 1);

	if (ret || en)
		return ret ?: -EBUSY;
	v = on ? (v & ~XGXS_IDDQ) | XGXS_TSCCLK_EN : v | XGXS_PWRDWN | XGXS_IDDQ;
	if (!on)
		v &= ~XGXS_TSCCLK_EN;
	ret = us48_sbus(u, BCM_SCHAN_WRITE_REG, blk, XLPORT_XGXS0_CTRL, &v, 1);
	v = !on;
	return ret ?: us48_sbus(u, BCM_SCHAN_WRITE_REG, blk, XLPORT_POWER_SAVE, &v, 1);
}

/* The octal of the management port (unit 0 ge1, up from boot: rc.d S04us48-net) is never gated. */
static bool octal_has_mgmt(struct us48_cmic *u, int g)
{
	int i;

	for (i = 0; i < u->nports; i++)
		if (!u->modid && u->cfgs[i].copper && u->cfgs[i].front == 1 && u->cfgs[i].blk == GPORT_BLK0 + g)
			return true;
	return false;
}

/* Probe, before any PHY access: undo a previous load's power-down (TOP state survives the cold init). */
void us48_pwr_restore(struct us48_cmic *u)
{
	int g, ret = top_rmw(u, TOP_QSGMII2X_CTRL, QSGMII2X_IDDQ(0) | QSGMII2X_IDDQ(1) | QSGMII2X_IDDQ(2), 0);

	for (g = 0; !ret && g < 2; g++)
		ret = octal_power(u, g, true);
	if (ret)
		dev_warn(u->dev, "pwrsave: power restore failed (%d)\n", ret);
	u->octal_off = 0;
}

/* Probe, after the copper ports were powered down (MII_CTRL PDN) and the xe ports enabled. */
void us48_pwr_save(struct us48_cmic *u)
{
	bool xe = false;
	int i, g, ret;

	for (i = 0; i < u->nports; i++)
		xe |= !u->cfgs[i].copper;
	for (i = 0; i < 2; i++) {
		u32 blk = BLK_XLPORT + i;	/* XLPORT0 = TSC0 (xe ports), XLPORT1 = TSC1 (HiGig, opt-in) */

		if (i == 0 && xe)
			continue;
		ret = xlport_power(u, blk, !pwrsave);
		if (ret)
			dev_warn(u->dev, "pwrsave: XLPORT block %u %s failed (%d)\n", blk, pwrsave ? "power down" : "power on", ret);
	}
	if (!pwrsave) {
		dev_info(u->dev, "pwrsave=0: unused blocks left powered\n");
		return;
	}
	ret = top_rmw(u, TOP_QSGMII2X_CTRL, 0, QSGMII2X_IDDQ(0) | QSGMII2X_IDDQ(1));
	for (g = 0; g < 3; g++) {
		if (octal_has_mgmt(u, g))
			continue;
		ret = ret ?: octal_power(u, g, false);
		if (!ret)
			u->octal_off |= BIT(g);
	}
	dev_info(u->dev, "pwrsave: XLPORT%s, QSGMII2X 0/1, GPORT octals %#x powered down%s\n", xe ? "1" : "0/1",
		 u->octal_off, ret ? " (TOP write FAILED)" : "");
}

/* Unit 0 XLPORT0 gate (#143): on at probe, off while both cages are empty and both xe ports down, on at insert/open. */
/* Presence poll 300 ms (Dan 2026-11-19); sfp.c polls its own at a fixed 100 ms. Sources: driver-internals 16. */
#define XE_PWR_POLL_MS		300

static bool xe_needed(struct us48_cmic *u)
{
	int i;

	for (i = 0; i < u->nports; i++) {
		struct us48_port *p = u->port[i];

		if (!p->cfg->copper && (p->open || us48_xe_cage_present(p) != 0))
			return true;	/* open, module present, or presence unknown */
	}
	return false;
}

/* ENABLE_REG back to its cold-init value 0 (driver-internals 16 live values), then the SDK power-down (xlport_power). */
static void xe_power_down(struct us48_cmic *u)
{
	u32 v = 0;
	int i, ret = us48_sbus(u, BCM_SCHAN_WRITE_REG, BLK_XLPORT, XLPORT_ENABLE_REG, &v, 1) ?:
		     xlport_power(u, BLK_XLPORT, false);

	if (ret) {
		dev_warn(u->dev, "pwrsave: XLPORT0 power down failed (%d)\n", ret);
		return;
	}
	for (i = 0; i < u->nports; i++)
		if (!u->port[i]->cfg->copper) {
			u->port[i]->fw_mode = -1;	/* TSC state lost; us48_tsc_init sets 0 again */
			u->port[i]->an_1g = -1;
			u->port[i]->rx_tuned = false;
		}
	u->xe_off = true;
	dev_info(u->dev, "pwrsave: XLPORT0/TSC0 powered down (both cages empty, xe ports down)\n");
}

int us48_pwr_xe_up(struct us48_cmic *u)
{
	const struct firmware *fw;
	int i, ret;

	if (!u->xe_off)
		return 0;
	ret = request_firmware(&fw, "us48-tsc.bin", u->dev);
	if (ret) {
		dev_err(u->dev, "pwrsave: XLPORT0 power up: us48-tsc.bin: %d\n", ret);
		return ret;
	}
	ret = us48_tsc_init(u, fw);	/* xlport_up() + TSC stage 0/2/3, as at probe */
	release_firmware(fw);
	if (ret) {
		dev_err(u->dev, "pwrsave: XLPORT0 power up failed (%d)\n", ret);
		return ret;
	}
	for (i = 0; i < u->nports; i++)
		if (!u->port[i]->cfg->copper)
			us48_xe_port_enable(u->port[i]);
	us48_led_chain_cfg(u);		/* I: the power-down may lose XLPORT_LED_CHAIN_CONFIG */
	u->xe_off = false;
	dev_info(u->dev, "pwrsave: XLPORT0/TSC0 powered up\n");
	return 0;
}

static void xe_pwr_work_fn(struct work_struct *w)
{
	struct us48_cmic *u = container_of(to_delayed_work(w), struct us48_cmic, xe_pwr_work);

	mutex_lock(&u->link_lock);
	if (xe_needed(u))
		us48_pwr_xe_up(u);
	else if (!u->xe_off)
		xe_power_down(u);
	mutex_unlock(&u->link_lock);
	schedule_delayed_work(&u->xe_pwr_work, msecs_to_jiffies(XE_PWR_POLL_MS));
}

/* Probe, after the netdevs and phylink exist: unit 0 only, pwrsave=1 only. */
void us48_pwr_xe_start(struct us48_cmic *u)
{
	int i;

	if (u->modid || !pwrsave)
		return;
	for (i = 0; i < u->nports && u->cfgs[i].copper; i++)
		;
	if (i == u->nports)
		return;
	INIT_DELAYED_WORK(&u->xe_pwr_work, xe_pwr_work_fn);
	u->xe_poll = true;
	schedule_delayed_work(&u->xe_pwr_work, msecs_to_jiffies(XE_PWR_POLL_MS));
}

void us48_pwr_xe_stop(struct us48_cmic *u)
{
	if (u->xe_poll)
		cancel_delayed_work_sync(&u->xe_pwr_work);
	u->xe_poll = false;
}

/* First open of a copper port, link_lock held: power its octal back up, PDN again on the octal's never-opened ports. */
int us48_pwr_octal_up(struct us48_port *p)
{
	struct us48_cmic *u = p->u;
	int i, g = p->cfg->blk - GPORT_BLK0, ret;

	if (g < 0 || g > 2 || !(u->octal_off & BIT(g)))
		return 0;
	ret = octal_power(u, g, true);
	if (ret) {
		netdev_err(p->ndev, "pwrsave: GPORT octal %d power up failed (%d)\n", g, ret);
		return ret;
	}
	u->octal_off &= ~BIT(g);
	for (i = 0; i < u->nports; i++) {
		struct us48_port *q = u->port[i];

		if (q != p && q->cfg->copper && q->cfg->blk == p->cfg->blk && !q->copper_ready)
			us48_copper_power(q, false);
	}
	netdev_info(p->ndev, "pwrsave: GPORT octal %d powered up\n", g);
	return 0;
}
