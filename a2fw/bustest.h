/* SPDX-License-Identifier: GPL-2.0 */
/*
 * a2fw/bustest results struct, shared by the firmware (a2fw/bustest.c) and the a1 module (kernel/us48-bustest).
 * Lives in the carveout at BT_RES_OFF; a2 writes it through a non-cacheable alias, a1 reads it through a WC mapping.
 * All fields u32. Performance fields are timer ticks of the last measurement (BT_TICK_HZ), sizes are BT_* below.
 */
#ifndef A2FW_BUSTEST_H
#define A2FW_BUSTEST_H

#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
typedef uint32_t __u32;
#endif

#include "us48_hwdb.h"

#define BT_MAGIC	0x42555354	/* "BUST" */
#define BT_VERSION	1
#define BT_TICK_HZ	200000000U	/* global timer, PERIPHCLK 200 MHz (hwdb gtimer) */

/* Layout inside the carveout (offsets from the carveout base; MiB-aligned buffers = one MMU section each) */
/* a2_shared (hwdb): resource table +0, trace0 +0x1000, results +0x2000, page table +0x4000 */
#define BT_SHARED_OFF	(HW_A2_SHARED_BASE - HW_A2_CARVEOUT_BASE)
#define BT_RES_OFF	(BT_SHARED_OFF + 0x2000U)
#define BT_RES_SIZE	0x1000U
#define BT_PT_OFF	(BT_SHARED_OFF + 0x4000U)	/* 16 KiB aligned first-level page table */
#define BT_RBUF_OFF	0x00400000U	/* 1 MiB: cached read/write/copy */
#define BT_RBUF_SIZE	0x00100000U
#define BT_XBUF_OFF	0x00500000U	/* a2 writes a fixed pattern, a1 verifies */
#define BT_YBUF_OFF	0x00600000U	/* a1 writes a seeded pattern, a2 verifies */
#define BT_XY_SIZE	0x10000U
#define BT_SBUF_OFF	0x00700000U	/* a2 integrity sweep */
#define BT_UBYTES	0x10000U	/* uncached tests (word accesses over PCIe) */
#define BT_CBYTES	0x40000U	/* cached sweep */
#define BT_LAT_N	1024U
#define BT_IFETCH_BYTES	8192U
#define BT_MISS_MAX	4

/* Fields in order; BT_FIELDS(X) generates the struct and the name table used for the a1 text dump. */
#define BT_FIELDS(X) \
	X(magic) X(version) X(hb) X(pass) X(now) X(mmu) X(sctlr) X(phase) \
	X(rd_c) X(wr_c) X(cpy_c) X(rd_u) X(wr_u) X(cpy_u) \
	X(timer_ovh) X(lat_u_sum) X(lat_u_min) X(lat_u_max) X(lat_line_sum) \
	X(ifetch_cold) X(ifetch_warm) \
	X(sweep_pass) X(sweep_words) X(sweep_ticks) \
	X(mismatch) X(x_ready) X(y_seq) X(y_ack) X(y_seed) X(y_mismatch) \
	X(miss_n) \
	X(miss0_addr) X(miss0_exp) X(miss0_got) X(miss0_pass) X(miss0_t) \
	X(miss1_addr) X(miss1_exp) X(miss1_got) X(miss1_pass) X(miss1_t) \
	X(miss2_addr) X(miss2_exp) X(miss2_got) X(miss2_pass) X(miss2_t) \
	X(miss3_addr) X(miss3_exp) X(miss3_got) X(miss3_pass) X(miss3_t)

#define BT_F(n) __u32 n;
struct bt_results {
	BT_FIELDS(BT_F)
};
#undef BT_F

/* Fixed patterns: a2 fills XBUF with bt_xpat(i) (a1 verifies); a1 fills YBUF with bt_ypat(seed, i) (a2 verifies) */
static inline __u32 bt_xpat(__u32 i)
{
	return i * 0x9e3779b1U ^ 0xa5a5a5a5U ^ (i >> 5);
}

static inline __u32 bt_ypat(__u32 seed, __u32 i)
{
	return (i + seed) * 0x85ebca6bU ^ seed * 0xc2b2ae35U ^ (i << 9);
}

#define BT_FIELD_OFF(n)	(__builtin_offsetof(struct bt_results, n))

#endif
