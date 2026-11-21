/* Start-up, main loop and the hook bodies that are not I2C / channel ISRs. Spec: docs/poe-fw.md sections 3, 5. */
#include "common.h"

/* stock 0x80BF / 0x80C1..0x80C3: channel notified by ISR 2 for each of reg 0x17 bits 3:0 (defaults from 0x236E) */
static __xdata u8 evch[4];

/* hook 2, stock isr_event_ext 0x4879 (bank 3): reg 0x17 bit n -> channel evch[n] ev_lo |= 0x10, all channels serviced. */
void isr2_body(void) __using(3)
{
	u8 v = HREG(R_MISC_CONF);
	u8 n;

	for (n = 0; n < 4; n++)
		if (v & (u8)(1 << n))
			chans[evch[n] & 3].ev_lo |= 0x10;     /* channel index bounded (stock unchecked) */
	svc0 = 1; svc1 = 1; svc2 = 1; svc3 = 1;
	MUX_SEL = 0x3F;
}

/* hook 3, stock isr_periodic 0x4EBA (bank 2): stock runs 0x1B03 / 0x37D2 / 0x34D0 / 0x29EE / 0x316B here (about 4.4 calls per tick,
 * board 2026-11-21). Ours only counts the call; the main loop runs the chain (periodic_chain): the long arithmetic needs the
 * non-reentrant library routines and bank 0, so it must not run in this ISR. */
__xdata u8 per_cnt;

void periodic_body(void) __using(2)
{
	per_cnt++;
	HREG(R_REG_16) |= 0x01;      /* bench status: hook 3 body entered */
}

/* hook 8 body: stock tick_timers */
void tick_body(void) __using(3)
{
	chan_tick();
	wd_tick();
}

/* Timer liveness (G, probe v3 freeze): about 65536 main passes (several seconds) with neither the hook-8 nor the hook-3 counter moving
 * means the timer interrupts are dead: re-arm timer 1, INT1 and the priorities. HREG(0x33) counts recoveries in the probe v3 image. */
__xdata u8 storm_cnt;       /* channel ISR entries since the last main-loop pass (incremented by chan_isr.s, build.py) */

/* Interrupt storm limiter (G): if a channel event line is stuck asserted the channel ISRs would starve the timers and the main loop
 * (board 2026-11-22, freeze after the first detect result). chan_isr.s switches EX1 off at 128 entries; here it comes back after a
 * cool-down of 256 main passes. HREG(0x33) high nibble counts storms in the probe v3 image. */
static void storm_check(void)
{
	static __xdata u8 wait;

	if (!storm_cnt)
		return;
	if (storm_cnt < 0x80) {
		storm_cnt = 0;
		return;
	}
	if (++wait == 0) {
		storm_cnt = 0;
		EX1 = 1;
#ifdef POE_FW_PROBE3
		HREG(0x33) += 0x10;
#endif
	}
}

void live_check(void)
{
	storm_check();
	static __xdata u16 n;
	static __xdata u8 t8, p3;

	if (++n)
		return;
	if (HREG(0x2D) == t8 && per_cnt == p3) {
		EA = 0;
		TMOD |= 0x10;
		TL1 = 0xF4;
		TH1 = 0xE5;
		TR1 = 1;
		ET1 = 1;
		EX1 = 1;
		SFR_IP = 0x3E;
		EA = 1;
#ifdef POE_FW_PROBE3
		HREG(0x33)++;
#endif
	}
	t8 = HREG(0x2D);
	p3 = per_cnt;
}

void fw_main(void)
{
	u8 strap;
	u8 n;

	for (n = 0; n < 4; n++)
		evch[n] = n;
	P0 = 0x01;                      /* stock 0x3CE9: P0.0 strap, picks the register default set */
	strap = P0 & 0x01;
	rom_strap_call(strap);          /* stock 0x3CF1..0x3CF7 / 0x3CFF..0x3D05 */
	hostregs_init(strap);
	hal_timer_init();               /* 0x5083 */
	hal_ext_init();                 /* 0x50B7 */
	hal_sfr_init();                 /* tail of init_hw_channels 0x3922 */
	chan_init();                    /* head of init_hw_channels 0x3922 */
	meas_init();                    /* stock 0x415C */
	EA = 1;
	HREG(R_CRC_STATUS) = 0xAA;      /* firmware running (stock writes this first, 0x4071; moved after init) */

	for (;;) {                      /* main_loop 0x3D1A */
#ifdef POE_FW_PROBE3
		HREG(0x34)++;               /* main loop pass counter (low byte) */
#endif
		live_check();
		wd_service();
		periodic_chain();
		poll_common();
		if (svc0) { svc0 = 0; chan_run(0); }
		if (svc1) { svc1 = 0; chan_run(1); }
		if (svc2) { svc2 = 0; chan_run(2); }
		if (svc3) { svc3 = 0; chan_run(3); }
		/* stock 0x3D51: TEMP raw < 0x40 -> SUP_EVT 0x80. Gated: without the measurement chain TEMP stays 0 (TODO). */
		if (hal_temp_valid() && HREG(R_TEMP) < 0x40 && !(HREG(R_SUP_EVT) & 0x80))
			sup_event(0x80);
		if (HREG(R_REG_4A)) {           /* i2c_error_flag, set by the ISR */
			HREG(R_REG_4A) = 0;
			chan_i2c_error_reload();
		}
	}
}
