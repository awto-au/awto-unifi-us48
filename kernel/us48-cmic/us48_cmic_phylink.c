// SPDX-License-Identifier: GPL-2.0
/*
 * xe ports (TSC lane, 10GBASE-R; unit 0 SFP 1/2 = 1000BASE-X, #78) on phylink + the kernel sfp module (#21): DT port node under "ethernet-ports"
 * (reg = front port) with sfp = <&cage>. The TSC is the PCS; its init stays in us48_tsc_init. Module ID, DDM hwmon,
 * ethtool -m, hot-plug and TX disable are the sfp module's; FIRMWARE_MODE follows the module caps (pcs_config).
 */
#include <linux/ethtool.h>
#include <linux/gpio/consumer.h>
#include <linux/gpio/driver.h>
#include <linux/of.h>
#include <linux/phylink.h>
#include <linux/property.h>
#include <linux/sfp.h>

#include "us48_cmic.h"

#define TSC_AER		0xffde
#define PCS_LIVE	0xc154		/* RX_X4 PCS live status: b1 link (driver-internals 13.2) */

static struct us48_port *pcs_port(struct phylink_pcs *pcs)
{
	return container_of(pcs, struct us48_port, pcs);
}

static struct us48_port *cfg_port(struct phylink_config *c)
{
	return container_of(c, struct us48_port, plc);
}

/* 1000BASE-X: PCS_LIVE link + CL37 AN word through the c22 decoder (LPA bits = 802.3 CL37 base page, as SDK tscmod_autoneg_get). */
static void xe_pcs_get_state_1g(struct us48_port *p, unsigned int neg_mode, struct phylink_link_state *st)
{
	u16 live = 0, an = 0, lp = 0, bmsr = 0;

	if (us48_tsc_1g_status(p, &live, &an, &lp)) {
		st->link = false;
		return;
	}
	if (live & BIT(1))
		bmsr |= BMSR_LSTATUS;
	if (an & BIT(15))
		bmsr |= BMSR_ANEGCOMPLETE;
	phylink_mii_c22_pcs_decode_state(st, neg_mode, bmsr, lp);
}

/* phylink polls this once a second (pcs.poll). First link up: one RX restart, reported as down this poll (#87). */
static void xe_pcs_get_state(struct phylink_pcs *pcs, unsigned int neg_mode, struct phylink_link_state *st)
{
	struct us48_port *p = pcs_port(pcs);
	struct us48_cmic *u = p->u;
	u16 v = 0;
	bool up;

	if (us48_cfg_1g(p->cfg)) {
		xe_pcs_get_state_1g(p, neg_mode, st);
		return;
	}

	mutex_lock(&u->mdio);
	up = !us48_tsc_wr(u, TSC_AER, p->cfg->lane) && !us48_tsc_rd(u, PCS_LIVE, &v) && (v & BIT(1));
	mutex_unlock(&u->mdio);
	if (up && !p->rx_tuned) {
		p->rx_tuned = true;
		us48_tsc_rx_restart(u, p->cfg);
		up = false;
	}
	st->link = up;
	st->speed = SPEED_10000;
	st->duplex = DUPLEX_FULL;
}

/* Called at phylink_start and on every module insert that changes the link modes: DAC (PORT_DA) = CR mode. */
static int xe_pcs_config(struct phylink_pcs *pcs, unsigned int neg_mode, phy_interface_t iface,
			 const unsigned long *adv, bool permit_pause)
{
	struct us48_port *p = pcs_port(pcs);
	struct sfp_bus *bus = p->ndev->sfp_bus;
	u16 mode = bus && sfp_get_module_caps(bus)->port == PORT_DA ? 2 : 0;

	if (us48_cfg_1g(p->cfg)) {	/* inband = CL37 AN; anything else = forced 1000BASE-X FD */
		bool an = neg_mode == PHYLINK_PCS_NEG_INBAND_ENABLED;

		return p->an_1g == an ? 0 : us48_tsc_1g_an(p, an);
	}
	if (p->fw_mode < 0 || mode == p->fw_mode)
		return 0;
	return us48_tsc_fw_mode(p, mode);
}

static void xe_pcs_an_restart(struct phylink_pcs *pcs)
{
	struct us48_port *p = pcs_port(pcs);

	if (us48_cfg_1g(p->cfg))
		us48_tsc_1g_an_restart(p);
}

/* 10GBASE-R: no inband (0 = as without the op); 1000BASE-X: CL37 on or off. */
static unsigned int xe_pcs_inband_caps(struct phylink_pcs *pcs, phy_interface_t iface)
{
	return iface == PHY_INTERFACE_MODE_1000BASEX ? LINK_INBAND_DISABLE | LINK_INBAND_ENABLE : 0;
}

static const struct phylink_pcs_ops xe_pcs_ops = {
	.pcs_inband_caps = xe_pcs_inband_caps,
	.pcs_get_state = xe_pcs_get_state,
	.pcs_config = xe_pcs_config,
	.pcs_an_restart = xe_pcs_an_restart,
};

static struct phylink_pcs *xe_select_pcs(struct phylink_config *c, phy_interface_t iface)
{
	return &cfg_port(c)->pcs;
}

/* MAC was enabled once at probe (port_enable, 1G: XLMAC_MODE set by us48_tsc_init); link changes need no MAC writes. */
static void xe_mac_config(struct phylink_config *c, unsigned int mode, const struct phylink_link_state *st)
{
}

static void xe_mac_link_down(struct phylink_config *c, unsigned int mode, phy_interface_t iface)
{
}

static void xe_mac_link_up(struct phylink_config *c, struct phy_device *phy, unsigned int mode, phy_interface_t iface,
			   int speed, int duplex, bool tx_pause, bool rx_pause)
{
}

static const struct phylink_mac_ops xe_mac_ops = {
	.mac_select_pcs = xe_select_pcs,
	.mac_config = xe_mac_config,
	.mac_link_down = xe_mac_link_down,
	.mac_link_up = xe_mac_link_up,
};

static int xe_get_ksettings(struct net_device *nd, struct ethtool_link_ksettings *k)
{
	struct us48_port *p = netdev_priv(nd);

	return phylink_ethtool_ksettings_get(p->pl, k);
}

static int xe_set_ksettings(struct net_device *nd, const struct ethtool_link_ksettings *k)
{
	struct us48_port *p = netdev_priv(nd);

	return phylink_ethtool_ksettings_set(p->pl, k);
}

static int xe_nway_reset(struct net_device *nd)
{
	struct us48_port *p = netdev_priv(nd);

	return phylink_ethtool_nway_reset(p->pl);
}

/* ethtool -m and the module EEPROM pages go to nd->sfp_bus in the ethtool core: no op here. */
static const struct ethtool_ops xe_ethtool_ops = {
	.get_link = ethtool_op_get_link,
	.get_link_ksettings = xe_get_ksettings,
	.set_link_ksettings = xe_set_ksettings,
	.nway_reset = xe_nway_reset,
};

/* Unit 1: DT child "cmic" of the endpoint (aux device node). Unit 0's platform device has no node: by path. */
static struct fwnode_handle *xe_port_node(struct us48_cmic *u, u8 front)
{
	struct fwnode_handle *ports, *child;
	u32 reg;

	if (dev_fwnode(u->dev))
		ports = device_get_named_child_node(u->dev, "ethernet-ports");
	else
		ports = of_fwnode_handle(of_find_node_by_path("/ethernet-switch/ethernet-ports"));
	if (!ports)
		return NULL;
	fwnode_for_each_available_child_node(ports, child) {
		if (!fwnode_property_read_u32(child, "reg", &reg) && reg == front)
			break;
	}
	fwnode_handle_put(ports);
	return child;
}

/* Port node -> sfp = <&cage> -> mod-def0-gpios: the line the sfp module owns; we only read it (pwrsave gate, #143). */
static void xe_moddef0_find(struct us48_port *p, struct fwnode_handle *fw)
{
	struct fwnode_handle *cage = fwnode_find_reference(fw, "sfp", 0);
	struct fwnode_reference_args args;

	if (IS_ERR(cage))
		return;
	if (!fwnode_property_get_reference_args(cage, "mod-def0-gpios", "#gpio-cells", 0, 0, &args) && args.nargs >= 2) {
		p->moddef0 = args.fwnode;
		p->moddef0_line = args.args[0];
		p->moddef0_low = args.args[1] & BIT(0);	/* dt-bindings GPIO_ACTIVE_LOW */
	} else {
		netdev_warn(p->ndev, "sfp cage without mod-def0-gpios: presence unknown, XLPORT0 stays powered\n");
	}
	fwnode_handle_put(cage);
}

int us48_xe_cage_present(struct us48_port *p)
{
	struct gpio_device *gdev;
	int v;

	if (!p->moddef0)
		return -ENODEV;
	gdev = gpio_device_find_by_fwnode(p->moddef0);
	if (!gdev)
		return -EPROBE_DEFER;		/* us48_cca_gpio not bound yet (rc.d S05) */
	v = gpiod_get_raw_value_cansleep(gpio_device_get_desc(gdev, p->moddef0_line));
	gpio_device_put(gdev);
	if (v < 0)
		return v;
	return p->moddef0_low ? !v : !!v;
}

int us48_xe_phylink_create(struct us48_port *p)
{
	struct fwnode_handle *fw = xe_port_node(p->u, p->cfg->front);
	phy_interface_t iface = PHY_INTERFACE_MODE_10GBASER;
	struct phylink *pl;

	if (!fw) {
		netdev_err(p->ndev, "no DT node ethernet-ports/port reg = <%d>: no SFP cage, link stays down\n", p->cfg->front);
		return -ENODEV;
	}
	xe_moddef0_find(p, fw);
	p->plc.dev = &p->ndev->dev;
	p->plc.type = PHYLINK_NETDEV;
	if (us48_cfg_1g(p->cfg)) {	/* no MAC pause programming yet: not advertised */
		iface = PHY_INTERFACE_MODE_1000BASEX;
		p->plc.mac_capabilities = MAC_1000FD;
	} else {
		p->plc.mac_capabilities = MAC_10000FD;
	}
	__set_bit(iface, p->plc.supported_interfaces);
	p->pcs.ops = &xe_pcs_ops;
	p->pcs.poll = true;
	__set_bit(iface, p->pcs.supported_interfaces);
	pl = phylink_create(&p->plc, fw, iface, &xe_mac_ops);
	fwnode_handle_put(fw);
	if (IS_ERR(pl)) {
		netdev_err(p->ndev, "phylink_create: %pe, link stays down\n", pl);
		return PTR_ERR(pl);
	}
	p->pl = pl;
	p->ndev->ethtool_ops = &xe_ethtool_ops;
	return 0;
}

/* After unregister_netdev, without rtnl (the sfp bus detach takes it). */
void us48_xe_phylink_destroy(struct us48_port *p)
{
	if (p->pl)
		phylink_destroy(p->pl);
	p->pl = NULL;
	fwnode_handle_put(p->moddef0);
	p->moddef0 = NULL;
}
