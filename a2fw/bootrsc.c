// SPDX-License-Identifier: GPL-2.0
/* Resource table + trace of the boot stub image (the stub itself is boot.S). */
#include "rsc.h"

char trace[TRACE_SZ] __attribute__((aligned(4096), section(".trace"))) = "a2 boot stub image: payload via us48_a2\n";
