/* Measurement chain, ported from the stock periodic ISR functions as arithmetic (names TSV, hwdb/firmware/bcm59111-v21.names.tsv):
 *   0x415C calibration, 0x37D2 temperature, 0x34D0 convert (CM / VM per channel, 0x802E / 0x802F), 0x4900 class decode.
 * Stock runs them from the periodic ISR (period not known); ours runs once per 10 ms tick from the main loop (G).
 * The math unit E9..EF (16x16 multiply, 32-bit shift right) is replaced by C arithmetic with the same result (I).
 * NOT ported: 0x1B03 current offset trim (feedback writes to AB = c | 4). Hardware meaning of all analog SFRs: U. */
#include "common.h"

__xdata s16 cal_temp_off;
__xdata s16 cal_slope;
__xdata u8 cal_c49;
__xdata s16 cal_off4c;
__xdata u8 cal_ch[4];
__bit __at(0x05) meas_due;
static __xdata u8 meas_ok;

void meas_init(void)
{
	hal_meas_calib();
	HREG(R_REG_16) = 0x42;        /* bench status: variant 4 (rewrite), IP.1 / T2CON.5 armed */
	meas_ok = 0;
	meas_due = 0;
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

#ifdef POE_FW_PROBE2
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
	if ((++div & 3) == 0)
		meas_step();
	class_step();
}
