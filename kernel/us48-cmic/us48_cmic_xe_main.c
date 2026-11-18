// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_xe: aux driver (us48_pci.cmic) for the second chip (unit 1, BCM53346 behind PCIe): SFP+ 1/2 (xe49/xe50)
 * plus copper ge25-ge48. Separate module so it can be loaded and unloaded while us48_cmic.ko
 * (core + unit 0 ge1, the management port) keeps running. The two CMICs have separate DMA channels and rings.
 */
#include <linux/auxiliary_bus.h>
#include <linux/firmware.h>
#include <linux/module.h>

#include "us48_cmic.h"
#include "us48_pci.h"

static struct us48_cmic *dump_u;

/* Unit 1 S-channel ops go through the PCI owner's lock, shared with us48_pvtmon (#125). */
static int xe_schan(void *ctx, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	return us48_pci_schan(ctx, op, blk, addr, data, words, ack);
}
static DEFINE_MUTEX(dump_lock);	/* sysfs debug params vs unbind */

static int us48_xe_probe(struct auxiliary_device *adev, const struct auxiliary_device_id *id)
{
	struct us48_pci *p = us48_pci_from_adev(adev);
	const struct firmware *fw = NULL;
	struct us48_cmic *u = NULL;
	int ret;

	ret = us48_pci_link_ok(p);
	if (ret)
		return ret;
	/* Missing microcode is logged by us48_tsc_init; the copper ports still come up. */
	if (request_firmware(&fw, "us48-tsc.bin", &adev->dev))
		fw = NULL;
	/* Every load resets the chip and runs the full cold init; DMA is stopped first. */
	ret = us48_chip_cold_init(&adev->dev, us48_pci_cmic_base(p), xe_schan, p, 1);
	/* devres on the aux device (netdev memory), DMA on the PCI device (mask, window limit) */
	ret = ret ?: us48_probe_common(&adev->dev, us48_pci_dev(p), us48_pci_cmic_base(p), xe_schan, p, us48_unit1_cfgs,
				       us48_unit1_nports, 1, fw, us48_tsc_init, &u);
	release_firmware(fw);
	if (ret)
		return ret;
	auxiliary_set_drvdata(adev, u);
	mutex_lock(&dump_lock);
	dump_u = u;
	mutex_unlock(&dump_lock);
	return 0;
}

/* Unbind (rmmod): netdevs unregistered, DMA stopped, self-checked; devres then frees the rings. */
static void us48_xe_remove(struct auxiliary_device *adev)
{
	struct us48_cmic *u = auxiliary_get_drvdata(adev);

	mutex_lock(&dump_lock);
	dump_u = NULL;
	mutex_unlock(&dump_lock);
	if (u)
		us48_remove_common(u);
}

/* Reboot keeps unit 1 out of reset (PERST# held released, patch 952): a live DMA hangs stock U-Boot's PCIe init. */
static void us48_xe_shutdown(struct auxiliary_device *adev)
{
	struct us48_cmic *u = auxiliary_get_drvdata(adev);

	if (u)
		us48_quiesce(u);
}

/* echo 1 > /sys/module/us48_cmic_xe/parameters/tsc: log TSC + XLMAC status of xe49/xe50 (read-only, #46). */
static int tsc_set(const char *val, const struct kernel_param *kp)
{
	int ret = -ENODEV;

	mutex_lock(&dump_lock);
	if (dump_u) {
		us48_tsc_dump(dump_u);
		ret = 0;
	}
	mutex_unlock(&dump_lock);
	return ret;
}

static const struct kernel_param_ops tsc_ops = { .set = tsc_set };
module_param_cb(tsc, &tsc_ops, NULL, 0200);

/* echo 1 > /sys/module/us48_cmic_xe/parameters/phy_scan: read internal/external bus-0 PHYs (us48_mdio_scan: no config writes). */
static int phy_scan_set(const char *val, const struct kernel_param *kp)
{
	unsigned int scan;
	int ret = kstrtouint(val, 0, &scan);

	if (ret || !scan)
		return ret;
	mutex_lock(&dump_lock);
	if (!dump_u)
		ret = -ENODEV;
	else {
		us48_mdio_scan(dump_u);
		ret = 0;
	}
	mutex_unlock(&dump_lock);
	return ret;
}

static const struct kernel_param_ops phy_scan_ops = { .set = phy_scan_set };
module_param_cb(phy_scan, &phy_scan_ops, NULL, 0200);

/* Run fn on the xe port of that front number under dump_lock (unbind waits for it). */
static int xe_port_op(unsigned int front, u32 lo, u32 hi, int aer, bool restart)
{
	struct us48_cmic *u;
	int i, ret = -EINVAL;

	mutex_lock(&dump_lock);
	u = front == 51 ? us48_unit0() : dump_u;	/* xe51 = unit 0 SFP 1 (us48_cmic sfp10g=1) */
	if (!u || !dump_u)
		ret = -ENODEV;
	for (i = 0; u && dump_u && i < u->nports; i++) {
		if (u->cfgs[i].copper || u->cfgs[i].front != front)
			continue;
		if (restart) {
			ret = us48_tsc_rx_restart(u, &u->cfgs[i]);
		} else {
			us48_tsc_range(u, &u->cfgs[i], lo, hi, aer);
			ret = 0;
		}
	}
	mutex_unlock(&dump_lock);
	return ret;
}

/* echo 49 > /sys/module/us48_cmic_xe/parameters/rx_restart: per-lane RX restart of that front port, then PCS status. */
static int rx_restart_set(const char *val, const struct kernel_param *kp)
{
	unsigned int front;
	int ret = kstrtouint(val, 0, &front);

	return ret ?: xe_port_op(front, 0, 0, -1, true);
}

/* echo "49 c010 c01f [aer]" > .../tscreg: log those TSC registers of front 49/50 (AER = its lane unless given); "49 19200 19212" = devad 1 (PMD). */
static int tscreg_set(const char *val, const struct kernel_param *kp)
{
	unsigned int front, lo, hi;
	int aer = -1, n = sscanf(val, "%u %x %x %i", &front, &lo, &hi, &aer);

	if (n < 3 || hi < lo || hi - lo > 63 || hi > 0x1ffff || aer > 3)
		return -EINVAL;
	return xe_port_op(front, lo, hi, n == 4 ? aer : -1, false);
}

/*
 * echo "50" > .../media_check: SFP hot-plug path without moving a module (#105): re-read A0h, switch FIRMWARE_MODE only on
 * a type change. "50 force": re-apply the detected mode (+ RX restart) as if the type had changed. Result in dmesg.
 */
static int media_check_set(const char *val, const struct kernel_param *kp)
{
	char how[8] = "";
	unsigned int front;
	struct us48_cmic *u;
	int i, n = sscanf(val, "%u %7s", &front, how), ret = -EINVAL;

	if (n < 1 || (n == 2 && strcmp(how, "force")))
		return -EINVAL;
	mutex_lock(&dump_lock);
	u = front == 51 ? us48_unit0() : dump_u;
	if (!u || !dump_u)
		ret = -ENODEV;
	for (i = 0; u && dump_u && i < u->nports; i++) {
		if (u->cfgs[i].copper || u->cfgs[i].front != front || !u->port[i])
			continue;
		mutex_lock(&u->link_lock);
		ret = us48_tsc_media_check(u->port[i], n == 2);
		mutex_unlock(&u->link_lock);
	}
	mutex_unlock(&dump_lock);
	return ret < 0 ? ret : 0;
}

static const struct kernel_param_ops media_check_ops = { .set = media_check_set };
module_param_cb(media_check, &media_check_ops, NULL, 0200);

static const struct kernel_param_ops tscreg_ops = { .set = tscreg_set };
module_param_cb(tscreg, &tscreg_ops, NULL, 0200);

/* echo "<front> <aer> <reg> <mask> <val>" > .../tscwr: RMW one TSC register (bring-up experiments, AER = lane or 0 for core regs). */
static int tscwr_set(const char *val, const struct kernel_param *kp)
{
	unsigned int front, aer, reg, mask, v;
	u16 cur = 0;
	int i, ret = -EINVAL;

	if (sscanf(val, "%u %x %x %x %x", &front, &aer, &reg, &mask, &v) != 5 || reg > 0x1ffff || mask > 0xffff || v > 0xffff)
		return -EINVAL;
	mutex_lock(&dump_lock);
	if (!dump_u)
		ret = -ENODEV;
	for (i = 0; dump_u && i < dump_u->nports; i++) {
		if (dump_u->cfgs[i].copper || dump_u->cfgs[i].front != front)
			continue;
		mutex_lock(&dump_u->mdio);
		ret = us48_tsc_wr(dump_u, 0xffde, aer);
		if (!ret && mask != 0xffff)
			ret = us48_tsc_rd(dump_u, reg, &cur);
		ret = ret ?: us48_tsc_wr(dump_u, reg, (cur & ~mask) | v);
		mutex_unlock(&dump_u->mdio);
	}
	mutex_unlock(&dump_lock);
	return ret;
}

static const struct kernel_param_ops tscwr_ops = { .set = tscwr_set };
module_param_cb(tscwr, &tscwr_ops, NULL, 0200);

/* echo "2000 2004 2008" > .../cmicrd: log unit 1 CMIC registers at those offsets (read only; GP_DATA_IN/OUT/OUT_EN = SFP TX_DISABLE pins, docs/board.md). */
static int cmicrd_set(const char *val, const struct kernel_param *kp)
{
	unsigned int off[4], n, i;
	int ret = -ENODEV;

	n = sscanf(val, "%x %x %x %x", &off[0], &off[1], &off[2], &off[3]);
	if (!n)
		return -EINVAL;
	mutex_lock(&dump_lock);
	for (i = 0; dump_u && i < n; i++) {
		if (off[i] & 3 || off[i] > 0x3ffff)
			continue;
		dev_info(dump_u->dev, "unit 1 CMIC 0x%05x = 0x%08x\n", off[i], readl(dump_u->cmic + off[i]));
		ret = 0;
	}
	mutex_unlock(&dump_lock);
	return ret;
}

static const struct kernel_param_ops cmicrd_ops = { .set = cmicrd_set };
module_param_cb(cmicrd, &cmicrd_ops, NULL, 0200);

/* Opt-in, default off (#57): echo 1 > .../higig = HiGig link bring-up on both units (no table writes); 2 = status log. */
static int higig_set(const char *val, const struct kernel_param *kp)
{
	struct us48_cmic *u[2];
	const struct firmware *fw = NULL;
	unsigned int op;
	int i, ret = kstrtouint(val, 0, &op);

	if (ret || (op != 1 && op != 2))
		return ret ?: -EINVAL;
	mutex_lock(&dump_lock);
	u[0] = us48_unit0();
	u[1] = dump_u;
	if (!u[0] || !u[1]) {
		mutex_unlock(&dump_lock);
		return -ENODEV;
	}
	if (op == 1 && request_firmware(&fw, "us48-tsc.bin", u[1]->dev)) {
		mutex_unlock(&dump_lock);
		return -ENOENT;
	}
	for (i = 0; i < 2; i++) {
		if (op == 1)
			ret = ret ?: us48_higig_up(u[i], fw);
		us48_higig_status(u[i]);
	}
	release_firmware(fw);
	mutex_unlock(&dump_lock);
	return ret;
}

static const struct kernel_param_ops higig_ops = { .set = higig_set };
module_param_cb(higig, &higig_ops, NULL, 0200);

static const struct kernel_param_ops rx_restart_ops = { .set = rx_restart_set };
module_param_cb(rx_restart, &rx_restart_ops, NULL, 0200);

static const struct auxiliary_device_id us48_xe_ids[] = {
	{ .name = US48_PCI_AUX_ID(US48_PCI_AUX_CMIC) },
	{ }
};
MODULE_DEVICE_TABLE(auxiliary, us48_xe_ids);

static struct auxiliary_driver us48_xe_driver = {
	.name		= "xe",
	.id_table	= us48_xe_ids,
	.probe		= us48_xe_probe,
	.remove		= us48_xe_remove,
	.shutdown	= us48_xe_shutdown,
};
module_auxiliary_driver(us48_xe_driver);

MODULE_DESCRIPTION("UniFi US-48-500W BCM53346 SFP+ 1/2 (xe49/xe50) and ge40, unit 1 over PCIe (aux device of us48_pci)");
MODULE_LICENSE("GPL");
