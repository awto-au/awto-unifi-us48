/* Class mux schedule, SAT_Pn publishing and powered-channel monitor. Spec: docs/poe-fw-seq/02-class-monitor.md.
 * Not wired into the main loop; compiled only with -DPOE_FW_SEQ. Stock addresses = v21 (v18 map in the spec). */
#ifndef SEQ_CLASS_H
#define SEQ_CLASS_H
#include "common.h"

#ifdef POE_FW_SEQ

/* Next-state ids = stock state pointers written to 0x8087 + 18c (C). */
enum seq_next {
	SEQ_NEXT_NONE = 0,
	SEQ_NEXT_MONITOR = 0x2C79,      /* v18 0x2CA6 */
	SEQ_NEXT_POWER_GOOD = 0x2F8C,   /* v18 0x2F8C */
	SEQ_NEXT_FAULT = 0x4D5C,        /* v18 0x4DC4 */
	SEQ_NEXT_IDLE = 0x4EEF,         /* v18 0x4F32 */
	SEQ_NEXT_CLEANUP = 0x4F9F,      /* v18 0x4FE2 */
	SEQ_NEXT_CLASS_EVAL = 0x448D    /* v18 0x454A */
};

/* Per-channel record: the stock fields this cluster touches, native layout. Stock address = base + 18c unless noted. */
struct seq_ch {
	u8 ev_a;       /* 0x8078 channel-ISR flags: 0x40 ev.7, 0x10 ev.3, 0x08 ev.2, 0x01 ev.1 (+EV95.1), 0x80 ev.0; bit 1 acked by monitor */
	u8 ev_b;       /* 0x8079 bit 1 = class sample done (set here, consumed by st_class_done 0x47E4), bit 0 ev.6, bit 2 ev.4, bit 4 ext pin */
	u8 req;        /* 0x807B bit 2 = host power-on request, bit 3 = host power-off request, bit 4 = off because OP_MOD = 0 */
	u8 wait_a;     /* 0x807C bit 1 = monitor refresh timer expired */
	u8 code;       /* 0x8077 state code written with the next-state pointer: 0x0A power requested, 0x0B monitor */
	u16 next;      /* 0x8087 next-state pointer (enum seq_next) */
	u8 cls;        /* 0x80DD + 2c: bits 7:6 mux phase, bits 5:3 class code, bits 2:0 power state (2 = on) */
	u8 ctl;        /* 0x80DE + 2c: bit 5 power enabled via F2, bit 1 ctrl pending, bit 3 (v18 only) */
	u8 pgcnt;      /* 0x8120 + c consecutive power-good-low passes */
	u8 mon_tmr;    /* 0x8081 monitor timer, ticks (set_timer 0x5024) */
	u8 shadow;     /* IRAM 0x42 + c: SAT_Pn shadow, bits 3:0 detect code, bits 6:4 class code */
	u16 w8;        /* 0x80C7 + 6c: word(AB = c|8), captured by 0x34D0 earlier in the same periodic pass */
	u16 cm, vm;    /* 0x80F5 + 4c / 0x80F3 + 4c: published by the monitor to regs 0x30+4c */
};

extern __xdata struct seq_ch seq_ch[NCH];
extern __xdata u8 seq_active;       /* 0x8103: channels waiting for a class sample (set by st_class_gate) */
extern __xdata u8 seq_map[NCH];     /* 0x8125 + c logical -> physical channel */
extern __bit seq_remap;             /* IRAM 0x24 != 0 (regs 0x5E / 0x5F remap on) */

void seq_class_mux(void);                  /* stock 0x29EE, one call per periodic pass; v18 0x29E5 */
void seq_capture_w8(void);                 /* word(AB = c|8) part of stock 0x34D0 */
void seq_monitor(u8 ch);                   /* stock 0x2C79 state handler; v18 0x2CA6 */
void seq_sat_clear_detect(u8 ch);          /* 0x1A98..0x1AAF at detect phase 2 */
void seq_sat_detect(u8 ch, u8 code);       /* 0x37C0 + 0x3B3C publish */
void seq_sat_class(u8 ch);                 /* 0x4658 */
void seq_sat_reset(u8 ch);                 /* 0x4AD7 SAT part */

#endif
#endif
