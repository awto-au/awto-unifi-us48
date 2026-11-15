/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef US48_PCIE_H
#define US48_PCIE_H

/* 0 = link up and unit 1 BARs in place, <0 = failed (already logged) */
int us48_pcie_init(void);

#endif
