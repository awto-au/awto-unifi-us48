/* hal_analog.c: the only file that touches the analog / power SFRs (D1..D6, A1..A7, B1..B4, C9, E5..E8, F1/F2, 93..96, FF).
 * Hardware meaning of these SFRs is not verified (docs/poe-fw.md section 8). Every access copies the stock sequence;
 * the comment names the stock function (names TSV) and address. U = UNVERIFIED, nothing here is invented. */
#include "common.h"

/* stock tbl 0x4C23 */
static const __code u8 bit_of[4] = { 0x01, 0x02, 0x04, 0x08 };

/* stock init_hw_channels 0x39F3..0x3A2F (tail), copied in order. U. */
void hal_sfr_init(void)
{
	CH0_CTRL = 0; CH1_CTRL = 0; CH2_CTRL = 0; CH3_CTRL = 0;
	SFR_A5 = 0;
	CH0_LIMIT = 0xF0; CH1_LIMIT = 0xF0; CH2_LIMIT = 0xF0; CH3_LIMIT = 0xF0;
	PORT_GATE = 0x0F;
	SFR_A7 = 0;
	PWR_EN_SET = 0;
	PWR_EN_CLR = 0;
	SFR_E5 = 4; SFR_E6 = 4; SFR_E7 = 4; SFR_E8 = 4;
	MASK96 = 0xFF;
	T2CON_6 = 1;                     /* stock SETB 0xC8.6 (0xCE) */
	IP_5 = 1; IP_4 = 1; IP_3 = 1; IP_2 = 1;
	SFR_FF = 0x60;
}

/* stock leaf_5083 0x5083: timer 1 mode 1, reload 0xE5F4 (6668 counts per tick). */
void hal_timer_init(void)
{
	TMOD |= 0x10;
	TL1 = 0xF4;
	TH1 = 0xE5;
	ET1 = 1;
	TR1 = 1;
}

/* stock leaf_50B7 0x50B7: SFR 0x93 from P2[2:0], enable IE.2. U. */
void hal_ext_init(void)
{
	MUX_SEL = (P2 & 0x07) | 0x28;
	EX1 = 1;
}

/* stock leaf_4CE4 0x4CE4: ack channel events (EV95 pair mask, BCx = 0xFF). U. */
void hal_ack_events(u8 ch)
{
	switch (ch) {
	case 0: EV95 = 0x03; CH0_EVENT = 0xFF; break;
	case 1: EV95 = 0x0C; CH1_EVENT = 0xFF; break;
	case 2: EV95 = 0x30; CH2_EVENT = 0xFF; break;
	default: EV95 = 0xC0; CH3_EVENT = 0xFF; break;
	}
}

/* stock st_port_cfg 0x44ED..0x4528 via 0x493F / 0x497E / 0x4FF6 / 0x4AA1 with R5 = 0:
 * B(ch) bit7 = 0, bit2 = 1, bits 5:4 = 0; C9 |= bit(ch). U. */
void hal_ctrl_defaults(u8 ch)
{
	switch (ch) {
	case 0: CH0_CTRL &= 0x7F; CH0_CTRL |= 0x04; CH0_CTRL &= 0xCF; break;
	case 1: CH1_CTRL &= 0x7F; CH1_CTRL |= 0x04; CH1_CTRL &= 0xCF; break;
	case 2: CH2_CTRL &= 0x7F; CH2_CTRL |= 0x04; CH2_CTRL &= 0xCF; break;
	default: CH3_CTRL &= 0x7F; CH3_CTRL |= 0x04; CH3_CTRL &= 0xCF; break;
	}
	PORT_GATE |= bit_of[ch];
}

/* stock set_chan_sfr_a 0x4ED5: A1..A4[ch] = v. U. */
void hal_set_limit(u8 ch, u8 v)
{
	switch (ch) {
	case 0: CH0_LIMIT = v; break;
	case 1: CH1_LIMIT = v; break;
	case 2: CH2_LIMIT = v; break;
	default: CH3_LIMIT = v; break;
	}
}

/* stock poll_common 0x23EF..0x23F8: C9 = ~DIS_SEN_EN (C: code; a set C9 bit = disconnect sense off, I). */
void hal_dis_sen_all(u8 v)
{
	PORT_GATE = (u8)~v;
}

/* stock port_output_gate 0x4FF6: on -> C9 &= ~bit, off -> C9 |= bit. */
void hal_dis_sen(u8 ch, u8 on)
{
	if (on)
		PORT_GATE &= (u8)~bit_of[ch];
	else
		PORT_GATE |= bit_of[ch];
}

/* stock st_detect_start 0x4601..0x4635: D1..D4[ch] = 1. U. */
void hal_detect_start(u8 ch)
{
	switch (ch) {
	case 0: CH0_DET = 1; break;
	case 1: CH1_DET = 1; break;
	case 2: CH2_DET = 1; break;
	default: CH3_DET = 1; break;
	}
}

/* stock st_detect_wait 0x1A60..0x1A95: D1..D4[ch] = 2. U. */
void hal_detect_phase2(u8 ch)
{
	switch (ch) {
	case 0: CH0_DET = 2; break;
	case 1: CH1_DET = 2; break;
	case 2: CH2_DET = 2; break;
	default: CH3_DET = 2; break;
	}
}

/* stock st_detect_result 0x3B3C..0x3B72: raw = D1..D4[ch]; code = raw >> 4 (stock also scales via 0x3651, not ported: TODO). U. */
u8 hal_detect_raw(u8 ch)
{
	switch (ch) {
	case 0: return CH0_DET;
	case 1: return CH1_DET;
	case 2: return CH2_DET;
	default: return CH3_DET;
	}
}

/* stock chcfg_4DD0 0x4DD0 (via 0x225C / 0x4DE2): F2 |= bit (on) or F2 &= ~bit (off), EA off around the RMW. */
void hal_chan_enable(u8 ch, u8 on)
{
	EA = 0;
	if (on)
		PWR_EN_CLR |= bit_of[ch];
	else
		PWR_EN_CLR &= (u8)~bit_of[ch];
	EA = 1;
}

/* stock 0x5074: F3 & bit(ch). C (oracle: the stock handler reads F3 after BC bit 4). */
u8 hal_pwr_good(u8 ch)
{
	return (PWR_GOOD & bit_of[ch]) != 0;
}

/* power-good block B writes: B2 04 then 00 (oracle tick 25), later bit 7 (tick 27). Meaning of the bits I. */
void hal_ctrl_pulse(u8 ch)
{
	switch (ch) {
	case 0: CH0_CTRL |= 0x04; CH0_CTRL &= 0xFB; break;
	case 1: CH1_CTRL |= 0x04; CH1_CTRL &= 0xFB; break;
	case 2: CH2_CTRL |= 0x04; CH2_CTRL &= 0xFB; break;
	default: CH3_CTRL |= 0x04; CH3_CTRL &= 0xFB; break;
	}
}

void hal_ctrl_bit2(u8 ch)
{
	switch (ch) {
	case 0: CH0_CTRL |= 0x04; break;
	case 1: CH1_CTRL |= 0x04; break;
	case 2: CH2_CTRL |= 0x04; break;
	default: CH3_CTRL |= 0x04; break;
	}
}

void hal_ctrl_trigger(u8 ch)
{
	switch (ch) {
	case 0: CH0_CTRL |= 0x80; break;
	case 1: CH1_CTRL |= 0x80; break;
	case 2: CH2_CTRL |= 0x80; break;
	default: CH3_CTRL |= 0x80; break;
	}
}

#ifdef POE_FW_NO_POWER
/* Build without any power-up path (bench stage 1/2): F1 is never written non-zero. */
void hal_power_on(u8 ch, u8 hp)
{
	(void)ch; (void)hp;
}
#else
/* stock st_power_on 0x4A31..0x4A52: F1 |= (HP bit set ? bit : bit * 0x11); HP bit test is on the HIGH nibble of HP_EN (C: code). */
void hal_power_on(u8 ch, u8 hp)
{
	PWR_EN_SET |= hp ? bit_of[ch] : (u8)(bit_of[ch] * 0x11);
}
#endif

/* stock st_port_reset 0x4AD7 via 0x2143: F2 &= ~bit, F1 &= ~(bit * 0x11), EA off. */
void hal_power_off(u8 ch)
{
	EA = 0;
	PWR_EN_CLR &= (u8)~bit_of[ch];
	PWR_EN_SET &= (u8)~(bit_of[ch] * 0x11);
	EA = 1;
}

void hal_periodic(void) __using(2)
{
}

/* stock wh_cfg42 0x4FE2: SFR F9 = 0x0B if (v & 0x1E) == 0x16 else 0x0A. */
void hal_wdt_mode(u8 v) __using(1)
{
	I2C_MODE = ((v & 0x1E) == 0x16) ? 0x0B : 0x0A;
}

/* ---- measurement chain: indirect analog register file (AB = index, AC = low, AD = high) and trim registers (B5 select, B6 / B7) ---- */

/* stock 0x34ED..0x3513 / 0x37D8..0x37E0: word = (AD << 8) | AC after AB = idx (word cells are stored AD first, AC second). I. */
u16 hal_ind_word(u8 idx)
{
	u8 lo;

	AB_IDX = idx;
	lo = AC_LO;
	return ((u16)AD_HI << 8) | lo;
}

/* stock 0x1B03 write side: AB = idx, AC = low, AD = high. C */
void hal_ind_write(u8 idx, u16 w)
{
	AB_IDX = idx;
	AC_LO = (u8)w;
	AD_HI = (u8)(w >> 8);
}

/* calibration values read once, stock 0x415C..0x41D6 (names TSV meas_*): written to cal[] by meas.c via these raw accessors. */
extern __xdata s16 cal_temp_off;     /* IRAM 0x4E:0x4F  B5 = 0x14, B6 sign-extended */
extern __xdata s16 cal_slope;        /* IRAM 0x4A:0x4B  B5 = 1, B7 sign-extended */
extern __xdata u8 cal_c49;           /* IRAM 0x49       B5 = 0, B7 */
extern __xdata s16 cal_off4c;        /* IRAM 0x4C:0x4D  B5 = 0 B6 + B5 = 0x15 B6, sign-extended */
extern __xdata u8 cal_ch[4];         /* IRAM 0x62..0x65 per-channel offset trims (consumer: 0x1B03, not ported) */

void hal_meas_calib(void)
{
	u8 base, d7, c;

	SFR_F4 = 0;
	B5_SEL = 0x14;
	cal_temp_off = (s8)B6_DAT;
	SFR_FF = 0x60;
	d7 = D7_TRIM;
	cal_valid = d7 & 0x80;           /* 0x6A.7 */
	base = d7 & 0x3F;
	if (base & 0x20)
		base = (u8)(base + 0xC0);
	base = (u8)(base + 0x51);
	for (c = 0; c < 4; c++) {
		B5_SEL = (u8)(0x10 + c);
		cal_ch[c] = (u8)(base + B6_DAT);
	}
	B5_SEL = 1;
	cal_slope = (s8)B7_DAT;
	B5_SEL = 0;
	cal_c49 = B7_DAT;
	cal_off4c = (s8)B6_DAT;
	B5_SEL = 0x15;
	cal_off4c += (s8)B6_DAT;
	/* stock 0x41D4 / 0x41D6: arm the periodic hook (slot 3). Without both the ROM never calls it (board 2026-11-21: probeB). C */
	IP_1 = 1;
	T2CON_5 = 1;
}

/* stock 0x29EE: AF = ch * 4 + v, v = 0 / 1 / 2 (C values, U meaning). */
void hal_af(u8 ch, u8 v)
{
	AF_MUX = (u8)(ch * 4) + v;
}
