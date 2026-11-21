/* Class mux schedule (stock 0x29EE), SAT_Pn publishing and powered-channel monitor (stock 0x2C79).
 * Spec: docs/poe-fw-seq/02-class-monitor.md (C = code bytes, I = inferred, U = unknown). Addresses = stock v21.
 * Not wired: built only with -DPOE_FW_SEQ. Analog access only through hal_analog.c. */
#include "seq_class.h"

#ifdef POE_FW_SEQ

__xdata struct seq_ch seq_ch[NCH];
__xdata u8 seq_active;
__xdata u8 seq_map[NCH];
__bit seq_remap;

/* stock 0x8134..0x813A */
static __xdata struct {
	u8 sub, tick, ch, mapped, hp, code1, code2;
	u16 s1, s2;
} mux;

#define SVC_SET(c) do { switch (c) { case 0: svc0 = 1; break; case 1: svc1 = 1; break; case 2: svc2 = 1; break; default: svc3 = 1; break; } } while (0)

static u8 bit_lo(u8 ch)
{
	return (u8)(1u << ch);
}

/* stock 0x50E0 / 0x22A4: physical channel when the remap is on, else identity. */
static u8 map_ch(u8 ch)
{
	return seq_remap ? (u8)(seq_map[ch] & 3) : ch;
}

/* stock 0x4C33: OP_MOD 2 bits of channel m. C */
static u8 opmode(u8 m)
{
	return (HREG(R_OP_MOD) >> (2 * m)) & 3;
}

/* stock 0x4A69: HP_EN low-nibble bit and HP_Pn_MOD bit 0 of the physical channel. C */
static u8 hp_flag(u8 m)
{
	return (HREG(R_HP_EN) & bit_lo(m)) && (HREG(R_HP_MOD(m)) & 1);
}

static void arm(u8 ch, u16 next, u8 code)         /* stock arm_channel 0x49FF */
{
	SVC_SET(ch);
	if (next)
		seq_ch[ch].next = next;
	if (code)
		seq_ch[ch].code = code;
}

static u16 sample(u8 ch)                          /* stock 0x2A96 / 0x2B13: stored word, AF.7 -> 0xFFFF */
{
	u16 w = seq_ch[ch].w8;

	if (hal_af_read() & 0x80)
		w = 0xFFFF;
	return w;
}

/* word(AB = c|8) for all channels: stock 0x34D0 0x3509..0x3518, runs before 0x29EE in the periodic pass. */
void seq_capture_w8(void)
{
	u8 c;

	for (c = 0; c < NCH; c++)
		seq_ch[c].w8 = hal_ind_word((u8)(c | 8));
}

static void mux_af(u8 v)
{
	hal_af_write((u8)(mux.ch * 4) | v);
}

/* final step, stock 0x2B75..0x2C75. Class code stored = second sample code (code2). */
static void mux_finish(void)
{
	u8 ch = mux.ch;
	u8 m;
	struct seq_ch __xdata *s = &seq_ch[ch];

	if (mux.hp) {
		if (mux.code1 == 4 && mux.code2 == 4) {
			if (opmode(map_ch(ch)) == 3 || (s->req & 0x04)) {   /* v21: also in auto mode (see spec v18 table) */
				u8 hpbit = (HREG(R_HP_EN) & (u8)(0x10 << mux.mapped)) != 0;

				s->ctl |= 0x20;
				hal_chan_enable(ch, 1);
				hal_power_on(ch, hpbit);             /* F1 |= bit if HP else bit * 0x11 */
				s->code = 0x0A;
				s->next = SEQ_NEXT_POWER_GOOD;
			} else {
				mux_af(0);
			}
		} else {
			mux_af(0);
		}
	}
	m = mux.mapped;
	HREG(R_HP_P1_SAT + 5 * m) = ((s->cls >> 6) & 3) == 2;   /* stock 0x4DAA */
	SVC_SET(ch);
	s->cls = (u8)((s->cls & 0xC7) | ((mux.code2 & 7) << 3));
	s->ev_b |= 0x02;
	mux.sub = 0;
	seq_active &= (u8)~bit_lo(ch);
	mux.ch = (u8)((ch + 1) & 3);
}

/* stock 0x29EE. Sub-states: 0 select, 1 first sample (tick 5), 2 mark (tick 9), 3 second sample (tick 14), 4 (tick 17), 5 finish (tick >= 10). */
void seq_class_mux(void)
{
	u8 act = seq_active;
	u8 ch;
	struct seq_ch __xdata *s;

	if (!act)
		return;
	mux.tick++;
	ch = mux.ch;
	s = &seq_ch[ch];
	switch (mux.sub) {
	case 0:
		mux.tick = 0;
		if (act & bit_lo(ch)) {
			mux.sub = 1;
			mux_af(1);
			mux.mapped = map_ch(ch);
#ifdef POE_FW_SEQ_V18
			mux.hp = (opmode(ch) == 3) ? 0 : hp_flag(mux.mapped);   /* v18 0x2A4D: no two-event class in auto mode */
#else
			mux.hp = hp_flag(mux.mapped);
#endif
		} else {
			mux.ch = ch = (u8)((ch + 1) & 3);
			s = &seq_ch[ch];
		}
		s->cls = (u8)((s->cls & 0x3F) | 0x40);
		break;
	case 1:
		if (mux.tick != 5)
			break;
		mux.s1 = sample(ch);
		mux.code1 = class_code(mux.s1);
		if (mux.hp && mux.code1 == 4) {
			mux.sub = 2;
			mux_af(2);
			s->cls = (u8)((s->cls & 0x3F) | 0x80);
		} else {
			mux.sub = 5;
			mux_af(0);
			mux.code2 = mux.code1;
		}
		break;
	case 2:
		if (mux.tick != 9)
			break;
		mux.sub = 3;
		mux_af(1);
		break;
	case 3:
		if (mux.tick != 14)
			break;
		mux.s2 = sample(ch);
		mux.code2 = class_code(mux.s2);
		if (mux.code1 == mux.code2) {
			mux.sub = 4;
			mux_af(2);
		} else {
			mux_af(0);
			mux.sub = 5;
		}
		break;
	case 4:
		if (mux.tick == 17)
			mux.sub = 5;
		break;
	case 5:
		if (mux.tick >= 10)
			mux_finish();
		break;
	default:
		break;
	}
}

/* ---- SAT_Pn: shadow (bits 3:0 detect, 6:4 class), copied whole into reg 0x0C + m ---- */

/* stock 0x1A98..0x1AAF: new detect, detect nibble and bit 7 cleared in shadow and SAT; class bits kept. */
void seq_sat_clear_detect(u8 ch)
{
	u8 m = map_ch(ch);

	seq_ch[m].shadow &= 0xF0;
	HREG(R_SAT(m)) = seq_ch[m].shadow;
	evt_raise(R_DET_CLASS_EVT, bit_lo(m), 0x08);
}

/* stock 0x37C0 (shadow low nibble) + 0x3B3C..0x3BBA (publish). MISC_CONF bit 6: event only when the nibble changed. */
void seq_sat_detect(u8 ch, u8 code)
{
	u8 m = map_ch(ch);
	u8 sh = (u8)((seq_ch[m].shadow & 0xF0) | (code & 0x0F));
	u8 chg = 1;

	if (HREG(R_MISC_CONF) & 0x40)
		chg = (HREG(R_SAT(m)) & 0x0F) != (sh & 0x0F);
	seq_ch[m].shadow = sh;
	HREG(R_SAT(m)) = sh;
	if (chg)
		evt_raise(R_DET_CLASS_EVT, bit_lo(m), 0x08);
}

/* stock 0x4658: shadow bits 6:4 = ([80DD + 2c] & 0x38) << 1, copied to SAT. MISC_CONF bit 2: event only on class change. */
void seq_sat_class(u8 ch)
{
	u8 m = map_ch(ch);
	u8 sh = (u8)((seq_ch[m].shadow & 0x8F) | ((seq_ch[ch].cls & 0x38) << 1));
	u8 chg = 1;

	if (HREG(R_MISC_CONF) & 0x04)
		chg = ((HREG(R_SAT(m)) >> 4) & 7) != ((sh >> 4) & 7);
	seq_ch[m].shadow = sh;
	HREG(R_SAT(m)) = sh;
	if (chg)
		evt_raise(R_DET_CLASS_EVT, (u8)(0x10 << m), 0x10);
}

/* stock 0x4AD7 (tail): class power bits 2:0 and class code cleared, shadow &= 0x80 -> SAT. */
void seq_sat_reset(u8 ch)
{
	u8 m = map_ch(ch);

	seq_ch[ch].cls &= 0xC0;
	seq_ch[m].shadow &= 0x80;
	HREG(R_SAT(m)) = seq_ch[m].shadow;
}

/* ---- monitor, stock 0x2C79 (state code 0x0B) ---- */

static void cnt_nib(u8 m, u8 hi)                  /* stock 0x2CD3 / 0x2D1B: CNT_SHORT nibble++ (wraps at 16) */
{
	u8 r = R_CNT(m, CNT_SHORT);
	u8 v = HREG(r);

	if (hi)
		HREG(r) = (u8)((((v >> 4) + 1) & 0x0F) << 4 | (v & 0x0F));
	else
		HREG(r) = (u8)((v & 0xF0) | ((v + 1) & 0x0F));
}

static void off_status(u8 m)                      /* SAT_PWR enable + good cleared, PWR_EVT both nibbles */
{
	HREG(R_SAT_PWR) &= (u8)~(0x11 << m);
	evt_raise(R_PWR_EVT, (u8)(0x11 << m), 0x03);
}

void seq_monitor(u8 ch)
{
	struct seq_ch __xdata *s = &seq_ch[ch];
	u8 m = map_ch(ch);
	u8 a;

	if (s->ev_a & 0x02)                           /* 0x2C7E: acknowledge only */
		s->ev_a &= (u8)~0x02;

	if (s->ev_a & 0x41) {                         /* 0x2C94: overload / TCUT */
		s->ev_a &= (u8)~0x41;
		arm(ch, SEQ_NEXT_FAULT, 0x0C);
		off_status(m);
		evt_raise(R_TS_EVT, (u8)(0x10 << m), 0x20);
		cnt_nib(m, 1);
		return;
	}
	if (s->ev_a & 0x10) {                         /* 0x2CEE: current limit event */
		s->ev_a &= (u8)~0x10;
		arm(ch, SEQ_NEXT_FAULT, 0x0D);
		off_status(m);
		evt_raise(R_FAULT_EVT, bit_lo(ch), 0x20);
		cnt_nib(m, 0);
		return;
	}
	if (s->ev_a & 0x08) {                         /* 0x2D31: disconnect (MPS lost) */
		s->ev_a &= (u8)~0x08;
		arm(ch, SEQ_NEXT_IDLE, 0x02);
		evt_raise(R_FAULT_EVT, (u8)(0x10 << m), 0x04);
		off_status(m);
		HREG(R_CNT(m, CNT_MPS))++;
		seq_ch[m].shadow = 0x06;                  /* SAT_Pn = 0x06 */
		HREG(R_SAT(m)) = 0x06;
		return;
	}
	a = (s->ev_a & 0x20) | (s->ev_b & 0x30);
	if (a) {                                      /* 0x2DA4 */
		arm(ch, SEQ_NEXT_CLEANUP, 0x0E);
		s->ev_a &= (u8)~0x20;
		s->ev_b &= (u8)~0x30;
		off_status(m);
		return;
	}
	if (opmode(m) == 0)                           /* 0x2DD4 */
		s->req |= 0x10;
	if (s->req & 0x18) {                          /* 0x2DE7: off requested */
		arm(ch, SEQ_NEXT_CLEANUP, 0x0E);
		hal_f1_clear_lo(ch);
		seq_ch[m].shadow = 0;
		HREG(R_SAT(m)) = 0;
		s->req &= (u8)~0x18;
		off_status(m);
		return;
	}
	if (!hal_pg_status(m)) {                      /* 0x2E3C: power good low */
		if (++s->pgcnt >= 4) {
			s->pgcnt = 0;
			HREG(R_CNT(m, CNT_DENIED))++;
			arm(ch, SEQ_NEXT_FAULT, 0x0C);
			off_status(m);
			evt_raise(R_TS_EVT, (u8)(0x10 << m), 0x20);
			return;
		}
	}
	if (s->wait_a & 0x02) {                       /* 0x2E91: 2-tick refresh */
		s->wait_a &= (u8)~0x02;
		if (seq_ch[ch].ctl & 0x02) {
			seq_ch[ch].ctl &= (u8)~0x02;
			hal_ctrl_set_hi(ch);
		}
		s->req &= (u8)~0x04;
		HREG_PUT_LE16(R_P1_CM_LSB + 4 * m, s->cm);
		HREG_PUT_LE16(R_P1_VM_LSB + 4 * m, s->vm);
		s->mon_tmr = 2;                           /* stock 0x501E: wait_a |= 2 after 2 ticks */
	}
}

#endif
