/* SPDX-License-Identifier: GPL-2.0 */
/* Boot stub contract between the us48_a2 remoteproc driver and a2fw/boot.S (payload mode). */
#ifndef US48_A2_BOOT_H
#define US48_A2_BOOT_H

#define US48_A2_BOOT_MAGIC	0x62323461	/* "a24b" */
#define US48_A2_BOOT_ARGS_OFF	0x100		/* from the carveout start; the driver writes it after the ELF load */

#ifndef __ASSEMBLER__
#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
typedef uint32_t __u32;
#endif
/* kernel and dtb are addresses in the a2 view; status becomes 1 when the stub reaches the jump */
struct us48_a2_boot_args {
	__u32 magic, kernel, dtb, status;
};
#endif

#endif
