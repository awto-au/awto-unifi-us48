/* Cluster 3: power-up, power good, limits, fault / disconnect / re-detect state handlers of the stock v21 image.
 * Spec: docs/poe-fw-seq/03-powergood-limits.md. Not wired into the build (see that doc). Compile: -DSEQ_V18 selects the
 * Meraki v18 variant of the differing functions. */
#ifndef SEQ_POWER_H
#define SEQ_POWER_H
#include "common.h"

/* Per-channel block, stock XRAM 0x8077 + 0x12 * ch .. 0x8088 (names: stock address of each field). */
struct sq_ch {
	u8 code;        /* 0x8077 state / phase code (arm R3); read only by 0x3555 (== 0x0B) and 0x3A54 (== 7) */
	u8 ev_hi;       /* 0x8078 channel ISR flags: 0x80 BC.0, 0x40 BC.7, 0x10 BC.3, 0x08 BC.2, 0x01 BC.1 with EV95.1 */
	u8 ev_lo;       /* 0x8079 0x04 BC.4, 0x01 BC.6, 0x40 latch grant, 0x02 class mux */
	u8 pend_a;      /* 0x807A */
	u8 pend_b;      /* 0x807B 0x01 detect req, 0x02 class req, 0x04 power-on req, 0x08 power-off req, 0x10 host shutdown */
	u8 wa, wb;      /* 0x807C / 0x807D wait flags, set by the timers */
	u8 ta_cnt, ta_ma, ta_mb;   /* 0x807E timer A counter, mask 0x807F (-> wa) / 0x8080 (-> wb) */
	u8 tb_cnt, tb_ma, tb_mb;   /* 0x8081 timer B counter, mask 0x8082 (-> wa) / 0x8083 (-> wb) */
	u8 state;       /* SQ_* (stock 0x8087:0x8088 code pointer) */
	u8 cls;         /* 0x80DD + 2 ch: bits 2:0 phase, 5:3 class code, 7:6 probe field */
	u8 flg;         /* 0x80DE + 2 ch: 0x20 HP / F2, 0x08 v18 hold, 0x04 powered, 0x02 power good seen */
	u8 pg_miss;     /* 0x8120 + ch consecutive monitor passes with F3 bit clear */
};

/* State ids = stock handler addresses. SQ_KEEP = stock R4:R5 == 0. */
enum {
	SQ_KEEP = 0,
	SQ_PORT_RESET, SQ_PORT_SHUTDOWN, SQ_PORT_CFG, SQ_MODE_SELECT, SQ_POWER_REQUEST, SQ_POWER_ON, SQ_POWER_GOOD,
	SQ_FAULT, SQ_FAULT_COOLDOWN, SQ_DISCONNECT_CLEANUP, SQ_IDLE, SQ_WAIT_A7, SQ_WAIT_A6, SQ_WAIT_B1, SQ_WAIT_B6B,
	SQ_WAIT_B3, SQ_POWERON_HOLD, SQ_RETRY_A, SQ_RETRY_B, SQ_DETECT_INVALID,
	SQ_EXT_FIRST,   /* ids >= this belong to the other clusters */
	SQ_DETECT_ARM = SQ_EXT_FIRST,   /* 0x46AB (cluster 1) */
	SQ_MONITOR      /* 0x2C79 (cluster 2) */
};

extern __xdata struct sq_ch sq[NCH];
extern __xdata u8 sq_svc;                 /* bit per channel: run seq_run() for it (stock 0x23.1..4) */
extern void (*seq_ext_run)(u8 ch);        /* other clusters' states (detect, class, monitor) */

void seq_init(void);
void seq_tick(void);                      /* stock tick_timers 0x3C1E, once per 10 ms */
void seq_run(u8 ch);                      /* stock run_state 0x50AB for this channel */
void seq_icut_written(u8 ch);             /* host wrote ICUT: stock poll_common 0x2607.. */
void seq_ilim_written(u8 ch);             /* host wrote ILIM: stock poll_common 0x261B.. */
u8 seq_calc_limit(u8 ch);                 /* stock 0x3334 */

/* hal_analog_seq3.c */
u8 hal3_pg(u8 ch);
void hal3_ctrl_bit7(u8 ch, u8 on);
void hal3_ctrl_bit2(u8 ch, u8 clr);
void hal3_ctrl_field(u8 ch, u8 v);
void hal3_f1_off_low(u8 ch);
#endif
