// SPDX-License-Identifier: GPL-2.0
/*
 * us48_pci: PCI owner for the second chip (BCM53346, unit 1). Creates the aux devices a2, cmic, clk
 * and provides the unit 1 AXI regmap over the PAXB IMAP window. Facts: docs/pci-layer.md.
 */
#include <linux/auxiliary_bus.h>
#include <linux/debugfs.h>
#include <linux/dma-mapping.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/pci.h>
#include <linux/property.h>
#include <linux/regmap.h>
#include <linux/slab.h>
#include <linux/sizes.h>
#include "bcm_cmic.h"
#include "us48_hwdb.h"
#include "us48_unit1_regmap.h"
#include "us48_pci.h"
/* Shared S-channel core, single source in common/bcm-cmic/ (included, not copied) */
#include "../../common/bcm-cmic/bcm_cmic_schan.c"

/* PAXB of the endpoint sits at BAR0 + HW_PAXB1_EP_BASE; offsets below are BAR0 offsets */
#define PAXB_IMAP0_7	(HW_PAXB1_EP_BASE + HW_PAXB1_EP_IMAP0_7)
#define PAXB_OARR_0	(HW_PAXB1_EP_BASE + HW_PAXB1_EP_OARR_0)
#define PAXB_OMAP_0_LO	(HW_PAXB1_EP_BASE + HW_PAXB1_EP_OMAP_0_LO)
#define PAXB_OMAP_0_HI	(HW_PAXB1_EP_BASE + HW_PAXB1_EP_OMAP_0_HI)
#define AXI_WIN		HW_BAR0_AXI_WIN_BASE
#define AXI_PAGE_MASK	(HW_BAR0_AXI_WIN_SIZE - 1)
/* IMAP0_7 power-on value on unit 1 (hwdb paxb0.IMAP0_7 desc): restored at remove/shutdown */
#define IMAP0_7_DEF	0x18032001
#define IMAP_VALID	1
#define OUT_GRANULE	SZ_64M	/* OARR/OMAP ADDR[31:26] */

/* awto-uboot board/ubiquiti/us48/pcie.c: set before its first unit 1 BAR2 read, cleared after it */
#define U1_MARK_PROBING	0x31554d43U

enum { ADEV_A2, ADEV_CMIC, ADEV_CLK, ADEV_BUSTEST, ADEV_PVTMON, ADEV_GPIO, ADEV_NUM };

struct us48_pci_adev {
	struct auxiliary_device adev;
};

struct us48_pci {
	struct pci_dev *pdev;
	void __iomem *bar0, *bar2;
	struct bcm_cmic_unit cmic;
	struct regmap *map;
	struct mutex win_lock;	/* IMAP0_7 + window page, OARR_0/OMAP_0, every unit 1 AXI access */
	struct mutex schan_lock;
	bool alive;
	bool page_valid;
	bool out_mapped;
	u32 page;
	struct us48_pci_adev *adev[ADEV_NUM];
	struct dentry *dbg;
};

struct us48_pci *us48_pci_from_adev(struct auxiliary_device *adev)
{
	return dev_get_drvdata(adev->dev.parent);
}
EXPORT_SYMBOL_GPL(us48_pci_from_adev);

struct device *us48_pci_dev(struct us48_pci *p)
{
	return &p->pdev->dev;
}
EXPORT_SYMBOL_GPL(us48_pci_dev);

struct regmap *us48_pci_regmap(struct us48_pci *p)
{
	return p->map;
}
EXPORT_SYMBOL_GPL(us48_pci_regmap);

void __iomem *us48_pci_cmic_base(struct us48_pci *p)
{
	return p->bar2;
}
EXPORT_SYMBOL_GPL(us48_pci_cmic_base);

/* PERST asserted or link down: the endpoint's config space reads all-ones. */
int us48_pci_link_ok(struct us48_pci *p)
{
	return p->alive && pci_device_is_present(p->pdev) ? 0 : -ENODEV;
}
EXPORT_SYMBOL_GPL(us48_pci_link_ok);

int us48_pci_schan(struct us48_pci *p, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	int ret;

	mutex_lock(&p->schan_lock);
	ret = us48_pci_link_ok(p) ? -ENODEV : bcm_cmic_schan_op(&p->cmic, op, blk, addr, data, words, ack);
	mutex_unlock(&p->schan_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_schan);

/* --- unit 1 AXI regmap: IMAP0_7 selects the 4 KiB page, the window shows it. win_lock is the regmap lock. --- */

static void win_page(struct us48_pci *p, u32 addr)
{
	u32 v = (addr & ~AXI_PAGE_MASK) | IMAP_VALID;

	if (p->page_valid && p->page == v)
		return;
	writel(v, p->bar0 + PAXB_IMAP0_7);
	readl(p->bar0 + PAXB_IMAP0_7);	/* flush: window read right after the posted write returned the old page (#80) */
	p->page = v;
	p->page_valid = true;
}

static int axi_reg_read(void *ctx, unsigned int reg, unsigned int *val)
{
	struct us48_pci *p = ctx;

	if (us48_pci_link_ok(p))
		return -ENODEV;
	win_page(p, reg);
	*val = readl(p->bar0 + AXI_WIN + (reg & AXI_PAGE_MASK));
	/* a dead link reads all-ones: tell it from real data */
	if (*val == ~0U && us48_pci_link_ok(p))
		return -ENODEV;
	return 0;
}

static int axi_reg_write(void *ctx, unsigned int reg, unsigned int val)
{
	struct us48_pci *p = ctx;

	if (us48_pci_link_ok(p))
		return -ENODEV;
	win_page(p, reg);
	writel(val, p->bar0 + AXI_WIN + (reg & AXI_PAGE_MASK));
	return 0;
}

static void axi_lock(void *ctx)
{
	mutex_lock(&((struct us48_pci *)ctx)->win_lock);
}

static void axi_unlock(void *ctx)
{
	mutex_unlock(&((struct us48_pci *)ctx)->win_lock);
}

static const struct regmap_bus us48_axi_bus = {
	.reg_read = axi_reg_read,
	.reg_write = axi_reg_write,
};

static const struct regmap_config us48_axi_cfg = {
	.name = "us48-unit1-axi",
	.reg_bits = 32,
	.val_bits = 32,
	.reg_stride = 4,
	.max_register = US48_UNIT1_MAX_REGISTER,
	.rd_table = &us48_unit1_rd_table,
	.wr_table = &us48_unit1_wr_table,
	.volatile_table = &us48_unit1_volatile_table,
	.cache_type = REGCACHE_NONE,
	.lock = axi_lock,
	.unlock = axi_unlock,
};

void __iomem *us48_pci_win_move(struct us48_pci *p, u32 axi)
{
	if (us48_pci_link_ok(p))
		return ERR_PTR(-ENODEV);
	win_page(p, axi);
	return p->bar0 + AXI_WIN + (axi & AXI_PAGE_MASK);
}
EXPORT_SYMBOL_GPL(us48_pci_win_move);

void __iomem *us48_pci_win_get(struct us48_pci *p, u32 axi)
{
	void __iomem *va;

	mutex_lock(&p->win_lock);
	va = us48_pci_win_move(p, axi);
	if (IS_ERR(va))
		mutex_unlock(&p->win_lock);
	return va;
}
EXPORT_SYMBOL_GPL(us48_pci_win_get);

void us48_pci_win_put(struct us48_pci *p)
{
	mutex_unlock(&p->win_lock);
}
EXPORT_SYMBOL_GPL(us48_pci_win_put);

/* --- a2 outbound window to host RAM --- */

int us48_pci_outbound_map(struct us48_pci *p, phys_addr_t host, size_t len, u32 *axi)
{
	u64 base = host & ~((u64)OUT_GRANULE - 1);
	int ret = 0;

	if (!len || host + len > base + OUT_GRANULE)
		return -EINVAL;
	mutex_lock(&p->win_lock);
	if (us48_pci_link_ok(p)) {
		ret = -ENODEV;
	} else if (p->out_mapped) {
		ret = -EBUSY;
	} else {
		writel(lower_32_bits(base), p->bar0 + PAXB_OMAP_0_LO);
		writel(upper_32_bits(base), p->bar0 + PAXB_OMAP_0_HI);
		writel(HW_A2_HOST_WIN_BASE | HW_PAXB1_EP_OARR_0_VALID_MASK, p->bar0 + PAXB_OARR_0);
		p->out_mapped = true;
		*axi = HW_A2_HOST_WIN_BASE + (u32)(host - base);
	}
	mutex_unlock(&p->win_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_outbound_map);

/* Caller holds win_lock. */
static void outbound_off(struct us48_pci *p)
{
	if (p->out_mapped && p->alive)
		writel(0, p->bar0 + PAXB_OARR_0);
	p->out_mapped = false;
}

void us48_pci_outbound_unmap(struct us48_pci *p)
{
	mutex_lock(&p->win_lock);
	outbound_off(p);
	mutex_unlock(&p->win_lock);
}
EXPORT_SYMBOL_GPL(us48_pci_outbound_unmap);

/* --- aux devices --- */

static void adev_release(struct device *dev)
{
	of_node_put(dev->of_node);
	kfree(container_of(dev, struct us48_pci_adev, adev.dev));
}

static int adev_add(struct us48_pci *p, int idx, const char *name)
{
	struct us48_pci_adev *a = kzalloc(sizeof(*a), GFP_KERNEL);
	int ret;

	if (!a)
		return -ENOMEM;
	a->adev.name = name;
	a->adev.id = 0;
	a->adev.dev.parent = &p->pdev->dev;
	a->adev.dev.release = adev_release;
	/* DT child of the endpoint node named like the aux device (pvtmon, gpio): thermal zones and gpio consumers need it */
	device_set_node(&a->adev.dev, of_fwnode_handle(of_get_child_by_name(p->pdev->dev.of_node, name)));
	ret = auxiliary_device_init(&a->adev);
	if (ret) {
		of_node_put(a->adev.dev.of_node);
		kfree(a);
		return ret;
	}
	ret = auxiliary_device_add(&a->adev);
	if (ret) {
		auxiliary_device_uninit(&a->adev);
		return ret;
	}
	p->adev[idx] = a;
	return 0;
}

static void adevs_del(struct us48_pci *p)
{
	int i;

	for (i = ADEV_NUM - 1; i >= 0; i--) {
		if (!p->adev[i])
			continue;
		auxiliary_device_delete(&p->adev[i]->adev);
		auxiliary_device_uninit(&p->adev[i]->adev);
		p->adev[i] = NULL;
	}
}

/* --- debugfs paxb: read-only PAXB AXI/ordering config of both units (#79) --- */

#define PAXB_RC_AXI_CONFIG	0x18012100	/* RC_AXI_CONFIG, EP_AXI_CONFIG, +8 unused, +0xc RX_DEBUG_CONTROL_0 */
#define PAXB_U0_PHYS		0x18012100

static int paxb_show(struct seq_file *m, void *unused)
{
	static const char * const nm[] = { "RC_AXI_CONFIG", "EP_AXI_CONFIG", "(0x108)", "RX_DEBUG_CONTROL_0" };
	struct us48_pci *p = m->private;
	void __iomem *va;
	int i;

	va = ioremap(PAXB_U0_PHYS, 0x10);
	if (va) {
		for (i = 0; i < 4; i++)
			seq_printf(m, "unit0 0x%08x %-18s %08x\n", PAXB_U0_PHYS + 4 * i, nm[i], readl(va + 4 * i));
		iounmap(va);
	}
	va = us48_pci_win_get(p, PAXB_RC_AXI_CONFIG);
	if (IS_ERR(va))
		return PTR_ERR(va);
	for (i = 0; i < 4; i++)
		seq_printf(m, "unit1 0x%08x %-18s %08x\n", PAXB_RC_AXI_CONFIG + 4 * i, nm[i], readl(va + 4 * i));
	us48_pci_win_put(p);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(paxb);

/* --- debugfs genpll: read-only GENPLL (AXI/APB source) + CCU ARM_DIV/policy of both units (#79) --- */

static const struct { const char *n; u32 a; } genpll_regs[] = {
	{ "GENPLL_CTRL0", HW_GENPLL_BASE + HW_GENPLL_CTRL0 }, { "GENPLL_CTRL1", HW_GENPLL_BASE + HW_GENPLL_CTRL1 },
	{ "GENPLL_CTRL2", HW_GENPLL_BASE + HW_GENPLL_CTRL2 }, { "GENPLL_CTRL3", HW_GENPLL_BASE + HW_GENPLL_CTRL3 },
	{ "GENPLL_STATUS", HW_GENPLL_BASE + HW_GENPLL_STATUS },
	{ "CCU_ARM_DIV", HW_CCU_BASE + HW_CCU_ARM_DIV }, { "CCU_POLICY_FREQ", HW_CCU_BASE + HW_CCU_POLICY_FREQ },
	{ "CCU_POLICY_DBG", HW_CCU_BASE + HW_CCU_POLICY_DBG },
};

static int genpll_show(struct seq_file *m, void *unused)
{
	struct us48_pci *p = m->private;
	void __iomem *va;
	int i;

	for (i = 0; i < ARRAY_SIZE(genpll_regs); i++) {
		u32 a = genpll_regs[i].a;

		va = ioremap(a & ~0xfffU, 0x1000);
		if (va) {
			seq_printf(m, "unit0 0x%08x %-16s %08x\n", a, genpll_regs[i].n, readl(va + (a & 0xfff)));
			iounmap(va);
		}
		va = us48_pci_win_get(p, a);
		if (IS_ERR(va))
			return PTR_ERR(va);
		seq_printf(m, "unit1 0x%08x %-16s %08x\n", a, genpll_regs[i].n, readl(va));
		us48_pci_win_put(p);
	}
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(genpll);

/* write-only test poke "<unit 0|1> <0x18012100..0x1801210c> <value>"; only that PAXB block, board experiments (#79) */
static ssize_t poke_write(struct file *file, const char __user *ubuf, size_t len, loff_t *ppos)
{
	struct us48_pci *p = file->private_data;
	unsigned int unit, addr, val;
	void __iomem *va;
	char buf[64];

	if (len >= sizeof(buf) || copy_from_user(buf, ubuf, len))
		return -EINVAL;
	buf[len] = 0;
	if (sscanf(buf, "%u %i %i", &unit, &addr, &val) != 3 || unit > 1 || addr < PAXB_RC_AXI_CONFIG ||
	    addr > PAXB_RC_AXI_CONFIG + 0xc || addr & 3)
		return -EINVAL;
	if (unit == 0) {
		va = ioremap(addr, 4);
		if (!va)
			return -ENOMEM;
		writel(val, va);
		iounmap(va);
	} else {
		va = us48_pci_win_get(p, addr);
		if (IS_ERR(va))
			return PTR_ERR(va);
		writel(val, va);
		us48_pci_win_put(p);
	}
	return len;
}

static const struct file_operations poke_fops = {
	.open = simple_open,
	.write = poke_write,
};

/* --- PCI --- */

static int us48_pci_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	static const char * const names[ADEV_NUM] = {
		[ADEV_A2] = US48_PCI_AUX_A2, [ADEV_CMIC] = US48_PCI_AUX_CMIC, [ADEV_CLK] = US48_PCI_AUX_CLK,
		[ADEV_BUSTEST] = US48_PCI_AUX_BUSTEST, [ADEV_PVTMON] = US48_PCI_AUX_PVTMON,
		[ADEV_GPIO] = US48_PCI_AUX_GPIO,
	};
	struct regmap_config axi_cfg;
	struct us48_pci *p;
	bool cmic_suspect;
	int i, ret;

	/* 01:00.1 shares the ID; only function 0 carries BAR0/BAR2 as used here */
	if (PCI_FUNC(pdev->devfn) != 0)
		return -ENODEV;
	ret = pcim_enable_device(pdev);
	if (ret)
		return ret;
	pci_set_master(pdev);
	/* Chip inbound window is 128 MiB (US48_PCI_DMA_LIMIT); mask stays 32-bit, clients keep bus < limit */
	ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;

	p = devm_kzalloc(&pdev->dev, sizeof(*p), GFP_KERNEL);
	if (!p)
		return -ENOMEM;
	mutex_init(&p->win_lock);
	mutex_init(&p->schan_lock);
	p->pdev = pdev;
	p->bar0 = pcim_iomap(pdev, US48_PCI_BAR0, 0);
	p->bar2 = pcim_iomap(pdev, US48_PCI_CMIC_BAR, 0);
	if (!p->bar0 || !p->bar2 || pci_resource_len(pdev, US48_PCI_BAR0) < AXI_WIN + HW_BAR0_AXI_WIN_SIZE)
		return -ENOMEM;
	p->cmic.base = p->bar2;
	axi_cfg = us48_axi_cfg;
	axi_cfg.lock_arg = p;	/* custom lock() gets lock_arg, not the bus context: NULL oopsed in a2_reset */
	p->map = devm_regmap_init(&pdev->dev, &us48_axi_bus, p, &axi_cfg);
	if (IS_ERR(p->map))
		return PTR_ERR(p->map);
	p->alive = true;
	pci_set_drvdata(pdev, p);
	p->dbg = debugfs_create_dir("us48_pci", NULL);
	debugfs_create_file("paxb", 0444, p->dbg, p, &paxb_fops);
	debugfs_create_file("genpll", 0444, p->dbg, p, &genpll_fops);
	debugfs_create_file("poke", 0200, p->dbg, p, &poke_fops);

	/* awto-uboot probe marker (#98): a CMIC read stalled on an earlier boot; any BAR2 access would hang a1 */
	cmic_suspect = readl(p->bar0 + HW_PAXB1_EP_BASE + HW_PAXB1_EP_PCIE_SYS_EQ_PAGE_UPPER) == U1_MARK_PROBING;
	if (cmic_suspect)
		dev_warn(&pdev->dev, "unit 1 CMIC marked suspect by awto-uboot, no CMIC aux devices (power-cycle clears)\n");
	for (i = 0; i < ADEV_NUM; i++) {
		if (cmic_suspect && (i == ADEV_CMIC || i == ADEV_PVTMON || i == ADEV_GPIO))
			continue;
		ret = adev_add(p, i, names[i]);
		if (ret) {
			adevs_del(p);
			debugfs_remove_recursive(p->dbg);
			return ret;
		}
	}
	return 0;
}

/* Caller holds win_lock: back to the power-on page, a2 window off. */
static void win_restore(struct us48_pci *p)
{
	if (p->page_valid) {
		writel(IMAP0_7_DEF, p->bar0 + PAXB_IMAP0_7);
		p->page_valid = false;
	}
	outbound_off(p);
}

static void us48_pci_remove(struct pci_dev *pdev)
{
	struct us48_pci *p = pci_get_drvdata(pdev);

	debugfs_remove_recursive(p->dbg);
	adevs_del(p);	/* clients stop first, while the device still answers */
	mutex_lock(&p->schan_lock);
	mutex_lock(&p->win_lock);
	win_restore(p);
	p->alive = false;
	mutex_unlock(&p->win_lock);
	mutex_unlock(&p->schan_lock);
	pci_clear_master(pdev);
}

/* Reboot keeps unit 1 out of reset: a live DMA master hangs stock U-Boot's PCIe init. Aux clients shut down first. */
static void us48_pci_shutdown(struct pci_dev *pdev)
{
	struct us48_pci *p = pci_get_drvdata(pdev);

	/* Stops the CMIC pollers (fan, pvtmon): a read in flight at PERST# wedges unit 1 (#98) */
	adevs_del(p);
	mutex_lock(&p->win_lock);
	if (p->alive)
		win_restore(p);
	mutex_unlock(&p->win_lock);
	pci_clear_master(pdev);
}

static const struct pci_device_id us48_pci_ids[] = {
	{ PCI_DEVICE(PCI_VENDOR_ID_BROADCOM, 0x8346) },
	{ }
};
MODULE_DEVICE_TABLE(pci, us48_pci_ids);

static struct pci_driver us48_pci_driver = {
	.name		= "us48_pci",
	.id_table	= us48_pci_ids,
	.probe		= us48_pci_probe,
	.remove		= us48_pci_remove,
	.shutdown	= us48_pci_shutdown,
};
module_pci_driver(us48_pci_driver);

MODULE_DESCRIPTION("UniFi US-48-500W second chip (BCM53346) PCI owner: aux devices a2/cmic/clk, unit 1 AXI regmap");
MODULE_LICENSE("GPL");
