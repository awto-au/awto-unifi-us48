/* SPDX-License-Identifier: GPL-2.0 */
/* Boot stub contract between the us48_a2 remoteproc driver and a2fw/boot.S (payload mode). */
#ifndef US48_A2_BOOT_H
#define US48_A2_BOOT_H

#define US48_A2_BOOT_MAGIC	0x62323461	/* "a24b" */
#define US48_A2_BOOT_ARGS_OFF	0x200		/* from the carveout start; the driver writes it after the ELF load */
#define US48_A2_BOOT_F_DCC	1		/* flags: the stub writes marker characters to DCC (proves the console path) */
#define US48_A2_BOOT_DCC_SPIN	0x100000	/* stub: max DSCR polls per marker, see boot.S */

#ifndef __ASSEMBLER__
#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
typedef uint32_t __u32;
#endif
/*
 * kernel and dtb are addresses in the a2 view; the driver writes magic..flags, the stub writes the rest.
 * status 1 = the stub reached the jump; stage 1 = magic ok, 2 = first DCC marker written, 3 = at the jump;
 * dcc_fail counts markers skipped because DSCR.DTRTXfull stayed set for US48_A2_BOOT_DCC_SPIN polls.
 */
struct us48_a2_boot_args {
	__u32 magic, kernel, dtb, status, flags, dcc_fail, stage, rsvd;
};
#endif

#endif
