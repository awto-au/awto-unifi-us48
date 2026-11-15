/* SPDX-License-Identifier: GPL-2.0 */
/* a2 probe knobs (#79): Cortex-A9 Diagnostic Control (CP15 c15 0 c0 1, secure RMW) and NIC-301 PCIe0 slave issuing override. */
#ifndef A2FW_DIAG_H
#define A2FW_DIAG_H

#include <stdint.h>

/* DDI0397I Table 3-2: IB reg 0x008 fn_mod_bm_iss bit0 read_iss_override, bit1 write: switch issues 1 transaction to the slave. */
#define A2_NIC_PCIE0_S0_ISS	0x1a004008U

static inline uint32_t a2_diag_rmw(uint32_t set)
{
	uint32_t v;

	__asm__ volatile("mrc p15, 0, %0, c15, c0, 1" : "=r"(v));
	if (set) {
		v |= set;
		__asm__ volatile("mcr p15, 0, %0, c15, c0, 1\n\tisb" : : "r"(v));
		__asm__ volatile("mrc p15, 0, %0, c15, c0, 1" : "=r"(v));
	}
	return v;
}

#endif
