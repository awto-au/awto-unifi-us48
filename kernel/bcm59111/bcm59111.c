// SPDX-License-Identifier: GPL-2.0
/*
 * bcm59111: Broadcom BCM59111 4-port PoE PSE as a pse-pd provider (c33) + die temperature hwmon. Design, safety and
 * register evidence: docs/poe-pse.md, docs/poe.md. Reference drivers: Ubiquiti UDM bcm59111.c (GPL), quenode/mx65w-poe-openwrt.
 * Every port is shut down at probe and stays off until an explicit admin enable. Event registers 0x00/0x03/0x07 are never read.
 */
#include <linux/delay.h>
#include <linux/firmware.h>
#include <linux/hwmon.h>
#include <linux/hwmon-sysfs.h>
#include <linux/i2c.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/pse-pd/pse.h>
#include <linux/property.h>
#include <linux/regulator/consumer.h>
#include <linux/regulator/driver.h>

#define NPORT		4
#define REG_STAT_P(c)	(0x0c + (c))		/* [3:0] detect, [6:4] class */
#define REG_STAT_PWR	0x10			/* [3:0] power enable, [7:4] power good (stock naming) */
#define REG_OP_MODE	0x12			/* 2 bits per channel, channel 0 = bits 1:0 */
#define REG_DIS_SENSE	0x13
#define REG_DET_CLASS	0x14			/* [3:0] detect enable, [7:4] class enable */
#define REG_TEMP	0x2c
#define REG_CM(c)	(0x30 + 4 * (c))	/* current, 16 bit, LSB first */
#define REG_VM(c)	(0x32 + 4 * (c))	/* voltage, 16 bit, LSB first */
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

static unsigned long long passive_ports;
module_param(passive_ports, ullong, 0444);
MODULE_PARM_DESC(passive_ports, "UNVERIFIED (M1/M6): 24 V passive front ports; NOT IMPLEMENTED (needs MSPI field 1 in us48_boardio), non-zero only warns");

struct bcm59111 {
	struct i2c_client *client;
	const char *label;
	const char *fw_name;
	struct pse_controller_dev pcdev;
	u32 reserve_mw;
	bool reserved[NPORT];
	bool taken[NPORT];		/* counted in budget_used */
	u8 pg;				/* last power-good nibble seen by the poll */
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

static int bcm_pi_get_actual_pw(struct pse_controller_dev *pcdev, int id)
{
	struct bcm59111 *b = to_bcm(pcdev);
	int pwr = rd(b, REG_STAT_PWR), vm, cm;

	if (pwr < 0)
		return pwr;
	if (!(pwr & BIT(id + 4)))
		return 0;
	vm = rd16(b, REG_VM(id));
	cm = rd16(b, REG_CM(id));
	if (vm < 0 || cm < 0)
		return vm < 0 ? vm : cm;
	/* mW = (vm * 5.835 mV) * (cm * 122.07 uA) */
	return div64_u64((u64)vm * UV_PER_VM * cm * NA_PER_CM, 1000000ULL * 1000000ULL);
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
	if (ret) {
		budget_give(b, id);
		if (took) {
			regulator_free_power_budget(supply, b->reserve_mw);
			b->reserved[id] = false;
		}
	}
	return ret;
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
			  st[min_t(u32, ps.c33_pw_status, ARRAY_SIZE(st) - 1)], cls, uv, mw,
			  b->reserved[id] ? b->reserve_mw : 0);
}

static SENSOR_DEVICE_ATTR_RW(port1_enable, enable, 0);
static SENSOR_DEVICE_ATTR_RW(port2_enable, enable, 1);
static SENSOR_DEVICE_ATTR_RW(port3_enable, enable, 2);
static SENSOR_DEVICE_ATTR_RW(port4_enable, enable, 3);
static SENSOR_DEVICE_ATTR_RO(port1_status, status, 0);
static SENSOR_DEVICE_ATTR_RO(port2_status, status, 1);
static SENSOR_DEVICE_ATTR_RO(port3_status, status, 2);
static SENSOR_DEVICE_ATTR_RO(port4_status, status, 3);

static struct attribute *bcm_attrs[] = {
	&sensor_dev_attr_port1_enable.dev_attr.attr, &sensor_dev_attr_port2_enable.dev_attr.attr,
	&sensor_dev_attr_port3_enable.dev_attr.attr, &sensor_dev_attr_port4_enable.dev_attr.attr,
	&sensor_dev_attr_port1_status.dev_attr.attr, &sensor_dev_attr_port2_status.dev_attr.attr,
	&sensor_dev_attr_port3_status.dev_attr.attr, &sensor_dev_attr_port4_status.dev_attr.attr,
	NULL
};

static const struct attribute_group bcm_group = { .attrs = bcm_attrs };

/* ---- hwmon: die temperature ---- */

static umode_t bcm_is_visible(const void *data, enum hwmon_sensor_types type, u32 attr, int ch)
{
	const struct bcm59111 *b = data;

	if (type != hwmon_temp)
		return 0;
	if (attr == hwmon_temp_input || (attr == hwmon_temp_label && b->label))
		return 0444;
	return 0;
}

static int bcm_read(struct device *dev, enum hwmon_sensor_types type, u32 attr, int ch, long *val)
{
	struct bcm59111 *b = dev_get_drvdata(dev);
	int raw;

	if (type != hwmon_temp || attr != hwmon_temp_input)
		return -EOPNOTSUPP;
	raw = rd(b, REG_TEMP);
	if (raw < 0)
		return raw;
	if (raw < TEMP_RAW_MIN)
		return -ENODATA;
	*val = (TEMP_OFS_C - 2 * raw) * 1000L;
	return 0;
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
	HWMON_CHANNEL_INFO(temp, HWMON_T_INPUT | HWMON_T_LABEL),
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
	return ret;
}

/* The PSE half is optional: a chip that does not answer (reset, no firmware) keeps its hwmon device and no ports. */
static int bcm_pse_init(struct bcm59111 *b)
{
	struct device *dev = &b->client->dev;
	bool cold = false;
	unsigned int mask;
	int id, port, crc, rev, ret;

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
	if (crc != CRC_RUNNING) {
		if (!fw_load) {
			dev_warn(dev, "firmware not running (reg 0x75 = %d); fw_load=0, no PSE registered\n", crc);
			return 0;
		}
		ret = bcm_fw_load(b);
		if (ret)
			return 0;
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
	ret = devm_add_action_or_reset(dev, bcm_budget_release, b);
	if (ret)
		return ret;
	ret = devm_device_add_group(dev, &bcm_group);
	if (ret)
		return ret;
	if (bcm_chip_mask(b, passive_ports, "passive_ports"))
		dev_warn(dev, "passive_ports: not implemented, no port switched to 24 V\n");
	mask = bcm_chip_mask(b, enable_ports, "enable_ports");
	for (port = 0; port < NPORT; port++) {
		if (!(mask & BIT(port)))
			continue;
		mutex_lock(&b->pcdev.lock);
		ret = bcm_pi_enable(&b->pcdev, port);
		mutex_unlock(&b->pcdev.lock);
		if (ret)
			dev_warn(dev, "enable_ports: port %d not enabled: %d\n", port + 1, ret);
	}
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

static struct i2c_driver bcm_driver = {
	.driver = { .name = "bcm59111", .of_match_table = bcm_of_match },
	.probe = bcm_probe,
	.id_table = bcm_ids,
};
module_i2c_driver(bcm_driver);

MODULE_DESCRIPTION("Broadcom BCM59111 PoE PSE controller: pse-pd provider and die temperature");
MODULE_LICENSE("GPL");
