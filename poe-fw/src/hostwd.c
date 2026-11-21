/* Host watchdog (awto-image-only regs 0x20 HOST_WD_TIME, 0x21 HOST_WD_STAT). The host must write HOST_WD_TIME (the poke) before
 * the timeout, in 10 ms ticks, else all ports are shut, EXPIRED is latched and INT (SUP, SUP_EVT bit 0x40) is raised.
 * Reg 0x42 WDT_TIME is not used: it programs the 8051's own watchdog (SFR F9). Docs: docs/poe-fw-rewrite.md. */
#include "common.h"

__data u8 wd_armed;
__data u8 wd_cnt;
__bit __at(0x03) wd_trip;
__bit __at(0x04) wd_restart;

void wd_init(void)
{
	wd_armed = 0;
	wd_cnt = 0;
	wd_trip = 0;
	wd_restart = 0;
	HREG(R_HOST_WD_TIME) = 0;
	HREG(R_HOST_WD_STAT) = WDS_PRESENT;
}

/* Poke: any non-zero value arms and reloads the countdown (< 2 clamps to 2); 0 disarms. Read returns the written value. */
void wh_HOST_WD_TIME(void) __using(1)
{
	u8 v = i2c_wdata;

	HREG(R_HOST_WD_TIME) = v;
	if (!v) {
		wd_armed = 0;
		HREG(R_HOST_WD_STAT) &= (u8)~WDS_ARMED;
		return;
	}
	wd_cnt = (v < 2) ? 2 : v;
	wd_armed = 1;
	HREG(R_HOST_WD_STAT) |= WDS_ARMED;
}

/* Restart request (b7 = 1): clear EXPIRED and its INT cause now, disarm; the main loop re-initialises the channels. */
void wh_HOST_WD_STAT(void) __using(1)
{
	if (!(i2c_wdata & WDS_PRESENT))
		return;
	wd_armed = 0;
	HREG(R_HOST_WD_STAT) = WDS_PRESENT;
	HREG(R_SUP_EVT) &= (u8)~WD_SUP_BIT;
	HREG(R_SUP_EVT_CLR) &= (u8)~WD_SUP_BIT;
	if (!(HREG(R_SUP_EVT) | HREG(R_SUP_EVT_CLR)))
		HREG(R_INT) &= 0x7F;
	INT_UPDATE();
	wd_restart = 1;
}

/* Called from the timer-1 hook (bank 3), once per 10 ms tick. Expiry: latch, disarm, INT; the main loop drops the power. */
void wd_tick(void) __using(3)
{
	if (!wd_armed)
		return;
	if (--wd_cnt)
		return;
	wd_armed = 0;
	HREG(R_HOST_WD_STAT) = (HREG(R_HOST_WD_STAT) | WDS_EXPIRED) & (u8)~WDS_ARMED;
	HREG(R_SUP_EVT) |= WD_SUP_BIT;
	HREG(R_SUP_EVT_CLR) = HREG(R_SUP_EVT);
	HREG(R_INT) |= 0x80;
	if ((HREG(R_INT_MASK) & 0x80) && (HREG(R_MISC_CONF) & 0x80))
		INT_OUT = 1;
	wd_trip = 1;
}

/* Main loop: shut everything after a trip; re-initialise after a restart request. */
void wd_service(void)
{
	if (wd_trip) {
		wd_trip = 0;
		chan_shutdown_all();
	}
	if (wd_restart) {
		wd_restart = 0;
		chan_shutdown_all();
		chan_init();
	}
}
