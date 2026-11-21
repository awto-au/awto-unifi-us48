/* Measurement chain, ported from the stock periodic ISR functions as arithmetic (names TSV, hwdb/firmware/bcm59111-v21.names.tsv):
 *   0x415C calibration, 0x37D2 temperature, 0x34D0 convert (CM / VM per channel, 0x802E / 0x802F), 0x4900 class decode.
 * Stock runs them from the periodic ISR (period not known); ours runs once per 10 ms tick from the main loop (G).
 * The math unit E9..EF (16x16 multiply, 32-bit shift right) is replaced by C arithmetic with the same result (I).
 * 0x1B03 offset trim: meas_trim() below. Hardware meaning of all analog SFRs: U. */
#include "common.h"

__xdata s16 cal_temp_off;
__xdata s16 cal_slope;
__xdata u8 cal_c49;
__xdata s16 cal_off4c;
__xdata u8 cal_ch[4];
__xdata u8 cal_valid;
__bit __at(0x05) meas_due;
static __xdata u8 meas_ok;

void meas_init(void)
{
	hal_meas_calib();
	HREG(R_REG_16) = 0x42;        /* bench status: variant 4 (rewrite), IP.1 / T2CON.5 armed */
	meas_ok = 0;
	meas_due = 0;
#ifdef POE_FW_PROBE_ARM
	IP_1 = 1;                     /* stock 0x41D4 / 0x41D6: last two instructions of meas_init */
	T2CON_5 = 1;
#endif
}

u8 hal_temp_valid(void)
{
	return meas_ok;
}

/* stock 0x4900: class code from the class probe ADC word. 6 = class 0, 1..4 = class 1..4 (as the host reads SAT_Pn bits 6:4),
 * 7 = above class 4. Thresholds are the stock constants. C */
u8 class_code(u16 w)
{
	if (w < 0x006F)
		return 6;
	if (w < 0x00F7)
		return 1;
	if (w < 0x0189)
		return 2;
	if (w < 0x0233)
		return 3;
	if (w < 0x0322)
		return 4;
	return 7;
}

/* stock 0x37D2 + tail of 0x34D0 (0x3620..0x364D): TEMP raw, 0x802E / 0x802F. I */
static void meas_temp(void)
{
	static __xdata u16 sum, low, absv, f, x11;   /* XDATA statics: IRAM is full (docs/poe-fw-rewrite.md) */
	static __xdata s16 t, prod;
	static __xdata s32 p;
	static __xdata u32 q;

	sum = (u16)cal_temp_off + hal_ind_word(0x2C);
	t = (s16)(sum - 0x151);
	prod = (s16)(cal_slope * t);
	p = ((s32)prod >> 2) + (((u16)cal_c49 << 7) + 0x280);
	low = (u16)p;
	absv = (low & 0x8000) ? (u16)(-(s16)low) : low;
	q = ((u32)hal_ind_word(0x30) * absv) >> 14;
	if (p < 0)
		q = (u32)(-(s32)q);
	q += (u32)(s32)cal_off4c;
	if ((s32)q < 0)
		f = 0;
	else {
		f = (u16)q;
		if (f & 0x0400)
			f = 0x03FF;
	}
	x11 = (u16)(f * 11);
	HREG(R_TEMP) = (u8)(sum >> 1);
	HREG(R_REG_2E) = (u8)x11;
	HREG(R_REG_2F) = (u8)(x11 >> 8);
}

/* stock 0x34D0 per channel + 0x2EB2..0x2ED3 publish (442B): VM = word(AB = c | 4) * 11, CM = word(AB = c | 8) * k >> 10, k = 6403
 * for an F2-enabled (class 4) channel (stock: bit 5 of the 0x80DE + 2c byte, set with F2) else 3201. Powered channels only (G: stock gates
 * on a sequencer state byte == 0x0B). */
static void meas_channel(u8 ch, u8 powered, u8 hp)
{
	static __xdata u16 vm, cm;

	vm = 0;
	cm = 0;
	if (powered) {
		vm = (u16)(hal_ind_word((u8)(ch | 4)) * 11);
		cm = (u16)(((u32)hal_ind_word((u8)(ch | 8)) * (hp ? 6403u : 3201u)) >> 10);
	}
#ifndef POE_FW_PROBE2
	HREG_PUT_LE16(R_P1_CM_LSB + 4 * ch, cm);        /* probe2: 0x30..0x3F hold the D ring */
	HREG_PUT_LE16(R_P1_VM_LSB + 4 * ch, vm);
#endif
}

/* ---- stock 0x1B03: per-channel trim of the AB = c | 8 and c | 4 words, written back every periodic call. Port of seq_offset_trim
 * (wip/poe-seq-4, docs/poe-fw-seq/04-trim-measure.md section 6; arithmetic C, meaning of the registers U). The math unit E9..EF
 * is replaced by C arithmetic. Validated against the stock image in the oracle (scripts/poe_fw_oracle_diff.py, AB/AC/AD writes). ---- */
static __xdata u16 sq_acc[NCH];     /* 0x80E5 + 2c */
static __xdata u8 sq_cnt[NCH];      /* IRAM 0x25 + c */

/* macros, not functions: parameters of non-reentrant functions would take IRAM (full) */
/* shifts by 6 / 14 built from byte moves (a 32-bit shift loop in the compiler library costs ten times more) */
#define SHR6(x)  (((u32)(x) >> 8 << 2) | ((u8)(x) >> 6))
#define SHR14(x) ((((u32)(x) >> 16) << 2) | ((u8)((u32)(x) >> 8) >> 6))
#define sq_shr(v, n) ((u32)(v) >> (n))
#define sq_abs(hi, lo, cy) ((u16)(((u16)(u8)(~(hi) + (cy)) << 8) | (u8)(~(lo) + 1)))   /* stock abs idiom 0x1C9A: CY = carry of the preceding add */
#define sq_add32(pa, pb, pcy) do { sq_sum = *(pa) + (pb); (pcy) = sq_sum < *(pa); *(pa) = sq_sum; } while (0)
static __xdata u32 sq_sum;

void meas_trim(void)
{
	static __xdata u32 a32, b32, t;
	static __xdata u16 w, d, v, v2, absv;
	static __xdata s16 x, s;
	static __xdata u8 c, ae, flag0, acc_on, cy, top, sgn, y;

	acc_on = 0;
	for (c = 0; c < NCH; c++) {
		flag0 = 1;
		ae = AE_STAT;
		if (ae & (u8)(1 << (2 * c))) {
			acc_on = 1;
		} else {
			if (ae & (u8)(2 << (2 * c)))
				flag0 = 0;
			sq_cnt[c] = 0;
		}

		w = hal_ind_word(c);
		d = cal_valid ? (u16)(w - (0x100 + cal_ch[c])) : (u16)(w - 0x151);
		a32 = 0;
		b32 = 0;
		if (d != 0) {
			B5_SEL = (u8)(2 * c + 9);
			x = (s8)B7_DAT;
			if (cal_valid)
				x += flag0 ? -18 : -50;
			if (x != 0) {
				v = (u16)((u16)x * d);
				sgn = (u8)(((u8)(d >> 8) ^ (u8)((u16)x >> 8)) & 0x80);
				v = (u16)(v >> 2) | (sgn ? 0xC000u : 0);
				a32 = (u32)(s32)(s16)v;
			}
			x = (s8)B6_DAT;
			if (x != 0) {
				v = (u16)((u16)x * d);
				sgn = (u8)(((u8)(d >> 8) ^ (u8)((u16)x >> 8)) & 0x80);
				b32 = ((u32)(sgn ? 0xFFFFu : 0) << 16) | v;
			}
		}
		b32 += 0x80;

		B5_SEL = (u8)(2 * c + 8);
		y = B7_DAT;
		if (cal_valid)
			y = (u8)(y + (flag0 ? 0x0D : 0xE0));
		sq_add32(&a32, (u32)(u16)((u16)y << 7), cy);
		top = (u8)(a32 >> 24);
		absv = (top & 0x80) ? sq_abs(top, (u8)a32, cy) : (u16)a32;
		t = (u32)hal_ind_word((u8)(c | 0x28)) * absv;
		t = SHR6(t);
		if (top & 0x80)
			t = -t;
		t += b32;
		s = (s8)B6_DAT;
		if (!flag0)
			s -= 5;
		v = (u16)((u16)(t >> 8) + (u16)s);
		if (v & 0x8000)
			v = 0;
		if ((v >> 8) & 0x7C)
			v = 0x03FF;
		hal_ind_write((u8)(c | 8), v);
#ifdef POE_FW_PROBE3
		if (c == 1) {
			HREG(0x3C) = (u8)v; HREG(0x3D) = (u8)(v >> 8);       /* last word written at AB = 9 */
			HREG(0x3E) = (u8)w; HREG(0x3F) = (u8)(w >> 8);       /* raw word read at AB = 1 */
		}
#endif

		B5_SEL = (u8)(2 * c + 1);
		x = (s8)B7_DAT;
		v = (u16)((u16)x * d);
		v = (u16)((u16)((s16)v >> 2));
		a32 = (u32)(s32)(s16)v;
		B5_SEL = (u8)(2 * c);
		sq_add32(&a32, (u32)(u16)((u16)B7_DAT << 7), cy);
		top = (u8)(a32 >> 24);
		absv = ((u8)(a32 >> 8) & 0x80) ? sq_abs((u8)(a32 >> 8), (u8)a32, cy) : (u16)a32;
		t = (u32)hal_ind_word((u8)(c | 0x24)) * absv;
		t = SHR14(t);
		if (top & 0x80)
			t = -t;
		t += (u32)(s32)(s8)B6_DAT;
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
#ifdef POE_FW_PROBE3
		if (c == 1) {
			HREG(0x3A) = (u8)v2; HREG(0x3B) = (u8)(v2 >> 8);     /* last word written at AB = 5 */
		}
#endif
	}
#ifdef POE_FW_PROBE3
	if (!++HREG(0x38))
		HREG(0x39)++;                                            /* trim call count */
#endif
}

void meas_step(void)
{
	u8 c;

	meas_temp();
	if (!meas_ok)
		HREG(R_SUP_EVT) |= 0x80;      /* oracle: stock sets SUP_EVT bit 7 at the first measurement, no INT (meaning I) */
	meas_ok = 1;          /* TEMP is valid from the first measurement on: no bogus SUP event before it */
	/* Bench diagnostics (awto image only, read-only): live raw detect SFRs D1..D4 in regs 0x71..0x74 and the class-probe word
	 * (AB = c | 8, AD:AC) per channel: low bytes in regs 0x24..0x27, high bytes in 0x28..0x2B (0x76.. overlap our channel block). Lets the board show what the hardware returns. */
	for (c = 0; c < NCH; c++) {
		static __xdata u16 w;

#ifdef POE_FW_PROBE3
		continue;                             /* regs 0x71..0x73 are probe v3 regs */
#elif defined(POE_FW_PROBE2)
		if (c == 3)
			continue;                     /* reg 0x74 = probe end delta */
#endif
		HREG(0x71 + c) = hal_detect_raw(c);
#ifndef POE_FW_PROBE2
		w = hal_ind_word((u8)(c | 8));
		HREG(0x24 + c) = (u8)w;
		HREG(0x28 + c) = (u8)(w >> 8);
#else
		w = 0;
#endif
	}
	for (c = 0; c < NCH; c++)
		meas_channel(c, (HREG(R_SAT_PWR) & (u8)(0x10 << c)) != 0, (chans[c].flags & 2) != 0);
}

/* Main-loop side of hook 3: one run per hook-3 call in the stock order (0x1B03 not ported, 0x37D2 / 0x34D0 = meas_step every 4th
 * call because it is the expensive part, 0x29EE = class_step every call). If the main loop was starved, older calls are dropped. */
void periodic_chain(void)
{
	static __xdata u8 done, div;
	u8 lag = (u8)(per_cnt - done);

	if (lag == 0)
		return;
	if (lag > 8)
		done = (u8)(per_cnt - 8);
	done++;
	probe_poll();
	meas_trim();                         /* stock order 0x1B03, 0x37D2, 0x34D0, 0x29EE */
	if ((++div & 3) == 0)
		meas_step();
	class_step();
}
