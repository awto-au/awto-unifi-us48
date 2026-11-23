// SPDX-License-Identifier: GPL-2.0
/*
 * bcm59111: Broadcom BCM59111 4-port PoE PSE as a pse-pd provider (c33) + die temperature hwmon. Design, safety and
 * register evidence: docs/poe-pse.md, docs/poe.md. Reference drivers: Ubiquiti UDM bcm59111.c (GPL), quenode/mx65w-poe-openwrt.
 * Every port is shut down at probe and stays off until an explicit admin enable. Event registers 0x00/0x03/0x07 are never read.
 */
#include <linux/delay.h>
#include <linux/firmware.h>
#include <linux/gpio/consumer.h>
#include <linux/hwmon.h>
#include <linux/hwmon-sysfs.h>
#include <linux/i2c.h>
#include <linux/ktime.h>
#include <linux/leds.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/netdevice.h>
#include <linux/of.h>
#include <linux/pse-pd/pse.h>
#include <linux/property.h>
#include <linux/regulator/consumer.h>
#include <linux/regulator/driver.h>
#include "../us48-boardio/us48_boardio.h"

#define NPORT		4
#define REG_STAT_P(c)	(0x0c + (c))		/* [3:0] detect, [6:4] class */
#define REG_STAT_PWR	0x10			/* [3:0] power enable, [7:4] power good (stock naming) */
#define REG_OP_MODE	0x12			/* 2 bits per channel, channel 0 = bits 1:0 */
#define REG_DIS_SENSE	0x13
#define REG_DET_CLASS	0x14			/* [3:0] detect enable, [7:4] class enable */
#define REG_TEMP	0x2c
#define REG_CM(c)	(0x30 + 4 * (c))	/* current, 16 bit, LSB first */
#define REG_VM(c)	(0x32 + 4 * (c))	/* voltage, 16 bit, LSB first */
#define REG_HWD_TIME	0x20			/* awto image only: host watchdog, 10 ms ticks, write = arm / poke, 0 = off */
#define REG_HWD_STAT	0x21			/* awto image only: b7 feature present, b1 armed, b0 expired (latched); write b7 = restart */
#define HWD_FEATURE	BIT(7)
#define HWD_EXPIRED	BIT(0)
#define HWD_RESTART	BIT(7)
#define REG_INT_MASK	0x01			/* stock init value 0x66; here the configured marker (cold config last write) */
#define CFG_MARK	0x66
#define SCAN_BASE	0x10			/* power scan block: 0x10 STAT_PWR .. 0x3f (TEMP 0x2c, CM/VM 0x30..0x3f), no clear-on-read register inside */
#define SCAN_LEN	0x30
#define REG_FW_REV	0x41
#define REG_DEV_ID	0x43
#define REG_HP_EN	0x44
#define REG_HP_MODE(c)	(0x46 + 5 * (c))
#define REG_ICUT(c)	(0x47 + 5 * (c))
#define REG_ILIM(c)	(0x48 + 5 * (c))
#define REG_FW_DWN	0x70
#define REG_CRC		0x75

#define OP_SHUTDOWN	0x0
#define OP_AUTO		0x3
#define DEV_ID_59111	0xc0			/* (reg 0x43 & 0xe0): stock accepts any silicon revision */
#define CRC_RUNNING	0xaa
#define NCHIP_MAX	12
#define FW_SIZE		0x4000			/* stock blob length word */
#define FW_BLOCK	32
#define FW_START	0x80			/* first byte of every block write */
#define FW_FORCE_CRC	0x40
#define FW_POLL_MS	50			/* UDM polls reg 0x75 every 50 ms */
#define FW_WAIT_MS	2500			/* stock sleeps 2 s then checks once: x1.25 */
#define TEMP_RAW_MIN	0x54			/* T = 367 - 2 * raw degC, valid only when raw > 0x53 */
#define TEMP_OFS_C	367
#define UV_PER_VM	5835			/* 5.835 mV per count */
#define NA_PER_CM	122070			/* 122.07 uA per count */
#define RESERVE_DEFAULT_MW 30000		/* 802.3at class 4 PSE output */

/* Tunables: choice and evidence in docs/poe-architecture.md. All 0444 (set at load); every default leaves ports off. */
static char *fw_override;
module_param(fw_override, charp, 0444);
MODULE_PARM_DESC(fw_override, "bench: \"PoE-NN=<file>[,PoE-MM=<file>...]\" loads <file> from /lib/firmware into that chip instead of the default image (docs/poe-fw-rewrite.md)");

static unsigned int temp_shed_c;
module_param(temp_shed_c, uint, 0444);
MODULE_PARM_DESC(temp_shed_c, "shut all ports of a chip whose die reaches this many degrees C (blocked until enabled by hand); 0 = off (alarm and log only at the stock limit, temp1_max)");

static unsigned int enable_stagger_ms = 200;
module_param(enable_stagger_ms, uint, 0444);
MODULE_PARM_DESC(enable_stagger_ms, "enable_ports at probe: one port every N ms in front-port order, none once the measured total reaches power_limit_mw; 0 = all at once (stock poed stagger_ms default 200, docs/poe.md 2)");

static unsigned int fw_check_ms = 20000;
module_param(fw_check_ms, uint, 0444);
MODULE_PARM_DESC(fw_check_ms, "check reg 0x75 of every chip each N ms and reset + reload all chips on firmware loss; 0 = off (stock: about every 20 s, docs/poe.md 4 step 10)");

static bool fw_bcast = true;
module_param(fw_bcast, bool, 0444);
MODULE_PARM_DESC(fw_bcast, "firmware load: 1 = one broadcast download (stock: address 0x30, mux channels 4+5 at once) loads all 12 chips together, 0 = one chip at a time (default 1; ignored with fw_override)");

static bool fw_load;
module_param(fw_load, bool, 0444);
MODULE_PARM_DESC(fw_load, "download firmware when the chip reports none running (needs the reset line and /lib/firmware); default off");

static unsigned long long enable_ports;
module_param(enable_ports, ullong, 0444);
MODULE_PARM_DESC(enable_ports, "front ports (bit 0 = port 1) admin-enabled at probe, chip from the PoE-NN label; default 0 = all off (us48-poe-off)");

static unsigned int budget_mw;
module_param(budget_mw, uint, 0444);
MODULE_PARM_DESC(budget_mw, "module-wide cap on reserved mW over all chips, on top of the DT regulators; default 0 = DT only (stock 436000)");

static unsigned int reserve_mw;
module_param(reserve_mw, uint, 0444);
MODULE_PARM_DESC(reserve_mw, "mW reserved per enabled port, overrides DT brcm,port-reserve-milliwatt; default 0 = DT, else 30000 (802.3at class 4)");

static int icut_raw = -1;
module_param(icut_raw, int, 0444);
MODULE_PARM_DESC(icut_raw, "UNVERIFIED (M2): raw ICUT byte (reg 0x47+5c) written on port enable; default -1 = chip value kept");

static int ilim_raw = -1;
module_param(ilim_raw, int, 0444);
MODULE_PARM_DESC(ilim_raw, "UNVERIFIED (M2): raw ILIM byte (reg 0x48+5c) written on port enable; default -1 = chip value kept");

static unsigned int poll_ms;
module_param(poll_ms, uint, 0444);
MODULE_PARM_DESC(poll_ms, "log power-good changes every N ms; default 0 = off; 300 suggested (FW disconnect timer 30 ticks x 10 ms, docs/poe-fw.md 11)");

static unsigned int host_wd_ms = 200;
module_param(host_wd_ms, uint, 0444);
MODULE_PARM_DESC(host_wd_ms, "awto firmware host watchdog: chip drops all PoE when not poked for this long (10 ms steps, 20..2550); poked every half period; 0 = off; stock firmware has none");

static unsigned int power_limit_mw = 436000;
module_param(power_limit_mw, uint, 0644);
MODULE_PARM_DESC(power_limit_mw, "measured total power cap over all chips: above it the most recently powered port is shut and blocked; 436000 = 500 W PSU - 64 W (poe.md 7); 0 = off");

static unsigned int power_poll_ms = 1000;
module_param(power_poll_ms, uint, 0444);
MODULE_PARM_DESC(power_poll_ms, "scan period of the measured-power watcher; 0 = off. One scan of 12 chips is ~240 I2C byte reads");

static unsigned int total_mw;
module_param(total_mw, uint, 0444);
MODULE_PARM_DESC(total_mw, "read-only: total measured port power at the last scan");

static unsigned long long passive_ports;
module_param(passive_ports, ullong, 0444);
MODULE_PARM_DESC(passive_ports, "opt-in mask (bit 0 = front port 1) of ports that portN_mode may switch to passive 24 V (spare pairs powered, no detection, hazard to any other device); default 0 = none");

struct bcm59111 {
	struct i2c_client *client;
	const char *label;
	const char *fw_name;
	struct pse_controller_dev pcdev;
	u32 reserve_mw;
	bool reserved[NPORT];
	bool taken[NPORT];		/* counted in budget_used */
	u8 pg;				/* last power-good nibble seen by the poll */
	struct led_trigger *poe_trig[NPORT];	/* ge<N>-poe-good: the PoE LED of us48_boardio follows power-good (#178) */
	char poe_trig_name[NPORT][16];	/* the LED core keeps the name pointer, not a copy */
	bool temp_over;			/* die at or over temp1_max at the last scan */
	u8 led_on;			/* last power-good nibble sent to the triggers */
	unsigned int stag_pending;	/* enable_ports bits still to enable (stagger worker) */
	bool fw_ok;			/* PSE registered: firmware was running at probe (guard: fw_check_ms) */
	bool passive[NPORT];		/* MSPI field = 1: 24 V passive; the 802.3 chip channel stays off */
	bool want[NPORT];		/* admin-enabled by the driver: restored after a watchdog restart */
	bool hwd;			/* firmware has the host watchdog and it is armed by us */
	struct delayed_work hwd_work;
	u32 pw_mw[NPORT];		/* last measured power, watcher */
	u64 pw_since_ms[NPORT];		/* first scan that saw the port powered; 0 = not powered */
	bool blocked[NPORT];		/* shut by the watcher (over budget); cleared by an explicit enable */
	struct delayed_work poll;
};

static DEFINE_MUTEX(budget_lock);
static unsigned int budget_used;

static struct bcm59111 *to_bcm(struct pse_controller_dev *pcdev)
{
	return container_of(pcdev, struct bcm59111, pcdev);
}

static int rd(struct bcm59111 *b, u8 reg)
{
	return i2c_smbus_read_byte_data(b->client, reg);
}

static int wr(struct bcm59111 *b, u8 reg, u8 val)
{
	return i2c_smbus_write_byte_data(b->client, reg, val);
}

/* 16 bit measurement: LSB first (the read latches the MSB), as stock and UDM */
/* One write + repeated-start read of n bytes (the firmware auto-increments, bench 2026-11-23; adapter limit seen up to 20, FIFO 64) */
static int rd_blk(struct bcm59111 *b, u8 reg, u8 *buf, int n)
{
	struct i2c_msg m[2] = {
		{ .addr = b->client->addr, .len = 1, .buf = &reg },
		{ .addr = b->client->addr, .flags = I2C_M_RD, .len = n, .buf = buf },
	};
	int ret = i2c_transfer(b->client->adapter, m, 2);

	return ret == 2 ? 0 : ret < 0 ? ret : -EIO;
}

static int rd16(struct bcm59111 *b, u8 reg)
{
	int lo = rd(b, reg), hi;

	if (lo < 0)
		return lo;
	hi = rd(b, reg + 1);
	return hi < 0 ? hi : (hi << 8) | lo;
}

static int set_op_mode(struct bcm59111 *b, int id, u8 mode)
{
	int v = rd(b, REG_OP_MODE);

	if (v < 0)
		return v;
	v = (v & ~(3 << (2 * id))) | (mode << (2 * id));
	return wr(b, REG_OP_MODE, v);
}

static int budget_take(struct bcm59111 *b, int id)
{
	int ret = 0;

	if (!budget_mw || b->taken[id])
		return 0;
	mutex_lock(&budget_lock);
	if (budget_used + b->reserve_mw > budget_mw)
		ret = -ERANGE;
	else {
		budget_used += b->reserve_mw;
		b->taken[id] = true;
	}
	mutex_unlock(&budget_lock);
	return ret;
}

static void budget_give(struct bcm59111 *b, int id)
{
	if (!b->taken[id])
		return;
	mutex_lock(&budget_lock);
	budget_used -= b->reserve_mw;
	b->taken[id] = false;
	mutex_unlock(&budget_lock);
}

/* ---- pse-pd ops ---- */

static int bcm_pi_get_admin_state(struct pse_controller_dev *pcdev, int id, struct pse_admin_state *as)
{
	int v = rd(to_bcm(pcdev), REG_OP_MODE);

	if (v < 0)
		return v;
	as->c33_admin_state = (v >> (2 * id)) & 3 ? ETHTOOL_C33_PSE_ADMIN_STATE_ENABLED :
						    ETHTOOL_C33_PSE_ADMIN_STATE_DISABLED;
	return 0;
}

static int bcm_pi_get_pw_status(struct pse_controller_dev *pcdev, int id, struct pse_pw_status *ps)
{
	struct bcm59111 *b = to_bcm(pcdev);
	int op = rd(b, REG_OP_MODE), pwr, st;

	if (op < 0)
		return op;
	if (!((op >> (2 * id)) & 3)) {
		ps->c33_pw_status = ETHTOOL_C33_PSE_PW_D_STATUS_DISABLED;
		return 0;
	}
	pwr = rd(b, REG_STAT_PWR);
	if (pwr < 0)
		return pwr;
	if (pwr & BIT(id + 4)) {
		ps->c33_pw_status = ETHTOOL_C33_PSE_PW_D_STATUS_DELIVERING;
		return 0;
	}
	st = rd(b, REG_STAT_P(id));
	if (st < 0)
		return st;
	/* detect nibble 1 = short; 2, 3, 5 = invalid signature, 6 = open: all "searching" */
	ps->c33_pw_status = (st & 0xf) == 1 ? ETHTOOL_C33_PSE_PW_D_STATUS_FAULT :
					      ETHTOOL_C33_PSE_PW_D_STATUS_SEARCHING;
	return 0;
}

/* Class of a detected PD: nibble 1..4 = class 1..4, 6 = class 0. Anything else (no valid detection): 0. */
static int bcm_pi_get_pw_class(struct pse_controller_dev *pcdev, int id)
{
	int st = rd(to_bcm(pcdev), REG_STAT_P(id)), cls;

	if (st < 0)
		return st;
	cls = (st >> 4) & 7;
	return (st & 0xf) == 4 && cls >= 1 && cls <= 4 ? cls : 0;
}

static int bcm_pi_get_voltage(struct pse_controller_dev *pcdev, int id)
{
	int vm = rd16(to_bcm(pcdev), REG_VM(id));

	return vm < 0 ? vm : vm * UV_PER_VM;
}

static int mw_of(int vm, int cm)
{
	/* mW = (vm * 5.835 mV) * (cm * 122.07 uA) */
	return div64_u64((u64)vm * UV_PER_VM * cm * NA_PER_CM, 1000000ULL * 1000000ULL);
}

static int port_mw(struct bcm59111 *b, int id)
{
	int vm, cm;

	vm = rd16(b, REG_VM(id));
	cm = rd16(b, REG_CM(id));
	if (vm < 0 || cm < 0)
		return vm < 0 ? vm : cm;
	return mw_of(vm, cm);
}

static int bcm_pi_get_actual_pw(struct pse_controller_dev *pcdev, int id)
{
	struct bcm59111 *b = to_bcm(pcdev);
	int pwr = rd(b, REG_STAT_PWR);

	if (pwr < 0)
		return pwr;
	return pwr & BIT(id + 4) ? port_mw(b, id) : 0;
}

/*
 * No IRQ consumer or detection events yet, so the core's static budget (needs classification events) is not used.
 * Admission control instead: every enabled port reserves reserve_mw on its vpwr supply chain (card -> total).
 */
static int bcm_pi_enable(struct pse_controller_dev *pcdev, int id)
{
	struct bcm59111 *b = to_bcm(pcdev);
	struct regulator *supply;
	bool took = false;
	int v, ret;

	if (!pcdev->pi[id].rdev)
		return -ENODEV;	/* port without a pse-pi node */
	if (b->passive[id])
		return -EBUSY;	/* 24 V passive owns the port until portN_mode = 802.3 */
	ret = budget_take(b, id);
	if (ret)
		return ret;
	supply = pcdev->pi[id].rdev->supply;
	if (!b->reserved[id] && supply) {
		ret = regulator_request_power_budget(supply, b->reserve_mw);
		if (ret) {
			budget_give(b, id);
			return ret;
		}
		b->reserved[id] = true;
		took = true;
	}
	/* detect + class enable for the channel, as the UDM port_mode_set and stock opmode path */
	v = rd(b, REG_DET_CLASS);
	ret = v < 0 ? v : wr(b, REG_DET_CLASS, v | BIT(id) | BIT(id + 4));
	if (!ret && icut_raw >= 0)
		ret = wr(b, REG_ICUT(id), icut_raw);
	if (!ret && ilim_raw >= 0)
		ret = wr(b, REG_ILIM(id), ilim_raw);
	if (!ret)
		ret = set_op_mode(b, id, OP_AUTO);
	if (!ret)
		b->want[id] = true;
	if (ret) {
		budget_give(b, id);
		if (took) {
			regulator_free_power_budget(supply, b->reserve_mw);
			b->reserved[id] = false;
		}
	}
	return ret;
}

/* Front port (0-based) of chip channel id: label PoE-NN = chip NN, front ports 4(NN-1)+1..4(NN-1)+4 */
static int bcm_front_port(struct bcm59111 *b, int id)
{
	unsigned int n;

	if (!b->label || strncmp(b->label, "PoE-", 4) || kstrtouint(b->label + 4, 10, &n) || n < 1 || n > 12)
		return -EINVAL;
	return 4 * (n - 1) + id;
}

/*
 * Passive 24 V = MSPI port field 1 (docs/poe.md 6). Switch order and settle times are stock's (`0x60620c`):
 * field 0, 100 ms, new field, 50 ms. The chip channel is off while passive (checked by the caller).
 * Returns to 802.3 (field 2) on request; unbind does the same.
 */
static int bcm_passive_set(struct bcm59111 *b, int id, bool on)
{
	int port = bcm_front_port(b, id), ret;

	if (port < 0)
		return port;
	ret = us48_boardio_poe_field(port, US48_POE_FIELD_NONE);
	if (ret)
		return ret;
	msleep(100);
	ret = us48_boardio_poe_field(port, on ? US48_POE_FIELD_PASSIVE : US48_POE_FIELD_8023);
	if (ret)
		return ret;
	msleep(50);
	b->passive[id] = on;
	return 0;
}

/* Driver unbind (rmmod, reboot path, probe failure after this point) = PoE off on this chip, stock firmware included (Dan 2026-11-21). */
static void bcm_ports_off(void *data)
{
	struct bcm59111 *b = data;
	int id, ret;

	mutex_lock(&b->pcdev.lock);
	ret = wr(b, REG_OP_MODE, 0);
	if (!ret)
		ret = rd(b, REG_OP_MODE);
	mutex_unlock(&b->pcdev.lock);
	for (id = 0; id < NPORT; id++) {
		b->want[id] = false;
		if (b->passive[id])
			bcm_passive_set(b, id, false);	/* 24 V off on unbind, as 802.3 */
	}
	if (ret)
		dev_err(&b->client->dev, "unbind: ports not shut down (reg 0x12 = %d)\n", ret);
	else
		dev_info(&b->client->dev, "unbind: all 4 ports shut down\n");
}

/* The DT regulator budget outlives the driver: hand back this chip's reservations on unbind (a reload otherwise starts with them leaked). */
static void bcm_budget_release(void *data)
{
	struct bcm59111 *b = data;
	int id;

	for (id = 0; id < NPORT; id++) {
		struct regulator *supply = b->pcdev.pi[id].rdev ? b->pcdev.pi[id].rdev->supply : NULL;

		if (b->reserved[id] && supply)
			regulator_free_power_budget(supply, b->reserve_mw);
		b->reserved[id] = false;
		budget_give(b, id);
	}
}

static int bcm_pi_disable(struct pse_controller_dev *pcdev, int id)
{
	struct bcm59111 *b = to_bcm(pcdev);
	struct regulator *supply = pcdev->pi[id].rdev ? pcdev->pi[id].rdev->supply : NULL;
	int ret = set_op_mode(b, id, OP_SHUTDOWN);

	if (!ret) {
		b->want[id] = false;
		budget_give(b, id);
		if (b->reserved[id]) {
			if (supply)
				regulator_free_power_budget(supply, b->reserve_mw);
			b->reserved[id] = false;
		}
	}
	return ret;
}

static const struct pse_controller_ops bcm_pse_ops = {
	.pi_get_admin_state = bcm_pi_get_admin_state,
	.pi_get_pw_status = bcm_pi_get_pw_status,
	.pi_get_pw_class = bcm_pi_get_pw_class,
	.pi_get_actual_pw = bcm_pi_get_actual_pw,
	.pi_get_voltage = bcm_pi_get_voltage,
	.pi_enable = bcm_pi_enable,
	.pi_disable = bcm_pi_disable,
};

/* ---- sysfs: per-port admin control and status until the ports have a phy_device for ethtool --set-pse (docs/poe-pse.md) ---- */

static ssize_t enable_show(struct device *dev, struct device_attribute *a, char *buf)
{
	struct bcm59111 *b = dev_get_drvdata(dev);
	struct pse_admin_state as = {};
	int id = to_sensor_dev_attr(a)->index, ret;

	mutex_lock(&b->pcdev.lock);
	ret = bcm_pi_get_admin_state(&b->pcdev, id, &as);
	mutex_unlock(&b->pcdev.lock);
	return ret ? ret : sysfs_emit(buf, "%d\n", as.c33_admin_state == ETHTOOL_C33_PSE_ADMIN_STATE_ENABLED);
}

static ssize_t enable_store(struct device *dev, struct device_attribute *a, const char *buf, size_t len)
{
	struct bcm59111 *b = dev_get_drvdata(dev);
	int id = to_sensor_dev_attr(a)->index, ret;
	bool on;

	ret = kstrtobool(buf, &on);
	if (ret)
		return ret;
	mutex_lock(&b->pcdev.lock);
	if (on)
		b->blocked[id] = false;
	ret = on ? bcm_pi_enable(&b->pcdev, id) : bcm_pi_disable(&b->pcdev, id);
	mutex_unlock(&b->pcdev.lock);
	return ret ? ret : len;
}

static ssize_t status_show(struct device *dev, struct device_attribute *a, char *buf)
{
	/* indexed by enum ethtool_c33_pse_pw_d_status (UNKNOWN = 1) */
	static const char * const st[] = { "?", "unknown", "disabled", "searching", "delivering", "test", "fault", "otherfault" };
	struct bcm59111 *b = dev_get_drvdata(dev);
	struct pse_pw_status ps = {};
	int id = to_sensor_dev_attr(a)->index, ret, cls, uv, mw;

	mutex_lock(&b->pcdev.lock);
	ret = bcm_pi_get_pw_status(&b->pcdev, id, &ps);
	cls = ret ? ret : bcm_pi_get_pw_class(&b->pcdev, id);
	uv = cls < 0 ? cls : bcm_pi_get_voltage(&b->pcdev, id);
	mw = uv < 0 ? uv : bcm_pi_get_actual_pw(&b->pcdev, id);
	mutex_unlock(&b->pcdev.lock);
	if (mw < 0)
		return mw;
	return sysfs_emit(buf, "%s class %d voltage_uV %d power_mW %d reserved_mW %u\n",
			  b->blocked[id] ? "blocked_over_budget" : st[min_t(u32, ps.c33_pw_status, ARRAY_SIZE(st) - 1)], cls, uv, mw,
			  b->reserved[id] ? b->reserve_mw : 0);
}

static ssize_t mode_show(struct device *dev, struct device_attribute *a, char *buf)
{
	struct bcm59111 *b = dev_get_drvdata(dev);

	return sysfs_emit(buf, "%s\n", b->passive[to_sensor_dev_attr(a)->index] ? "passive24" : "802.3");
}

/*
 * passive24 refuses unless: the port is in passive_ports (opt-in), the channel is off (reg 0x12 field 0), the front
 * port's netdev geN exists and has no carrier (a device with a link would get 24 V on its spare pairs). Hazard: docs/poe-pse.md.
 */
static ssize_t mode_store(struct device *dev, struct device_attribute *a, const char *buf, size_t len)
{
	struct bcm59111 *b = dev_get_drvdata(dev);
	int id = to_sensor_dev_attr(a)->index, port, op, ret;
	struct net_device *nd;
	char name[16];
	bool passive;

	if (sysfs_streq(buf, "passive24"))
		passive = true;
	else if (sysfs_streq(buf, "802.3"))
		passive = false;
	else
		return -EINVAL;
	port = bcm_front_port(b, id);
	if (port < 0)
		return port;
	mutex_lock(&b->pcdev.lock);
	if (passive == b->passive[id]) {
		ret = 0;
	} else if (passive) {
		op = rd(b, REG_OP_MODE);
		snprintf(name, sizeof(name), "ge%d", port + 1);
		nd = dev_get_by_name(&init_net, name);
		if (!(passive_ports & BIT_ULL(port)))
			ret = -EPERM;
		else if (op < 0)
			ret = op;
		else if ((op >> (2 * id)) & 3)
			ret = -EBUSY;	/* 802.3 channel not off */
		else if (!nd)
			ret = -ENODEV;	/* link state not verifiable */
		else if (netif_carrier_ok(nd))
			ret = -EBUSY;
		else
			ret = bcm_passive_set(b, id, true);
		if (nd)
			dev_put(nd);
		if (!ret)
			dev_warn(dev, "port %d: passive 24 V ON\n", port + 1);
	} else {
		ret = bcm_passive_set(b, id, false);
		if (!ret)
			dev_info(dev, "port %d: passive 24 V off\n", port + 1);
	}
	mutex_unlock(&b->pcdev.lock);
	return ret ? ret : len;
}

static SENSOR_DEVICE_ATTR_RW(port1_mode, mode, 0);
static SENSOR_DEVICE_ATTR_RW(port2_mode, mode, 1);
static SENSOR_DEVICE_ATTR_RW(port3_mode, mode, 2);
static SENSOR_DEVICE_ATTR_RW(port4_mode, mode, 3);
static SENSOR_DEVICE_ATTR_RW(port1_enable, enable, 0);
static SENSOR_DEVICE_ATTR_RW(port2_enable, enable, 1);
static SENSOR_DEVICE_ATTR_RW(port3_enable, enable, 2);
static SENSOR_DEVICE_ATTR_RW(port4_enable, enable, 3);
static SENSOR_DEVICE_ATTR_RO(port1_status, status, 0);
static SENSOR_DEVICE_ATTR_RO(port2_status, status, 1);
static SENSOR_DEVICE_ATTR_RO(port3_status, status, 2);
static SENSOR_DEVICE_ATTR_RO(port4_status, status, 3);

static struct attribute *bcm_attrs[] = {
	&sensor_dev_attr_port1_mode.dev_attr.attr, &sensor_dev_attr_port2_mode.dev_attr.attr,
	&sensor_dev_attr_port3_mode.dev_attr.attr, &sensor_dev_attr_port4_mode.dev_attr.attr,
	&sensor_dev_attr_port1_enable.dev_attr.attr, &sensor_dev_attr_port2_enable.dev_attr.attr,
	&sensor_dev_attr_port3_enable.dev_attr.attr, &sensor_dev_attr_port4_enable.dev_attr.attr,
	&sensor_dev_attr_port1_status.dev_attr.attr, &sensor_dev_attr_port2_status.dev_attr.attr,
	&sensor_dev_attr_port3_status.dev_attr.attr, &sensor_dev_attr_port4_status.dev_attr.attr,
	NULL
};

static const struct attribute_group bcm_group = { .attrs = bcm_attrs };

/* ---- hwmon: die temperature ---- */

/* Stock per-chip alert limits PoE-01..12 (docs/board.md Temperature sensors, last column), degrees C */
static const u8 temp_max_c[12] = { 75, 75, 75, 80, 85, 85, 80, 75, 75, 75, 70, 65 };

static int bcm_temp_max(const struct bcm59111 *b)
{
	unsigned int n;

	if (!b->label || strncmp(b->label, "PoE-", 4) || kstrtouint(b->label + 4, 10, &n) || n < 1 || n > 12)
		return -ENODATA;
	return temp_max_c[n - 1];
}

/* Die temperature in milli-degrees C; caller needs no lock (single register read) */
static int bcm_temp_mc(struct bcm59111 *b, long *val)
{
	int raw = rd(b, REG_TEMP);

	if (raw < 0)
		return raw;
	if (raw < TEMP_RAW_MIN)
		return -ENODATA;
	*val = (TEMP_OFS_C - 2 * raw) * 1000L;
	return 0;
}

static umode_t bcm_is_visible(const void *data, enum hwmon_sensor_types type, u32 attr, int ch)
{
	const struct bcm59111 *b = data;

	if (type != hwmon_temp)
		return 0;
	if (attr == hwmon_temp_input || (attr == hwmon_temp_label && b->label))
		return 0444;
	if ((attr == hwmon_temp_max || attr == hwmon_temp_max_alarm) && bcm_temp_max(b) >= 0)
		return 0444;
	return 0;
}

static int bcm_read(struct device *dev, enum hwmon_sensor_types type, u32 attr, int ch, long *val)
{
	struct bcm59111 *b = dev_get_drvdata(dev);
	long t;
	int ret;

	if (type != hwmon_temp)
		return -EOPNOTSUPP;
	switch (attr) {
	case hwmon_temp_input:
		return bcm_temp_mc(b, val);
	case hwmon_temp_max:
		ret = bcm_temp_max(b);
		*val = ret < 0 ? 0 : ret * 1000L;
		return ret < 0 ? ret : 0;
	case hwmon_temp_max_alarm:
		ret = bcm_temp_mc(b, &t);
		if (ret)
			return ret;
		*val = t >= bcm_temp_max(b) * 1000L;
		return 0;
	}
	return -EOPNOTSUPP;
}

static int bcm_read_string(struct device *dev, enum hwmon_sensor_types type, u32 attr, int ch, const char **str)
{
	struct bcm59111 *b = dev_get_drvdata(dev);

	*str = b->label;
	return 0;
}

static const struct hwmon_ops bcm_hwmon_ops = {
	.is_visible = bcm_is_visible,
	.read = bcm_read,
	.read_string = bcm_read_string,
};

static const struct hwmon_channel_info * const bcm_info[] = {
	HWMON_CHANNEL_INFO(temp, HWMON_T_INPUT | HWMON_T_LABEL | HWMON_T_MAX | HWMON_T_MAX_ALARM),
	NULL
};

static const struct hwmon_chip_info bcm_chip = { .ops = &bcm_hwmon_ops, .info = bcm_info };

/* ---- optional poll: power-good changes to the log (no event register is read: clear-on-read, poe-fw.md 4) ---- */

static void bcm_poll(struct work_struct *w)
{
	struct bcm59111 *b = container_of(to_delayed_work(w), struct bcm59111, poll);
	int pwr;

	mutex_lock(&b->pcdev.lock);
	pwr = rd(b, REG_STAT_PWR);
	mutex_unlock(&b->pcdev.lock);
	if (pwr >= 0 && (pwr >> 4) != b->pg) {
		dev_info(&b->client->dev, "power good 0x%x -> 0x%x\n", b->pg, pwr >> 4);
		b->pg = pwr >> 4;
	}
	schedule_delayed_work(&b->poll, msecs_to_jiffies(poll_ms));
}

static void bcm_poll_stop(void *data)
{
	cancel_delayed_work_sync(data);
}

/* ---- host watchdog (awto firmware): poke every half period; on expiry the chip already dropped all PoE -> log, restart, re-enable ---- */

static void bcm_hwd(struct work_struct *w)
{
	struct bcm59111 *b = container_of(to_delayed_work(w), struct bcm59111, hwd_work);
	int st, id, n = 0, ret = 0;

	mutex_lock(&b->pcdev.lock);
	st = rd(b, REG_HWD_STAT);
	if (st >= 0 && (st & HWD_EXPIRED)) {
		for (id = 0; id < NPORT; id++)
			n += b->want[id];
		dev_err(&b->client->dev, "firmware host watchdog expired: all PoE was dropped; restarting and re-enabling %d port(s)\n", n);
		ret = wr(b, REG_HWD_STAT, HWD_RESTART);
		for (id = 0; !ret && id < NPORT; id++)
			if (b->want[id]) {
				b->want[id] = false;
				ret = bcm_pi_enable(&b->pcdev, id);
			}
	}
	if (!ret)
		ret = wr(b, REG_HWD_TIME, host_wd_ms / 10);	/* the poke */
	if (ret < 0 || st < 0)
		dev_warn(&b->client->dev, "host watchdog poke failed (%d / %d)\n", ret, st);
	mutex_unlock(&b->pcdev.lock);
	schedule_delayed_work(&b->hwd_work, msecs_to_jiffies(host_wd_ms / 2));
}

static void bcm_hwd_stop(void *data)
{
	struct bcm59111 *b = data;

	cancel_delayed_work_sync(&b->hwd_work);
	mutex_lock(&b->pcdev.lock);
	wr(b, REG_HWD_TIME, 0);		/* disarm first: the ports are shut explicitly right after (bcm_ports_off), no expiry latch left behind */
	mutex_unlock(&b->pcdev.lock);
}

static int bcm_hwd_start(struct bcm59111 *b)
{
	int st;

	if (!host_wd_ms)
		return 0;
	if (host_wd_ms < 20 || host_wd_ms > 2550) {
		dev_warn(&b->client->dev, "host_wd_ms %u outside 20..2550: watchdog not armed\n", host_wd_ms);
		return 0;
	}
	st = rd(b, REG_HWD_STAT);
	if (st < 0 || !(st & HWD_FEATURE)) {
		dev_info(&b->client->dev, "firmware has no host watchdog (reg 0x%02x = %d): PoE stays up if the kernel dies\n", REG_HWD_STAT, st);
		return 0;
	}
	if (wr(b, REG_HWD_STAT, HWD_RESTART) || wr(b, REG_HWD_TIME, host_wd_ms / 10)) {
		dev_warn(&b->client->dev, "host watchdog: arming failed\n");
		return 0;
	}
	b->hwd = true;
	INIT_DELAYED_WORK(&b->hwd_work, bcm_hwd);
	schedule_delayed_work(&b->hwd_work, msecs_to_jiffies(host_wd_ms / 2));
	dev_info(&b->client->dev, "host watchdog armed: %u ms, poke every %u ms\n", host_wd_ms, host_wd_ms / 2);
	return devm_add_action_or_reset(&b->client->dev, bcm_hwd_stop, b);
}

/* ---- measured-power watcher: total over all chips > power_limit_mw -> shut + block the most recently powered port ---- */

static struct bcm59111 *chips[12];
static DEFINE_MUTEX(chips_lock);
static struct delayed_work watch_work;

static int bcm_chip_no(struct bcm59111 *b);
/* Per scan, caller holds b->pcdev.lock: log the crossing of temp1_max, optionally shut the chip at temp_shed_c (#179) */
static void bcm_temp_check(struct bcm59111 *b, int raw)
{
	long t;
	int max = bcm_temp_max(b), id;

	if (max < 0 || raw < TEMP_RAW_MIN)
		return;
	t = (TEMP_OFS_C - 2 * raw) * 1000L;
	if ((t >= max * 1000L) != b->temp_over) {
		b->temp_over = t >= max * 1000L;
		dev_warn(&b->client->dev, "die %ld C %s the stock limit of %d C\n", t / 1000, b->temp_over ? "reached" : "back under", max);
	}
	if (temp_shed_c && t >= temp_shed_c * 1000L) {
		for (id = 0; id < NPORT; id++)
			if (!b->blocked[id] && bcm_pi_disable(&b->pcdev, id) == 0) {
				b->blocked[id] = true;
				dev_err(&b->client->dev, "die %ld C >= temp_shed_c %u: port %d shut and blocked\n", t / 1000, temp_shed_c, id + 1);
			}
	}
}

/* PoE LEDs (#178): one simple trigger per port, fired from the power scan (power-good = field 1 on the PoE LED chain) */
static void bcm_led_update(struct bcm59111 *b, u8 pg)
{
	int id;

	for (id = 0; id < NPORT; id++)
		if (b->poe_trig[id] && ((pg ^ b->led_on) & BIT(id)))
			led_trigger_event(b->poe_trig[id], pg & BIT(id) ? LED_FULL : LED_OFF);
	b->led_on = pg;
}

static void bcm_led_remove(void *data)
{
	struct bcm59111 *b = data;
	int id;

	for (id = 0; id < NPORT; id++)
		if (b->poe_trig[id]) {
			led_trigger_event(b->poe_trig[id], LED_OFF);
			led_trigger_unregister_simple(b->poe_trig[id]);
			b->poe_trig[id] = NULL;
		}
}

static int bcm_led_add(struct bcm59111 *b)
{
	int id, chip = bcm_chip_no(b);

	if (chip < 0)
		return 0;
	for (id = 0; id < NPORT; id++) {
		snprintf(b->poe_trig_name[id], sizeof(b->poe_trig_name[id]), "ge%d-poe-good", 4 * chip + id + 1);
		led_trigger_register_simple(b->poe_trig_name[id], &b->poe_trig[id]);
	}
	return devm_add_action_or_reset(&b->client->dev, bcm_led_remove, b);
}

static void bcm_watch(struct work_struct *w)
{
	unsigned int total = 0;
	int i, id;

	mutex_lock(&chips_lock);
	for (i = 0; i < ARRAY_SIZE(chips); i++) {
		struct bcm59111 *b = chips[i];
		u8 sc[SCAN_LEN];
		int pwr, rc;

		if (!b)
			continue;
		mutex_lock(&b->pcdev.lock);
		/* one transfer per chip: STAT_PWR, TEMP and CM/VM of all 4 channels (was 2 + 4 reads per powered port) */
		rc = rd_blk(b, SCAN_BASE, sc, SCAN_LEN);
		pwr = rc ? rc : sc[REG_STAT_PWR - SCAN_BASE];
		if (!rc) {
			bcm_led_update(b, (pwr >> 4) & 0xf);
			bcm_temp_check(b, sc[REG_TEMP - SCAN_BASE]);
		}
		for (id = 0; id < NPORT; id++) {
			int mw = -1;

			if (!rc && (pwr & BIT(id + 4)))
				mw = mw_of(sc[REG_VM(id) - SCAN_BASE] | sc[REG_VM(id) + 1 - SCAN_BASE] << 8,
					   sc[REG_CM(id) - SCAN_BASE] | sc[REG_CM(id) + 1 - SCAN_BASE] << 8);

			if (mw < 0) {
				b->pw_mw[id] = 0;
				b->pw_since_ms[id] = pwr < 0 ? b->pw_since_ms[id] : 0;	/* read failure: keep the age */
				continue;
			}
			if (!b->pw_since_ms[id])
				b->pw_since_ms[id] = ktime_to_ms(ktime_get());
			b->pw_mw[id] = mw;
			total += mw;
		}
		mutex_unlock(&b->pcdev.lock);
	}
	total_mw = total;
	while (power_limit_mw && total > power_limit_mw) {
		struct bcm59111 *nb = NULL;
		int nid = 0;
		u64 newest = 0;

		for (i = 0; i < ARRAY_SIZE(chips); i++)
			for (id = 0; chips[i] && id < NPORT; id++)
				if (chips[i]->pw_since_ms[id] && chips[i]->pw_since_ms[id] >= newest) {
					newest = chips[i]->pw_since_ms[id];	/* ties: the last in scan order = highest front port */
					nb = chips[i];
					nid = id;
				}
		if (!nb)
			break;
		mutex_lock(&nb->pcdev.lock);
		bcm_pi_disable(&nb->pcdev, nid);
		mutex_unlock(&nb->pcdev.lock);
		nb->blocked[nid] = true;
		dev_warn(&nb->client->dev, "over budget (%u mW > %u mW): port %d (%s) shut and blocked, was %u mW; enable it by hand to retry\n",
			 total, power_limit_mw, nid + 1, nb->label ?: "?", nb->pw_mw[nid]);
		total -= min(total, nb->pw_mw[nid]);
		nb->pw_mw[nid] = 0;
		nb->pw_since_ms[nid] = 0;
		total_mw = total;
	}
	mutex_unlock(&chips_lock);
	schedule_delayed_work(&watch_work, msecs_to_jiffies(power_poll_ms));
}

static void bcm_watch_remove(void *data)
{
	struct bcm59111 *b = data;
	bool last = true;
	int i;

	mutex_lock(&chips_lock);
	for (i = 0; i < ARRAY_SIZE(chips); i++) {
		if (chips[i] == b)
			chips[i] = NULL;
		last &= !chips[i];
	}
	mutex_unlock(&chips_lock);
	if (last)
		cancel_delayed_work_sync(&watch_work);
}

static int bcm_watch_add(struct bcm59111 *b)
{
	bool first = true;
	int i, slot = -1;

	if (!power_poll_ms || !power_limit_mw)
		return 0;
	mutex_lock(&chips_lock);
	for (i = 0; i < ARRAY_SIZE(chips); i++) {
		if (chips[i])
			first = false;
		else if (slot < 0)
			slot = i;
	}
	if (slot >= 0)
		chips[slot] = b;
	mutex_unlock(&chips_lock);
	if (slot < 0) {
		dev_warn(&b->client->dev, "power watcher: more than 12 chips, this one is not watched\n");
		return 0;
	}
	if (first) {
		INIT_DELAYED_WORK(&watch_work, bcm_watch);
		schedule_delayed_work(&watch_work, msecs_to_jiffies(power_poll_ms));
	}
	return devm_add_action_or_reset(&b->client->dev, bcm_watch_remove, b);
}

/* Port bits of enable_ports for this chip: label PoE-NN = chip NN, front ports 4(NN-1)+1..4(NN-1)+4 */
static unsigned int bcm_chip_mask(struct bcm59111 *b, unsigned long long ports, const char *what)
{
	unsigned int n;

	if (!ports)
		return 0;
	if (!b->label || strncmp(b->label, "PoE-", 4) || kstrtouint(b->label + 4, 10, &n) || n < 1 || n > 12) {
		dev_warn(&b->client->dev, "%s ignored: label is not PoE-NN\n", what);
		return 0;
	}
	return (ports >> (4 * (n - 1))) & 0xf;
}

/* ---- firmware + bring-up ---- */

/* Chip index 0..11 from the label PoE-NN, -EINVAL without one */
static int bcm_chip_no(struct bcm59111 *b)
{
	unsigned int n;

	if (!b->label || strncmp(b->label, "PoE-", 4) || kstrtouint(b->label + 4, 10, &n) || n < 1 || n > 12)
		return -EINVAL;
	return n - 1;
}

static int bcm_fw_get(struct bcm59111 *b, const struct firmware **fw)
{
	struct device *dev = &b->client->dev;
	int ret = request_firmware(fw, b->fw_name, dev);

	if (ret)
		return dev_err_probe(dev, ret, "firmware %s\n", b->fw_name);
	if ((*fw)->size != FW_SIZE) {
		dev_err(dev, "firmware %s is %zu bytes, expected %u\n", b->fw_name, (*fw)->size, FW_SIZE);
		release_firmware(*fw);
		return -EINVAL;
	}
	return 0;
}

/* Wait for reg 0x75 == 0xaa (CRC ok, firmware running): poll FW_POLL_MS, limit FW_WAIT_MS, expiry logs the register */
static int bcm_fw_wait(struct bcm59111 *b)
{
	unsigned int waited = 0;
	int crc;

	for (;;) {
		crc = rd(b, REG_CRC);
		if (crc == CRC_RUNNING)
			return 0;
		if (waited >= FW_WAIT_MS)
			break;
		msleep(FW_POLL_MS);
		waited += FW_POLL_MS;
	}
	dev_err(&b->client->dev, "firmware not running after %u ms: reg 0x75 = %d (limit %u ms)\n", waited, crc, FW_WAIT_MS);
	return -ETIMEDOUT;
}

/*
 * Broadcast download as stock (`0x1087ad0`, docs/poe.md 4): PCA9548 channels 4+5 together (0x30), the same 512 blocks to
 * address 0x30, then 0x70 <- 0x40. All chips behind the mux get the image at once. Runs on the root adapter under its
 * root lock, so no other transfer can change the mux selection in between; the mux is deselected at the end.
 * The chips are reset first (fw_reset or cold boot); each chip's probe then waits for its own 0x75 (bcm_pse_init).
 */
#define MUX_ADDR	0x70
#define MUX_BOTH_CARDS	0x30
#define BCAST_ADDR	0x30
static unsigned long bcast_pending;	/* chips (bit n = PoE-(n+1)) loaded by the broadcast, not yet probed; bcast_lock */
static bool bcast_tried;
static DEFINE_MUTEX(bcast_lock);

static int bcm_fw_load_bcast(struct bcm59111 *b)
{
	struct device *dev = &b->client->dev;
	struct i2c_adapter *root = i2c_parent_is_i2c_adapter(b->client->adapter);
	const struct firmware *fw;
	u8 blk[4 + FW_BLOCK] = { REG_FW_DWN, FW_START };
	u8 sel = MUX_BOTH_CARDS, stop[2] = { REG_FW_DWN, FW_FORCE_CRC }, off0 = 0;
	struct i2c_msg m = { .addr = MUX_ADDR, .flags = 0, .len = 1, .buf = &sel };
	unsigned int off;
	int ret;

	if (!root || !i2c_check_functionality(root, I2C_FUNC_I2C))
		return dev_err_probe(dev, -EOPNOTSUPP, "no root adapter for the broadcast load\n");
	ret = bcm_fw_get(b, &fw);
	if (ret)
		return ret;
	i2c_lock_bus(root, I2C_LOCK_ROOT_ADAPTER);
	ret = __i2c_transfer(root, &m, 1);
	if (ret != 1)
		goto out;
	m.addr = BCAST_ADDR;
	m.len = sizeof(blk);
	m.buf = blk;
	for (off = 0; off < FW_SIZE; off += FW_BLOCK) {
		blk[2] = off >> 8;
		blk[3] = off;
		memcpy(blk + 4, fw->data + off, FW_BLOCK);
		ret = __i2c_transfer(root, &m, 1);
		if (ret != 1) {
			dev_err(dev, "broadcast block at 0x%04x: %d\n", off, ret);
			break;
		}
	}
	if (ret == 1) {
		m.len = sizeof(stop);
		m.buf = stop;
		ret = __i2c_transfer(root, &m, 1);
	}
out:
	m.addr = MUX_ADDR;	/* deselect, also after a failure */
	m.len = 1;
	m.buf = &off0;
	__i2c_transfer(root, &m, 1);
	i2c_unlock_bus(root, I2C_LOCK_ROOT_ADAPTER);
	release_firmware(fw);
	if (ret != 1)
		return ret < 0 ? ret : -EIO;
	return 0;
}

#ifndef RST_PULSE_MS
#define RST_PULSE_MS	10
#define RST_SETTLE_MS	1000
#endif
/*
 * Reset of all 12 chips from the probe of PoE-01 before a broadcast load: a partly loaded state (awto-uboot stopped half way,
 * some chips running) must not receive a download (stock resets the whole bus first, poe.md 4). Same line and delays as fw_reset.
 */
static int bcm_chip_reset(struct bcm59111 *b)
{
	struct gpio_desc *g = gpiod_get(&b->client->dev, "reset", GPIOD_ASIS);
	int ret;

	if (IS_ERR(g))
		return PTR_ERR(g);
	ret = gpiod_direction_output(g, 1);	/* logical 1 = asserted = low */
	if (!ret) {
		msleep(RST_PULSE_MS);
		gpiod_direction_output(g, 0);
		gpiod_direction_input(g);	/* pulled up: released */
		msleep(RST_SETTLE_MS);
	}
	gpiod_put(g);
	return ret;
}

/* Stock 0x1087ad0 / UDM format: raw write [0x70][0x80][off BE16][32 data] x 512, then 0x70 <- 0x40, then 0x75 == 0xaa. */
static int bcm_fw_load(struct bcm59111 *b)
{
	struct device *dev = &b->client->dev;
	const struct firmware *fw;
	u8 blk[4 + FW_BLOCK] = { REG_FW_DWN, FW_START };
	unsigned int off, waited = 0;
	int ret, crc;

	if (!i2c_check_functionality(b->client->adapter, I2C_FUNC_I2C))
		return dev_err_probe(dev, -EOPNOTSUPP, "adapter cannot send raw I2C blocks\n");
	ret = request_firmware(&fw, b->fw_name, dev);
	if (ret)
		return dev_err_probe(dev, ret, "firmware %s\n", b->fw_name);
	if (fw->size != FW_SIZE) {
		dev_err(dev, "firmware %s is %zu bytes, expected %u\n", b->fw_name, fw->size, FW_SIZE);
		release_firmware(fw);
		return -EINVAL;
	}
	for (off = 0; off < FW_SIZE; off += FW_BLOCK) {
		blk[2] = off >> 8;
		blk[3] = off;
		memcpy(blk + 4, fw->data + off, FW_BLOCK);
		ret = i2c_master_send(b->client, blk, sizeof(blk));
		if (ret != sizeof(blk)) {
			dev_err(dev, "firmware block at 0x%04x: %d\n", off, ret);
			release_firmware(fw);
			return ret < 0 ? ret : -EIO;
		}
	}
	release_firmware(fw);
	ret = wr(b, REG_FW_DWN, FW_FORCE_CRC);
	if (ret)
		return ret;
	for (;;) {
		crc = rd(b, REG_CRC);
		if (crc == CRC_RUNNING)
			return 0;
		if (waited >= FW_WAIT_MS)
			break;
		msleep(FW_POLL_MS);
		waited += FW_POLL_MS;
	}
	dev_err(dev, "firmware not running after %u ms: reg 0x75 = %d (limit %u ms)\n", waited, crc, FW_WAIT_MS);
	return -ETIMEDOUT;
}

/* Stock runtime defaults (docs/poe.md 4 step 11, 11 step 6), only after a firmware load by this driver; ports stay shut. */
static int bcm_cold_config(struct bcm59111 *b)
{
	int c, ret = wr(b, REG_DIS_SENSE, 0x0f);

	ret = ret ?: wr(b, REG_DET_CLASS, 0xff);
	ret = ret ?: wr(b, REG_HP_EN, 0x0f);
	for (c = 0; c < NPORT && !ret; c++) {
		ret = wr(b, REG_HP_MODE(c), 0x01);
		ret = ret ?: wr(b, REG_ICUT(c), 0xe2);		/* 638 mA, stock default cfg */
		ret = ret ?: wr(b, REG_ILIM(c), 0xc0);
	}
	return ret ?: wr(b, REG_INT_MASK, CFG_MARK);	/* last: "configured" marker, also written by awto-uboot (poe.c) */
}

/*
 * Staggered boot enable (#165): one port per enable_stagger_ms, front-port order, across all chips. Before each port the
 * measured total (bcm_watch, power_limit_mw) is checked: at or over the limit the remaining ports are blocked, not enabled
 * (an unmanaged all-at-once start of 48 ports can exceed the PSU before the first watcher scan).
 */
static struct bcm59111 *stag_chips[12];
static DEFINE_MUTEX(stag_lock);
static struct delayed_work stag_work;

static void bcm_stagger(struct work_struct *w)
{
	struct bcm59111 *b = NULL;
	int i, port = 0, ret;
	bool more = false;

	mutex_lock(&stag_lock);
	for (i = 0; i < ARRAY_SIZE(stag_chips) && !b; i++)
		if (stag_chips[i] && stag_chips[i]->stag_pending) {
			b = stag_chips[i];
			port = __ffs(b->stag_pending);
		}
	if (b) {
		b->stag_pending &= ~BIT(port);
		if (power_limit_mw && total_mw >= power_limit_mw) {
			dev_warn(&b->client->dev, "boot enable: measured %u mW >= budget %u mW, port %d and the rest stay blocked\n",
				 total_mw, power_limit_mw, port + 1);
			for (i = 0; i < ARRAY_SIZE(stag_chips); i++) {
				struct bcm59111 *o = stag_chips[i];
				int p;

				for (p = 0; o && p < NPORT; p++)
					if (o->stag_pending & BIT(p) || (o == b && p == port))
						o->blocked[p] = true;
				if (o)
					o->stag_pending = 0;
			}
		} else {
			mutex_lock(&b->pcdev.lock);
			ret = bcm_pi_enable(&b->pcdev, port);
			mutex_unlock(&b->pcdev.lock);
			if (ret)
				dev_warn(&b->client->dev, "enable_ports: port %d not enabled: %d\n", port + 1, ret);
		}
	}
	for (i = 0; i < ARRAY_SIZE(stag_chips); i++)
		more |= stag_chips[i] && stag_chips[i]->stag_pending;
	mutex_unlock(&stag_lock);
	if (more)
		schedule_delayed_work(&stag_work, msecs_to_jiffies(enable_stagger_ms));
}

static void bcm_stagger_remove(void *data)
{
	struct bcm59111 *b = data;
	int i;

	mutex_lock(&stag_lock);
	b->stag_pending = 0;
	for (i = 0; i < ARRAY_SIZE(stag_chips); i++)
		if (stag_chips[i] == b)
			stag_chips[i] = NULL;
	mutex_unlock(&stag_lock);
}

/* Queue the chip's enable_ports bits; first chip starts the worker. 0 = queued, -ENOSPC = no slot (caller enables at once) */
static int bcm_stagger_add(struct bcm59111 *b, unsigned int mask)
{
	int i, slot = -1;
	bool idle = true;

	mutex_lock(&stag_lock);
	for (i = 0; i < ARRAY_SIZE(stag_chips); i++) {
		if (stag_chips[i] && stag_chips[i]->stag_pending)
			idle = false;
		if (!stag_chips[i] && slot < 0)
			slot = i;
	}
	if (slot >= 0) {
		stag_chips[slot] = b;
		b->stag_pending = mask;
	}
	mutex_unlock(&stag_lock);
	if (slot < 0)
		return -ENOSPC;
	if (idle)
		schedule_delayed_work(&stag_work, msecs_to_jiffies(enable_stagger_ms));
	return devm_add_action_or_reset(&b->client->dev, bcm_stagger_remove, b);
}

/* The PSE half is optional: a chip that does not answer (reset, no firmware) keeps its hwmon device and no ports. */
static int bcm_pse_init(struct bcm59111 *b)
{
	struct device *dev = &b->client->dev;
	bool cold = false;
	unsigned int mask;
	int id, port, crc, rev, ret, chip;

	id = rd(b, REG_DEV_ID);
	if (id < 0) {
		dev_warn(dev, "no answer (%d): chip in reset or without firmware, no PSE registered\n", id);
		return 0;
	}
	if ((id & 0xe0) != DEV_ID_59111) {
		dev_err(dev, "device id 0x%02x is not a BCM59111, no PSE registered\n", id);
		return 0;
	}
	crc = rd(b, REG_CRC);
	chip = bcm_chip_no(b);
	mutex_lock(&bcast_lock);
	if (chip >= 0 && test_and_clear_bit(chip, &bcast_pending)) {
		/* loaded by the broadcast of another chip's probe: wait for our own CRC, configure as after our own load */
		ret = bcm_fw_wait(b);
		if (ret) {
			mutex_unlock(&bcast_lock);
			return 0;
		}
		cold = true;
	} else if (crc != CRC_RUNNING) {
		if (!fw_load) {
			mutex_unlock(&bcast_lock);
			dev_warn(dev, "firmware not running (reg 0x75 = %d); fw_load=0, no PSE registered\n", crc);
			return 0;
		}
		if (fw_bcast && !fw_override && chip >= 0 && !bcast_tried) {
			bcast_tried = true;
			if (chip == 0 && device_property_present(dev, "reset-gpios")) {
				ret = bcm_chip_reset(b);
				if (ret)
					dev_warn(dev, "chip reset before the broadcast load failed (%d), loading without it\n", ret);
			}
			ret = bcm_fw_load_bcast(b);
			if (ret) {
				mutex_unlock(&bcast_lock);
				dev_err(dev, "broadcast firmware load failed (%d), no PSE registered (fw_bcast=0 loads per chip)\n", ret);
				return 0;
			}
			bcast_pending = GENMASK(NCHIP_MAX - 1, 0) & ~BIT(chip);
			ret = bcm_fw_wait(b);
		} else {
			ret = bcm_fw_load(b);
		}
		if (ret) {
			mutex_unlock(&bcast_lock);
			return 0;
		}
		cold = true;
	}
	mutex_unlock(&bcast_lock);
	if (!cold && rd(b, REG_INT_MASK) != CFG_MARK) {
		/* firmware running but never configured by us, awto-uboot or stock (e.g. awto-uboot's load stopped half way) */
		dev_info(dev, "chip not configured (reg 0x01 = %d): applying the cold config\n", rd(b, REG_INT_MASK));
		cold = true;
	}
	rev = rd(b, REG_FW_REV);
	/* ports off before anything else: stock "swctrl poe set off" + us48-poe-off, verified by read back (#24) */
	ret = wr(b, REG_OP_MODE, 0);
	if (!ret)
		ret = rd(b, REG_OP_MODE);
	if (ret) {
		dev_err(dev, "ports not shut down (reg 0x12 = %d), no PSE registered\n", ret);
		return 0;
	}
	if (cold && bcm_cold_config(b))
		dev_warn(dev, "cold-start configuration failed, chip left as is\n");
	dev_info(dev, "BCM59111 firmware rev 0x%02x, 4 ports shut down%s\n", rev, cold ? ", firmware loaded" : "");

	b->pcdev.owner = THIS_MODULE;
	b->pcdev.ops = &bcm_pse_ops;
	b->pcdev.dev = dev;
	b->pcdev.types = ETHTOOL_PSE_C33;
	b->pcdev.nr_lines = NPORT;
	ret = devm_pse_controller_register(dev, &b->pcdev);
	if (ret)
		return dev_err_probe(dev, ret, "pse register\n");
	b->fw_ok = true;
	ret = bcm_led_add(b);
	if (ret)
		return ret;
	ret = devm_add_action_or_reset(dev, bcm_budget_release, b);
	if (ret)
		return ret;
	ret = devm_add_action_or_reset(dev, bcm_ports_off, b);
	if (ret)
		return ret;
	ret = devm_device_add_group(dev, &bcm_group);
	if (ret)
		return ret;
	ret = bcm_hwd_start(b);
	if (ret)
		return ret;
	if (bcm_chip_mask(b, passive_ports, "passive_ports"))
		dev_warn(dev, "passive_ports: ports allowed to switch to passive 24 V through portN_mode (none switched at probe)\n");
	mask = bcm_chip_mask(b, enable_ports, "enable_ports");
	if (mask && enable_stagger_ms && !bcm_stagger_add(b, mask))
		mask = 0;	/* the stagger worker enables them */
	for (port = 0; port < NPORT; port++) {
		if (!(mask & BIT(port)))
			continue;
		mutex_lock(&b->pcdev.lock);
		ret = bcm_pi_enable(&b->pcdev, port);
		mutex_unlock(&b->pcdev.lock);
		if (ret)
			dev_warn(dev, "enable_ports: port %d not enabled: %d\n", port + 1, ret);
	}
	ret = bcm_watch_add(b);
	if (ret)
		return ret;
	if (poll_ms) {
		INIT_DELAYED_WORK(&b->poll, bcm_poll);
		ret = devm_add_action_or_reset(dev, bcm_poll_stop, &b->poll);
		if (ret)
			return ret;
		schedule_delayed_work(&b->poll, msecs_to_jiffies(poll_ms));
	}
	return 0;
}

static int bcm_probe(struct i2c_client *client)
{
	struct device *dev = &client->dev;
	struct bcm59111 *b;
	struct device *hw;

	if (!i2c_check_functionality(client->adapter, I2C_FUNC_SMBUS_BYTE_DATA))
		return -EOPNOTSUPP;
	b = devm_kzalloc(dev, sizeof(*b), GFP_KERNEL);
	if (!b)
		return -ENOMEM;
	b->client = client;
	b->reserve_mw = RESERVE_DEFAULT_MW;
	b->fw_name = "bcm59111-v21.bin";
	device_property_read_string(dev, "label", &b->label);
	device_property_read_string(dev, "firmware-name", &b->fw_name);
	if (fw_override && b->label) {
		const char *e = fw_override;
		size_t n = strlen(b->label);

		/* "PoE-NN=<file>[,PoE-MM=<file>...]" */
		while (e && *e) {
			if (!strncmp(e, b->label, n) && e[n] == '=') {
				const char *f = e + n + 1, *end = strchr(f, ',');

				b->fw_name = end ? kstrndup(f, end - f, GFP_KERNEL) : f;
				dev_warn(dev, "fw_override: loading %s instead of the default image\n", b->fw_name);
				break;
			}
			e = strchr(e, ',');
			if (e)
				e++;
		}
	}
	device_property_read_u32(dev, "brcm,port-reserve-milliwatt", &b->reserve_mw);
	if (reserve_mw)
		b->reserve_mw = reserve_mw;
	dev_set_drvdata(dev, b);
	hw = devm_hwmon_device_register_with_info(dev, "bcm59111", b, &bcm_chip, NULL);
	if (IS_ERR(hw))
		return PTR_ERR(hw);
	return bcm_pse_init(b);
}

static const struct of_device_id bcm_of_match[] = {
	{ .compatible = "brcm,bcm59111" },
	{ }
};
MODULE_DEVICE_TABLE(of, bcm_of_match);

static const struct i2c_device_id bcm_ids[] = {
	{ "bcm59111" },
	{ }
};
MODULE_DEVICE_TABLE(i2c, bcm_ids);

static struct i2c_driver bcm_driver;

/*
 * Chip reset line (CCA GPIO 9, shared by all 12 chips, active low, bench 2026-11-23: low = firmware gone, reg 0x75 0xaa -> 0):
 * fw_reset = off (all drivers unbound = ports shut, line held low, firmware gone), on (release, wait, drivers rebind = firmware
 * load as fw_load says), pulse (off, 10 ms, on). Delays are stock's host path (docs/poe.md 8): 10 ms pulse, 1 s before the load.
 */
#define NCHIP		12
static DEFINE_MUTEX(rst_lock);
static struct gpio_desc *rst_gpio;		/* held asserted while the firmware is off */
static struct device *rst_devs[NCHIP];
static int rst_n;

static int rst_collect(struct device *dev, void *data)
{
	if (rst_n < NCHIP)
		rst_devs[rst_n++] = get_device(dev);
	return 0;
}

static int rst_off_locked(void)
{
	struct gpio_desc *g = NULL;
	int i, ret = 0;

	if (rst_gpio)
		return -EBUSY;	/* already off */
	ret = driver_for_each_device(&bcm_driver.driver, NULL, NULL, rst_collect);
	if (ret)
		return ret;
	if (!rst_n) {
		pr_err("bcm59111: fw_reset: no chip is bound to the driver (probe not finished or none present), nothing done\n");
		return -ENODEV;
	}
	for (i = 0; i < rst_n; i++)
		device_release_driver(rst_devs[i]);	/* unbind: ports shut (bcm_ports_off), budget released */
	for (i = 0; i < rst_n && !g; i++) {
		if (!device_property_present(rst_devs[i], "reset-gpios"))
			continue;
		g = gpiod_get(rst_devs[i], "reset", GPIOD_ASIS);
		if (IS_ERR(g)) {
			ret = PTR_ERR(g);
			g = NULL;
		}
	}
	if (!g) {
		pr_err("bcm59111: no reset-gpios on any chip (%d), chips unbound but not reset\n", ret);
		return ret ?: -ENODEV;
	}
	ret = gpiod_direction_output(g, 1);	/* logical 1 = asserted = low */
	if (ret) {
		gpiod_put(g);
		return ret;
	}
	rst_gpio = g;
	mutex_lock(&bcast_lock);
	bcast_pending = 0;
	bcast_tried = false;
	mutex_unlock(&bcast_lock);
	pr_info("bcm59111: reset asserted, %d chips unbound, firmware off\n", rst_n);
	return 0;
}

static int rst_on_locked(void)
{
	int i;

	if (!rst_gpio)
		return -EINVAL;	/* not off */
	gpiod_direction_output(rst_gpio, 0);
	gpiod_direction_input(rst_gpio);	/* pulled up: released */
	gpiod_put(rst_gpio);
	rst_gpio = NULL;
	msleep(RST_SETTLE_MS);
	for (i = 0; i < rst_n; i++) {
		if (device_attach(rst_devs[i]) < 0)	/* probe loads the firmware (fw_load=1) */
			pr_err("bcm59111: rebind of chip %d failed\n", i);
		put_device(rst_devs[i]);
	}
	pr_info("bcm59111: reset released, %d chips rebound\n", rst_n);
	rst_n = 0;
	return 0;
}

static int fw_reset_set(const char *val, const struct kernel_param *kp)
{
	int ret;

	mutex_lock(&rst_lock);
	if (sysfs_streq(val, "off")) {
		ret = rst_off_locked();
	} else if (sysfs_streq(val, "on")) {
		ret = rst_on_locked();
	} else if (sysfs_streq(val, "pulse")) {
		ret = rst_off_locked();
		if (!ret) {
			msleep(RST_PULSE_MS);
			ret = rst_on_locked();
		}
	} else {
		ret = -EINVAL;
	}
	mutex_unlock(&rst_lock);
	return ret;
}

static const struct kernel_param_ops fw_reset_ops = { .set = fw_reset_set };
module_param_cb(fw_reset, &fw_reset_ops, NULL, 0200);
MODULE_PARM_DESC(fw_reset, "write-only: off = unbind all chips, hold the reset line (firmware gone, PoE off); on = release + rebind (loads firmware); pulse = off, 10 ms, on");

/*
 * Firmware guard (#175, stock docs/poe.md 4 step 10): every fw_check_ms read reg 0x75 of each chip with a running PSE.
 * Not 0xaa (or no answer) = the chip lost its firmware (brown-out, reset): the same reset + broadcast reload as fw_reset=pulse,
 * up to FW_RECOVER_TRIES times. Ports enabled only through sysfs are lost with the rebind; enable_ports applies again.
 */
#define FW_RECOVER_TRIES	3	/* stock: 3 more attempts (poe.md 4 step 7) */
static struct delayed_work fw_guard_work;
static struct work_struct fw_recover_work;
static atomic_t fw_lost = ATOMIC_INIT(0);

static int fw_guard_chip(struct device *dev, void *data)
{
	struct bcm59111 *b;
	int crc;
	bool bad = false;

	device_lock(dev);
	if (dev->driver == &bcm_driver.driver && (b = dev_get_drvdata(dev)) && b->fw_ok) {
		mutex_lock(&b->pcdev.lock);
		crc = rd(b, REG_CRC);
		mutex_unlock(&b->pcdev.lock);
		bad = crc != CRC_RUNNING;
		if (bad)
			dev_err(dev, "firmware lost: reg 0x75 = %d\n", crc);
	}
	device_unlock(dev);
	if (bad)
		atomic_inc(&fw_lost);
	return 0;
}

static int fw_all_running(struct device *dev, void *data)
{
	int *bad = data;
	struct bcm59111 *b;

	device_lock(dev);
	if (dev->driver != &bcm_driver.driver || !(b = dev_get_drvdata(dev)) || !b->fw_ok)
		(*bad)++;
	device_unlock(dev);
	return 0;
}

static void fw_recover(struct work_struct *w)
{
	int tries, bad = 1, ret, chips_bad;

	for (tries = 1; tries <= FW_RECOVER_TRIES && bad; tries++) {
		pr_err("bcm59111: firmware lost on a chip: reset + reload of all chips, attempt %d of %d\n", tries, FW_RECOVER_TRIES);
		mutex_lock(&rst_lock);
		ret = rst_off_locked();
		if (!ret) {
			msleep(RST_PULSE_MS);
			ret = rst_on_locked();
		}
		mutex_unlock(&rst_lock);
		bad = ret;
		chips_bad = 0;
		if (!bad)
			bad = driver_for_each_device(&bcm_driver.driver, NULL, &chips_bad, fw_all_running) ?: chips_bad;
	}
	if (bad)
		pr_err("bcm59111: firmware recovery failed after %d attempts, chips left as they are (fw_reset=pulse to retry)\n", FW_RECOVER_TRIES);
	else
		pr_info("bcm59111: firmware recovered, all chips running\n");
}

static void fw_guard(struct work_struct *w)
{
	int ret;

	/* no check while a recovery runs: chips are unbound or loading, the recovery judges the result itself */
	if (!work_busy(&fw_recover_work)) {
		atomic_set(&fw_lost, 0);
		ret = driver_for_each_device(&bcm_driver.driver, NULL, NULL, fw_guard_chip);
		if (!ret && atomic_read(&fw_lost))
			schedule_work(&fw_recover_work);
	}
	schedule_delayed_work(&fw_guard_work, msecs_to_jiffies(fw_check_ms));
}

static struct i2c_driver bcm_driver = {
	.driver = { .name = "bcm59111", .of_match_table = bcm_of_match },
	.probe = bcm_probe,
	.id_table = bcm_ids,
};

static int __init bcm_init(void)
{
	int ret;

	INIT_DELAYED_WORK(&stag_work, bcm_stagger);
	ret = i2c_add_driver(&bcm_driver);
	if (!ret && fw_check_ms) {
		INIT_WORK(&fw_recover_work, fw_recover);
		INIT_DELAYED_WORK(&fw_guard_work, fw_guard);
		schedule_delayed_work(&fw_guard_work, msecs_to_jiffies(fw_check_ms));
	}
	return ret;
}

static void __exit bcm_exit(void)
{
	int i;

	cancel_delayed_work_sync(&stag_work);
	if (fw_check_ms) {
		cancel_delayed_work_sync(&fw_guard_work);
		cancel_work_sync(&fw_recover_work);
	}
	mutex_lock(&rst_lock);
	if (rst_gpio) {		/* unloaded while off: the line is released, chips come up without firmware */
		gpiod_direction_output(rst_gpio, 0);
		gpiod_direction_input(rst_gpio);
		gpiod_put(rst_gpio);
		rst_gpio = NULL;
	}
	for (i = 0; i < rst_n; i++)
		put_device(rst_devs[i]);
	rst_n = 0;
	mutex_unlock(&rst_lock);
	i2c_del_driver(&bcm_driver);
}
module_init(bcm_init);
module_exit(bcm_exit);

MODULE_DESCRIPTION("Broadcom BCM59111 PoE PSE controller: pse-pd provider and die temperature");
MODULE_LICENSE("GPL");
