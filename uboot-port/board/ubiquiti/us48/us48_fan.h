/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef US48_FAN_H
#define US48_FAN_H

#include <linux/types.h>

struct us48_fan_rb {
	bool spif;
	int polls;
	u32 status, endqp, tx[7], a_out, a_oe, b_out, b_oe;
};

void us48_fan_boot(void);
void us48_smb_init(void);
u32 us48_smb_rx(void);
int us48_smb_xfer(const u32 *tx, int ntx, u32 cmd, int polls);
/* PoE: reset + broadcast firmware download; 0 = ok, -EINTR = key press, other errno = failed (boot continues, Linux loads) */
int us48_poe_load(void);

#endif
