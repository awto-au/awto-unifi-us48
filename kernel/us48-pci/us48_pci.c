// SPDX-License-Identifier: GPL-2.0
/*
 * us48_pci: PCI owner for the second chip (BCM53346, unit 1) and its Cortex-A9 (a2).
 * No netdev code. API: us48_pci.h; facts and unknowns: docs/pci-layer.md.
 */
#include <linux/delay.h>
#include <linux/dma-mapping.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/kref.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pci.h>
#include <linux/slab.h>
#include "bcm_cmic.h"
#include "us48_pci.h"
/* Shared S-channel core, single source in common/bcm-cmic/ (included, not copied) */
#include "../../common/bcm-cmic/bcm_cmic_schan.c"

/* CoreSight debug regs of a2 (offsets from US48_A2_DBG_BASE), scripts/a2_loader.py */
#define DBG_DTRRX	0x80
#define DBG_ITR		0x84
#define DBG_DSCR	0x88
#define DBG_DRCR	0x90
#define DBG_PCSR	0x84	/* same offset as ITR: read = PCSR, write = ITR; per a2_force_pc.py reads */
#define DBG_LAR		0xfb0
#define DBG_LAR_KEY	0xc5acce55
#define DSCR_HALTED	BIT(0)
#define DSCR_RESTARTED	BIT(1)
#define DSCR_ITREN	BIT(13)
#define DSCR_HDBGEN	BIT(14)
#define DSCR_INSTRCOMPL	BIT(24)
#define DRCR_HALT	BIT(0)
#define DRCR_RESTART	BIT(1)
#define DRCR_CLR_STICKY	BIT(2)
#define ARM_MRC_DTRRX_R0	0xee100e15	/* mrc p14, 0, r0, c0, c5, 0 */
#define ARM_MOV_PC_R0		0xe1a0f000
#define CRU_ARM_RST_N		BIT(4)
/*
 * Debug handshakes are a few A9 cycles + one PCIe round trip each (~us); 1 ms backstop, not measured on
 * the bus. Expiry: log op + DSCR, -ETIMEDOUT. A stalled a2 (bad AXI read) never halts: use a2_reset.
 */
#define DBG_WAIT_US	1000

struct us48_pci {
	struct kref ref;
	struct pci_dev *pdev;
	void __iomem *bar0, *bar2;
	resource_size_t len0, len2;
	struct bcm_cmic_unit cmic;
	struct mutex win_lock;	/* IMAP0_7 + window, OARR_0, a2 debug sequences */
	struct mutex schan_lock;
	bool alive;
	bool win_dirty;
};

static DEFINE_MUTEX(g_lock);
static struct us48_pci *g_dev;

static void us48_pci_free(struct kref *k)
{
	kfree(container_of(k, struct us48_pci, ref));
}

struct us48_pci *us48_pci_get(void)
{
	struct us48_pci *p;

	mutex_lock(&g_lock);
	p = g_dev;
	if (p)
		kref_get(&p->ref);
	mutex_unlock(&g_lock);
	return p ? p : ERR_PTR(-ENODEV);
}
EXPORT_SYMBOL_GPL(us48_pci_get);

void us48_pci_put(struct us48_pci *p)
{
	if (!IS_ERR_OR_NULL(p))
		kref_put(&p->ref, us48_pci_free);
}
EXPORT_SYMBOL_GPL(us48_pci_put);

/* Raw register access: bounds + liveness, caller holds no lock (single 32-bit MMIO is atomic). */
static int reg_rw(struct us48_pci *p, void __iomem *bar, resource_size_t len, u32 off, u32 *rd, u32 wr, bool write)
{
	int ret = 0;

	if ((off & 3) || off >= len)
		return -EINVAL;
	mutex_lock(&p->win_lock);
	if (!p->alive || !bar)
		ret = -ENODEV;
	else if (write)
		writel(wr, bar + off);
	else
		*rd = readl(bar + off);
	mutex_unlock(&p->win_lock);
	return ret;
}

int us48_pci_cmic_read(struct us48_pci *p, u32 off, u32 *val)
{
	return reg_rw(p, p->bar2, p->len2, off, val, 0, false);
}
EXPORT_SYMBOL_GPL(us48_pci_cmic_read);

int us48_pci_cmic_write(struct us48_pci *p, u32 off, u32 val)
{
	return reg_rw(p, p->bar2, p->len2, off, NULL, val, true);
}
EXPORT_SYMBOL_GPL(us48_pci_cmic_write);

int us48_pci_bar0_read(struct us48_pci *p, u32 off, u32 *val)
{
	return reg_rw(p, p->bar0, p->len0, off, val, 0, false);
}
EXPORT_SYMBOL_GPL(us48_pci_bar0_read);

int us48_pci_bar0_write(struct us48_pci *p, u32 off, u32 val)
{
	return reg_rw(p, p->bar0, p->len0, off, NULL, val, true);
}
EXPORT_SYMBOL_GPL(us48_pci_bar0_write);

int us48_pci_paxb_read(struct us48_pci *p, u32 off, u32 *val)
{
	return us48_pci_bar0_read(p, US48_PAXB_BASE + off, val);
}
EXPORT_SYMBOL_GPL(us48_pci_paxb_read);

int us48_pci_paxb_write(struct us48_pci *p, u32 off, u32 val)
{
	return us48_pci_bar0_write(p, US48_PAXB_BASE + off, val);
}
EXPORT_SYMBOL_GPL(us48_pci_paxb_write);

int us48_pci_schan(struct us48_pci *p, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	int ret;

	mutex_lock(&p->schan_lock);
	ret = p->alive ? bcm_cmic_schan_op(&p->cmic, op, blk, addr, data, words, ack) : -ENODEV;
	mutex_unlock(&p->schan_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_schan);

/* Point the window at the 4 KiB page of addr. Caller holds win_lock. */
static void win_page(struct us48_pci *p, u32 addr)
{
	writel((addr & ~0xfffU) | 1, p->bar0 + US48_PAXB_IMAP0_7);
	p->win_dirty = true;
}

static int win_rw(struct us48_pci *p, u32 addr, u32 *rd, u32 wr, bool write)
{
	int ret = 0;

	if (addr & 3)
		return -EINVAL;
	mutex_lock(&p->win_lock);
	if (!p->alive) {
		ret = -ENODEV;
	} else {
		win_page(p, addr);
		if (write)
			writel(wr, p->bar0 + US48_AXI_WIN + (addr & 0xfff));
		else
			*rd = readl(p->bar0 + US48_AXI_WIN + (addr & 0xfff));
	}
	mutex_unlock(&p->win_lock);
	return ret;
}

int us48_pci_axi_read32(struct us48_pci *p, u32 addr, u32 *val)
{
	return win_rw(p, addr, val, 0, false);
}
EXPORT_SYMBOL_GPL(us48_pci_axi_read32);

int us48_pci_axi_write32(struct us48_pci *p, u32 addr, u32 val)
{
	return win_rw(p, addr, NULL, val, true);
}
EXPORT_SYMBOL_GPL(us48_pci_axi_write32);

/* Caller holds win_lock. Word writes, as the scripts do (no wider access shown to work). */
static int win_write_locked(struct us48_pci *p, u32 addr, const u32 *w, size_t words)
{
	u32 cur = ~0U;
	size_t i;

	for (i = 0; i < words; i++, addr += 4) {
		if ((addr & ~0xfffU) != cur) {
			cur = addr & ~0xfffU;
			win_page(p, cur);
		}
		writel(w[i], p->bar0 + US48_AXI_WIN + (addr & 0xfff));
	}
	return 0;
}

int us48_pci_axi_write(struct us48_pci *p, u32 addr, const void *buf, size_t len)
{
	const u32 *w = buf;
	size_t words = len / 4;
	int ret = 0;

	if ((addr & 3) || (len & 3) || (uintptr_t)buf & 3)
		return -EINVAL;
	mutex_lock(&p->win_lock);
	if (!p->alive)
		ret = -ENODEV;
	else
		ret = win_write_locked(p, addr, w, words);
	mutex_unlock(&p->win_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_axi_write);

int us48_pci_a2_host_window(struct us48_pci *p, bool enable)
{
	int ret = 0;

	mutex_lock(&p->win_lock);
	if (!p->alive) {
		ret = -ENODEV;
	} else {
		/* a2.md: OARR_0 = 0x08000001, OMAP_0 = 0 -> AXI 0x08000000.. = host phys 0x0.. (inside 0x08000000-0x0fffffff only) */
		writel(0, p->bar0 + US48_PAXB_OMAP_0_LO);
		writel(0, p->bar0 + US48_PAXB_OMAP_0_HI);
		writel(enable ? 0x08000001 : 0, p->bar0 + US48_PAXB_OARR_0);
	}
	mutex_unlock(&p->win_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_a2_host_window);

/* --- a2 control. Caller holds win_lock for the *_locked helpers. --- */

static u32 dbg_rd(struct us48_pci *p, u32 off)
{
	return readl(p->bar0 + US48_AXI_WIN + off);	/* window parked on the debug page */
}

static void dbg_wr(struct us48_pci *p, u32 off, u32 v)
{
	writel(v, p->bar0 + US48_AXI_WIN + off);
}

static void dbg_page(struct us48_pci *p)
{
	win_page(p, US48_A2_DBG_BASE);
	dbg_wr(p, DBG_LAR, DBG_LAR_KEY);
}

static int dbg_wait(struct us48_pci *p, u32 mask, u32 want, const char *what)
{
	u32 d;
	int ret = read_poll_timeout_atomic(dbg_rd, d, (d & mask) == want, 1, DBG_WAIT_US, false, p, DBG_DSCR);

	if (ret)
		dev_err(&p->pdev->dev, "a2 %s: DSCR %#x after %d us\n", what, d, DBG_WAIT_US);
	return ret;
}

int us48_pci_a2_reset(struct us48_pci *p)
{
	u32 v;

	mutex_lock(&p->win_lock);
	if (!p->alive) {
		mutex_unlock(&p->win_lock);
		return -ENODEV;
	}
	/* RMW keeps the other CRU_CONTROL bits (unit 1 reads 0x30); scripts write 0x20 then 0x30 */
	win_page(p, US48_A2_CRU_CONTROL);
	v = readl(p->bar0 + US48_AXI_WIN);
	writel(v & ~CRU_ARM_RST_N, p->bar0 + US48_AXI_WIN);
	writel(v | CRU_ARM_RST_N, p->bar0 + US48_AXI_WIN);
	mutex_unlock(&p->win_lock);
	return 0;
}
EXPORT_SYMBOL_GPL(us48_pci_a2_reset);

static int a2_halt_locked(struct us48_pci *p)
{
	u32 d;

	dbg_page(p);
	d = dbg_rd(p, DBG_DSCR);
	dbg_wr(p, DBG_DSCR, d | DSCR_HDBGEN);
	dbg_wr(p, DBG_DRCR, DRCR_HALT);
	return dbg_wait(p, DSCR_HALTED, DSCR_HALTED, "halt");
}

int us48_pci_a2_halt(struct us48_pci *p)
{
	int ret;

	mutex_lock(&p->win_lock);
	ret = p->alive ? a2_halt_locked(p) : -ENODEV;
	mutex_unlock(&p->win_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_a2_halt);

static int a2_itr(struct us48_pci *p, u32 ins, const char *what)
{
	dbg_wr(p, DBG_ITR, ins);
	return dbg_wait(p, DSCR_INSTRCOMPL, DSCR_INSTRCOMPL, what);
}

static int a2_set_pc_locked(struct us48_pci *p, u32 pc)
{
	u32 d;
	int ret;

	dbg_page(p);
	d = dbg_rd(p, DBG_DSCR);
	if (!(d & DSCR_HALTED))
		return -EBUSY;
	dbg_wr(p, DBG_DSCR, d | DSCR_ITREN | DSCR_HDBGEN);
	dbg_wr(p, DBG_DTRRX, pc);
	ret = a2_itr(p, ARM_MRC_DTRRX_R0, "mrc DTRRX");
	if (!ret)
		ret = a2_itr(p, ARM_MOV_PC_R0, "mov pc,r0");
	dbg_wr(p, DBG_DSCR, dbg_rd(p, DBG_DSCR) & ~DSCR_ITREN);
	return ret;
}

int us48_pci_a2_set_pc(struct us48_pci *p, u32 pc)
{
	int ret;

	mutex_lock(&p->win_lock);
	ret = p->alive ? a2_set_pc_locked(p, pc) : -ENODEV;
	mutex_unlock(&p->win_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_a2_set_pc);

static int a2_release_locked(struct us48_pci *p)
{
	dbg_page(p);
	dbg_wr(p, DBG_DRCR, DRCR_RESTART | DRCR_CLR_STICKY);
	return dbg_wait(p, DSCR_RESTARTED | DSCR_HALTED, DSCR_RESTARTED, "restart");
}

int us48_pci_a2_release(struct us48_pci *p)
{
	int ret;

	mutex_lock(&p->win_lock);
	ret = p->alive ? a2_release_locked(p) : -ENODEV;
	mutex_unlock(&p->win_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_a2_release);

int us48_pci_a2_load(struct us48_pci *p, u32 addr, const void *img, size_t len)
{
	return us48_pci_axi_write(p, addr, img, len);
}
EXPORT_SYMBOL_GPL(us48_pci_a2_load);

int us48_pci_a2_run(struct us48_pci *p, u32 entry)
{
	int ret;

	mutex_lock(&p->win_lock);
	if (!p->alive) {
		ret = -ENODEV;
	} else {
		ret = a2_halt_locked(p);
		if (!ret)
			ret = a2_set_pc_locked(p, entry);
		/* a2 left halted on error: caller pulses a2_reset (docs/a2.md undo) */
		if (!ret)
			ret = a2_release_locked(p);
	}
	mutex_unlock(&p->win_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_a2_run);

int us48_pci_a2_pc_sample(struct us48_pci *p, u32 *pc)
{
	int ret = 0;

	mutex_lock(&p->win_lock);
	if (!p->alive) {
		ret = -ENODEV;
	} else {
		dbg_page(p);
		*pc = dbg_rd(p, DBG_PCSR);
	}
	mutex_unlock(&p->win_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_pci_a2_pc_sample);

int us48_pci_a2_boot_from_pcie(struct us48_pci *p)
{
	/* STRAPS_SW_OVERRIDE BOOT_DEV=7: a2 did not take the path after a core reset (docs/a2.md, #53). */
	return -EOPNOTSUPP;
}
EXPORT_SYMBOL_GPL(us48_pci_a2_boot_from_pcie);

/* --- PCI --- */

static int us48_pci_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	struct us48_pci *p;
	int ret;

	/* 01:00.1 shares the ID; only function 0 carries BAR0/BAR2 as used here */
	if (PCI_FUNC(pdev->devfn) != 0)
		return -ENODEV;
	ret = pcim_enable_device(pdev);
	if (ret)
		return ret;
	pci_set_master(pdev);
	/* Chip inbound window is 128 MiB (US48_PCI_DMA_LIMIT); mask stays 32-bit like us48_cmic, clients keep bus < limit */
	ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;

	p = kzalloc(sizeof(*p), GFP_KERNEL);
	if (!p)
		return -ENOMEM;
	kref_init(&p->ref);
	mutex_init(&p->win_lock);
	mutex_init(&p->schan_lock);
	p->pdev = pdev;
	p->len0 = pci_resource_len(pdev, US48_PCI_BAR0);
	p->len2 = pci_resource_len(pdev, US48_PCI_CMIC_BAR);
	p->bar0 = pcim_iomap(pdev, US48_PCI_BAR0, 0);
	p->bar2 = pcim_iomap(pdev, US48_PCI_CMIC_BAR, 0);
	if (!p->bar0 || !p->bar2 || p->len0 < US48_AXI_WIN + 0x1000) {
		kfree(p);
		return -ENOMEM;
	}
	p->cmic.base = p->bar2;
	p->alive = true;
	pci_set_drvdata(pdev, p);

	mutex_lock(&g_lock);
	g_dev = p;
	mutex_unlock(&g_lock);
	dev_info(&pdev->dev, "BAR0 %pa BAR2 %pa\n", &p->len0, &p->len2);
	return 0;
}

static void win_restore(struct us48_pci *p)
{
	if (p->win_dirty) {
		writel(US48_PAXB_IMAP0_7_DEF, p->bar0 + US48_PAXB_IMAP0_7);
		p->win_dirty = false;
	}
}

static void us48_pci_remove(struct pci_dev *pdev)
{
	struct us48_pci *p = pci_get_drvdata(pdev);

	mutex_lock(&g_lock);
	g_dev = NULL;
	mutex_unlock(&g_lock);
	mutex_lock(&p->schan_lock);
	mutex_lock(&p->win_lock);
	win_restore(p);
	p->alive = false;
	p->bar0 = p->bar2 = NULL;	/* devres unmaps after this returns */
	mutex_unlock(&p->win_lock);
	mutex_unlock(&p->schan_lock);
	pci_clear_master(pdev);
	kref_put(&p->ref, us48_pci_free);
}

/* Reboot keeps unit 1 out of reset: a live DMA master hangs stock U-Boot's PCIe init (driver-internals, us48_cmic shutdown). */
static void us48_pci_shutdown(struct pci_dev *pdev)
{
	struct us48_pci *p = pci_get_drvdata(pdev);

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

MODULE_DESCRIPTION("UniFi US-48-500W second chip (BCM53346) PCI access layer: BAR0/BAR2, S-channel, a2 control");
MODULE_LICENSE("GPL");
