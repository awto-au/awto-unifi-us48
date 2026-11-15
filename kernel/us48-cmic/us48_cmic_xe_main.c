// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_xe: aux driver (us48_pci.cmic) for the second chip (unit 1, BCM53346 behind PCIe): SFP+ 1/2 (xe49/xe50)
 * plus the copper test port ge40. Separate module so it can be loaded and unloaded while us48_cmic.ko
 * (core + unit 0 ge1, the management port) keeps running. The two CMICs have separate DMA channels and rings.
 */
#include <linux/auxiliary_bus.h>
#include <linux/firmware.h>
#include <linux/module.h>

#include "us48_cmic.h"
#include "us48_pci.h"

static struct us48_cmic *dump_u;
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
	/* Missing microcode is logged by us48_tsc_init; the ports and ge40 still come up. */
	if (request_firmware(&fw, "us48-tsc.bin", &adev->dev))
		fw = NULL;
	/* Every load resets the chip and runs the full cold init; DMA is stopped first. */
	ret = us48_chip_cold_init(&adev->dev, us48_pci_cmic_base(p), 1);
	/* devres on the aux device (netdev memory), DMA on the PCI device (mask, window limit) */
	ret = ret ?: us48_probe_common(&adev->dev, us48_pci_dev(p), us48_pci_cmic_base(p), us48_unit1_cfgs,
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

/* Run fn on the xe port of that front number under dump_lock (unbind waits for it). */
static int xe_port_op(unsigned int front, unsigned int lo, unsigned int hi, bool restart)
{
	int i, ret = -EINVAL;

	mutex_lock(&dump_lock);
	if (!dump_u)
		ret = -ENODEV;
	for (i = 0; dump_u && i < dump_u->nports; i++) {
		if (dump_u->cfgs[i].copper || dump_u->cfgs[i].front != front)
			continue;
		if (restart) {
			ret = us48_tsc_rx_restart(dump_u, &dump_u->cfgs[i]);
		} else {
			us48_tsc_range(dump_u, &dump_u->cfgs[i], lo, hi);
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

	return ret ?: xe_port_op(front, 0, 0, true);
}

/* echo "49 c010 c01f" > .../tscreg: log those TSC registers of front 49/50 (AER = its lane). */
static int tscreg_set(const char *val, const struct kernel_param *kp)
{
	unsigned int front, lo, hi;

	if (sscanf(val, "%u %x %x", &front, &lo, &hi) != 3 || hi < lo || hi - lo > 63 || hi > 0xffff)
		return -EINVAL;
	return xe_port_op(front, lo, hi, false);
}

static const struct kernel_param_ops tscreg_ops = { .set = tscreg_set };
module_param_cb(tscreg, &tscreg_ops, NULL, 0200);

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
