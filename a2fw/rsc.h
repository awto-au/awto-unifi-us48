/* SPDX-License-Identifier: GPL-2.0 */
/* remoteproc resource table of the a2 firmwares: carveout + trace0. Including file defines `trace`. */
#ifndef A2FW_RSC_H
#define A2FW_RSC_H

#include <stddef.h>
#include <stdint.h>
#include "us48_hwdb.h"

#define RSC_CARVEOUT	0
#define RSC_TRACE	2
#ifndef TRACE_SZ
#define TRACE_SZ	4096
#endif

struct fw_rsc_carveout {
	uint32_t da, pa, len, flags, reserved;
	char name[32];
};

struct fw_rsc_trace {
	uint32_t da, len, reserved;
	char name[32];
};

/* Layout of include/linux/remoteproc.h: header, offsets, then (type, resource) entries */
struct a2_rsc_table {
	uint32_t ver, num, reserved[2];
	uint32_t offset[2];
	uint32_t carveout_type;
	struct fw_rsc_carveout carveout;
	uint32_t trace_type;
	struct fw_rsc_trace trace;
};

extern char trace[TRACE_SZ];

__attribute__((section(".resource_table"), used))
const struct a2_rsc_table a2_rsc = {
	.ver = 1,
	.num = 2,
	.offset = { offsetof(struct a2_rsc_table, carveout_type), offsetof(struct a2_rsc_table, trace_type) },
	.carveout_type = RSC_CARVEOUT,
	.carveout = { .da = HW_A2_CARVEOUT_BASE, .len = HW_A2_CARVEOUT_SIZE, .name = "a2-carveout" },
	.trace_type = RSC_TRACE,
	.trace = { .da = (uint32_t)trace, .len = TRACE_SZ, .name = "trace0" },
};

#endif
