/* Detect classifier (stock 0x3651) and class mux schedule (stock 0x29EE). Specs: docs/poe-fw-seq/04-trim-measure.md (0x3651),
 * 02-class-monitor.md (0x29EE) on branches wip/poe-seq-4 / -2; code read from the disassembly, not merged from those branches.
 * Evidence: C = code bytes, U = analog meaning unknown, G = ours. Stock runs 0x29EE once per periodic-ISR pass (period unknown);
 * ours once per 10 ms tick from the main loop (G). */
#include "common.h"

static __xdata u8 cm_active, cm_sub, cm_tick, cm_ch, cm_hp, cm_code1, cm_code2;
static __xdata u8 cm_done;                 /* bit per channel: sample finished (stock B(ch) bit 1) */
static __xdata u8 cm_cls[4];               /* class code per channel (stock [0x80DD + 2c] bits 5:3) */
static __xdata u8 cm_phase[4];             /* 1 = idle, 2 = two-event branch ran (stock [0x80DD + 2c] bits 7:6) */

/* stock 0x3651 sample file: D9 = idx, hi = DA, lo = DB (DA read first). U meaning */
static u16 d9word(u8 idx)
{
	u8 hi;

	D9_IDX = idx;
	hi = DA_HI;
	return ((u16)hi << 8) | DB_LO;
}

/* stock 0x3651 detect classifier: raw nibble -> detect code. nib 0xF = software classification from four samples; HP_Pn_MOD
 * bits 3 / 2 override to 4 (good); 9 -> 6. Codes: 1 short, 2 CPD too high, 3 R-sig too low, 4 good, 5 too high, 6 open. C */
__xdata u8 dc_nib;
__xdata u8 dc_diag[8];

u8 detect_classify(u8 ch)
{
	static __xdata u16 a, b, c, d;
	static __xdata s16 ac, bd, ad, ab, cd;
	static __xdata u8 code, mod;

	code = dc_nib;
	a = d9word((u8)(4 * ch));
	b = d9word((u8)(4 * ch + 1));
	c = d9word((u8)(4 * ch + 2));
	d = d9word((u8)(4 * ch + 3));
	dc_diag[0] = (u8)a; dc_diag[1] = (u8)(a >> 8);
	dc_diag[2] = (u8)b; dc_diag[3] = (u8)(b >> 8);
	dc_diag[4] = (u8)c; dc_diag[5] = (u8)(c >> 8);
	dc_diag[6] = (u8)d; dc_diag[7] = (u8)(d >> 8);
	ac = (s16)(a - c);
	bd = (s16)(b - d);
	ad = (s16)(a - d);
	ab = (s16)(a - b);
	cd = (s16)(c - d);
	if (dc_nib == 0x0F) {
		if (cd >= 0x5C || c >= 0x0355)
			code = 6;
		else if (bd >= 0x96 || ac >= 0x96)
			code = 5;
		else if (ac > 0x55 && bd > 0x55 && ad > 0x55)
			code = 2;
		else
			code = (ad > (s16)(3 * ab + 10)) ? 2 : 3;
	}
	mod = HREG(R_HP_MOD(ch));
	if (mod & 8) {
		if (code != 9 && code != 1)
			code = 4;
	} else if (mod & 4) {
		if (a > b && b > c && c > d && ad > 0x6E)
			code = 4;
	}
	if (code == 9)
		code = 6;
	return code;
}

void class_request(u8 ch)
{
	EA = 0;
	cm_active |= (u8)(1 << ch);
	cm_done &= (u8)~(1 << ch);
	EA = 1;
}

void class_cancel(u8 ch)
{
	EA = 0;
	cm_active &= (u8)~(1 << ch);
	cm_done &= (u8)~(1 << ch);
	EA = 1;
	if (cm_ch == ch) {
		cm_sub = 0;
		cm_tick = 0;
	}
	hal_af(ch, 0);
}

u8 class_take_done(u8 ch)
{
	if (!(cm_done & (u8)(1 << ch)))
		return 0;
	cm_done &= (u8)~(1 << ch);
	return 1;
}

u8 class_code_of(u8 ch)
{
	return cm_cls[ch];
}

/* sample = word(AB = ch | 8), forced to 0xFFFF when AF bit 7 reads back set (stock 0x2A9E / 0x2B1B) */
static u8 sample_code(void)
{
	static __xdata u16 w;

	w = hal_ind_word((u8)(cm_ch | 8));
	if (AF_MUX & 0x80)
		w = 0xFFFF;
	return class_code(w);
}

/* stock 0x29EE, one call per tick (G). AF = ch * 4 + {0, 1, 2}. */
void class_step(void)
{
	if (!cm_active)
		return;
	cm_tick++;
	switch (cm_sub) {
	case 0:                                          /* 0x2A29 select */
		cm_tick = 0;
		if (cm_active & (u8)(1 << cm_ch)) {
			hal_af(cm_ch, 1);
			cm_sub = 1;
			cm_hp = (HREG(R_HP_EN) & (u8)(1 << cm_ch)) ? (HREG(R_HP_MOD(cm_ch)) & 1) : 0;   /* 0x4A69 */
		} else {
			cm_ch = (cm_ch + 1) & 3;
		}
		cm_phase[cm_ch] = 1;
		break;
	case 1:                                          /* 0x2A84, tick 5 */
		if (cm_tick != 5)
			break;
		cm_code1 = sample_code();
		if (cm_hp && cm_code1 == 4) {
			hal_af(cm_ch, 2);
			cm_phase[cm_ch] = 2;
			cm_sub = 2;
		} else {
			hal_af(cm_ch, 0);
			cm_code2 = cm_code1;
			cm_sub = 5;
		}
		break;
	case 2:                                          /* 0x2AEA, tick 9 */
		if (cm_tick != 9)
			break;
		hal_af(cm_ch, 1);
		cm_sub = 3;
		break;
	case 3:                                          /* 0x2B01, tick 14 */
		if (cm_tick != 14)
			break;
		cm_code2 = sample_code();
		if (cm_code1 == cm_code2) {
			hal_af(cm_ch, 2);
			cm_sub = 4;
		} else {
			hal_af(cm_ch, 0);
			cm_sub = 5;
		}
		break;
	case 4:                                          /* 0x2B55, tick 17 */
		if (cm_tick == 17)
			cm_sub = 5;
		break;
	default:                                         /* 0x2B69, tick >= 10: finish */
		if (cm_tick < 10)
			break;
		/* stock powers the port here for class 4 in auto mode (F2 / F1, state 0x0A); ours does it in chan.c (POWER_ON) */
		if (cm_hp && !(cm_code1 == 4 && cm_code2 == 4))
			hal_af(cm_ch, 0);
		HREG(0x49 + 5 * cm_ch) = (cm_phase[cm_ch] == 2);          /* HP_Pn_SAT via 0x4DAA */
		cm_cls[cm_ch] = cm_code2;
		cm_done |= (u8)(1 << cm_ch);
		svc_request(cm_ch);
		cm_sub = 0;
		cm_active &= (u8)~(1 << cm_ch);
		cm_ch = (cm_ch + 1) & 3;
		break;
	}
}
