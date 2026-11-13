/* SPDX-License-Identifier: GPL-2.0 */
/*
 * us48_pci: generic access to the second chip (BCM53346, unit 1, PCIe 0000:01:00.0) and its Cortex-A9 (a2).
 * Owns the PCI probe, BAR0 (PAXB + chipcommon) and BAR2 (CMIC). Clients: us48_cmic (netdev), a2 loader.
 * Facts + unknowns (C/I): docs/pci-layer.md.
 */
#ifndef US48_PCI_H
#define US48_PCI_H

#include <linux/types.h>
#include <linux/err.h>

struct us48_pci;
struct bcm_cmic_unit;

/* BAR0 layout: chipcommon at +0x0, endpoint PAXB regs at +0x2000 + PAXB offset (driver-internals 14.2) */
#define US48_PCI_BAR0		0
#define US48_PCI_CMIC_BAR	2
#define US48_PAXB_BASE		0x2000
#define US48_PAXB_IMAP0_7	0x2c1c		/* BAR0 offset; selects the 4 KiB AXI page shown at the window */
#define US48_PAXB_IMAP0_7_DEF	0x18032001
#define US48_PAXB_OARR_0	0x2d20		/* BAR0 offset */
#define US48_PAXB_OMAP_0_LO	0x2d40
#define US48_PAXB_OMAP_0_HI	0x2d44
#define US48_AXI_WIN		0x7000		/* BAR0 offset of the 4 KiB window */
/* Chip inbound window: bus addresses used by the chip for DMA must stay below this (driver-internals 14.2) */
#define US48_PCI_DMA_LIMIT	0x08000000UL

/* a2 (unit 1 A9) AXI addresses, docs/a2.md and scripts/a2_loader.py */
#define US48_A2_SRAM		0x1b000000
#define US48_A2_SRAM_SIZE	0x4000
#define US48_A2_CRU_CONTROL	0x1803e000
#define US48_A2_DBG_BASE	0x19010000

/* Client handle: NULL-safe error pointer; drop with us48_pci_put(). -ENODEV if the device is not bound. */
struct us48_pci *us48_pci_get(void);
void us48_pci_put(struct us48_pci *p);

/* Raw BAR2 (CMIC) and BAR0 access. Offsets are bounds-checked: -EINVAL out of range, -ENODEV after unbind. */
int us48_pci_cmic_read(struct us48_pci *p, u32 off, u32 *val);
int us48_pci_cmic_write(struct us48_pci *p, u32 off, u32 val);
int us48_pci_bar0_read(struct us48_pci *p, u32 off, u32 *val);
int us48_pci_bar0_write(struct us48_pci *p, u32 off, u32 val);
/* PAXB register: off is relative to the PAXB base (BAR0 + 0x2000). */
int us48_pci_paxb_read(struct us48_pci *p, u32 off, u32 *val);
int us48_pci_paxb_write(struct us48_pci *p, u32 off, u32 val);

/* One S-channel op on the unit-1 CMIC (serialised per device). See bcm_cmic.h for args. */
int us48_pci_schan(struct us48_pci *p, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack);

/* Any unit-1 AXI word via PAXB IMAP0_7 + BAR0 window. addr must be 4-byte aligned. */
int us48_pci_axi_read32(struct us48_pci *p, u32 addr, u32 *val);
int us48_pci_axi_write32(struct us48_pci *p, u32 addr, u32 val);
int us48_pci_axi_write(struct us48_pci *p, u32 addr, const void *buf, size_t len);

/* Map a2 AXI 0x08000000.. to host physical 0x0.. (OARR_0/OMAP_0). */
int us48_pci_a2_host_window(struct us48_pci *p, bool enable);

/* a2 control. Sequences taken from scripts/a2_loader.py and a2_force_pc.py only. */
int us48_pci_a2_reset(struct us48_pci *p);		/* pulse ARM_RST_N: a2 back to the boot ROM loop */
int us48_pci_a2_halt(struct us48_pci *p);		/* CoreSight halt request */
int us48_pci_a2_set_pc(struct us48_pci *p, u32 pc);	/* halted a2 only: DTRRX -> r0, mov pc,r0 */
int us48_pci_a2_release(struct us48_pci *p);		/* restart a halted a2 */
int us48_pci_a2_load(struct us48_pci *p, u32 addr, const void *img, size_t len);	/* host -> a2 AXI */
int us48_pci_a2_run(struct us48_pci *p, u32 entry);	/* halt, set_pc, release */
int us48_pci_a2_pc_sample(struct us48_pci *p, u32 *pc);	/* PCSR; semantics unverified (docs/pci-layer.md) */
/* Not known to work (a2.md: PCIe boot path not taken after a core reset). Returns -EOPNOTSUPP. */
int us48_pci_a2_boot_from_pcie(struct us48_pci *p);

#endif /* US48_PCI_H */
