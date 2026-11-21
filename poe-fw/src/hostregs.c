/* Host register handlers (called from the I2C ISR, register bank 1) and event / INT line logic. Spec: docs/poe-fw.md section 4, 9. */
#include "common.h"

__data u8 i2c_wdata;

void wh_nop(void) __using(1) { }
void rh_nop(void) __using(1) { }

/* store-only handlers: stock 0x518C 0x510E 0x5193 0x519A 0x51A1 0x51A8 (C) */
#define WH_PLAIN(n) void wh_##n(void) __using(1) { HREG(R_##n) = i2c_wdata; }
/* store + dirty flag, applied by poll_common (stock 0x50FC ...) */
#define WH_DIRTY(n, flag) void wh_##n(void) __using(1) { HREG(R_##n) = i2c_wdata; flag = 1; }
/* push-button regs OR into the register (stock 0x50C2 / 0x50CC / 0x50D6, C) */
#define WH_OR(n, flag) void wh_##n(void) __using(1) { HREG(R_##n) |= i2c_wdata; flag = 1; }

WH_PLAIN(MS_BO_EN)
WH_PLAIN(MISC_CONF)
WH_PLAIN(HP_P1_MOD)
WH_PLAIN(HP_P2_MOD)
WH_PLAIN(HP_P3_MOD)
WH_PLAIN(HP_P4_MOD)
WH_DIRTY(DIS_SEN_EN, d_dis_sen)
WH_DIRTY(DET_CLASS_EN, d_det_en)
WH_DIRTY(TIMER_P1P2, d_t1e)
WH_DIRTY(TIMER_P3P4, d_t1f)
WH_DIRTY(HP_EN, d_hp_en)
WH_DIRTY(HP_P1_OV_CUT, d_icut0)
WH_DIRTY(HP_P1_CL_FBC, d_ilim0)
WH_DIRTY(HP_P2_OV_CUT, d_icut1)
WH_DIRTY(HP_P2_CL_FBC, d_ilim1)
WH_DIRTY(HP_P3_OV_CUT, d_icut2)
WH_DIRTY(HP_P3_CL_FBC, d_ilim2)
WH_DIRTY(HP_P4_OV_CUT, d_icut3)
WH_DIRTY(HP_P4_CL_FBC, d_ilim3)
WH_DIRTY(REG_5E, d_cfg5e)
WH_DIRTY(REG_5F, d_cfg5e)
WH_OR(DET_CLASS_PUSH, d_pb_dc)
WH_OR(PWR_ONOFF_PUSH, d_pb_pwr)
WH_OR(RST_PUSH, d_pb_rst)

/* stock wh_int_mask 0x5031: store, drop the line when nothing pending. */
void wh_INT_MASK(void) __using(1)
{
	HREG(R_INT_MASK) = i2c_wdata;
	if (!(HREG(R_INT) & i2c_wdata))
		INT_OUT = 0;
}

/* stock wh_op_mod 0x2F7B: store, restart all four channel state machines (C). */
void wh_OP_MOD(void) __using(1)
{
	HREG(R_OP_MOD) = i2c_wdata;
	svc0 = 1; svc1 = 1; svc2 = 1; svc3 = 1;
}

/* stock wh_cfg42 0x4FE2 */
void wh_WDT_TIME(void) __using(1)
{
	HREG(R_WDT_TIME) = i2c_wdata;
	hal_wdt_mode(i2c_wdata);
}

/* Clear-on-read handlers (C: stock 0x4F57 0x4F6F 0x4D0E 0x4D35 0x4F87). The value was already sent from XRAM by the ISR. */
void rh_PWR_EVT_CLR(void) __using(1)
{
	HREG(R_PWR_EVT) = 0; HREG(R_PWR_EVT_CLR) = 0;
	HREG(R_INT) &= 0xFC;
	INT_UPDATE();
}

void rh_DET_CLASS_EVT_CLR(void) __using(1)
{
	HREG(R_DET_CLASS_EVT) = 0; HREG(R_DET_CLASS_EVT_CLR) = 0;
	HREG(R_INT) &= 0xE7;
	INT_UPDATE();
}

void rh_FAULT_EVT_CLR(void) __using(1)
{
	HREG(R_FAULT_EVT) = 0; HREG(R_FAULT_EVT_CLR) = 0;
	if (HREG(R_TS_EVT) & 0xF0)
		HREG(R_INT) &= 0xFB;
	else
		HREG(R_INT) &= 0xDB;
	INT_UPDATE();
}

void rh_TS_EVT_CLR(void) __using(1)
{
	HREG(R_TS_EVT) = 0; HREG(R_TS_EVT_CLR) = 0;
	if (HREG(R_FAULT_EVT) & 0x0F)
		HREG(R_INT) &= 0xBF;
	else
		HREG(R_INT) &= 0x9F;
	INT_UPDATE();
}

void rh_SUP_EVT_CLR(void) __using(1)
{
	HREG(R_SUP_EVT) = 0; HREG(R_SUP_EVT_CLR) = 0;
	HREG(R_INT) &= 0x7F;
	INT_UPDATE();
}

/* counters 0x60..0x6F: cleared after read (stock 0x51B6 ..., C) */
#define RH_CNT(n) void rh_##n(void) __using(1) { HREG(R_##n) = 0; }
RH_CNT(CNT_P1_INVALID_SIG) RH_CNT(CNT_P1_PWR_DENIED) RH_CNT(CNT_P1_SHORT_OVL) RH_CNT(CNT_P1_MPS_ABSENT)
RH_CNT(CNT_P2_INVALID_SIG) RH_CNT(CNT_P2_PWR_DENIED) RH_CNT(CNT_P2_SHORT_OVL) RH_CNT(CNT_P2_MPS_ABSENT)
RH_CNT(CNT_P3_INVALID_SIG) RH_CNT(CNT_P3_PWR_DENIED) RH_CNT(CNT_P3_SHORT_OVL) RH_CNT(CNT_P3_MPS_ABSENT)
RH_CNT(CNT_P4_INVALID_SIG) RH_CNT(CNT_P4_PWR_DENIED) RH_CNT(CNT_P4_SHORT_OVL) RH_CNT(CNT_P4_MPS_ABSENT)

/* Every handler the generated table names must exist: an undefined symbol fails the link (poe_fw_build.py). */

/* Main-context event helpers ------------------------------------------------------------------------------------ */

/* stock int_raise_pwr_good 0x4E1A / int_raise_class 0x4E3D: pair register |= mask (both copies), INT |= bit, raise the line. */
void evt_raise(u8 pair_reg, u8 mask, u8 intbit)
{
	EA = 0;
	HREG(pair_reg) |= mask;
	HREG(pair_reg + 1) = HREG(pair_reg);
	HREG(R_INT) |= intbit;
	if ((HREG(R_INT_MASK) & intbit) && (HREG(R_MISC_CONF) & 0x80))
		INT_OUT = 1;
	EA = 1;
}

/* stock set_sup_event 0x500A: SUP_EVT |= mask (both copies), INT SUP. */
void sup_event(u8 mask)
{
	evt_raise(R_SUP_EVT, mask, 0x80);
}

/* stock st_port_shutdown 0x2EE0: drop this channel's latches (G: bit layout from the clear-on-read handlers). */
void evt_clear_channel(u8 ch)
{
	u8 m = (u8)(1 << ch);

	EA = 0;
	HREG(R_PWR_EVT) &= (u8)~(m * 0x11);
	HREG(R_PWR_EVT_CLR) &= (u8)~(m * 0x11);
	HREG(R_FAULT_EVT) &= (u8)~(m * 0x11);
	HREG(R_FAULT_EVT_CLR) &= (u8)~(m * 0x11);
	if (!(HREG(R_PWR_EVT) | HREG(R_PWR_EVT_CLR)))
		HREG(R_INT) &= 0xFC;
	if (!(HREG(R_FAULT_EVT) | HREG(R_FAULT_EVT_CLR)))
		HREG(R_INT) &= 0xFB;
	INT_UPDATE();
	EA = 1;
}

/* Counter increment, saturating at the byte (G: stock 0x48BE increments, saturation unknown). */
void cnt_inc(u8 ch, u8 k)
{
	u8 r = R_CNT(ch, k);

	if (HREG(r) != 0xFF)
		HREG(r)++;
}

/* stock poll_common 0x230D..: consume dirty flags. Only effects code-evident in the stock disassembly are applied. */
void poll_common(void)
{
	u8 c, b;

	if (d_dis_sen) {                 /* 0x23EF: C9 = ~reg 0x13 (C) */
		d_dis_sen = 0;
		hal_dis_sen_all(HREG(R_DIS_SEN_EN));
	}
	if (d_pb_dc) {                   /* 0x247A: push-button detect/class, bits 3:0 detect, 7:4 class (I, LTC4266 layout) */
		d_pb_dc = 0;
		EA = 0;
		b = HREG(R_DET_CLASS_PUSH);
		HREG(R_DET_CLASS_PUSH) = 0;
		EA = 1;
		for (c = 0; c < NCH; c++)
			if (b & (u8)(0x11 << c))
				chan_request(c, CH_PEND_DETCLASS);
	}
	if (d_pb_pwr) {                  /* G: LTC4266 layout, bits 3:0 power on, 7:4 power off */
		d_pb_pwr = 0;
		EA = 0;
		b = HREG(R_PWR_ONOFF_PUSH);
		HREG(R_PWR_ONOFF_PUSH) = 0;
		EA = 1;
		for (c = 0; c < NCH; c++) {
			if (b & (u8)(0x01 << c))
				chan_request(c, CH_PEND_PWR_ON);
			if (b & (u8)(0x10 << c))
				chan_request(c, CH_PEND_PWR_OFF);
		}
	}
	if (d_pb_rst) {                  /* G: bits 3:0 reset channel */
		d_pb_rst = 0;
		EA = 0;
		b = HREG(R_RST_PUSH);
		HREG(R_RST_PUSH) = 0;
		EA = 1;
		for (c = 0; c < NCH; c++)
			if (b & (u8)(1 << c))
				chan_request(c, CH_PEND_RESET);
	}
	/* Dirty flags with no verified consumer yet: cleared, value stays in the register (TODO: apply via calc_limit 0x3334). */
	d_det_en = 0; d_hp_en = 0; d_t1e = 0; d_t1f = 0; d_cfg5e = 0;
	d_icut0 = 0; d_icut1 = 0; d_icut2 = 0; d_icut3 = 0;
	d_ilim0 = 0; d_ilim1 = 0; d_ilim2 = 0; d_ilim3 = 0;
}

/* Register defaults. Stock init_regs_set_a 0x4B40 (P0.0 strap = 1) / set_b 0x4C90 (strap = 0), init_fw_rev_crc 0x4071. */
#define FW_REV_AWTO 0xA1   /* new rev byte: stock v21 = 0x15, Meraki = 0x12; read by the Linux driver for a log line only */

void hostregs_init(u8 strap)
{
	u8 c;
	u8 pins = P1 & 0x0F;

	/* init_fw_rev_crc: FW_REV, 0x805E:5F = 0x00E4 (stock word, big-endian as written by Keil). CRC_STATUS 0xAA is written
	 * last, from main (deviation: host must not see "running" before the ports are in reset). */
	HREG(R_FW_REV) = FW_REV_AWTO;
	HREG_PUT_BE16(R_REG_5E, 0x00E4);

	HREG(R_SAT_PIN) = (u8)(((pins << 4) & 0x30) | (pins & 0x0C) | (strap ? 0x01 : 0x00));   /* 0x40BE: (swap(a) & 0x30) | (a & 0x0C), a = P1[3:0] */
	HREG(R_HP_EN) = strap ? 0xFF : 0x00;
	HREG(R_TIMER_P1P2) = 0;
	HREG(R_TIMER_P3P4) = 0;
	for (c = 0; c < NCH; c++) {
		HREG(R_HP_MOD(c)) = 0x01;                 /* init_hp_defaults 0x4089 */
		HREG(R_HP_ICUT(c)) = strap ? 0x54 : 0x14; /* init_icut_defaults 0x40A2 */
		HREG(R_HP_ILIM(c)) = strap ? 0x80 : 0x00; /* init_ilim_defaults 0x40B0 */
	}
	/* Not in stock: all ports start shut. Stock leaves OP_MOD as the ROM / reset left it (feature DEFAULT_AF_AUTO_MODE). */
	HREG(R_OP_MOD) = 0x00;
	wd_init();
}
