/* SPDX-License-Identifier: GPL-2.0 */
/* DCC console of the a2 (CoreSight DBGDTRTX/DBGDTRRX) as a tty on the host side. */
#ifndef US48_A2_DCC_H
#define US48_A2_DCC_H

struct dentry;
struct device;
struct regmap;
struct us48_a2_dcc;

/* Registers the tty (ttyA2) and the log; no polling until _start(). Freed with @dev. */
struct us48_a2_dcc *us48_a2_dcc_create(struct device *dev, struct regmap *map);
/* debugfs file dcc_log (last 8 KiB of a2 console output) in the remoteproc directory. */
void us48_a2_dcc_debugfs(struct us48_a2_dcc *d, struct dentry *dir);
/* Poll from now on: call before a2 is released (hvc_dcc_check on a2 gives up after 100 ms). */
void us48_a2_dcc_start(struct us48_a2_dcc *d);
void us48_a2_dcc_stop(struct us48_a2_dcc *d);

#endif
