// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_xe: PCI front-end for the second chip (unit 1, BCM53346 behind PCIe): SFP+ 1/2 (xe49/xe50)
 * plus the copper test port ge40. Separate module so it can be loaded and unloaded while us48_cmic.ko
 * (core + unit 0 ge1, the management port) keeps running. The two CMICs have separate DMA channels and rings.
 */
#include <linux/module.h>
#include <linux/pci.h>

#include "us48_cmic.h"

static struct us48_cmic *dump_u;

static int us48_xe_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	struct us48_cmic *u = NULL;
	void __iomem *cmic;
	int ret;

	if (PCI_FUNC(pdev->devfn) != 0)
		return -ENODEV;
	ret = pcim_enable_device(pdev);
	if (ret)
		return ret;
	pci_set_master(pdev);
	cmic = pcim_iomap(pdev, CMIC_BAR, 0);
	if (!cmic)
		return -ENOMEM;
	ret = us48_probe_common(&pdev->dev, cmic, us48_unit1_cfgs, us48_unit1_nports, 1, &u);
	if (u)
		pci_set_drvdata(pdev, u);
	if (!ret)
		dump_u = u;
	return ret;
}

/* Unbind (rmmod): stop DMA and polling; devres then unregisters the netdevs and frees the rings. */
static void us48_xe_remove(struct pci_dev *pdev)
{
	struct us48_cmic *u = pci_get_drvdata(pdev);

	dump_u = NULL;
	if (u)
		us48_quiesce(u);
}

/* Reboot keeps unit 1 out of reset (PERST# held released, patch 952): a live DMA hangs stock U-Boot's PCIe init. */
static void us48_xe_shutdown(struct pci_dev *pdev)
{
	struct us48_cmic *u = pci_get_drvdata(pdev);

	if (!u)
		return;
	us48_quiesce(u);
	pci_clear_master(pdev);
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

static const struct pci_device_id us48_xe_ids[] = {
	/* Function 1 (01:00.1) shares the ID; probe rejects it, only function 0 carries the CMIC. */
	{ PCI_DEVICE(PCI_VENDOR_ID_BROADCOM, 0x8346) },
	{ }
};
MODULE_DEVICE_TABLE(pci, us48_xe_ids);

static struct pci_driver us48_xe_driver = {
	.name		= "us48_cmic_xe",
	.id_table	= us48_xe_ids,
	.probe		= us48_xe_probe,
	.remove		= us48_xe_remove,
	.shutdown	= us48_xe_shutdown,
};

static int __init us48_xe_init(void)
{
	return pci_register_driver(&us48_xe_driver);
}

static void __exit us48_xe_exit(void)
{
	dump_u = NULL;
	pci_unregister_driver(&us48_xe_driver);
}

module_init(us48_xe_init);
module_exit(us48_xe_exit);

MODULE_DESCRIPTION("UniFi US-48-500W BCM53346 SFP+ 1/2 (xe49/xe50) and ge40, unit 1 over PCIe");
MODULE_LICENSE("GPL");
