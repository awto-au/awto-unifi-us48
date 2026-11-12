// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic: bring-up module for the BCM53346 (unit 1) CMIC on the UniFi US-48-500W.
 * Step 1: bind PCI function 0, map the CMIC BAR, log the CMC0 packet-DMA state. Read-only.
 * Register map: docs/driver-internals.md section 5 (offsets relative to the CMIC base).
 */
#include <linux/module.h>
#include <linux/pci.h>

#define CMIC_BAR		2
#define CMC0_DMA_CTRL(ch)	(0x31140 + 4 * (ch))
#define CMC0_DMA_STAT		0x31150
#define CMC0_DMA_DESC(ch)	(0x31158 + 4 * (ch))
#define CMC0_DMA_CURR_DESC(ch)	(0x311a8 + 4 * (ch))
#define CMC0_IRQ_STAT0		0x31400

static int us48_cmic_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	void __iomem *cmic;
	int ch, ret;

	ret = pcim_enable_device(pdev);
	if (ret)
		return ret;
	cmic = pcim_iomap(pdev, CMIC_BAR, 0);
	if (!cmic)
		return -ENOMEM;

	dev_info(&pdev->dev, "CMIC BAR%d %pR\n", CMIC_BAR, &pdev->resource[CMIC_BAR]);
	for (ch = 0; ch < 4; ch++)
		dev_info(&pdev->dev, "ch%d DMA_CTRL %08x DESC %08x CURR %08x\n", ch,
			 ioread32(cmic + CMC0_DMA_CTRL(ch)), ioread32(cmic + CMC0_DMA_DESC(ch)),
			 ioread32(cmic + CMC0_DMA_CURR_DESC(ch)));
	dev_info(&pdev->dev, "DMA_STAT %08x IRQ_STAT0 %08x\n",
		 ioread32(cmic + CMC0_DMA_STAT), ioread32(cmic + CMC0_IRQ_STAT0));
	return 0;
}

static const struct pci_device_id us48_cmic_ids[] = {
	/* Function 1 (01:00.1) shares the ID; only function 0 carries the CMIC. */
	{ PCI_DEVICE(PCI_VENDOR_ID_BROADCOM, 0x8346) },
	{ }
};
MODULE_DEVICE_TABLE(pci, us48_cmic_ids);

static int us48_cmic_match_fn0(struct pci_dev *pdev, const struct pci_device_id *id)
{
	if (PCI_FUNC(pdev->devfn) != 0)
		return -ENODEV;
	return us48_cmic_probe(pdev, id);
}

static struct pci_driver us48_cmic_driver = {
	.name		= "us48_cmic",
	.id_table	= us48_cmic_ids,
	.probe		= us48_cmic_match_fn0,
};
module_pci_driver(us48_cmic_driver);

MODULE_DESCRIPTION("UniFi US-48-500W BCM53346 CMIC bring-up (read-only step 1)");
MODULE_LICENSE("GPL");
