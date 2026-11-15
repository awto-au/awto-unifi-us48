/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef US48_PCIE_H
#define US48_PCIE_H

#include <linux/types.h>

/* 0 = link up and unit 1 BARs in place, <0 = failed (already logged) */
int us48_pcie_init(void);

/* Live check: link up, init succeeded and the CMIC probe passed. Gate every unit 1 BAR2 (CMIC) access on it. */
bool us48_pcie_up(void);

/*
 * Unit 1 CMIC probe (#98): arm = health read (BAR0 PAXB id) + marker; false = skip the CMIC (logged).
 * After the first BAR2 read call done(); us48_pcie_up() then also covers the CMIC.
 */
bool us48_unit1_cmic_arm(void);
void us48_unit1_cmic_done(bool ok);

#endif
