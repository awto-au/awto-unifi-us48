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
	ST_DETECT_PHASE2,   /* 0x1A60 */
	ST_DETECT_RESULT,   /* 0x3B3C */
	ST_DETECT_INVALID,  /* 0x48BE */
	ST_CLASS_GATE,      /* 0x41D9 */
	ST_CLASS_START,     /* 0x49BD */
	ST_CLASS_DONE,      /* 0x47E4 */
	ST_POWER_REQUEST,   /* 0x3FBE */
	ST_POWER_ON,        /* 0x4A31 */
	ST_POWER_GOOD,      /* 0x2F8C */
	ST_MONITOR,         /* parked after power good [I] */
	ST_FAULT,           /* 0x4D5C */
	ST_DISCONNECT,      /* 0x4F9F */
	ST_IDLE,            /* 0x4EEF */
	ST_WAIT             /* generic st_wait_b* family: wait for a timer bit, then next */
};

/* Event timer bits (stock set_timer R3 mask) and tick counts (10 ms): docs/poe-fw.md section 17. */
#define TMR_DET    0   /* detect window 50 ticks (0x4601: R3=1 R5=0x32) */
#define TMR_RETRY  1   /* re-detect spacing 200 ticks (0x5091: 0x02 / 0xC8) */
#define TMR_CLASS  3   /* class window 100 ticks (0x49BD: R2=8 R5=0x64) */
#define TMR_FAULT  5   /* fault cool-down 75 ticks (0x4D5C: R3=0x20 R5=0x4B) */
#define TMR_CLEAN  6   /* disconnect cleanup 30 ticks (0x4F9F) */
#define TMR_IDLE   7   /* idle 60 ticks / power-up hold 20 ticks (0x4EEF / 0x4FB7) */

#define F_POWERED 0x01

static __data u8 probe_owner;     /* one channel at a time uses the detect / class hardware [G: replaces stock 0x3DA6 / 0x316B] */

#define SVC_SET(c) do { switch (c) { case 0: svc0 = 1; break; case 1: svc1 = 1; break; case 2: svc2 = 1; break; default: svc3 = 1; break; } } while (0)

static void svc_set(u8 ch)
{
	SVC_SET(ch);
}

/* stock arm_channel 0x49FF: set next state; dly == 0 runs it at the next main-loop pass, else after dly ticks [I]. */
static void arm(u8 ch, u8 st, u8 dly)
{
	struct chan __xdata *c = &chans[ch];

	EA = 0;
	c->state = st;
	c->dly = dly;
	EA = 1;
	if (!dly)
		svc_set(ch);
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
void chan_tick(void) __using(3)
{
	u8 c, i;
	struct chan __xdata *p;

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
	struct chan __xdata *c = &chans[ch];
	u8 m = (u8)(1 << ch);
	u8 mode = opmode(ch);
	u8 i;

	if ((c->pend & CH_PEND_RESET) && c->state != ST_PORT_RESET) {
		c->pend &= (u8)~CH_PEND_RESET;
		arm(ch, ST_PORT_RESET, 0);
		return;
	}

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
		/* 0x4601: D(ch) = 1, 50 tick window (Tdet) */
		c->pend &= (u8)~CH_PEND_DETCLASS;
		hal_detect_start(ch);
		wait_then(ch, TMR_DET, 50, ST_DETECT_PHASE2, 0);
		break;

	case ST_DETECT_PHASE2:
		/* 0x1A60: D(ch) = 2, result after 4 ticks */
		hal_detect_phase2(ch);
		arm(ch, ST_DETECT_RESULT, 4);
		break;

	case ST_DETECT_RESULT:
		/* 0x3B3C: code = D(ch) >> 4 into SAT low nibble; 4 = good. Scaling via 0x3651 not ported: TODO */
		c->det = (u8)(hal_detect_raw(ch) >> 4) & 0x0F;
		sat_set(ch, 0x0F, c->det);
		evt_raise(R_DET_CLASS_EVT, m, 0x08);
		release_probe(ch);
		if (c->det == 4)
			arm(ch, ST_CLASS_GATE, 8);
		else
			arm(ch, ST_DETECT_INVALID, 15);
		break;

	case ST_DETECT_INVALID:
		/* 0x48BE: count invalid signatures (not open circuit = 6), re-detect after 200 ticks [I] */
		if (c->det != 6) {
			cnt_inc(ch, CNT_INVALID);
			wait_then(ch, TMR_RETRY, 200, ST_IDLE, 2);
		} else {
			arm(ch, ST_IDLE, 0);
		}
		break;

	case ST_CLASS_GATE:
		/* 0x41D9: class enabled (DET_CLASS_EN high nibble)? */
		if (!(HREG(R_DET_CLASS_EN) & (u8)(0x10 << ch)))
			arm(ch, ST_POWER_REQUEST, 10);
		else
			arm(ch, ST_CLASS_START, 0);
		break;

	case ST_CLASS_START:
		/* 0x49BD: D5 / D6 |= bit, 100 tick window */
		if (!acquire_probe(ch)) {
			arm(ch, ST_CLASS_START, 1);
			break;
		}
		hal_class_start(ch);
		wait_then(ch, TMR_CLASS, 100, ST_CLASS_DONE, 0);
		break;

	case ST_CLASS_DONE: {
		/* 0x47E4: class into SAT bits 6:4. Hardware class result not ported: unknown -> 6 = class 0 [G] */
		u8 r = hal_class_result(ch);

		hal_class_stop();
		sat_set(ch, 0x70, (u8)(((r == 0xFF) ? 6 : (r & 7)) << 4));
		evt_raise(R_DET_CLASS_EVT, (u8)(m << 4), 0x10);
		release_probe(ch);
		arm(ch, ST_POWER_REQUEST, 10);
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
		/* 0x4A31: only after a good detect and while the mode allows it; F1 |= mask */
		if (mode == 0 || c->det != 4) {
			arm(ch, ST_PORT_RESET, 1);
			break;
		}
#ifdef POE_FW_NO_POWER
		arm(ch, ST_IDLE, 0);                            /* bench build: stop after detect / class */
#else
		c->pend &= (u8)~CH_PEND_PWR_ON;
		hal_dis_sen(ch, (HREG(R_DIS_SEN_EN) & m) != 0);   /* 0x30C3 [I] */
		hal_chan_enable(ch, 1);
		hal_power_on(ch, (HREG(R_HP_EN) & (u8)(0x10 << ch)) != 0);
		wait_then(ch, TMR_IDLE, 20, ST_POWER_GOOD, 0);    /* 0x4FB7: power-up hold 20 ticks (<= Tpon) */
#endif
		break;

	case ST_POWER_GOOD:
		/* 0x2F8C: SAT_PWR enable (3:0) and good (7:4) bits, PWR_EVT, INT. Power good is assumed after the hold: no
		 * hardware good indication is read (TODO, U) */
		HREG(R_SAT_PWR) |= (u8)(m * 0x11);
		evt_raise(R_PWR_EVT, (u8)(m * 0x11), 0x03);
		c->flags |= F_POWERED;
		c->state = ST_MONITOR;
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
