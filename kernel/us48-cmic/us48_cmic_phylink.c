// SPDX-License-Identifier: GPL-2.0
/*
 * xe ports (TSC lane, 10GBASE-R) on phylink + the kernel sfp module (#21): DT port node under "ethernet-ports"
 * (reg = front port) with sfp = <&cage>. The TSC is the PCS; its init stays in us48_tsc_init. Module ID, DDM hwmon,
 * ethtool -m, hot-plug and TX disable are the sfp module's; FIRMWARE_MODE follows the module caps (pcs_config).
 */
#include <linux/ethtool.h>
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

/* phylink polls this once a second (pcs.poll). First link up: one RX restart, reported as down this poll (#87). */
static void xe_pcs_get_state(struct phylink_pcs *pcs, unsigned int neg_mode, struct phylink_link_state *st)
{
	struct us48_port *p = pcs_port(pcs);
	struct us48_cmic *u = p->u;
	u16 v = 0;
	bool up;

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

	if (p->fw_mode < 0 || mode == p->fw_mode)
		return 0;
	return us48_tsc_fw_mode(p, mode);
}

static const struct phylink_pcs_ops xe_pcs_ops = {
	.pcs_get_state = xe_pcs_get_state,
	.pcs_config = xe_pcs_config,
};

static struct phylink_pcs *xe_select_pcs(struct phylink_config *c, phy_interface_t iface)
{
	return &cfg_port(c)->pcs;
}

/* MAC was enabled once at probe (port_enable); link changes need no MAC writes. */
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

int us48_xe_phylink_create(struct us48_port *p)
{
	struct fwnode_handle *fw = xe_port_node(p->u, p->cfg->front);
	struct phylink *pl;

	if (!fw) {
		netdev_err(p->ndev, "no DT node ethernet-ports/port reg = <%d>: no SFP cage, link stays down\n", p->cfg->front);
		return -ENODEV;
	}
	p->plc.dev = &p->ndev->dev;
	p->plc.type = PHYLINK_NETDEV;
	p->plc.mac_capabilities = MAC_10000FD;
	__set_bit(PHY_INTERFACE_MODE_10GBASER, p->plc.supported_interfaces);
	p->pcs.ops = &xe_pcs_ops;
	p->pcs.poll = true;
	__set_bit(PHY_INTERFACE_MODE_10GBASER, p->pcs.supported_interfaces);
	pl = phylink_create(&p->plc, fw, PHY_INTERFACE_MODE_10GBASER, &xe_mac_ops);
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
}
