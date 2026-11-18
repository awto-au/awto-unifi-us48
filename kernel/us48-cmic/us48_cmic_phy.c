// SPDX-License-Identifier: GPL-2.0
/*
 * phylib drivers for the copper PHYs of the BCM5334x/BCM56150 family and their netdev glue (docs/phy.md).
 * - 0x600d85b0: 16 integrated GPHYs per unit (internal MIIM bus 0). 0x600d845b: external BCM54282 octal (bus 0).
 * - The octal top reset (once per octal, read-back) stays in bcm_hr2_ge_bringup (GPORT block init, first open).
 * - Register recipe: docs/driver-internals.md 4, 7, 12.4 (OM bcm56150_phy_init, stock phy_bcm542xx_reset_setup).
 */
#include <linux/ethtool.h>
#include <linux/module.h>
#include <linux/phy.h>

#include "bcm-phy-lib.h"
#include "us48_cmic.h"

#define US48_PHY_ID_GPHY	0x600d85b0
#define US48_PHY_ID_OCTAL	0x600d8450	/* live ID 0x600d845b: revision nibble masked */

#define RDB_POWER_MII_CTRL	0x02a		/* b5 SUPER_ISOLATE */
#define RDB_DSP_TAP10		0x125
#define SHD_SGMII_SLAVE		0x15		/* b0 AUTO_DETECT */
#define SHD_AUTO_DETECT_MEDIUM	0x1e		/* b5 QFSD_WITH_SYNC_STATUS */
#define ECR_LED_TRAFFIC_EN	BIT(5)		/* MII_PHY_ECR reg 0x10 */
#define EEE_CTRL_MASK		(LPI_FEATURE_EN | LPI_FEATURE_EN_DIG1000X)	/* MMD 7 0x803d b15/b14 */

static bool phy_ecd;
module_param(phy_ecd, bool, 0644);
MODULE_PARM_DESC(phy_ecd, "1 = ethtool --cable-test runs the BCM54xx ECD (exp 0x0fc0..); not yet proven on the BCM54282");
static bool phy_eee;
module_param(phy_eee, bool, 0444);
MODULE_PARM_DESC(phy_eee, "1 = report EEE abilities and drive the LPI feature bits; the MAC has no LPI support yet");

struct us48_phy_priv {
	u64 *stats;			/* shadow of the clear-on-read counters (bcm-phy-lib) */
};

/* RDB access as stock does it: enter (exp 0x0f7e <- 0), 0x1e/0x1f access, exit (RDB 0x87 <- 0x8000), docs 12.4. */
static int rdb_modify(struct phy_device *ph, u16 rdb, u16 mask, u16 set, bool force)
{
	int val, ret, exit_ret;

	phy_lock_mdio_bus(ph);
	ret = __bcm_phy_write_exp(ph, BCM54XX_EXP_REG7E, 0);
	if (!ret) {
		val = force ? 0 : __bcm_phy_read_rdb(ph, rdb);
		ret = val < 0 ? val : 0;
		if (!ret && (force || ((val & ~mask) | set) != val))
			ret = __bcm_phy_write_rdb(ph, rdb, (val & ~mask) | set);
	}
	exit_ret = __bcm_phy_write_rdb(ph, BCM54XX_RDB_REG0087, BCM54XX_ACCESS_MODE_LEGACY_EN);
	phy_unlock_mdio_bus(ph);
	return ret ?: exit_ret;
}

static int us48_phy_probe(struct phy_device *ph)
{
	struct us48_phy_priv *priv = devm_kzalloc(&ph->mdio.dev, sizeof(*priv), GFP_KERNEL);

	if (!priv)
		return -ENOMEM;
	priv->stats = devm_kcalloc(&ph->mdio.dev, bcm_phy_get_sset_count(ph), sizeof(u64), GFP_KERNEL);
	if (!priv->stats)
		return -ENOMEM;
	ph->priv = priv;
	/* Down until the netdev opens: SDK _bcm_port_probe leaves ports disabled (#133). */
	return phy_set_bits(ph, MII_BMCR, BMCR_PDOWN);
}

/* The MAC runs full duplex only (bcm_hr2_ge_mac_up sets no HD_ENA). */
static int us48_phy_get_features(struct phy_device *ph)
{
	int ret = genphy_read_abilities(ph);

	if (ret)
		return ret;
	linkmode_clear_bit(ETHTOOL_LINK_MODE_10baseT_Half_BIT, ph->supported);
	linkmode_clear_bit(ETHTOOL_LINK_MODE_100baseT_Half_BIT, ph->supported);
	linkmode_clear_bit(ETHTOOL_LINK_MODE_1000baseT_Half_BIT, ph->supported);
	if (!phy_eee)
		linkmode_zero(ph->supported_eee);
	return 0;
}

/*
 * Octal lane init, the order of the former bcm54282_link_init (stock 0x10d116c + OM steps 2-5, driver-internals 4, 7):
 * writes before the PDN clear land with PDN set, as verified on the board.
 */
static int octal_config_init(struct phy_device *ph)
{
	int val, ret;

	ret = phy_write(ph, MII_BCM54XX_AUX_CTL, 0x0c00);		/* MII_AUX_CTRL = ENABLE_DSP_CLOCK | RESERVED */
	ret = ret ?: rdb_modify(ph, RDB_DSP_TAP10, 0, 0x011b, true);	/* stock + OM */
	ret = ret ?: rdb_modify(ph, RDB_POWER_MII_CTRL, BIT(5), 0, false);	/* SUPER_ISOLATE = 0 */
	val = ret ? ret : bcm_phy_read_shadow(ph, BCM54XX_SHD_MODE);	/* MODE_CTRL */
	ret = val < 0 ? val : bcm_phy_write_shadow(ph, BCM54XX_SHD_MODE, val & ~(BIT(1) | BIT(2)));	/* MODE_SEL 0 */
	ret = ret ?: phy_clear_bits(ph, MII_BMCR, BMCR_PDOWN);		/* later writes need PDN clear */
	val = ret ? ret : bcm_phy_read_shadow(ph, SHD_SGMII_SLAVE);
	ret = val < 0 ? val : bcm_phy_write_shadow(ph, SHD_SGMII_SLAVE, val | BIT(0));	/* AUTO_DETECT */
	ret = ret ?: bcm_phy_write_shadow(ph, SHD_AUTO_DETECT_MEDIUM, BIT(5));	/* whole reg: OM step 5 */
	ret = ret ?: phy_set_bits(ph, MII_BCM54XX_ECR, ECR_LED_TRAFFIC_EN);
	ret = ret ?: phy_write(ph, MII_BCM54XX_AUX_CTL, 0x4c00);	/* 0x0c00 | EXT_PKT_LEN: OM step 5 */
	/* Former GB_CTRL 0x0600: SWITCH_DEV (prefer master) is CTRL1000 b10, written by genphy_config_aneg. */
	ph->master_slave_set = MASTER_SLAVE_CFG_MASTER_PREFERRED;
	return ret;
}

/* LPI feature before the advertisement goes out (OM EEE_803D); EEE_ADV itself is phylib's. */
static int us48_phy_config_aneg(struct phy_device *ph)
{
	int ret = 0;

	if (phy_eee)
		ret = phy_modify_mmd(ph, MDIO_MMD_AN, BRCM_CL45VEN_EEE_CONTROL, EEE_CTRL_MASK,
				     ph->eee_cfg.eee_enabled ? EEE_CTRL_MASK : 0);
	return ret < 0 ? ret : genphy_config_aneg(ph);
}

/* Admin down: BMCR PDN (RMW), the peer loses link (#133). Counters clear on power-down: keep their shadow. */
static int us48_phy_suspend(struct phy_device *ph)
{
	struct us48_phy_priv *priv = ph->priv;

	mutex_lock(&ph->lock);
	bcm_phy_update_stats_shadow(ph, priv->stats);
	mutex_unlock(&ph->lock);
	return genphy_suspend(ph);
}

/* PDN clear + AN restart, as the bring-up value 0x1340 did (#133). */
static int us48_phy_resume(struct phy_device *ph)
{
	int ret = genphy_resume(ph);

	if (!ret && ph->autoneg == AUTONEG_ENABLE)
		ret = genphy_restart_aneg(ph);
	return ret < 0 ? ret : 0;
}

static void us48_phy_get_stats(struct phy_device *ph, struct ethtool_stats *stats, u64 *data)
{
	struct us48_phy_priv *priv = ph->priv;

	bcm_phy_get_stats(ph, priv->stats, stats, data);
}

static int us48_phy_get_tunable(struct phy_device *ph, struct ethtool_tunable *tuna, void *data)
{
	return tuna->id == ETHTOOL_PHY_DOWNSHIFT ? bcm_phy_downshift_get(ph, data) : -EOPNOTSUPP;
}

static int us48_phy_set_tunable(struct phy_device *ph, struct ethtool_tunable *tuna, const void *data)
{
	return tuna->id == ETHTOOL_PHY_DOWNSHIFT ? bcm_phy_downshift_set(ph, *(const u8 *)data) : -EOPNOTSUPP;
}

static int us48_phy_cable_start(struct phy_device *ph)
{
	return phy_ecd ? bcm_phy_cable_test_start(ph) : -EOPNOTSUPP;
}

static int us48_phy_cable_status(struct phy_device *ph, bool *finished)
{
	return phy_ecd ? bcm_phy_cable_test_get_status(ph, finished) : -EOPNOTSUPP;
}

#define US48_PHY_COMMON \
	.probe			= us48_phy_probe, \
	.get_features		= us48_phy_get_features, \
	.config_aneg		= us48_phy_config_aneg, \
	.read_status		= genphy_read_status, \
	.suspend		= us48_phy_suspend, \
	.resume			= us48_phy_resume, \
	.get_sset_count		= bcm_phy_get_sset_count, \
	.get_strings		= bcm_phy_get_strings, \
	.get_stats		= us48_phy_get_stats, \
	.get_tunable		= us48_phy_get_tunable, \
	.set_tunable		= us48_phy_set_tunable, \
	.cable_test_start	= us48_phy_cable_start, \
	.cable_test_get_status	= us48_phy_cable_status

static struct phy_driver us48_phy_drivers[] = {
	{
		PHY_ID_MATCH_MODEL(US48_PHY_ID_GPHY),
		.name		= "BCM56150 integrated GPHY",
		US48_PHY_COMMON,
	}, {
		PHY_ID_MATCH_MODEL(US48_PHY_ID_OCTAL),
		.name		= "BCM54282 octal GPHY",
		.config_init	= octal_config_init,
		US48_PHY_COMMON,
	},
};

int us48_phy_init(void)
{
	return phy_drivers_register(us48_phy_drivers, ARRAY_SIZE(us48_phy_drivers), THIS_MODULE);
}

void us48_phy_exit(void)
{
	phy_drivers_unregister(us48_phy_drivers, ARRAY_SIZE(us48_phy_drivers));
}

/* PHY link change (phydev->lock held): MAC speed + RX/TX follow the link; carrier is phylib's. */
static void us48_adjust_link(struct net_device *nd)
{
	struct us48_port *p = netdev_priv(nd);
	struct phy_device *ph = nd->phydev;
	struct bcm_cmic_unit cu = p->u->cu;
	struct bcm_hr2_ge ge = us48_ge_of(p);

	mutex_lock(&p->u->link_lock);
	if (ph->link) {
		bcm_hr2_ge_mac_up(&cu, &ge, ph->speed);
		WRITE_ONCE(p->speed, ph->speed);
	} else {
		bcm_hr2_ge_mac_down(&cu, &ge);
	}
	mutex_unlock(&p->u->link_lock);
	netdev_info(nd, "link %s%s\n", ph->link ? "up" : "down",
		    !ph->link ? "" : ph->speed == SPEED_1000 ? " 1000M" : ph->speed == SPEED_100 ? " 100M" : " 10M");
}

/* ndo_open, no driver lock held (phydev->lock is taken before link_lock by adjust_link). */
int us48_phy_connect(struct us48_port *p)
{
	struct phy_device *ph = us48_port_phy(p);
	int ret;

	if (p->ndev->phydev)
		return 0;
	if (!ph) {
		netdev_err(p->ndev, "no PHY at MIIM %s0 address %#x\n", p->cfg->external_phy ? "ext" : "int", p->cfg->phy);
		return -ENODEV;
	}
	ret = phy_connect_direct(p->ndev, ph, us48_adjust_link,
				 p->cfg->external_phy ? PHY_INTERFACE_MODE_QSGMII : PHY_INTERFACE_MODE_INTERNAL);
	if (ret)
		netdev_err(p->ndev, "phy_connect_direct failed (%d)\n", ret);
	return ret;
}

void us48_phy_start(struct us48_port *p)
{
	if (p->ndev->phydev)
		phy_start(p->ndev->phydev);
}

/* Link down + BMCR PDN happen inside phy_stop (state HALTED). */
void us48_phy_stop(struct us48_port *p)
{
	if (p->ndev->phydev)
		phy_stop(p->ndev->phydev);
}

void us48_phy_disconnect(struct us48_port *p)
{
	if (p->ndev->phydev)
		phy_disconnect(p->ndev->phydev);
}

/* Never-opened ports of a re-powered octal (us48_cmic_pwr.c): no phydev attached, BMCR only. */
int us48_copper_power(struct us48_port *p, bool on)
{
	struct phy_device *ph = us48_port_phy(p);
	int ret;

	if (!ph)
		return -ENODEV;
	ret = on ? phy_clear_bits(ph, MII_BMCR, BMCR_PDOWN) : phy_set_bits(ph, MII_BMCR, BMCR_PDOWN);
	if (ret < 0)
		netdev_warn(p->ndev, "PHY power %s failed: %d\n", on ? "up" : "down", ret);
	return ret < 0 ? ret : 0;
}

/* Settings of a never-opened port come from the unattached phydev (down, speed unknown). */
static int us48_get_ksettings(struct net_device *nd, struct ethtool_link_ksettings *k)
{
	struct phy_device *ph = nd->phydev ?: us48_port_phy(netdev_priv(nd));

	if (!ph)
		return -ENODEV;
	phy_ethtool_ksettings_get(ph, k);
	return 0;
}

static int us48_get_eee(struct net_device *nd, struct ethtool_keee *e)
{
	return nd->phydev ? phy_ethtool_get_eee(nd->phydev, e) : -ENODEV;
}

static int us48_set_eee(struct net_device *nd, struct ethtool_keee *e)
{
	return nd->phydev ? phy_ethtool_set_eee(nd->phydev, e) : -ENODEV;
}

static const struct ethtool_ops us48_copper_ethtool_ops = {
	.get_link		= ethtool_op_get_link,
	.get_link_ksettings	= us48_get_ksettings,
	.set_link_ksettings	= phy_ethtool_set_link_ksettings,
	.nway_reset		= phy_ethtool_nway_reset,
	.get_eee		= us48_get_eee,
	.set_eee		= us48_set_eee,
};

void us48_phy_netdev_init(struct net_device *nd)
{
	nd->ethtool_ops = &us48_copper_ethtool_ops;
}
