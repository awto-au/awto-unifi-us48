// SPDX-License-Identifier: GPL-2.0
/*
 * SFP module state per xe port (#147): sysfs sfp_state = none | ok | unsupported:<reason> | fault:<reason>, sfp_info = detail.
 * Presence from mod-def0 (us48_xe_cage_present); ID bytes via the sfp bus (sfp_get_module_eeprom); modes = the sfp module's
 * caps against the cage's phylink interfaces. sfp.c / phylink own insert, TX_DISABLE and carrier: a rejected module never
 * gets TX enabled (SFP_MOD_ERROR), we add the state and one log line.
 */
#include <linux/ethtool.h>
#include <linux/phylink.h>
#include <linux/sfp.h>
#include <linux/string.h>

#include "us48_cmic.h"

/* Same cadence as the presence poll in us48_cmic_pwr.c (XE_PWR_POLL_MS, driver-internals 16). */
#define SFP_POLL_MS		300
/* ID unreadable this long = fault: sfp.c retries 10 x 100 ms, then one 5 s slow retry (T_PROBE_RETRY_*). */
#define SFP_EEPROM_WAIT_MS	6000

static void sfp_set(struct us48_port *p, const char *state, const char *reason, const char *info)
{
	spin_lock(&p->sfp_lock);
	if (reason)
		scnprintf(p->sfp_state, sizeof(p->sfp_state), "%s:%s", state, reason);
	else
		strscpy(p->sfp_state, state, sizeof(p->sfp_state));
	strscpy(p->sfp_info, info, sizeof(p->sfp_info));
	spin_unlock(&p->sfp_lock);
}

static void ifaces_str(char *buf, size_t n, const unsigned long *mask)
{
	int i, len = 0;

	buf[0] = 0;
	for_each_set_bit(i, mask, PHY_INTERFACE_MODE_MAX)
		len += scnprintf(buf + len, n - len, "%s%s", len ? "," : "", phy_modes(i));
	if (!len)
		strscpy(buf, "none", n);
}

static void trim_field(char *dst, const char *src, size_t n)
{
	memcpy(dst, src, n);
	dst[n] = 0;
	strim(dst);
}

static void compliance_str(char *buf, size_t n, const struct sfp_eeprom_base *b)
{
	int len = 0;

	buf[0] = 0;
#define ADD(f, s) do { if (b->f) len += scnprintf(buf + len, n - len, "%s%s", len ? "+" : "", s); } while (0)
	ADD(e10g_base_sr, "10GBASE-SR");
	ADD(e10g_base_lr, "10GBASE-LR");
	ADD(e10g_base_lrm, "10GBASE-LRM");
	ADD(e10g_base_er, "10GBASE-ER");
	ADD(e1000_base_sx, "1000BASE-SX");
	ADD(e1000_base_lx, "1000BASE-LX");
	ADD(e1000_base_cx, "1000BASE-CX");
	ADD(e1000_base_t, "1000BASE-T");
	ADD(sfp_ct_passive, "DAC-passive");
	ADD(sfp_ct_active, "DAC-active");
#undef ADD
	if (!len)
		strscpy(buf, "unspecified", n);
}

static bool sum_ok(const u8 *id, int from, int to, u8 want)
{
	u8 sum = 0;
	int i;

	for (i = from; i < to; i++)
		sum += id[i];
	return sum == want;
}

/* ID bytes read twice alike (sfp.c has called module_insert by then): judge the module against the cage. */
static void sfp_judge(struct us48_port *p, struct sfp_bus *bus, const u8 *id)
{
	const struct sfp_eeprom_id *e = (const void *)id;
	const struct sfp_module_caps *caps = sfp_get_module_caps(bus);
	DECLARE_PHY_INTERFACE_MASK(common);
	char vn[17], pn[17], sn[17], comp[96], mods[96], cage[48], info[sizeof(p->sfp_info)];

	if (!sum_ok(id, 0, 63, id[63]) || !sum_ok(id, 64, 95, id[95])) {
		scnprintf(info, sizeof(info), "EEPROM CC_BASE/CC_EXT checksum mismatch: module ID unusable, sfp rejects it");
		sfp_set(p, "unsupported", "bad-checksum", info);
		netdev_err(p->ndev, "SFP module rejected: bad-checksum: %s\n", info);
		return;
	}
	trim_field(vn, e->base.vendor_name, 16);
	trim_field(pn, e->base.vendor_pn, 16);
	trim_field(sn, e->ext.vendor_sn, 16);
	compliance_str(comp, sizeof(comp), &e->base);
	phy_interface_and(common, caps->interfaces, p->plc.supported_interfaces);
	ifaces_str(cage, sizeof(cage), p->plc.supported_interfaces);

	if (phy_interface_empty(common)) {
		const char *why = caps->port == PORT_OTHER && phy_interface_empty(caps->interfaces) ?
				  "unknown-connector" : "no-common-mode";

		ifaces_str(mods, sizeof(mods), caps->interfaces);
		scnprintf(info, sizeof(info), "%s %s sn %s, %s: module modes %s, cage %s takes %s (connector 0x%02x): use a module with %s",
			  vn, pn, sn, comp, mods, p->ndev->name, cage, e->base.connector, cage);
		sfp_set(p, "unsupported", why, info);
		netdev_err(p->ndev, "SFP module rejected: %s: %s\n", why, info);
		return;
	}
	ifaces_str(mods, sizeof(mods), common);
	scnprintf(info, sizeof(info), "%s %s sn %s, %s, %u Mb/s nominal, supported modes: %s", vn, pn, sn, comp,
		  e->base.br_nominal * 100, mods);
	sfp_set(p, "ok", NULL, info);
	netdev_info(p->ndev, "SFP module: %s\n", info);
}

static void sfp_removed(struct us48_port *p)
{
	if (!p->sfp_seen)
		return;
	p->sfp_seen = false;
	p->sfp_stage = 0;
	sfp_set(p, "none", NULL, "");
	netdev_info(p->ndev, "SFP module removed\n");
}

static void sfp_present(struct us48_port *p)
{
	struct sfp_bus *bus = READ_ONCE(p->ndev->sfp_bus);
	struct ethtool_eeprom ee = { .cmd = ETHTOOL_GMODULEEEPROM, .offset = 0, .len = sizeof(p->sfp_id) };
	u8 id[sizeof(p->sfp_id)];
	int ret;

	if (!p->sfp_seen) {
		p->sfp_seen = true;
		p->sfp_flogged = false;
		p->sfp_stage = 0;
		p->sfp_t0 = jiffies;
	}
	if (p->sfp_stage == 2)
		return;
	ret = bus ? sfp_get_module_eeprom(bus, &ee, id) : -ENODEV;
	if (ret) {
		p->sfp_stage = 0;
		if (!p->sfp_flogged && time_after(jiffies, p->sfp_t0 + msecs_to_jiffies(SFP_EEPROM_WAIT_MS))) {
			p->sfp_flogged = true;
			sfp_set(p, "fault", "eeprom-unreadable", "module present, ID EEPROM unreadable (I2C)");
			netdev_err(p->ndev, "SFP module fault: eeprom-unreadable: ID read failed (%d) for %d ms\n", ret,
				   SFP_EEPROM_WAIT_MS);
		}
		return;
	}
	if (!p->sfp_stage || memcmp(id, p->sfp_id, sizeof(id))) {
		memcpy(p->sfp_id, id, sizeof(id));
		p->sfp_stage = 1;
		return;
	}
	p->sfp_stage = 2;
	sfp_judge(p, bus, id);
}

static void sfp_work_fn(struct work_struct *w)
{
	struct us48_port *p = container_of(to_delayed_work(w), struct us48_port, sfp_work);
	int pres = us48_xe_cage_present(p);

	if (pres == 0)
		sfp_removed(p);
	else if (pres > 0)
		sfp_present(p);		/* < 0: presence unknown, state stays as is */
	schedule_delayed_work(&p->sfp_work, msecs_to_jiffies(SFP_POLL_MS));
}

void us48_xe_sfp_init(struct us48_port *p)
{
	spin_lock_init(&p->sfp_lock);
	INIT_DELAYED_WORK(&p->sfp_work, sfp_work_fn);
	strscpy(p->sfp_state, "none", sizeof(p->sfp_state));
}

void us48_xe_sfp_start(struct us48_port *p)
{
	schedule_delayed_work(&p->sfp_work, msecs_to_jiffies(SFP_POLL_MS));
}

void us48_xe_sfp_stop(struct us48_port *p)
{
	cancel_delayed_work_sync(&p->sfp_work);
}

static ssize_t sfp_state_show(struct device *d, struct device_attribute *attr, char *buf)
{
	struct us48_port *p = netdev_priv(to_net_dev(d));
	ssize_t n;

	spin_lock(&p->sfp_lock);
	n = sysfs_emit(buf, "%s\n", p->sfp_state);
	spin_unlock(&p->sfp_lock);
	return n;
}
static DEVICE_ATTR_RO(sfp_state);

static ssize_t sfp_info_show(struct device *d, struct device_attribute *attr, char *buf)
{
	struct us48_port *p = netdev_priv(to_net_dev(d));
	ssize_t n;

	spin_lock(&p->sfp_lock);
	n = sysfs_emit(buf, "%s\n", p->sfp_info);
	spin_unlock(&p->sfp_lock);
	return n;
}
static DEVICE_ATTR_RO(sfp_info);

static struct attribute *us48_xe_attrs[] = {
	&dev_attr_sfp_state.attr,
	&dev_attr_sfp_info.attr,
	NULL
};
const struct attribute_group us48_xe_group = { .attrs = us48_xe_attrs };
