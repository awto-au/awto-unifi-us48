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

/* hook 3, stock isr_periodic 0x4EBA (bank 2): measurement chain, not ported (hal_periodic). */
void periodic_body(void) __using(2)
{
	hal_periodic();
}

/* hook 8 body: stock tick_timers */
void tick_body(void) __using(3)
{
	chan_tick();
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
	EA = 1;
	HREG(R_CRC_STATUS) = 0xAA;      /* firmware running (stock writes this first, 0x4071; moved after init) */

	for (;;) {                      /* main_loop 0x3D1A */
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
