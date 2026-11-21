// SPDX-License-Identifier: GPL-2.0
/*
 * CMIC MIIM as phylib MDIO buses: per unit, internal bus 0 (integrated GPHYs) and external bus 0 (BCM54282 octal),
 * clause 22 only. Bus 1 (TSC SerDes, clause 45) stays with us48_tsc_rd/wr. Design: docs/phy.md.
 * Every op takes u->mdio (TSC AER select + access must not interleave); lock order phydev -> bus -> u->mdio.
 * Buses are devm-registered: they go after the netdevs and their PHY attachments (us48_remove_common).
 */
#include <linux/mdio.h>
#include <linux/phy.h>

#include "us48_cmic.h"

static int us48_mdio_read(struct mii_bus *bus, int addr, int reg)
{
	struct us48_mdio *m = bus->priv;
	struct bcm_cmic_unit cu = m->u->cu;
	u16 v = 0;
	int ret;

	mutex_lock(&m->u->mdio);
	ret = bcm_cmic_miim_read(&cu, m->internal, 0, addr, reg, &v);
	mutex_unlock(&m->u->mdio);
	return ret ?: v;
}

static int us48_mdio_write(struct mii_bus *bus, int addr, int reg, u16 val)
{
	struct us48_mdio *m = bus->priv;
	struct bcm_cmic_unit cu = m->u->cu;
	int ret;

	mutex_lock(&m->u->mdio);
	ret = bcm_cmic_miim_write(&cu, m->internal, 0, addr, reg, val);
	mutex_unlock(&m->u->mdio);
	return ret;
}

/* Only the PHY addresses the port table lists are scanned: the others do not answer (docs/hardware.md). */
static int us48_mdio_bus_register(struct us48_cmic *u, bool internal)
{
	struct us48_mdio *m;
	struct mii_bus *bus;
	u32 used = 0;
	int i, ret;

	for (i = 0; i < u->nports; i++)
		if (u->cfgs[i].copper && u->cfgs[i].external_phy != internal)
			used |= BIT(u->cfgs[i].phy);
	if (!used)
		return 0;
	bus = devm_mdiobus_alloc_size(u->dev, sizeof(*m));
	if (!bus)
		return -ENOMEM;
	m = bus->priv;
	m->u = u;
	m->internal = internal;
	bus->name = internal ? "us48 CMIC MIIM internal bus 0" : "us48 CMIC MIIM external bus 0";
	snprintf(bus->id, MII_BUS_ID_SIZE, "us48-u%u-%s0", u->modid, internal ? "int" : "ext");
	bus->parent = u->dev;
	bus->read = us48_mdio_read;
	bus->write = us48_mdio_write;
	bus->phy_mask = ~used;
	ret = devm_mdiobus_register(u->dev, bus);
	if (ret) {
		dev_err(u->dev, "%s: devm_mdiobus_register failed (%d): its ports cannot attach a PHY\n", bus->id, ret);
		return ret;
	}
	u->mdio_bus[internal] = bus;
	return 0;
}

int us48_mdio_register(struct us48_cmic *u)
{
	int ret = us48_mdio_bus_register(u, true);

	return ret ?: us48_mdio_bus_register(u, false);
}

struct phy_device *us48_port_phy(const struct us48_port *p)
{
	struct mii_bus *bus = p->u->mdio_bus[!p->cfg->external_phy];

	return bus ? mdiobus_get_phy(bus, p->cfg->phy) : NULL;
}
