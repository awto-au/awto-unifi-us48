/* SPDX-License-Identifier: GPL-2.0+ */
/* SRAM-resident clock switch with rollback. Design: docs/sram-clock-switch.md. UNTESTED on hardware. */
#ifndef US48_SRAM_CLK_H
#define US48_SRAM_CLK_H

#include <linux/types.h>

/* ARM PLL / CCU registers, docs/clocks.md */
#define CCU_WR_ACCESS	0x19000000
#define CCU_POLICY_FREQ	0x19000008
#define CCU_POLICY_CTL	0x1900000c
#define CCU_PLLARMA	0x19000c00
#define CCU_PLLARMB	0x19000c04
#define CCU_PLLARMC	0x19000c08
#define CCU_PLLARMCTL5	0x19000c20
#define CCU_POLICY_DBG	0x19000ec0

/* GENPLL, docs/clocks.md E5 */
#define GENPLL_CTRL0	0x1803fc00
#define GENPLL_CTRL1	0x1803fc04
#define GENPLL_CTRL2	0x1803fc08
#define GENPLL_CTRL3	0x1803fc0c
#define GENPLL_STATUS	0x1803fc18

/* SRAM layout: code [0, 0x800), area at +0x800 (status + command), end 0xb00; rest free for DDR retrain code */
#define US48_SRAM_BASE		0x1b000000
#define US48_SRAM_CODE_MAX	0x800
#define US48_SRAM_AREA_OFF	0x800

/* Op codes. POLL ops: b = max reads (1..US48_POLL_MAX); CHECK: one read, (v & a) == b */
#define US48_OP_WR		1	/* *addr = a */
#define US48_OP_RMW		2	/* *addr = (*addr & ~a) | b */
#define US48_OP_POLL_SET	3	/* until (v & a) == a */
#define US48_OP_POLL_CLR	4	/* until (v & a) == 0 */
#define US48_OP_CHECK		5

/* 1e5 reads x <= ~1 us per device read = 0.1 s; far below the 10 s SP805; rejects garbage counts */
#define US48_POLL_MAX		100000

#define US48_CLK_MAXFWD		24
#define US48_CLK_MAXRB		16
#define US48_CLK_MAGIC		0x55343843	/* "U48C" */

struct us48_clk_op {
	u32 op, addr, a, b;
};

/* Forward ops first (nfwd), then rollback ops (nrb). Rollback runs only if a forward op fails. */
struct us48_clk_cmd {
	u32 magic, nfwd, nrb, pad;
	struct us48_clk_op ops[US48_CLK_MAXFWD + US48_CLK_MAXRB];
};

/* Status word values written by the SRAM code, read back by the caller */
#define US48_ST_PENDING		0xdead0000
#define US48_ST_RUNNING		0xdead0001
#define US48_ST_OK		0
#define US48_ST_ROLLED_POLL	1	/* a poll expired, rolled back */
#define US48_ST_RBFAIL		2	/* failure, and a rollback op failed too */
#define US48_ST_BADCMD		3	/* rejected before any write */
#define US48_ST_ROLLED_CHECK	4	/* a CHECK mismatched, rolled back */

struct us48_clk_area;
struct us48_clk_area {
	u32 status;
	u32 fail_idx;	/* 1-based index of the failing forward op, 0 if none */
	u32 fail_val;	/* last register value read by the failing op */
	u32 pad;
	struct us48_clk_cmd cmd;
};

/*
 * Copy to SRAM, IRQs off, run, restore IRQs. Returns 0, -ETIMEDOUT (poll expired, rolled back),
 * -EPROTO (check mismatch, rolled back), -EIO (rollback incomplete: undefined clock state), -EINVAL.
 */
int us48_sram_exec(struct us48_clk_area *ar);
void us48_sram_end(void);
int us48_sram_clk_run(const struct us48_clk_cmd *cmd);
void us48_sram_clk_fail_info(u32 *idx, u32 *val);

/* CPU 800 (E1) or 1000 (E2). Caller does the E0 gate and arms SP805. */
int us48_sram_clk_cpu(int mhz);
/* GENPLL ch3 (AXI) mdiv, ch4 mdiv (0 = leave). Needs CONFIG_US48_SRAM_CLK_GENPLL, else -EPERM. */
int us48_sram_clk_axi(u32 ch3_mdiv, u32 ch4_mdiv);
/* DDR PLL: not implemented (no shmoo), -ENOSYS */
int us48_sram_clk_ddrpll(u32 mhz);

#endif
