/* Legacy-powerup probe sequencer (stock periodic_detcls_sequencer 0x316B and its states). Spec: docs/poe-fw-seq/01-detect-sequencer.md.
 * Compiled only with -DPOE_FW_SEQ (default off, not wired into the main loop). */
#ifndef SEQ_DETECT_H
#define SEQ_DETECT_H
#include "common.h"

/* record states (stock record +0, C) */
#define SEQ_IDLE   0
#define SEQ_ARMED  1    /* set by seq_begin (stock 0x49DD); seq_prepare turns it into SEQ_START */
#define SEQ_START  2
#define SEQ_PRE    3
#define SEQ_RAMP_A 4
#define SEQ_RAMP_B 5
#define SEQ_SETT_A 6
#define SEQ_SETT_B 7
#define SEQ_HOLD_A 8
#define SEQ_HOLD_B 9
#define SEQ_DONE_A 0x0A  /* finished (>= 0x0A ends the probe) */
#define SEQ_DONE_B 0x0B
#define SEQ_LOST   0x0D  /* probe failed (voltage below baseline, ramp too steep, or start too high) */

void hal_class_select(u8 ch);   /* hal_analog.c, stock 0x3292..0x3299 */

void seq_begin(u8 ch);          /* stock 0x49BD tail: D5 / D6 |= bit, state = 1 */
void seq_prepare(void);         /* stock 0x34D0 tail 0x35EC..0x3616: state 1 -> 2, clear counters */
u8 seq_step(u8 ch);             /* stock 0x316B body for the probe owner ch: 1 = reached state >= 0x0A (caller releases the probe) */
u8 seq_eval(u8 ch);             /* stock 0x3A30 decision (class-valid bit): 1 = success */
u8 seq_state(u8 ch);

#endif
