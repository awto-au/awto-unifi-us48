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
#ifdef POE_FW_PROBE
	PCNT(P_H2_LO, P_H2_HI);
#endif
}

/* hook 3, stock isr_periodic 0x4EBA (bank 2): measurement chain, not ported (hal_periodic). */
void periodic_body(void) __using(2)
{
#ifdef POE_FW_PROBE
	PCNT(P_H3_LO, P_H3_HI);      /* probe: body deliberately empty (no measurement work), so the period is not distorted */
	HREG(P_STAT) |= 0x01;
#else
	hal_periodic();
#endif
}

/* hook 8 body: stock tick_timers */
void tick_body(void) __using(3)
{
	chan_tick();
	wd_tick();
#ifdef POE_FW_PROBE
	PCNT(P_H8_LO, P_H8_HI);
#ifdef POE_FW_PROBE_T2
	{
		static __xdata u8 t2n;

		if (++t2n >= 50) {       /* ~50 ticks: software-set EXF2 / TF2 (T2 interrupt request on an 8052), clear "entered" */
			t2n = 0;
			HREG(P_STAT) = (u8)((HREG(P_STAT) & ~0x01) | 0x04);
			T2CON_6 = 1;
			T2CON_7 = 1;
		}
	}
#endif
#endif
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
#ifdef POE_FW_PROBE
	{
		static const __code u8 pr[] = { P_H3_LO, P_H3_HI, P_H8_LO, P_H8_HI, P_H2_LO, P_H2_HI, 0x54, 0x59, 0x5A, 0x5B, 0x5C, 0x5D, 0x1C, 0x1D };

		for (n = 0; n < sizeof pr; n++)
			HREG(pr[n]) = 0;
		HREG(P_STAT) = (u8)((POE_FW_PROBE_ID << 4)
#ifdef POE_FW_PROBE_ARM
			| 0x02
#endif
		);
	}
#endif
	EA = 1;
	HREG(R_CRC_STATUS) = 0xAA;      /* firmware running (stock writes this first, 0x4071; moved after init) */

	for (;;) {                      /* main_loop 0x3D1A */
		wd_service();
		if (meas_due) {
			meas_due = 0;
			meas_step();
		}
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
