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

#endif
