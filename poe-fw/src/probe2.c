/* PROBE v2: evidence for the detect flow on silicon (awto-probe2 image only; empty in the other images).
 * Regs: 0x1E start tick, 0x1F ev6 delta, 0x74 end delta (D4 live raw is not written) (0xFF = none), 0x45 flags (D=1 written ch0..3 | ev6 seen ch0..3),
 * 0x4F flags (timeout ch0..3 | result read ch0..3), 0x40 re-arm (write 1), 0x24..0x2B state history of channel 1 (newest first),
 * 0x30..0x3F raw D ring (sampled in chan_tick, schedule probe_sched). Tick = hook 8 count low byte, reg 0x2D. */
#include "common.h"

#ifdef POE_FW_PROBE2
#define HCH 1
__xdata u8 pr_ch = 0xFF, pr_t, pr_idx;
static __xdata u8 pr_last = 0xFF;

void probe_hist(u8 ch, u8 st)
{
	static __xdata u8 i;

	if (ch != HCH || st == pr_last)
		return;
	pr_last = st;
	for (i = 7; i > 0; i--)
		HREG(0x24 + i) = HREG(0x24 + i - 1);
	HREG(0x24) = st;
}

void probe_start(u8 ch)
{
	static __xdata u8 i;

	for (i = 0; i < 16; i++)
		HREG(0x30 + i) = 0xEE;
	pr_t = 0;
	pr_idx = 0;
	pr_ch = ch;
	HREG(0x1E) = HREG(0x2D);
	HREG(0x1F) = 0xFF;
	HREG(0x74) = 0xFF;
	HREG(0x45) |= (u8)(1 << ch);
}

void probe_ev6(u8 ch)
{
	HREG(0x45) |= (u8)(0x10 << ch);
	if (HREG(0x1F) == 0xFF)
		HREG(0x1F) = (u8)(HREG(0x2D) - HREG(0x1E));
}

void probe_end(u8 ch, u8 tmo)
{
	HREG(0x4F) |= tmo ? (u8)(1 << ch) : (u8)(0x10 << ch);
	HREG(0x74) = (u8)(HREG(0x2D) - HREG(0x1E));
}

/* host write 1 to 0x40: clear the capture and restart detect on every channel that is not shut */
void probe_poll(void)
{
	static __xdata u8 c;

	if (!HREG(0x40))
		return;
	HREG(0x40) = 0;
	HREG(0x45) = 0;
	HREG(0x4F) = 0;
	for (c = 0; c < NCH; c++)
		if ((HREG(R_OP_MOD) >> (2 * c)) & 3)
			chan_request(c, CH_PEND_RESET);
}
#endif
