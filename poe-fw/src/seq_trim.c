/* Offset trim, temperature, CM / VM scaling, detect classification, calibration: faithful port of stock 0x1B03, 0x37D2, 0x34D0,
 * 0x3651, 0x415C (v21; v18 Meraki has byte-identical bodies at 0x1B03 / 0x380C / 0x350A / 0x368B / 0x41B1).
 * Spec: docs/poe-fw-seq/04-trim-measure.md. Evidence tags as in docs/poe-fw.md. Not wired; build with -DPOE_FW_SEQ.
 * The math unit E9..EF is replaced by C arithmetic (I); stock quirks are kept (see the helpers). */
#ifdef POE_FW_SEQ
#include "seq_trim.h"

/* Calibration, stock IRAM 0x49..0x4F, 0x61..0x65, 0x6A.7 */
static __xdata s16 sq_temp_off;   /* 0x4E:4F */
static __xdata s16 sq_slope;      /* 0x4A:4B */
static __xdata u8 sq_c49;         /* 0x49 */
static __xdata s16 sq_off4c;      /* 0x4C:4D */
static __xdata u8 sq_ch[NCH];     /* 0x62..0x65 */
static __xdata u8 sq_valid;       /* 0x6A.7 = D7.7 */
/* Working state */
static __xdata u16 sq_acc[NCH];   /* 0x80E5 + 2c */
static __xdata u8 sq_cnt[NCH];    /* IRAM 0x25 + c */
static __xdata u16 sq_tsum;       /* 0x5C:5D */
static __xdata u16 sq_f;          /* 0x5E:5F */
static __xdata u16 sq_raw[NCH][3];/* 0x80C5 + 6c: word(c|4), word(c|8), word(c) */
__xdata u16 seq_vm[NCH];          /* 0x80F3 + 4c */
__xdata u16 seq_cm[NCH];          /* 0x80F5 + 4c */

__xdata u8 seq_code[NCH];
__xdata u8 seq_rec[NCH][7];
__xdata u8 seq_cfgb[NCH];
__xdata u8 seq_stat[NCH];
__xdata u8 seq_remap_on;
__xdata u8 seq_remap[NCH];

/* Math unit shift, E9..EC >> n (EF = 0x20 | n). Truncating; rounding vs truncation is open question U1. */
static u32 sq_shr(u32 v, u8 n)
{
#ifdef SEQ_SHR_ROUND
	return (v + ((u32)1 << (n - 1))) >> n;
#else
	return v >> n;
#endif
}

/* 32-bit ADDC chain; returns the carry out of bit 31 (stock leaves it in CY). */
static u8 sq_add32(__xdata u32 *a, u32 b)
{
	u32 s = *a + b;
	u8 cy = s < *a;

	*a = s;
	return cy;
}

/* Stock abs idiom (0x1C9A, 0x1DF1, 0x3869): hi' = ~hi + CY, lo' = ~lo + 1 without carry out. CY is the leftover carry of the
 * preceding 32-bit add, not a carry of the negate (C). At 0x1C9A hi is the top byte of the 32-bit value, not byte 1. */
static u16 sq_abs(u8 hi, u8 lo, u8 cy)
{
	return ((u16)(u8)(~hi + cy) << 8) | (u8)(~lo + 1);
}

/* stock 0x415C: calibration at start-up. C */
void seq_trim_calib(void)
{
	u8 base, d7, c;

	hal_trim_f4_clear();
	hal_trim_sel(0x14);
	sq_temp_off = (s8)hal_trim_b6();
	hal_trim_ff_60();
	d7 = hal_trim_d7();
	base = d7 & 0x3F;
	sq_valid = d7 & 0x80;
	if (base & 0x20)
		base = (u8)(base + 0xC0);
	base = (u8)(base + 0x51);
	for (c = 0; c < NCH; c++) {
		hal_trim_sel((u8)(0x10 + c));
		sq_ch[c] = (u8)(base + hal_trim_b6());
	}
	hal_trim_sel(1);
	sq_slope = (s8)hal_trim_b7();
	hal_trim_sel(0);
	sq_c49 = hal_trim_b7();
	sq_off4c = (s8)hal_trim_b6();
	hal_trim_sel(0x15);
	sq_off4c += (s8)hal_trim_b6();
	hal_trim_end();
}

/* stock 0x1B03: per-channel trim of the c|8 and c|4 words. C (arithmetic), U (meaning of the registers) */
void seq_offset_trim(void)
{
	static __xdata u32 a32, b32, t;
	static __xdata u16 w, d, v, v2, absv;
	static __xdata s16 x, s;
	u8 c, ae, flag0, acc_on, cy, top, sgn;

	acc_on = 0;                                    /* 0x23.5 is cleared once per call, not per channel (C) */
	for (c = 0; c < NCH; c++) {
		flag0 = 1;                             /* 0x6A.0 */
		ae = hal_ae_status();
		if (ae & (u8)(1 << (2 * c))) {
			acc_on = 1;
		} else {
			if (ae & (u8)(2 << (2 * c)))
				flag0 = 0;
			sq_cnt[c] = 0;
		}

		w = hal_ind_word(c);
		d = sq_valid ? (u16)(w - (0x100 + sq_ch[c])) : (u16)(w - 0x151);
		a32 = 0;
		b32 = 0;
		if (d != 0) {
			hal_trim_sel((u8)(2 * c + 9));
			x = (s8)hal_trim_b7();
			if (sq_valid)
				x += flag0 ? -18 : -50;
			if (x != 0) {
				v = (u16)((u16)x * d);
				sgn = (u8)(((u8)(d >> 8) ^ (u8)((u16)x >> 8)) & 0x80);
				v = (u16)(v >> 2) | (sgn ? 0xC000u : 0);
				a32 = (u32)(s32)(s16)v;
			}
			x = (s8)hal_trim_b6();
			if (x != 0) {
				v = (u16)((u16)x * d);
				sgn = (u8)(((u8)(d >> 8) ^ (u8)((u16)x >> 8)) & 0x80);
				b32 = ((u32)(sgn ? 0xFFFFu : 0) << 16) | v;
			}
		}
		b32 += 0x80;

		hal_trim_sel((u8)(2 * c + 8));
		{
			u8 y = hal_trim_b7();

			if (sq_valid)
				y = (u8)(y + (flag0 ? 0x0D : 0xE0));
			cy = sq_add32(&a32, (u32)y << 7);
		}
		top = (u8)(a32 >> 24);
		absv = (top & 0x80) ? sq_abs(top, (u8)a32, cy) : (u16)a32;
		t = sq_shr((u32)hal_ind_word((u8)(c | 0x28)) * absv, 6);
		if (top & 0x80)
			t = -t;
		t += b32;
		s = (s8)hal_trim_b6();
		if (!flag0)
			s -= 5;
		v = (u16)((u16)(t >> 8) + (u16)s);
		if (v & 0x8000)
			v = 0;
		if ((v >> 8) & 0x7C)
			v = 0x03FF;
		hal_ind_write((u8)(c | 8), v);

		hal_trim_sel((u8)(2 * c + 1));
		x = (s8)hal_trim_b7();
		v = (u16)((u16)x * d);
		v = (u16)((u16)((s16)v >> 2));
		a32 = (u32)(s32)(s16)v;
		hal_trim_sel((u8)(2 * c));
		cy = sq_add32(&a32, (u32)hal_trim_b7() << 7);
		top = (u8)(a32 >> 24);
		absv = ((u8)(a32 >> 8) & 0x80) ? sq_abs((u8)(a32 >> 8), (u8)a32, cy) : (u16)a32;
		t = sq_shr((u32)hal_ind_word((u8)(c | 0x24)) * absv, 14);
		if (top & 0x80)
			t = -t;
		t += (u32)(s32)(s8)hal_trim_b6();
		v2 = (u16)t;
		if (t & 0x80000000UL)
			v2 = 0;
		if ((v2 >> 8) & 0x7C)
			v2 = 0x03FF;

		if (acc_on) {
			sq_cnt[c]++;
			if (sq_cnt[c] < 0x15) {
				sq_acc[c] = 0;
			} else {
				sq_acc[c] += v2;
				if (sq_cnt[c] >= 0x1C) {
					sq_cnt[c] = 0;
					v2 = (u16)sq_shr(sq_acc[c], 3);
				}
			}
		}
		hal_ind_write((u8)(c | 4), v2);
	}
}

/* stock 0x37D2: temperature sensor and supply word. C */
void seq_temp(void)
{
	static __xdata u32 a32, t;
	static __xdata u16 absv;
	static __xdata s16 tt, prod;
	u8 cy, top;
	u16 word30;

	sq_tsum = (u16)sq_temp_off + hal_ind_word(0x2C);
	tt = (s16)(sq_tsum - 0x151);
	prod = (s16)((u16)sq_slope * (u16)tt);
	a32 = (u32)(s32)(prod >> 2);
	cy = sq_add32(&a32, ((u16)sq_c49 << 7) + 0x280u);
	top = (u8)(a32 >> 24);
	absv = ((u8)(a32 >> 8) & 0x80) ? sq_abs((u8)(a32 >> 8), (u8)a32, cy) : (u16)a32;
	word30 = hal_ind_word(0x30);
	t = sq_shr((u32)word30 * absv, 14);
	if (top & 0x80)
		t = -t;
	t += (u32)(s32)sq_off4c;
	if (t & 0x80000000UL)
		sq_f = 0;
	else
		sq_f = (u16)t;
	if ((sq_f >> 8) & 0x04)          /* only bit 10 is tested (C) */
		sq_f = 0x03FF;
}

/* stock 0x34D0: raw words, VM / CM scaling, temperature publish. C */
void seq_convert(void)
{
	u8 c;
	u16 x11;

	for (c = 0; c < NCH; c++) {
		sq_raw[c][0] = hal_ind_word((u8)(c | 4));
		sq_raw[c][1] = hal_ind_word((u8)(c | 8));
		sq_raw[c][2] = hal_ind_word(c);
		if (seq_code[c] == 0x0B) {
			seq_vm[c] = (u16)(sq_raw[c][0] * 11u);
			seq_cm[c] = (u16)sq_shr((u32)sq_raw[c][1] * ((seq_cfgb[c] & 0x20) ? 6403u : 3201u), 10);
		} else {
			seq_vm[c] = 0;
			seq_cm[c] = 0;
		}
		if (seq_rec[c][0] == 1) {        /* hand-off to the detect sequencer (cluster 1) */
			seq_rec[c][0] = 2;
			seq_code[c] = 6;
			seq_rec[c][1] = 0;
			seq_rec[c][4] = 0;
			seq_rec[c][5] = 0;
			seq_rec[c][6] = 0;
		}
	}
	x11 = (u16)(sq_f * 11u);
	HREG(R_REG_2E) = (u8)x11;
	HREG(R_REG_2F) = (u8)(x11 >> 8);
	HREG(R_TEMP) = (u8)(sq_tsum >> 1);
}

void seq_meas_step(void)
{
	seq_offset_trim();
	seq_temp();
	seq_convert();
}

/* stock 0x2EBD..0x2ED3 -> 0x442B: CM lo / hi, VM lo / hi at 0x30 + 4c. The gate is the monitor (cluster 2). C */
void seq_publish(u8 c)
{
	HREG_PUT_LE16(R_P1_CM_LSB + 4 * c, seq_cm[c]);
	HREG_PUT_LE16(R_P1_VM_LSB + 4 * c, seq_vm[c]);
}

/* stock 0x4538..0x453F (st_port_cfg): zeroes the four bytes (R5 not set explicitly: I). */
void seq_publish_zero(u8 c)
{
	HREG_PUT_LE16(R_P1_CM_LSB + 4 * c, 0);
	HREG_PUT_LE16(R_P1_VM_LSB + 4 * c, 0);
}

/* stock 0x3651: detect code from the hardware nibble (D1..D4 >> 4) and four samples; I for the meaning of the codes
 * (1 short, 2 CPD high, 3 R-sig low, 4 good, 5 R-sig high, 6 open). Returns the code, updates seq_stat[p] low nibble. */
u8 seq_detect_classify(u8 c, u8 nib)
{
	u16 a, b, cc, d;
	s16 ac, bd, ad, ab, cd;
	u8 code = nib, p = c, m;

	if (seq_remap_on)
		p = seq_remap[c & 3];                 /* 0x50E0 / 0x22A4 */
	a = hal_det_sample((u8)(4 * c));
	b = hal_det_sample((u8)(4 * c + 1));
	cc = hal_det_sample((u8)(4 * c + 2));
	d = hal_det_sample((u8)(4 * c + 3));
	ac = (s16)(a - cc);
	bd = (s16)(b - d);
	ad = (s16)(a - d);
	ab = (s16)(a - b);
	cd = (s16)(cc - d);
	if (nib == 0x0F) {
		if (cd >= 0x5C || cc >= 0x0355)
			code = 6;
		else if (bd >= 0x96 || ac >= 0x96)
			code = 5;
		else if (ac > 0x55 && bd > 0x55 && ad > 0x55)
			code = 2;
		else
			code = (ad > (s16)((u16)ab * 3u + 10u)) ? 2 : 3;
	}
	m = HREG(R_HP_MOD(p & 3));
	if (m & 0x08) {
		if (code != 9 && code != 1)
			code = 4;
	} else if (m & 0x04) {
		if (a > b && b > cc && cc > d && ad > 0x6E)
			code = 4;
	}
	if (code == 9)
		code = 6;
	seq_stat[p & 3] = (u8)((seq_stat[p & 3] & 0xF0) | (code & 0x0F));
	return code;
}
#endif
