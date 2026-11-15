/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef US48_PCIE_H
#define US48_PCIE_H

#include <linux/types.h>

/* 0 = link up and unit 1 BARs in place, <0 = failed (already logged) */
int us48_pcie_init(void);

/* Live check: link up and init succeeded. Gate every access to 0x08000000-0x0fffffff on it. */
bool us48_pcie_up(void);

#endif
