// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_xe: aux driver (us48_pci.cmic) for the second chip (unit 1, BCM53346 behind PCIe): SFP+ 1/2 (xe49/xe50)
 * plus the copper test port ge40. Separate module so it can be loaded and unloaded while us48_cmic.ko
 * (core + unit 0 ge1, the management port) keeps running. The two CMICs have separate DMA channels and rings.
 */
#include <linux/auxiliary_bus.h>
#include <linux/module.h>

#include "us48_cmic.h"
#include "us48_pci.h"

static struct us48_cmic *dump_u;

static int us48_xe_probe(struct auxiliary_device *adev, const struct auxiliary_device_id *id)
{
	struct us48_pci *p = us48_pci_from_adev(adev);
	struct us48_cmic *u = NULL;
	int ret;

	/* devres on the aux device (netdevs go with the unbind), DMA on the PCI device (mask, window limit) */
	ret = us48_probe_common(&adev->dev, us48_pci_dev(p), us48_pci_cmic_base(p), us48_unit1_cfgs,
				us48_unit1_nports, 1, &u);
	if (u)
		auxiliary_set_drvdata(adev, u);
	if (!ret)
		dump_u = u;
	return ret;
}

/* Unbind (rmmod): stop DMA and polling; devres then unregisters the netdevs and frees the rings. */
static void us48_xe_remove(struct auxiliary_device *adev)
{
	struct us48_cmic *u = auxiliary_get_drvdata(adev);

	dump_u = NULL;
	if (u)
		us48_quiesce(u);
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
	if (!dump_u)
		return -ENODEV;
	us48_tsc_dump(dump_u);
	return 0;
}

static const struct kernel_param_ops tsc_ops = { .set = tsc_set };
module_param_cb(tsc, &tsc_ops, NULL, 0200);

/* echo 49 > /sys/module/us48_cmic_xe/parameters/rx_restart: per-lane RX restart of that front port, then PCS status. */
static int rx_restart_set(const char *val, const struct kernel_param *kp)
{
	unsigned int front;
	int i, ret = kstrtouint(val, 0, &front);

	if (ret)
		return ret;
	if (!dump_u)
		return -ENODEV;
	for (i = 0; i < dump_u->nports; i++)
		if (!dump_u->cfgs[i].copper && dump_u->cfgs[i].front == front)
			return us48_tsc_rx_restart(dump_u, &dump_u->cfgs[i]);
	return -EINVAL;
}

/* echo "49 c010 c01f" > .../tscreg: log those TSC registers of front 49/50 (AER = its lane). */
static int tscreg_set(const char *val, const struct kernel_param *kp)
{
	unsigned int front, lo, hi;
	int i;

	if (sscanf(val, "%u %x %x", &front, &lo, &hi) != 3 || hi < lo || hi - lo > 63 || hi > 0xffff)
		return -EINVAL;
	if (!dump_u)
		return -ENODEV;
	for (i = 0; i < dump_u->nports; i++)
		if (!dump_u->cfgs[i].copper && dump_u->cfgs[i].front == front) {
			us48_tsc_range(dump_u, &dump_u->cfgs[i], lo, hi);
			return 0;
		}
	return -EINVAL;
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
