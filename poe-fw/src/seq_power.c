/* Cluster 3 of the stock-image port: power-up, power good, limits, fault / disconnect / re-detect handlers.
 * Faithful to stock v21 (addresses in comments); SEQ_V18 = Meraki v18 variant. Spec: docs/poe-fw-seq/03-powergood-limits.md.
 * [C] code-evident, [I] inferred meaning, [U] unknown. Not wired: the integrator calls seq_tick / seq_run and the ISR side
 * fills ev_hi / ev_lo (decode table in the doc). Writes to F1 (the 54 V enable) happen only in st_power_on. */
#include "seq_power.h"

__xdata struct sq_ch sq[NCH];
__xdata u8 sq_svc;
void (*seq_ext_run)(u8 ch);
static __xdata u8 opsh;                 /* stock IRAM 0x41 OP_MOD shadow, applied only by st_port_shutdown / st_port_cfg */
static __xdata u8 satsh[NCH];           /* stock IRAM 0x42 + ch SAT_Pn shadow */

static const __code u8 bit_of[4] = { 0x01, 0x02, 0x04, 0x08 };   /* 0x4C23 */
static const __code u8 hi_of[4] = { 0x10, 0x20, 0x40, 0x80 };    /* 0x4C2B */
static const __code u8 pr_of[4] = { 0x11, 0x22, 0x44, 0x88 };    /* 0x4C2F */
static const __code u8 lim_tab[8] = { 0xF0, 0x48, 0x84, 0xF0, 0xCC, 0xF0, 0xF0, 0xF0 };    /* 0x4C03 */
static const __code u8 f2_tab[8] = { 0, 0, 0, 0, 1, 0, 0, 0 };                              /* 0x4C0B */
static const __code u8 icut_tab[8] = { 0x54, 0x46, 0x4B, 0x54, 0x11, 0x54, 0x54, 0x54 };   /* 0x4C13 */
static const __code u8 ilim_tab[8] = { 0x80, 0x80, 0x80, 0x80, 0xC0, 0x80, 0x80, 0x80 };   /* 0x4C1B */

#define PB_PWR_ON  0x04
#define PB_PWR_OFF 0x08
#define FLG_HP     0x20
#define FLG_HOLD   0x08
#define FLG_ON     0x04
#define FLG_GOOD   0x02

/* ---- helpers ---- */

/* stock arm_channel 0x49FF: service flag always; pointer if != KEEP; code if != 0. */
static void arm(u8 ch, u8 st, u8 code)
{
	sq_svc |= bit_of[ch];
	if (st != SQ_KEEP)
		sq[ch].state = st;
	if (code)
		sq[ch].code = code;
}

/* stock set_timer 0x5024: timer B, ticks, masks OR-ed into wa / wb at expiry. */
static void tmr_b(u8 ch, u8 ticks, u8 ma, u8 mb)
{
	sq[ch].tb_cnt = ticks;
	sq[ch].tb_ma = ma;
	sq[ch].tb_mb = mb;
}

static void int_line(u8 intbits)
{
	if ((HREG(R_INT_MASK) & intbits) && (HREG(R_MISC_CONF) & 0x80))
		INT_OUT = 1;
}

/* Host event register: reg |= set, shadow (reg + 1) = same value, INT |= intbits (stock 0x22EB / 0x22C8 / 0x2077 pattern). */
static void evt(u8 reg, u8 set, u8 intbits)
{
	u8 v = HREG(reg) | set;

	HREG(reg) = v;
	HREG(reg + 1) = v;
	HREG(R_INT) |= intbits;
}

static u8 opmode(u8 ch)     /* stock 0x4C33: shadow, 2 bits per channel */
{
	return (opsh >> (2 * ch)) & 3;
}

static void apply_opmod(u8 ch)   /* stock 0x482F */
{
	u8 m = (u8)(3 << (2 * ch));

	opsh = (opsh & (u8)~m) | (HREG(R_OP_MOD) & m);
}

static u8 hp_allowed(u8 ch)     /* stock 0x4A69: HP_EN low-nibble bit and HP_MOD.0 */
{
	return (HREG(R_HP_EN) & bit_of[ch]) && (HREG(R_HP_MOD(ch)) & 1);
}

static u8 cls_code(u8 ch)       /* stock 0x4BD3 (v18: no HP check, raw field) */
{
	u8 c = (sq[ch].cls >> 3) & 7;

#ifndef SEQ_V18
	if (c == 4 && !hp_allowed(ch))
		return 0;
#endif
	return c;
}

static u8 hp_default(u8 ch)     /* stock 0x45A7: F2 value for this channel */
{
	if (opmode(ch) == 3)
		return f2_tab[cls_code(ch)];
	return HREG(R_HP_ILIM(ch)) != 0x80;
}

static void f2_set(u8 ch, u8 on)     /* stock 0x4DD0: F2 (EA off inside hal_chan_enable), flg bit 5 follows */
{
	hal_chan_enable(ch, on);
	if (on)
		sq[ch].flg |= FLG_HP;
	else
		sq[ch].flg &= (u8)~FLG_HP;
}

static void f2_update(u8 ch)         /* stock 0x5055 */
{
	f2_set(ch, hp_default(ch));
}

static void limit_apply(u8 ch)       /* stock 0x4B71: A(ch) = calc_limit(ch) */
{
	hal_set_limit(ch, seq_calc_limit(ch));
}

static void cnt_byte(u8 ch, u8 k)
{
	HREG(R_CNT(ch, k))++;
}

/* stock 0x2F8C / 0x2C79: reg 0x62 nibbles, overload in bits 7:4 (hi != 0), short in bits 3:0. */
static void cnt_nib(u8 ch, u8 hi)
{
	u8 v = HREG(R_CNT(ch, CNT_SHORT));

	if (hi)
		v = (u8)((((v >> 4) + 1) & 0x0F) << 4) | (v & 0x0F);
	else
		v = (v & 0xF0) | ((v + 1) & 0x0F);
	HREG(R_CNT(ch, CNT_SHORT)) = v;
}

/* stock 0x4C62: SAT_PWR enable bit, PWR_EVT enable bit, INT bit 0. */
static void pwr_enable_evt(u8 ch)
{
	EA = 0;
	HREG(R_SAT_PWR) |= bit_of[ch];
	evt(R_PWR_EVT, bit_of[ch], 0x01);
	int_line(0x01);
	EA = 1;
}

/* stock 0x4658: class field (cls bits 5:3) -> SAT shadow bits 6:4 -> SAT_Pn. The MISC_CONF.2 compare / event (0x4E3D) is
 * cluster 2; here only the register update. */
static void publish_class(u8 ch)
{
	satsh[ch] = (satsh[ch] & 0x8F) | (u8)((sq[ch].cls & 0x38) << 1);
	HREG(R_SAT(ch)) = satsh[ch];
}

/* ---- limits ---- */

/* stock calc_limit 0x3334: auto = class table; else v = ICUT[5:0] clamped, scale by ILIM == 0x80 and ICUT.6. [C] arithmetic,
 * [U] physical unit of the result. */
u8 seq_calc_limit(u8 ch)
{
	u8 icut, v;

	if (opmode(ch) == 3)
		return lim_tab[cls_code(ch)];
	icut = HREG(R_HP_ICUT(ch));
	v = icut & 0x3F;
	if (HREG(R_HP_ILIM(ch)) == 0x80) {
		if (!(icut & 0x40)) {
			if (v > 0x0A)
				v = 0x0A;
			return (u8)((v << 3) + (v << 4));            /* 24 v */
		}
		if (v > 0x15)
			v = 0x15;
		return (u8)((v << 2) + (v << 3));                    /* 12 v */
	}
	if (icut & 0x40) {
		if (v > 0x2A)
			v = 0x2A;
		return (u8)((v << 1) + (v << 2));                    /* 6 v */
	}
	if (v > 0x15)
		v = 0x15;
	return (u8)((v << 2) + (v << 3));                            /* 12 v */
}

/* stock poll_common 0x2607 (ICUT) / 0x261B..0x2693 (ILIM): only while the channel's good bit (SAT_PWR 7:4) is set. */
void seq_icut_written(u8 ch)
{
	if (HREG(R_SAT_PWR) & hi_of[ch])
		limit_apply(ch);
}

void seq_ilim_written(u8 ch)
{
	if (HREG(R_SAT_PWR) & hi_of[ch]) {
		f2_update(ch);
		limit_apply(ch);
	}
}

/* ---- state handlers ---- */

static void st_port_reset(u8 ch)     /* 0x4AD7 */
{
	struct sq_ch __xdata *c = &sq[ch];

	hal_power_off(ch);                          /* F2 &= ~bit, F1 &= ~(0x11 << ch), EA off (0x2143) */
	c->cls &= 0xC0;
	c->flg &= (u8)~FLG_ON;
	satsh[ch] &= 0x80;
	HREG(R_SAT(ch)) = satsh[ch];
	arm(ch, SQ_PORT_SHUTDOWN, 0);
}

static void st_port_shutdown(u8 ch)  /* 0x2EE0 */
{
	struct sq_ch __xdata *c = &sq[ch];
	u8 m = (u8)~pr_of[ch];
	u8 f;

	c->ev_hi = 0; c->ev_lo = 0;
	c->wa = 0; c->wb = 0;
	c->ta_cnt = 0;
	apply_opmod(ch);
	EA = 0;
	f = HREG(R_FAULT_EVT_CLR) & m;              /* stock reads 0x8007 */
	HREG(R_FAULT_EVT_CLR) = f; HREG(R_FAULT_EVT) = f;
	HREG(R_TS_EVT_CLR) &= m;
	HREG(R_TS_EVT) = HREG(R_TS_EVT_CLR);
	if (!(f & 0xF0))
		HREG(R_INT) &= 0xFB;
	if (!(HREG(R_FAULT_EVT) & 0x0F) && !(HREG(R_TS_EVT) & 0xF0))
		HREG(R_INT) &= 0xDF;
	if (!(HREG(R_TS_EVT) & 0x0F))
		HREG(R_INT) &= 0xBF;
	INT_UPDATE();
	EA = 1;
	if (opmode(ch))
		arm(ch, SQ_PORT_CFG, 2);
	else {
		c->pend_a = 0;
		c->pend_b = 0;
	}
}

static void st_port_cfg(u8 ch)       /* 0x44ED */
{
	struct sq_ch __xdata *c = &sq[ch];

	hal_power_off(ch);
	c->ev_hi = 0; c->ev_lo = 0;
	c->wa = 0; c->wb = 0;
	c->tb_cnt = 0;
	hal_ack_events(ch);                         /* 0x4CE4 */
	apply_opmod(ch);
	hal3_ctrl_bit7(ch, 0);                      /* 0x493F, R5 = 0 */
	hal3_ctrl_bit2(ch, 0);                      /* 0x497E, R5 = 0: B.bit2 = 1 */
	hal_dis_sen(ch, 0);                         /* 0x4FF6, R5 = 0: C9 |= bit */
	hal3_ctrl_field(ch, 0);                     /* 0x4AA1 */
	f2_set(ch, 0);                              /* 0x4DD0, R5 = 0 */
	c->flg &= (u8)~(FLG_ON | FLG_GOOD);
#ifdef SEQ_V18
	c->flg &= (u8)~FLG_HOLD;
#endif
	HREG_PUT_LE16(R_P1_CM_LSB + 4 * ch, 0);
	HREG_PUT_LE16(R_P1_VM_LSB + 4 * ch, 0);
	arm(ch, SQ_MODE_SELECT, 0);
}

static void st_mode_select(u8 ch)    /* 0x454A */
{
	struct sq_ch __xdata *c = &sq[ch];
	u8 m = opmode(ch);

	c->ev_hi = 0; c->ev_lo = 0;
	c->wa = 0; c->wb = 0;
	EA = 0;
	if (!(c->pend_b & PB_PWR_ON))
		c->pend_b &= (u8)~PB_PWR_OFF;
	EA = 1;
	if (m == 0)
		arm(ch, SQ_PORT_RESET, 1);
	else if (m == 1 && (c->pend_b & PB_PWR_ON))
		arm(ch, SQ_POWER_REQUEST, 0x0A);
	else
		arm(ch, SQ_DETECT_ARM, 3);
}

static void st_power_request(u8 ch)  /* 0x3FBE */
{
	struct sq_ch __xdata *c = &sq[ch];
	u8 m = opmode(ch);

	if (m == 0) {
		arm(ch, SQ_PORT_RESET, 1);
		return;
	}
	if (m == 1 && !(c->pend_b & PB_PWR_ON)) {
		arm(ch, SQ_IDLE, 2);
		return;
	}
	if (m == 2 && !(c->pend_b & PB_PWR_ON)) {
		if (c->wb & 0x80) {                     /* timer A (37 ticks after detect good) expired: no request came */
			c->wb &= 0x7F;
			arm(ch, SQ_POWERON_HOLD, 2);
			cnt_byte(ch, CNT_DENIED);          /* 0x215B: 0x8061 + 4 ch */
		}
		return;
	}
	if (m != 1)
		c->ta_cnt = 0;                          /* cancel the 37 tick request timer */
	arm(ch, SQ_POWER_ON, 0x0A);
	EA = 0;
	c->pend_b &= (u8)~PB_PWR_ON;
	EA = 1;
	pwr_enable_evt(ch);                         /* 0x4C62 */
}

/* 0x4A31: THE POWER-UP. Order: F2 (HP mode) first, then F1 |= mask = port power on. */
static void st_power_on(u8 ch)
{
	u8 hp_hi = (HREG(R_HP_EN) & hi_of[ch]) != 0;

#ifdef SEQ_V18
	if (hp_hi) {
		f2_update(ch);
		EA = 0; hal_power_on(ch, 1); EA = 1;
	} else {
		EA = 0; hal_power_on(ch, 0); EA = 1;
		sq[ch].flg |= FLG_HOLD;
		tmr_b(ch, 8, 0x01, 0);                  /* v18: 8 tick hold, wa.0 -> synthetic power-good event */
	}
#else
	f2_update(ch);                              /* 0x4A5E -> 0x5055 -> 0x4DD0 */
	EA = 0;
	hal_power_on(ch, hp_hi);                    /* 0x4A52: ORL F1: hp_hi ? bit(ch) : 0x11 << ch */
	EA = 1;
#endif
	arm(ch, SQ_POWER_GOOD, 0);
}

/* 0x2F8C: powered, waiting for the hardware power event (ev_lo.2) and judging it with SFR F3. */
static void st_power_good(u8 ch)
{
	struct sq_ch __xdata *c = &sq[ch];
	u8 m = bit_of[ch];
	u8 cl;

	if (!c->ev_hi && c->ev_lo == 0x02) {        /* class mux finished on the class-4 path (0x29EE) */
		EA = 0; c->pend_b &= (u8)~PB_PWR_ON; EA = 1;
		publish_class(ch);
		pwr_enable_evt(ch);
	}
	c->ev_hi &= (u8)~0x02;
	if (c->ev_hi & 0xF1) {                      /* fault during power-up */
		c->ev_hi &= 0x0E;
		c->code = 0x0C;
		c->state = SQ_FAULT;
		cnt_nib(ch, 1);
		EA = 0;
		evt(R_TS_EVT, m, 0);                    /* low nibble */
		HREG(R_SAT_PWR) &= (u8)~pr_of[ch];
		evt(R_PWR_EVT, pr_of[ch], 0x41);
		int_line(0x41);
		EA = 1;
		arm(ch, SQ_KEEP, 0);
	}
#ifdef SEQ_V18
	if (c->wa & 0x01) {                         /* v18 hold timer expired: inject the event */
		c->wa &= (u8)~0x01;
		c->flg &= (u8)~FLG_HOLD;
		c->ev_lo |= 0x04;
		arm(ch, SQ_KEEP, 0);
		return;
	}
#endif
	if (!(c->ev_lo & 0x04))
		return;
	c->ev_lo &= (u8)~0x04;
	c->ta_cnt = 0;
#ifdef SEQ_V18
	if (c->flg & FLG_HOLD)
		return;
#endif
	c->pg_miss = 0;
	if (!hal3_pg(ch)) {                         /* F3 bit clear: power did not come good -> fault above on the next pass */
		c->ev_hi |= 0x01;
		return;
	}
	c->flg |= FLG_ON;
	c->code = 0x0B;
	c->state = SQ_MONITOR;
	c->cls = (c->cls & 0xF8) | 0x02;
	if (HREG(R_DIS_SEN_EN) & m)
		hal_dis_sen(ch, 1);                     /* C9 &= ~bit: disconnect sense on */
	if (opmode(ch) == 3) {
		cl = cls_code(ch);
		f2_set(ch, f2_tab[cl]);                 /* 0x4DD0 */
		hal_set_limit(ch, lim_tab[cls_code(ch)]);   /* 0x4ED5 */
		hal3_ctrl_field(ch, 0);                 /* 0x4AA1 */
		cl = cls_code(ch);
		HREG(R_HP_ICUT(ch)) = icut_tab[cl];     /* 0x46FC: host ICUT / ILIM := class defaults */
		HREG(R_HP_ILIM(ch)) = ilim_tab[cl];
	} else {
		f2_update(ch);                          /* 0x5055 */
		hal3_ctrl_field(ch, 0);                 /* 0x5216 */
		limit_apply(ch);                        /* 0x4B71 */
	}
	hal3_ctrl_bit2(ch, 1);                      /* 0x497E, R5 = 1: B.bit2 = 0, the auto trigger */
	c->flg |= FLG_GOOD;
	EA = 0;
	evt(R_PWR_EVT, hi_of[ch], 0x02);
	HREG(R_SAT_PWR) |= hi_of[ch];               /* power good bit */
	int_line(0x02);
	EA = 1;
	tmr_b(ch, 2, 0x02, 0);                      /* 0x501E: monitor re-run every 2 ticks */
}

static void st_fault(u8 ch)          /* 0x4D5C */
{
	hal3_f1_off_low(ch);                        /* only F1.bit(ch); F2 and F1.hi stay until st_port_cfg */
	sq[ch].cls = (sq[ch].cls & 0xF8) | 0x05;
	tmr_b(ch, 75, 0, 0x20);
	arm(ch, SQ_FAULT_COOLDOWN, 0);
}

static void st_fault_cooldown(u8 ch) /* 0x4E9D */
{
	if (sq[ch].wb & 0x20) {
		sq[ch].wb &= (u8)~0x20;
		arm(ch, SQ_IDLE, 2);
	}
}

static void st_disconnect_cleanup(u8 ch)   /* 0x4F9F */
{
	sq[ch].ta_cnt = 0;
	tmr_b(ch, 30, 0x40, 0);
	arm(ch, SQ_WAIT_A6, 0);
}

static void st_idle(u8 ch)           /* 0x4EEF */
{
	tmr_b(ch, 60, 0x80, 0);
	sq[ch].state = SQ_WAIT_A7;
}

static void st_poweron_hold(u8 ch)   /* 0x4FB7 */
{
	tmr_b(ch, 20, 0x80, 0);
	sq[ch].state = SQ_WAIT_A7;
}

static void st_wait(u8 ch, u8 w, u8 bit, u8 next, u8 code)   /* 0x4E60 / 0x4E80 / 0x4F09 / 0x4F3D */
{
	struct sq_ch __xdata *c = &sq[ch];
	u8 __xdata *f = w ? &c->wb : &c->wa;

	if (*f & bit) {
		*f &= (u8)~bit;
		arm(ch, next, code);
	}
}

static void st_retry(u8 ch, u8 next)  /* 0x5091 / 0x509E: re-detect spacing 200 ticks */
{
	tmr_b(ch, 200, 0, 0x02);
	sq[ch].state = next;
}

static void st_wait_b3(u8 ch)        /* 0x4BA2 */
{
	struct sq_ch __xdata *c = &sq[ch];

	if (c->wb & 0x02) {
		c->wb &= (u8)~0x02;
		tmr_b(ch, 200, 0, 0x08);
	}
	if (c->wb & 0x08) {
		c->wb &= (u8)~0x08;
		arm(ch, SQ_RETRY_A, 0);
	}
}

static void st_detect_invalid(u8 ch) /* 0x48BE (cluster 1 owns the detect result; the re-detect spacing is here) */
{
	cnt_byte(ch, CNT_INVALID);
	if ((HREG(R_MS_BO_EN) & bit_of[ch]) && (satsh[ch] & 0x0F) != 6)
		arm(ch, SQ_RETRY_A, 0x10);
	else
		arm(ch, SQ_IDLE, 2);
}

/* ---- scheduler ---- */

void seq_init(void)
{
	u8 c;

	for (c = 0; c < NCH; c++) {
		u8 i;
		u8 __xdata *p = (u8 __xdata *)&sq[c];

		for (i = 0; i < sizeof(struct sq_ch); i++)
			p[i] = 0;
		arm(c, SQ_PORT_RESET, 1);               /* stock init_hw_channels arms st_port_reset */
	}
	opsh = 0;
}

/* stock tick_timers 0x3C1E: per channel timer A then timer B; expiry ORs the masks into wa / wb and requests service. */
void seq_tick(void)
{
	u8 c;

	for (c = 0; c < NCH; c++) {
		struct sq_ch __xdata *p = &sq[c];

		if (p->ta_cnt && --p->ta_cnt == 0) {
			p->wa |= p->ta_ma; p->wb |= p->ta_mb;
			sq_svc |= bit_of[c];
		}
		if (p->tb_cnt && --p->tb_cnt == 0) {
			p->wa |= p->tb_ma; p->wb |= p->tb_mb;
			sq_svc |= bit_of[c];
		}
	}
}

void seq_run(u8 ch)
{
	switch (sq[ch].state) {
	case SQ_PORT_RESET: st_port_reset(ch); break;
	case SQ_PORT_SHUTDOWN: st_port_shutdown(ch); break;
	case SQ_PORT_CFG: st_port_cfg(ch); break;
	case SQ_MODE_SELECT: st_mode_select(ch); break;
	case SQ_POWER_REQUEST: st_power_request(ch); break;
	case SQ_POWER_ON: st_power_on(ch); break;
	case SQ_POWER_GOOD: st_power_good(ch); break;
	case SQ_FAULT: st_fault(ch); break;
	case SQ_FAULT_COOLDOWN: st_fault_cooldown(ch); break;
	case SQ_DISCONNECT_CLEANUP: st_disconnect_cleanup(ch); break;
	case SQ_IDLE: st_idle(ch); break;
	case SQ_WAIT_A7: st_wait(ch, 0, 0x80, SQ_PORT_CFG, 2); break;
	case SQ_WAIT_A6: st_wait(ch, 0, 0x40, SQ_IDLE, 2); break;
	case SQ_WAIT_B1: st_wait(ch, 1, 0x02, SQ_IDLE, 2); break;
	case SQ_WAIT_B6B: st_wait(ch, 1, 0x40, SQ_POWER_REQUEST, 0); break;
	case SQ_WAIT_B3: st_wait_b3(ch); break;
	case SQ_POWERON_HOLD: st_poweron_hold(ch); break;
	case SQ_RETRY_A: st_retry(ch, SQ_WAIT_B1); break;
	case SQ_RETRY_B: st_retry(ch, SQ_WAIT_B3); break;
	case SQ_DETECT_INVALID: st_detect_invalid(ch); break;
	default:
		if (seq_ext_run)
			seq_ext_run(ch);
		break;
	}
}
