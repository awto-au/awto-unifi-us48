/* SPDX-License-Identifier: GPL-2.0 */
/*
 * us48_pci: PCI owner of the second chip (BCM53346, unit 1, PCIe 0000:01:00.0) and auxiliary bus parent.
 * Aux devices (names us48_pci.<x>.0): a2 (remoteproc), cmic (netdev), clk (stub), bustest (#80), pvtmon + gpio (#19).
 * Facts + unknowns (C/I): docs/pci-layer.md, design: docs/a2-design.md.
 */
#ifndef US48_PCI_H
#define US48_PCI_H

#include <linux/err.h>
#include <linux/types.h>

struct auxiliary_device;
struct device;
struct regmap;
struct us48_pci;

#define US48_PCI_AUX_A2		"a2"
#define US48_PCI_AUX_CMIC	"cmic"
#define US48_PCI_AUX_CLK	"clk"
#define US48_PCI_AUX_BUSTEST	"bustest"
#define US48_PCI_AUX_PVTMON	"pvtmon"	/* DT child "pvtmon" of the endpoint node is the aux device's of_node */
#define US48_PCI_AUX_GPIO	"gpio"		/* DT child "gpio" */
/* aux driver id_table names: KBUILD_MODNAME.<name> */
#define US48_PCI_AUX_ID(n)	"us48_pci." n

#define US48_PCI_BAR0		0
#define US48_PCI_CMIC_BAR	2
/* Chip inbound window: bus addresses used by the chip for DMA must stay below this (driver-internals 14.2) */
#define US48_PCI_DMA_LIMIT	0x08000000UL

/* Parent handle of an aux device; valid until the aux driver's remove() returns. */
struct us48_pci *us48_pci_from_adev(struct auxiliary_device *adev);
/* PCI device: DMA masks and mappings of the chip's bus master. */
struct device *us48_pci_dev(struct us48_pci *p);

/*
 * Unit 1 AXI, 32-bit registers, access table from hwdb (hwdb/gen/us48_unit1_regmap.h): -EIO outside it.
 * Window moves are serialised by one lock; link down or PERST asserted: -ENODEV.
 */
struct regmap *us48_pci_regmap(struct us48_pci *p);

/*
 * Raw IMAP window access for the bus test (kernel/us48-bustest): takes the window lock, points the 4 KiB window at
 * the page of axi and returns the window address of axi (valid to the end of that page), or ERR_PTR(-ENODEV).
 * _move re-points while locked, _put unlocks. No access table check: the caller owns the address.
 */
void __iomem *us48_pci_win_get(struct us48_pci *p, u32 axi);
void __iomem *us48_pci_win_move(struct us48_pci *p, u32 axi);
void us48_pci_win_put(struct us48_pci *p);

/* BAR2 (CMIC) mapping for the cmic aux driver. */
void __iomem *us48_pci_cmic_base(struct us48_pci *p);
/* One S-channel op on the unit-1 CMIC (serialised). See bcm_cmic.h for args. */
int us48_pci_schan(struct us48_pci *p, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack);

/* 0 if the endpoint answers config reads (link up, PERST released), else -ENODEV. */
int us48_pci_link_ok(struct us48_pci *p);

/*
 * a2 -> host RAM window (PAXB OARR_0/OMAP_0), one at a time, owned here. Granule is 64 MiB: the a2 reaches the
 * whole 64 MiB aligned block holding [host, host+len); *axi = a2 AXI address of host.
 */
int us48_pci_outbound_map(struct us48_pci *p, phys_addr_t host, size_t len, u32 *axi);
void us48_pci_outbound_unmap(struct us48_pci *p);

/*
 * a2 memory pool (implemented by us48_a2, looked up with symbol_get by the bus test): returns the WC mapping of the
 * pool base while a2 is prepared (firmware loaded or running), else ERR_PTR(-ENODEV). Holds a lock that blocks
 * a2 stop/unprepare until _put; keep it short. *size = pool size.
 */
void *us48_a2_region_get(size_t *size);
void us48_a2_region_put(void);
/* us48_vnet (symbol_get): the a2 rproc, and the pool mapping, valid only inside an rproc subdev's prepare..unprepare (#85) */
struct rproc;
struct rproc *us48_a2_rproc(void);
void *us48_a2_pool_va(size_t *size);

#endif /* US48_PCI_H */
