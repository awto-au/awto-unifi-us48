/* BCM59111 PSE firmware rewrite (#26): shared declarations. Spec: docs/poe-fw.md. Names: hwdb/firmware/bcm59111-v21.names.tsv.
 * Evidence tags in comments: C = code-evident in the stock image, I = inferred, G = our guess, U = UNVERIFIED hardware meaning. */
#ifndef COMMON_H
#define COMMON_H
#include <stdint.h>
#include "bcm59111_regs.h"

typedef uint8_t u8;
typedef uint16_t u16;
typedef int8_t s8;
typedef int16_t s16;
typedef int32_t s32;
typedef uint32_t u32;

#define NCH 4

/* Host register window: host register r = XRAM 0x8000 + r (C). */
#define HREG(r) (*(volatile __xdata u8 *)(0x8000u + (r)))
/* Endian helpers: host-visible fields keep the stock byte order, everything else is native little-endian.
 * be16: stock 0x805E:0x805F (Keil big-endian word, init_fw_rev_crc). le16: CM/VM register pairs (LSB first, C). */
#define HREG_PUT_BE16(r, v) do { HREG(r) = (u8)((v) >> 8); HREG((r) + 1) = (u8)(v); } while (0)
#define HREG_PUT_LE16(r, v) do { HREG(r) = (u8)(v); HREG((r) + 1) = (u8)((v) >> 8); } while (0)

/* Per-channel sets of ICUT / ILIM / HP mode registers: stride 5 (C). */
#define R_HP_MOD(c)  (R_HP_P1_MOD + 5 * (c))
#define R_HP_ICUT(c) (R_HP_P1_OV_CUT + 5 * (c))
#define R_HP_ILIM(c) (R_HP_P1_CL_FBC + 5 * (c))
#define R_SAT(c)     (R_SAT_P1 + (c))
#define R_CNT(c, k)  (R_CNT_P1_INVALID_SIG + 4 * (c) + (k))
#define CNT_INVALID 0
#define CNT_DENIED  1
#define CNT_SHORT   2
#define CNT_MPS     3

/* SFRs, stock names from the names TSV. U = hardware meaning not verified. */
__sfr __at(0x80) P0;
__sfr __at(0x90) P1;
__sfr __at(0xA0) P2;
__sfr __at(0x89) TMOD;
__sfr __at(0x8B) TL1;
__sfr __at(0x8D) TH1;
__sfr __at(0x93) MUX_SEL;
__sfr __at(0x95) EV95;
__sfr __at(0x96) MASK96;
__sfr __at(0xA1) CH0_LIMIT;
__sfr __at(0xA2) CH1_LIMIT;
__sfr __at(0xA3) CH2_LIMIT;
__sfr __at(0xA4) CH3_LIMIT;
__sfr __at(0xA5) SFR_A5;
__sfr __at(0xA7) SFR_A7;
__sfr __at(0xAB) AB_IDX;   /* indirect analog register index (I: stock 0x34ED, 0x37D8) */
__sfr __at(0xAC) AC_LO;    /* indirect register data, low byte (I) */
__sfr __at(0xAD) AD_HI;    /* indirect register data, high byte (I) */
__sfr __at(0xAF) AF_MUX;   /* analog mux / class sample select: ch*4 + {1,2,3,5} (I: stock 0x29EE) */
__sfr __at(0xAE) AE_STAT;   /* trim ready / state bits per channel (C: stock 0x1B03 reads it once per channel) */
__sfr __at(0x81) SFR_SP;
__sfr __at(0xA8) SFR_IE;
__sfr __at(0xB8) SFR_IP;
__sfr __at(0x88) SFR_TCON;
__sfr __at(0xB5) B5_SEL;   /* trim / calibration register select (I: stock 0x415C) */
__sfr __at(0xB6) B6_DAT;   /* signed 8-bit data for B5 (I) */
__sfr __at(0xB7) B7_DAT;   /* signed 8-bit data for B5 (I) */
__sfr __at(0xD9) D9_IDX;   /* detect sample file index / high / low (stock 0x3651) */
__sfr __at(0xDA) DA_HI;
__sfr __at(0xDB) DB_LO;
__sfr __at(0xD7) D7_TRIM;  /* trim word: bits 5:0 and bit 7 (I: stock 0x416C) */
__sfr __at(0xF3) SFR_F3;   /* per-channel power-good status, bit n = channel n (I: stock 0x5074, POE_FW_SEQ) */
__sfr __at(0xF4) SFR_F4;
__sfr __at(0xB1) CH0_CTRL;
__sfr __at(0xB2) CH1_CTRL;
__sfr __at(0xB3) CH2_CTRL;
__sfr __at(0xB4) CH3_CTRL;
__sfr __at(0xBC) CH0_EVENT;
__sfr __at(0xBD) CH1_EVENT;
__sfr __at(0xBE) CH2_EVENT;
__sfr __at(0xBF) CH3_EVENT;
__sfr __at(0xC9) PORT_GATE;
__sfr __at(0xD1) CH0_DET;
__sfr __at(0xD2) CH1_DET;
__sfr __at(0xD3) CH2_DET;
__sfr __at(0xD4) CH3_DET;
__sfr __at(0xD5) CLASS_START_A;
__sfr __at(0xD6) CLASS_START_B;
__sfr __at(0xDD) I2C_STATUS;
__sfr __at(0xE5) SFR_E5;
__sfr __at(0xE6) SFR_E6;
__sfr __at(0xE7) SFR_E7;
__sfr __at(0xE8) SFR_E8;
__sfr __at(0xF1) PWR_EN_SET;
__sfr __at(0xF2) PWR_EN_CLR;
__sfr __at(0xF3) PWR_GOOD;    /* per-channel power-good status, read after BC bit 4 (stock 0x5074, oracle) */
__sfr __at(0xF5) I2C_CTRL;
__sfr __at(0xF6) I2C_TX;
__sfr __at(0xF7) I2C_RX;
__sfr __at(0xF9) I2C_MODE;
__sfr __at(0xFF) SFR_FF;
__sbit __at(0xF8) INT_OUT;   /* host INT line (I) */
__sbit __at(0x8E) TR1;
__sbit __at(0xAA) EX1;       /* IE.2 */
__sbit __at(0xAB) ET1;       /* IE.3 */
__sbit __at(0xAF) EA;
__sbit __at(0xCE) T2CON_6;
__sbit __at(0xCD) T2CON_5;
__sbit __at(0xB9) IP_1;
__sbit __at(0xBA) IP_2;
__sbit __at(0xBB) IP_3;
__sbit __at(0xBC) IP_4;
__sbit __at(0xBD) IP_5;

/* IRAM bit flags at the stock addresses (C). Service flags 0x23.1..4, dirty flags 0x20..0x22. */
__bit __at(0x19) svc0;
__bit __at(0x1A) svc1;
__bit __at(0x1B) svc2;
__bit __at(0x1C) svc3;
__bit __at(0x00) d_pb_dc;     /* 0x20.0 reg 0x18 */
__bit __at(0x01) d_restart;   /* 0x20.1 restart all channels (set by hal/ISR2 paths) */
__bit __at(0x02) d_cfg5e;     /* 0x20.2 regs 0x5E / 0x5F */
__bit __at(0x06) d_t1e;       /* 0x20.6 reg 0x1E */
__bit __at(0x07) d_t1f;       /* 0x20.7 reg 0x1F */
__bit __at(0x08) d_dis_sen;   /* 0x21.0 reg 0x13 */
__bit __at(0x09) d_ilim0;     /* 0x21.1 reg 0x48 */
__bit __at(0x0A) d_ilim1;     /* 0x21.2 reg 0x4D */
__bit __at(0x0B) d_ilim2;     /* 0x21.3 reg 0x52 */
__bit __at(0x0C) d_ilim3;     /* 0x21.4 reg 0x57 */
__bit __at(0x0D) d_icut0;     /* 0x21.5 reg 0x47 */
__bit __at(0x0E) d_icut1;     /* 0x21.6 reg 0x4C */
__bit __at(0x0F) d_icut2;     /* 0x21.7 reg 0x51 */
__bit __at(0x10) d_icut3;     /* 0x22.0 reg 0x56 */
__bit __at(0x11) d_det_en;    /* 0x22.1 reg 0x14 */
__bit __at(0x14) d_pb_pwr;    /* 0x22.4 reg 0x19 */
__bit __at(0x16) d_hp_en;     /* 0x22.6 reg 0x44 */
__bit __at(0x17) d_pb_rst;    /* 0x22.7 reg 0x1A */

/* I2C slave state (stock IRAM 0x30 / 0x32 / 0x34 / 0x35, C). */
extern __data u8 i2c_phase;
extern __data u8 i2c_wdata;
extern __data u8 i2c_rx;
extern __data u8 i2c_ptr;

/* asm: table dispatch, tail-jumps into the [FF hi lo] handler (hooks.s) */
void host_wr_dispatch(u8 reg) __using(1);
void host_rd_dispatch(u8 reg) __using(1);   /* __using: sdcc switches the bank around calls into another bank */

#define INT_UPDATE() do { if (!(HREG(R_INT) & HREG(R_INT_MASK))) INT_OUT = 0; } while (0)

/* host watchdog (hostwd.c): awto-image-only regs 0x20 / 0x21 */
#define WDS_EXPIRED 0x01
#define WDS_ARMED   0x02
#define WDS_PRESENT 0x80
#define WD_SUP_BIT  0x40      /* SUP_EVT bit raised on expiry; INT bit 7 (SUP) */
extern __data u8 wd_armed;
extern __data u8 wd_cnt;
extern __bit __at(0x03) wd_trip;         /* set by the tick ISR on expiry, served by the main loop */
extern __bit __at(0x04) wd_restart;      /* set by the STAT write handler, served by the main loop */
void wd_init(void);
void wd_tick(void) __using(3);
void wd_service(void);
void chan_shutdown_all(void);

/* host regs / events (hostregs.c) */
void evt_raise(u8 pair_reg, u8 mask, u8 intbit);
void evt_clear_channel(u8 ch);
void cnt_inc(u8 ch, u8 k);
void sup_event(u8 mask);
void poll_common(void);
void hostregs_init(u8 strap);

/* channel state machine (chan.c). struct chan lives at XRAM 0x8078, stride 20: the generated channel ISRs (chan_isr.s)
 * write ev_hi / ev_lo. Layout is ours (stock stride 0x12 is not kept, internal words are native little-endian). */
struct chan {
	u8 ev_hi, ev_lo;   /* +0 +1 event flags set by the channel ISR (bit meaning U) */
	u8 pend;           /* +2 host requests CH_PEND_* */
	u8 flags;          /* +3 */
	u8 wait_b;         /* +4 timer-expiry bits, set by chan_tick */
	u8 dly;            /* +5 state delay ticks */
	u8 state;          /* +6 */
	u8 tmr[8];         /* +7..+14 event timers (ticks) */
	u8 wnext, wbit, wdly; /* +15..+17 generic wait: next state, timer bit, next delay */
	u8 det;            /* +18 last detect code */
	u8 rsv;            /* +19 */
};
#define CH_STRIDE 20
#define CH_PEND_DETCLASS 0x01
#define CH_PEND_PWR_ON   0x02
#define CH_PEND_PWR_OFF  0x04
#define CH_PEND_RESET    0x08
void chan_request(u8 ch, u8 bit);
extern __xdata __at(0x8078) struct chan chans[NCH];
void rom_strap_call(u8 strap);   /* asm: stock ICALL through the ROM pointer at 0x1900 / 0x1903 (U) */
void isr_i2c_body(void) __using(1);
void chan_init(void);
void chan_run(u8 ch);
void chan_tick(void) __using(3);
void chan_i2c_error_reload(void);
void chan_restart_all(void);

/* hal_analog.c: every raw analog / power SFR access */
void hal_sfr_init(void);
void hal_timer_init(void);
void hal_ext_init(void);
void hal_ack_events(u8 ch);
void hal_ctrl_defaults(u8 ch);
void hal_set_limit(u8 ch, u8 v);
void hal_dis_sen_all(u8 v);
void hal_dis_sen(u8 ch, u8 on);
void hal_detect_start(u8 ch);
void hal_detect_phase2(u8 ch);
u8 hal_detect_raw(u8 ch);
void hal_chan_enable(u8 ch, u8 on);
void hal_power_on(u8 ch, u8 hp);
void hal_ind_write(u8 idx, u16 w);
extern __xdata u8 cal_valid;
void meas_trim(void);
void live_check(void);
u8 hal_pwr_good(u8 ch);
void hal_ctrl_pulse(u8 ch);
void hal_ctrl_trigger(u8 ch);
void hal_ctrl_bit2(u8 ch);
void hal_power_off(u8 ch);
u8 hal_temp_valid(void);
void hal_wdt_mode(u8 v) __using(1);
void hal_periodic(void) __using(2);

/* measurement chain (meas.c, hal_analog.c) */
u16 hal_ind_word(u8 idx);             /* AB = idx; (AD << 8) | AC */
void hal_meas_calib(void);            /* stock 0x415C */
void hal_af(u8 ch, u8 v);             /* AF = ch * 4 + v */
extern __xdata u8 dc_nib;
extern __xdata u8 dc_diag[8];
u8 detect_classify(u8 ch);   /* dc_nib in, dc_diag out (XDATA: IRAM is full) */
void class_request(u8 ch);
void class_cancel(u8 ch);
u8 class_take_done(u8 ch);
u8 class_code_of(u8 ch);
void class_step(void);
void svc_request(u8 ch);
u8 class_code(u16 w);                 /* stock 0x4900 */
void meas_step(void);
extern __xdata u8 per_cnt;            /* hook 3 entries (periodic ISR), consumed by the main loop */
void periodic_chain(void);
void meas_init(void);
extern __bit __at(0x05) meas_due;

#ifdef POE_FW_SEQ
/* seq_class.c accessors (hal_analog.c, POE_FW_SEQ only) */
u8 hal_af_read(void);
void hal_af_write(u8 v);
u8 hal_pg_status(u8 ch);
void hal_ctrl_set_hi(u8 ch);
void hal_f1_clear_lo(u8 ch);
#endif

#endif

/* PROBE v2 (bcm59111-awto-probe2.bin only): detect evidence in host regs, see docs/poe-fw-rewrite.md "Probe v2" */
#ifdef POE_FW_PROBE3
#define PR_N 3     /* raw D ring samples */
#else
#define PR_N 16
#endif
#ifdef POE_FW_PROBE2
void probe_hist(u8 ch, u8 st);
void probe_start(u8 ch);
void probe_ev6(u8 ch);
void probe_end(u8 ch, u8 tmo);
void probe_poll(void);
#else
#define probe_hist(a, b) ((void)0)
#define probe_start(a)   ((void)0)
#define probe_ev6(a)     ((void)0)
#define probe_end(a, b)  ((void)0)
#define probe_poll()     ((void)0)
#endif
