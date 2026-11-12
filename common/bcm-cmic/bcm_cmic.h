/* SPDX-License-Identifier: GPL-2.0 */
/*
 * bcm-cmic: shared core for the Broadcom HR2 CMIC (S-channel, MIIM).
 * Single source in common/bcm-cmic/; staged byte-identical by scripts/sync_bcm_cmic.py.
 * Facts: docs/driver-internals.md sections 1 and 4.
 */
#ifndef BCM_CMIC_H
#define BCM_CMIC_H

#include <linux/types.h>

struct bcm_cmic_unit {
	void __iomem *base;	/* unit 0: AXI 0x48000000 */
};

/* CMC0 offsets, driver-internals 1 */
#define BCM_CMIC_SCHAN_CTRL		0x31000
#define BCM_CMIC_SCHAN_MSG(n)		(0x3100c + 4 * (n))
#define BCM_CMIC_SCHAN_MSG_WORDS	22
#define BCM_CMIC_MIIM_PARAM		0x31080
#define BCM_CMIC_MIIM_RDATA		0x31084
#define BCM_CMIC_MIIM_ADDR		0x31088
#define BCM_CMIC_MIIM_CTRL		0x3108c
#define BCM_CMIC_MIIM_STAT		0x31090

/* S-channel opcodes (OpenMDK xgsm_schan.h) */
#define BCM_SCHAN_READ_MEM		0x07
#define BCM_SCHAN_WRITE_MEM		0x09
#define BCM_SCHAN_READ_REG		0x0b
#define BCM_SCHAN_WRITE_REG		0x0d

/*
 * One S-channel op. Header = op<<26 | blk<<20 | 4*words<<7, address word = addr (block only in the
 * header). Reads fill data[words] from the reply; ack (optional) gets the reply header.
 * Raw and unguarded: callers own the write policy.
 */
int bcm_cmic_schan_op(struct bcm_cmic_unit *u, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack);

static inline int bcm_cmic_reg_read(struct bcm_cmic_unit *u, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	return bcm_cmic_schan_op(u, BCM_SCHAN_READ_REG, blk, addr, data, words, ack);
}

static inline int bcm_cmic_mem_read(struct bcm_cmic_unit *u, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	return bcm_cmic_schan_op(u, BCM_SCHAN_READ_MEM, blk, addr, data, words, ack);
}

/* One clause-22 MIIM read. internal: INTERNAL_SEL (bus 0 = GPHYs, bus 1 = TSC). */
int bcm_cmic_miim_read(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 reg, u16 *val);

#endif /* BCM_CMIC_H */
