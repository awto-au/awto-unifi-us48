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
	void __iomem *base;	/* unit 0: AXI 0x48000000; unit 1: PCIe BAR2 */
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
#define BCM_SCHAN_TABLE_INSERT		0x24
#define BCM_SCHAN_TABLE_DELETE		0x26

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

static inline int bcm_cmic_mem_write(struct bcm_cmic_unit *u, u32 blk, u32 addr, u32 *data, int words)
{
	return bcm_cmic_schan_op(u, BCM_SCHAN_WRITE_MEM, blk, addr, data, words, NULL);
}

static inline int bcm_cmic_reg_write(struct bcm_cmic_unit *u, u32 blk, u32 addr, u32 *data, int words)
{
	return bcm_cmic_schan_op(u, BCM_SCHAN_WRITE_REG, blk, addr, data, words, NULL);
}

/* One clause-22 MIIM read. internal: INTERNAL_SEL (bus 0 = GPHYs, bus 1 = TSC). */
int bcm_cmic_miim_read(struct bcm_cmic_unit *u, bool internal, u32 bus, u32 phy, u32 reg, u16 *val);

/*
 * HR2 unit-1 (BCM53346) front-panel port 50 (xe1, logical port 27) bring-up and CPU packet I/O.
 * Same chip programming for Linux and U-Boot (bcm_cmic_hr2.c); reference history: us48_cmic.c.
 * DMA memory must be below the chip's 128 MiB inbound window (driver-internals 14.3).
 */
/* 10G port used for network: logical port + TSC lane of the same front port. Front 49 = 26/0, front 50 = 27/2 (the bench DAC is on 49 since 2026-11-13). */
#define BCM_HR2_XE1_PORT	26
#define BCM_HR2_XE1_LANE	0
#define BCM_HR2_PRIVATE_VID	48
#define BCM_HR2_RX_RING		16
#define BCM_HR2_BUF_SZ		2048
#define BCM_HR2_DCB_BYTES	64

/* Caller-provided memory; *_bus = address the chip uses, ptr = address the CPU uses. */
struct bcm_hr2_dma {
	__le32 *rx_dcb;			/* RX_RING x 16 words */
	u32 rx_dcb_bus;
	u8 *rx_buf;			/* RX_RING x BUF_SZ */
	u32 rx_buf_bus;
	__le32 *tx_dcb;			/* 2 x 16 words */
	u32 tx_dcb_bus;
	u8 *tx_buf;			/* BUF_SZ */
	u32 tx_buf_bus;
	int rx_next;
	unsigned int tx_len;		/* 0 = idle */
};

/* Abort both channels, then ch0 = TX, ch1 = RX, all CPU COS to ch1. */
void bcm_hr2_dma_init(struct bcm_cmic_unit *u);
/* Abort one channel (0 TX, 1 RX); safe on idle channels. */
void bcm_hr2_dma_abort(struct bcm_cmic_unit *u, int ch);
/* xe1 MAC/serdes enable (skipped if already enabled), link bitmap, EGR_ENABLE, private VLAN, PORT_VID. */
int bcm_hr2_xe1_bringup(struct bcm_cmic_unit *u, int cml);
/* Static L2X entry mac -> CPU in the private VLAN (del: remove it). */
int bcm_hr2_l2_cpu(struct bcm_cmic_unit *u, const u8 *mac, bool add);
/* Put PORT_TAB[27].PORT_VID back to VLAN 1 (shutdown). */
void bcm_hr2_port_vid_restore(struct bcm_cmic_unit *u);

void bcm_hr2_rx_arm(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d);
/* Next received frame (CRC and private VLAN tag stripped): length > 0 and *pkt, or 0 if none. */
int bcm_hr2_rx_next(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d, u8 **pkt);
/* Consume the frame bcm_hr2_rx_next returned; re-arms the ring when the chain ends. */
void bcm_hr2_rx_done(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d);
/* Start TX of one frame (no FCS) straight to port 27; -EBUSY if one is in flight, -EMSGSIZE if too long. */
int bcm_hr2_tx_start(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d, const u8 *frame, unsigned int len);
/* true once the frame in flight is finished (channel stopped, d->tx_len cleared). */
bool bcm_hr2_tx_reap(struct bcm_cmic_unit *u, struct bcm_hr2_dma *d);

#endif /* BCM_CMIC_H */
