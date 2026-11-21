/* Per-channel state machine: detect -> classify -> power-up -> monitor, 10 ms tick. Spec: docs/poe-fw.md section 11, 17.
 * Written from the spec and the stock state functions (addresses in comments); [I] = inferred, [G] = our guess.
 * Differences from stock are listed in poe-fw/STATUS.md. */
#include "common.h"

__xdata __at(0x8078) struct chan chans[NCH];

enum {
	ST_PORT_RESET,      /* stock st_port_reset 0x4AD7 */
	ST_PORT_SHUTDOWN,   /* 0x2EE0 */
	ST_PORT_CFG,        /* 0x44ED */
	ST_MODE_SELECT,     /* 0x454A */
	ST_DETECT_ARM,      /* 0x46AB */
	ST_DETECT_START,    /* 0x4601 */
	ST_DETECT_WAIT,     /* 0x1A60: waits for channel event bit 6 (ev_lo bit 0) or the 50-tick timer */
	ST_DETECT_RESULT,   /* 0x3B3C */
	ST_DETECT_INVALID,  /* 0x48BE */
	ST_CLASS_GATE,      /* 0x41D9 */
	ST_CLASS_WAIT,      /* 0x47E4: waits for the 0x29EE mux result or the 20-tick window */
	ST_POWER_REQUEST,   /* 0x3FBE */
	ST_POWER_ON,        /* 0x4A31 */
	ST_POWER_WAIT,      /* 0x2F8C: waits for the hardware power-good event (channel event bit 4) */
	ST_POWER_TRIG,      /* B bit 7 two ticks after power good (stock t+2, oracle) */
	ST_MONITOR,         /* parked after power good [I] */
	ST_FAULT,           /* 0x4D5C */
	ST_DISCONNECT,      /* 0x4F9F */
	ST_IDLE,            /* 0x4EEF */
	ST_WAIT             /* generic st_wait_b* family: wait for a timer bit, then next */
};

/* Event timer bits (stock set_timer R3 mask) and tick counts (10 ms): docs/poe-fw.md section 17. */
#define TMR_DET    0   /* detect window 50 ticks (0x4601: R3=1 R5=0x32) */
#define TMR_RETRY  1   /* re-detect spacing 200 ticks (0x5091: 0x02 / 0xC8) */
#define TMR_CLSWIN 2   /* class window 20 ticks (0x41D9: R3=4 R5=0x14) */
#define TMR_TRIG   3   /* B bit 7 trigger 2 ticks after power good (oracle) */
#define TMR_FAULT  5   /* fault cool-down 75 ticks (0x4D5C: R3=0x20 R5=0x4B) */
#define TMR_CLEAN  6   /* disconnect cleanup 30 ticks (0x4F9F) */
#define TMR_IDLE   7   /* idle 60 ticks / power-up hold 20 ticks (0x4EEF / 0x4FB7) */

#define F_POWERED 0x01
#define F_HP      0x02     /* class 4: F2 enabled, current scale 6403 (stock bit 5 of 0x80DE + 2c) */

/* stock tables 0x4C03 (A1..A4 limit per class code) and 0x4C0B (F2 enable per class code), read at 0x30EA / 0x30DA (C values, U meaning) */
static const __code u8 lim_tab[8] = { 0xF0, 0x48, 0x84, 0xF0, 0xCC, 0xF0, 0xF0, 0xF0 };
static const __code u8 hp_tab[8] = { 0, 0, 0, 0, 1, 0, 0, 0 };
/* host OV_CUT value written at power good: class 4 only (C, stock steady state); other classes keep the host value [G] */
static const __code u8 icut_tab[8] = { 0, 0, 0, 0, 0x11, 0, 0, 0 };

static __data u8 probe_owner;     /* one channel at a time uses the detect / class hardware [G: replaces stock 0x3DA6 / 0x316B] */

#define SVC_SET(c) do { switch (c) { case 0: svc0 = 1; break; case 1: svc1 = 1; break; case 2: svc2 = 1; break; default: svc3 = 1; break; } } while (0)

static void svc_set(u8 ch)
{
	SVC_SET(ch);
}

/* stock arm_channel 0x49FF (C, oracle): state set, service flag set at once; the delay re-sets the flag after dly ticks. */
static void arm(u8 ch, u8 st, u8 dly)
{
	struct chan __xdata *c = &chans[ch];

	EA = 0;
	c->state = st;
	c->dly = dly;
	EA = 1;
	svc_set(ch);                                 /* 0x49FF: SETB 0x23.(c+1) whatever the delay; tick_timers sets it again at 0 */
}

/* stock set_timer 0x5024 */
static void tmr_set(u8 ch, u8 idx, u8 ticks)
{
	struct chan __xdata *c = &chans[ch];

	EA = 0;
	c->tmr[idx] = ticks;
	c->wait_b &= (u8)~(1 << idx);
	EA = 1;
}

static void wait_then(u8 ch, u8 idx, u8 ticks, u8 next, u8 dly)
{
	struct chan __xdata *c = &chans[ch];

	tmr_set(ch, idx, ticks);
	c->wnext = next;
	c->wbit = idx;
	c->wdly = dly;
	c->state = ST_WAIT;
}

static u8 opmode(u8 ch)
{
	if (HREG(R_HOST_WD_STAT) & WDS_EXPIRED)       /* host watchdog expired: nothing powers until a restart request */
		return 0;
	return (HREG(R_OP_MOD) >> (2 * ch)) & 3;
}

static void sat_set(u8 ch, u8 mask, u8 val)
{
	HREG(R_SAT(ch)) = (HREG(R_SAT(ch)) & (u8)~mask) | val;
}

static void release_probe(u8 ch)
{
	if (probe_owner == ch)
		probe_owner = 0xFF;
}

static u8 acquire_probe(u8 ch)
{
	if (probe_owner != 0xFF && probe_owner != ch)
		return 0;
	probe_owner = ch;
	return 1;
}

void svc_request(u8 ch)
{
	svc_set(ch);
}

/* bench diagnostics (awto images): last detect path / channel / raw D, event and timeout counters, 0x3651 sample words */
static void diag_detect(u8 ch, u8 path, u8 raw)
{
	HREG(0x1C) = (u8)((ch << 4) | path);         /* path 1 = event (result read), 2 = timeout */
	HREG(0x1D) = raw;
}

static void diag_words(__xdata u8 *w)
{
	HREG(0x76) = w[0]; HREG(0x77) = w[1];        /* A */
	HREG(0x59) = w[2]; HREG(0x5A) = w[3];        /* B */
	HREG(0x5B) = w[4]; HREG(0x5C) = w[5];        /* C */
	HREG(0x5D) = w[6]; HREG(0x54) = w[7];        /* D */
}

void chan_request(u8 ch, u8 bit)
{
	EA = 0;
	chans[ch].pend |= bit;
	EA = 1;
	svc_set(ch);
}

void chan_init(void)
{
	u8 c, i;

	probe_owner = 0xFF;
	for (c = 0; c < NCH; c++) {
		u8 __xdata *p = (u8 __xdata *)&chans[c];

		for (i = 0; i < CH_STRIDE; i++)
			p[i] = 0;
		arm(c, ST_PORT_RESET, 1);          /* stock init_hw_channels arms st_port_reset, delay 1 */
	}
}

/* Host watchdog trip / restart: OP_MOD := 0 and the PORT_RESET path (power off, SAT_PWR, events) now, for every channel. */
void chan_shutdown_all(void)
{
	u8 c;

	HREG(R_OP_MOD) = 0;
	for (c = 0; c < NCH; c++) {
		chans[c].pend = 0;
		chans[c].state = ST_PORT_RESET;
		chan_run(c);
	}
}

void chan_restart_all(void)
{
	svc0 = 1; svc1 = 1; svc2 = 1; svc3 = 1;
}

/* stock reload_after_i2c_error 0x3D92 / 0x4CBA: the stock per-channel effect is not understood (flag bit of the 0x80DE+2c word),
 * so it is not ported. Deliberately no port action on a bus error. TODO. */
void chan_i2c_error_reload(void)
{
}

/* stock tick_timers 0x3C1E, called from the timer 1 hook. Bank 3, no calls. */
#ifdef POE_FW_PROBE2
extern __xdata u8 pr_ch, pr_t, pr_idx;
static const __code u8 probe_sched[16] = { 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50, 60, 70 };
#endif

void chan_tick(void) __using(3)
{
	static __xdata u8 c, i;
	static struct chan __xdata * __xdata p;

#ifdef POE_FW_PROBE2
	if (pr_ch < NCH) {                                   /* raw D(ch) ring, ticks counted from the D = 1 write */
		pr_t++;
		if (pr_t == probe_sched[pr_idx]) {
			switch (pr_ch) {
			case 0: HREG(0x30 + pr_idx) = CH0_DET; break;
			case 1: HREG(0x30 + pr_idx) = CH1_DET; break;
			case 2: HREG(0x30 + pr_idx) = CH2_DET; break;
			default: HREG(0x30 + pr_idx) = CH3_DET; break;
			}
			if (++pr_idx == 16)
				pr_ch = 0xFF;
		}
	}
#endif

	for (c = 0; c < NCH; c++) {
		p = &chans[c];
		if (p->dly && --p->dly == 0)
			SVC_SET(c);
		for (i = 0; i < 8; i++) {
			if (p->tmr[i] && --p->tmr[i] == 0) {
				p->wait_b |= (u8)(1 << i);
				SVC_SET(c);
			}
		}
	}
}

void chan_run(u8 ch)
{
	static struct chan __xdata * __xdata c;      /* XDATA statics: IRAM is full (chan_run is main-context only) */
	static __xdata u8 m, mode, i;

	c = &chans[ch];
	m = (u8)(1 << ch);
	mode = opmode(ch);
	probe_hist(ch, c->state);
	if (c->ev_lo & 0x01)
		probe_ev6(ch);

	if ((c->pend & CH_PEND_RESET) && c->state != ST_PORT_RESET) {
		c->pend &= (u8)~CH_PEND_RESET;
		arm(ch, ST_PORT_RESET, 0);
		return;
	}

restart:
	switch (c->state) {
	case ST_PORT_RESET:
		/* 0x4AD7: power off, clear detect / class / power status, counters stay (host reads clear them) */
		hal_power_off(ch);
		hal_chan_enable(ch, 0);
		sat_set(ch, 0x7F, 0);
		if (HREG(R_SAT_PWR) & (u8)(m * 0x11)) {
			HREG(R_SAT_PWR) &= (u8)~(m * 0x11);
			evt_raise(R_PWR_EVT, (u8)(m * 0x11), 0x03);
		}
		release_probe(ch);
		EA = 0;
		c->flags = 0;
		c->wait_b = 0;
		for (i = 0; i < 8; i++)
			c->tmr[i] = 0;
		EA = 1;
		arm(ch, ST_PORT_SHUTDOWN, 1);
		break;

	case ST_PORT_SHUTDOWN:
		/* 0x2EE0: clear this channel's event latches, re-apply OP_MOD (read live), next cfg after 2 ticks */
		evt_clear_channel(ch);
		if (mode)                                /* 0x2F5C: shutdown mode parks here (no re-arm) until OP_MOD changes */
			arm(ch, ST_PORT_CFG, 2);
		break;

	case ST_PORT_CFG:
		/* 0x44ED: power off first (stock 0x2143), clear flags and events, ctrl defaults, zero CM / VM */
		hal_power_off(ch);
		EA = 0;
		c->ev_hi = 0; c->ev_lo = 0; c->wait_b = 0; c->flags = 0;
		EA = 1;
		hal_ack_events(ch);
		hal_ctrl_defaults(ch);
		hal_ctrl_bit2(ch);                        /* 0x4AA1 + F2 clear after the gate (oracle tick 1: C9, B = 04, F2 = 00) */
		hal_chan_enable(ch, 0);
		HREG_PUT_LE16(R_P1_CM_LSB + 4 * ch, 0);
		HREG_PUT_LE16(R_P1_VM_LSB + 4 * ch, 0);
		arm(ch, ST_MODE_SELECT, 1);
		break;

	case ST_MODE_SELECT:
		/* 0x454A: shutdown -> reset (1); manual with a pending power request -> power request (10); else detect (3) */
		if (mode == 0)
			arm(ch, ST_PORT_RESET, 1);
		else if (mode == 1 && (c->pend & CH_PEND_PWR_ON))
			arm(ch, ST_POWER_REQUEST, 10);
		else
			arm(ch, ST_DETECT_ARM, 3);
		break;

	case ST_DETECT_ARM:
		/* 0x46AB: detect enabled for this channel (DET_CLASS_EN low nibble) or requested by push-button */
		if (mode == 0) {
			arm(ch, ST_PORT_RESET, 1);
		} else if (!(HREG(R_DET_CLASS_EN) & m) && !(c->pend & CH_PEND_DETCLASS)) {
			arm(ch, ST_IDLE, 0);
		} else if (!acquire_probe(ch)) {
			arm(ch, ST_DETECT_ARM, 1);
		} else {
			arm(ch, ST_DETECT_START, 0);
		}
		break;

	case ST_DETECT_START:
		/* 0x4601: D(ch) = 1 (stock: only with block+1 bit 6, the arbitration latch = our probe lock), timer 1 = 50 ticks,
		 * state pointer 0x1A60. The hardware answers with channel event bit 6 (ISR: ev_lo bit 0). */
		c->pend &= (u8)~CH_PEND_DETCLASS;
		EA = 0; c->ev_lo &= (u8)~0x01; EA = 1;
		hal_detect_start(ch);
		probe_start(ch);
		tmr_set(ch, TMR_DET, 50);
		c->state = ST_DETECT_WAIT;
		break;

	case ST_DETECT_WAIT:
		if (c->wait_b & 0x01) {                      /* 0x1A60 timeout path: no event within 50 ticks, no result read */
			EA = 0; c->wait_b &= (u8)~0x01; EA = 1;
			hal_detect_phase2(ch);               /* D(ch) = 2 */
			sat_set(ch, 0x0F, 0);
			evt_raise(R_DET_CLASS_EVT, m, 0x08);
			release_probe(ch);
			diag_detect(ch, 2, 0);
			probe_end(ch, 1);
			arm(ch, ST_IDLE, 2);
		} else if (c->ev_lo & 0x01) {                /* 0x1AF0: hardware event -> 0x3B3C after 4 ticks */
			EA = 0; c->ev_lo &= (u8)~0x01; EA = 1;
			tmr_set(ch, TMR_DET, 0);
			arm(ch, ST_DETECT_RESULT, 4);
		}
		break;

	case ST_DETECT_RESULT: {
		/* 0x3B3C: raw = D(ch), nibble = raw >> 4, code = 0x3651(ch, nibble), publish (MISC_CONF bit 6: event only on change) */
		static __xdata u8 raw, code;

		raw = hal_detect_raw(ch);
		dc_nib = raw >> 4;
		code = detect_classify(ch);
		diag_detect(ch, 1, raw);
		diag_words(dc_diag);
		probe_end(ch, 0);
		c->det = code;
		if (!(HREG(R_MISC_CONF) & 0x40) || (HREG(R_SAT(ch)) & 0x0F) != code) {
			sat_set(ch, 0x0F, code);
			evt_raise(R_DET_CLASS_EVT, m, 0x08);
		} else {
			sat_set(ch, 0x0F, code);
		}
		release_probe(ch);
		if (code == 4) {
			c->flags &= (u8)~F_HP;               /* 0x3BD4: clears the CM scale flag */
			arm(ch, ST_CLASS_GATE, 8);
		} else {
			arm(ch, ST_DETECT_INVALID, 15);      /* HP_Pn_MOD bit 1 legacy path (0x4F23) not ported [G] */
		}
		break;
	}

	case ST_DETECT_INVALID:
		/* 0x48BE: count invalid signatures (not open circuit = 6), re-detect after 200 ticks [I] */
		if (c->det != 6) {
			cnt_inc(ch, CNT_INVALID);
		}
		arm(ch, ST_IDLE, 0);                     /* oracle: re-detect every 61 ticks for any non-4 code (stock 0x48BE), not 200 [C] */
		break;

	case ST_CLASS_GATE:
		/* 0x41D9: class enabled (DET_CLASS_EN high nibble)? else power request; else start the 0x29EE mux, 20-tick window */
		if (!(HREG(R_DET_CLASS_EN) & (u8)(0x10 << ch))) {
			arm(ch, ST_POWER_REQUEST, 10);
		} else {
			class_request(ch);
			tmr_set(ch, TMR_CLSWIN, 25);   /* mux = 20 hook-3 calls (oracle, pt 1: 20 ticks) x 1.25 */
			c->state = ST_CLASS_WAIT;
		}
		break;

	case ST_CLASS_WAIT: {
		/* 0x47E4: mux finished -> class code into SAT bits 6:4 (0x4658, MISC_CONF bit 2: event only on change) */
		static __xdata u8 cls;

		if (class_take_done(ch)) {
			tmr_set(ch, TMR_CLSWIN, 0);
			cls = class_code_of(ch);
		} else if (c->wait_b & 0x04) {               /* window expired: class bits cleared (0x47FC) */
			EA = 0; c->wait_b &= (u8)~0x04; EA = 1;
			class_cancel(ch);
			cls = 0;
		} else {
			break;
		}
		if (!(HREG(R_MISC_CONF) & 0x04) || ((HREG(R_SAT(ch)) >> 4) & 7) != cls)
			evt_raise(R_DET_CLASS_EVT, (u8)(m << 4), 0x10);
		sat_set(ch, 0x70, (u8)(cls << 4));
		if (cls == 7) {
			cnt_inc(ch, CNT_DENIED);
			arm(ch, ST_IDLE, 0);
		} else if (cls == 0) {
			arm(ch, ST_IDLE, 2);
		} else {
			arm(ch, ST_POWER_REQUEST, 10);
			goto restart;                        /* stock reaches 0x4A31 in the same tick (oracle): no extra pass */
		}
		break;
	}

	case ST_POWER_REQUEST:
		/* 0x3FBE: shutdown -> reset; manual needs a request; semi-auto and auto power up [G for auto: stock auto is
		 * sequenced by 0x316B / 0x30C3, not ported] */
		if (mode == 0)
			arm(ch, ST_PORT_RESET, 1);
		else if (mode == 1 && !(c->pend & CH_PEND_PWR_ON))
			arm(ch, ST_IDLE, 2);
		else
			arm(ch, ST_POWER_ON, 0);
		break;

	case ST_POWER_ON:
		/* 0x4A31: only after a good detect and while the mode allows it. F2 / F1 only here; limits, gate and ICUT follow the
		 * hardware power-good event (oracle, docs/poe-fw-seq/06-oracle.md section 5). nopower build: no F2 / F1. */
		if (mode == 0 || c->det != 4) {
			arm(ch, ST_PORT_RESET, 1);
			break;
		}
		c->pend &= (u8)~CH_PEND_PWR_ON;
		{
			u8 cls = (HREG(R_SAT(ch)) >> 4) & 7;

			c->flags = hp_tab[cls] ? F_HP : 0;
#ifndef POE_FW_NO_POWER
			hal_chan_enable(ch, hp_tab[cls]);         /* 0x30DF: F2 only for class 4 [C] */
			hal_power_on(ch, (HREG(R_HP_EN) & (u8)(0x10 << ch)) != 0);
#endif
		}
		HREG(R_SAT_PWR) |= m;                             /* enable nibble */
		evt_raise(R_PWR_EVT, m, 0x01);                    /* INT bit 0 at power-on, bit 1 at power good (oracle) */
		EA = 0; c->ev_lo &= (u8)~0x04; EA = 1;
		c->state = ST_POWER_WAIT;                         /* no timeout: stock waits for the event indefinitely (C, oracle) */
		break;

	case ST_POWER_WAIT:
		if (mode == 0 || (c->pend & CH_PEND_PWR_OFF)) {
			c->pend &= (u8)~CH_PEND_PWR_OFF;
			arm(ch, ST_PORT_RESET, 1);
		} else if (c->ev_lo & 0x04) {                     /* BC bit 4 */
			EA = 0; c->ev_lo &= (u8)~0x04; EA = 1;
			if (!hal_pwr_good(ch))                    /* 0x5074: F3 bit clear = power-up failed, nothing is applied */
				break;
			{
				u8 cls = (HREG(R_SAT(ch)) >> 4) & 7;

				hal_dis_sen(ch, (HREG(R_DIS_SEN_EN) & m) != 0);   /* C9 gate release */
				hal_chan_enable(ch, hp_tab[cls]);
				hal_set_limit(ch, lim_tab[cls]);          /* 0x30EA: A(ch) from the class table [C] */
				hal_ctrl_pulse(ch);
				if (icut_tab[cls])
					HREG(R_HP_P1_OV_CUT + 5 * ch) = icut_tab[cls];
			}
			HREG(R_SAT_PWR) |= (u8)(m * 0x10);
			evt_raise(R_PWR_EVT, (u8)(m * 0x10), 0x02);
			c->flags |= F_POWERED;
			tmr_set(ch, TMR_TRIG, 2);
			c->state = ST_POWER_TRIG;
		}
		break;

	case ST_POWER_TRIG:
		if (c->wait_b & (u8)(1 << TMR_TRIG)) {            /* oracle: B bit 7 two ticks after the power-good event */
			EA = 0; c->wait_b &= (u8)~(1 << TMR_TRIG); EA = 1;
			hal_ctrl_trigger(ch);
			c->state = ST_MONITOR;
		}
		break;

	case ST_MONITOR:
		if (mode == 0 || (c->pend & CH_PEND_PWR_OFF)) {
			c->pend &= (u8)~CH_PEND_PWR_OFF;
			arm(ch, ST_PORT_RESET, 1);
		} else if (c->ev_hi & 0x41) {           /* 0x2C8D: ev_hi & 0x41 arms st_fault_shutdown (12) */
			EA = 0; c->ev_hi &= (u8)~0x41; EA = 1;
			arm(ch, ST_FAULT, 12);
		} else if ((c->ev_hi & 0x20) || (c->ev_lo & 0x30)) {   /* 0x2D97: arms st_disconnect_cleanup (14) */
			EA = 0; c->ev_hi &= (u8)~0x20; c->ev_lo &= (u8)~0x30; EA = 1;
			arm(ch, ST_DISCONNECT, 14);
		}
		break;

	case ST_FAULT:
		/* 0x4D5C: power off, count, 75 tick cool-down, then idle = auto re-detect (AUTO_FAULT_CLEAR) */
		hal_power_off(ch);
		hal_chan_enable(ch, 0);
		HREG(R_SAT_PWR) &= (u8)~(m * 0x11);
		cnt_inc(ch, CNT_SHORT);
		evt_raise(R_FAULT_EVT, m, 0x04);
		evt_raise(R_PWR_EVT, (u8)(m * 0x11), 0x03);
		c->flags = 0;
		wait_then(ch, TMR_FAULT, 75, ST_IDLE, 0);
		break;

	case ST_DISCONNECT:
		/* 0x4F9F: power off, MPS absent count, 30 tick cleanup (Tmpdo), then config */
		hal_power_off(ch);
		hal_chan_enable(ch, 0);
		HREG(R_SAT_PWR) &= (u8)~(m * 0x11);
		cnt_inc(ch, CNT_MPS);
		evt_raise(R_FAULT_EVT, (u8)(m << 4), 0x04);
		evt_raise(R_PWR_EVT, (u8)(m * 0x11), 0x03);
		c->flags = 0;
		wait_then(ch, TMR_CLEAN, 30, ST_PORT_CFG, 0);
		break;

	case ST_IDLE:
		/* 0x4EEF: 60 ticks then back to config -> mode select -> detect */
		wait_then(ch, TMR_IDLE, 60, ST_PORT_CFG, 0);
		break;

	case ST_WAIT:
		if (c->wait_b & (u8)(1 << c->wbit)) {
			EA = 0; c->wait_b &= (u8)~(1 << c->wbit); EA = 1;
			arm(ch, c->wnext, c->wdly);
		}
		break;

	default:
		arm(ch, ST_PORT_RESET, 1);
		break;
	}
}
