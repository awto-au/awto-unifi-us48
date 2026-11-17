/* SPDX-License-Identifier: GPL-2.0
 * Host-only fixture. The Python test inserts the actual production definitions.
 * Only models register access and ACTIVE clearing after ABORT, not the bus/DMA engine.
 */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef uint32_t __le32;
#define BIT(n) (1u << (n))
#define le32_to_cpu(v) (v)
/* PRODUCTION_MACROS */
/* PRODUCTION_STATE */
struct bcm_cmic_unit { u32 regs[0x31200 / 4]; };
struct bcm_hr2_dma { unsigned int tx_len; __le32 *tx_dcb; };
static unsigned int reads, writes, delays, errors, abort_writes, resumes;
static int active_left, active_ch;
static bool abort_requested, diag;

static u32 rd(struct bcm_cmic_unit *u, u32 off)
{
	reads++;
	if (off == DMA_STAT && abort_requested && active_left >= 0) {
		if (!active_left)
			u->regs[off / 4] &= ~DMA_ACTIVE(active_ch);
		else
			active_left--;
	}
	return u->regs[off / 4];
}
static void wr(struct bcm_cmic_unit *u, u32 off, u32 val)
{
	writes++;
	if ((off == DMA_CTRL(0) || off == DMA_CTRL(1)) && (val & DMA_ABORT)) {
		abort_requested = true;
		abort_writes++;
		assert(val & DMA_EN);
	}
	u->regs[off / 4] = val;
}
static void udelay(unsigned int us) { delays += us; }
#define pr_err(...) (errors++)
/* PRODUCTION_SHARED */
struct us48_eth_priv { struct bcm_cmic_unit unit; bool up; };
struct udevice { const char *name; struct us48_eth_priv *priv; };
static struct us48_eth_priv *dev_get_priv(struct udevice *d) { return d->priv; }
static unsigned long env_get_ulong(const char *s, int base, unsigned long def)
{
	assert(!strcmp(s, "us48_handoff_diag") && base == 10 && def == 0);
	return diag;
}
static void us48_quiesce(struct us48_eth_priv *p, bool on)
{
	(void)p;
	assert(!on);
	resumes++;
}
/* PRODUCTION_DRIVER */

int main(int argc, char **argv)
{
	struct us48_eth_priv p = {0};
	struct bcm_cmic_unit *u = &p.unit;
	struct udevice dev = { "unit-under-test", &p };
	const char *test;
	int ch;
	assert(argc == 2);
	test = argv[1];
	if (!strcmp(test, "idle") || !strcmp(test, "enabled_idle") ||
	    !strcmp(test, "active") || !strcmp(test, "timeout")) {
		for (ch = 0; ch < 2; ch++) {
			u32 keep = DMA_ENDIAN | (ch ? 0 : DMA_DIR_TX);
			reads = writes = delays = errors = abort_writes = 0;
			abort_requested = false;
			active_ch = ch;
			active_left = !strcmp(test, "timeout") ? -1 : 3;
			u->regs[DMA_CTRL(ch) / 4] = keep | (!strcmp(test, "idle") ? 0 : DMA_EN);
			u->regs[DMA_DESC(ch) / 4] = 0x03101000;
			u->regs[DMA_STAT / 4] = (!strcmp(test, "active") || !strcmp(test, "timeout")) ? DMA_ACTIVE(ch) : 0;
			u->regs[DMA_DESC(1 - ch) / 4] = 0x1234;
			bcm_hr2_dma_abort(u, ch);
			assert(u->regs[DMA_CTRL(ch) / 4] == keep);
			assert(u->regs[DMA_DESC(ch) / 4] == 0);
			assert(u->regs[DMA_DESC(1 - ch) / 4] == 0x1234);
			assert(u->regs[DMA_STAT_CLR / 4] == BIT(ch));
			if (!strcmp(test, "timeout")) {
				assert(errors == 1 && delays == ABORT_WAIT_US);
				/* Clearing CTRL/DESC is NOT proof of a successful abort. */
				assert(u->regs[DMA_STAT / 4] & DMA_ACTIVE(ch));
			} else {
				assert(!errors && !(u->regs[DMA_STAT / 4] & DMA_ACTIVE(ch)));
			}
			assert(abort_writes == ((!strcmp(test, "active") || !strcmp(test, "timeout")) ? 1u : 0u));
		}
	} else if (!strcmp(test, "snapshot")) {
		struct bcm_hr2_dma_state s;
		u->regs[DMA_CTRL(0) / 4] = 1;
		u->regs[DMA_DESC(0) / 4] = 0x03101000;
		u->regs[DMA_CTRL(1) / 4] = 2;
		u->regs[DMA_DESC(1) / 4] = 0x03100000;
		u->regs[DMA_STAT / 4] = DMA_ACTIVE(1);
		abort_requested = true; active_ch = 1; active_left = 1;
		bcm_hr2_dma_read_state(u, &s);
		assert(reads == 6 && !writes && !delays);
		assert(s.ctrl[0] == 1 && s.ctrl[1] == 2);
		assert(s.desc[0] == 0x03101000 && s.desc[1] == 0x03100000);
		assert(s.stat_before == DMA_ACTIVE(1) && s.stat_after == 0);
	} else if (!strcmp(test, "tx_reap")) {
		__le32 dcb[32] = {0};
		struct bcm_hr2_dma d = { .tx_len = 60, .tx_dcb = dcb };
		u->regs[DMA_CTRL(0) / 4] = DMA_DIR_TX | DMA_EN;
		u->regs[DMA_DESC(0) / 4] = 0x03101000;
		assert(!bcm_hr2_tx_reap(u, &d) && !writes);
		dcb[31] = DCB_W15_DONE;
		assert(bcm_hr2_tx_reap(u, &d));
		assert(u->regs[DMA_CTRL(0) / 4] == 1 && !d.tx_len);
		assert(u->regs[DMA_DESC(0) / 4] == 0x03101000);
	} else if (!strcmp(test, "remove")) {
		p.up = true;
		for (ch = 0; ch < 2; ch++) {
			u->regs[DMA_CTRL(ch) / 4] = (ch ? 0 : DMA_DIR_TX) | DMA_EN;
			u->regs[DMA_DESC(ch) / 4] = 0x03100000 + ch * 0x1000;
		}
		assert(!us48_eth_remove(&dev) && resumes == 1);
		assert(!u->regs[DMA_DESC(0) / 4] && !u->regs[DMA_DESC(1) / 4]);
		assert(u->regs[DMA_CTRL(0) / 4] == 1 && !u->regs[DMA_CTRL(1) / 4]);
		assert(!errors);
	} else if (!strcmp(test, "diag_guards")) {
		p.up = true;
		us48_eth_handoff_state(&dev, "disabled");
		assert(!reads && !writes);
		diag = true; p.up = false;
		assert(!us48_eth_remove(&dev));
		assert(!reads && !writes);
		p.up = true;
		us48_eth_handoff_state(&dev, "enabled");
		assert(reads == 6 && !writes);
	} else {
		assert(!"unknown case");
	}
	return 0;
}
