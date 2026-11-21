/* Legacy-powerup probe sequencer, ported from stock 0x316B (periodic_detcls_sequencer) + states 0x31E1 / 0x31EB / 0x3205 / 0x321F /
 * 0x3245 / 0x32DB and the decision half of st_class_wait 0x3A30. Spec + evidence: docs/poe-fw-seq/01-detect-sequencer.md.
 * Not wired into the main loop. Hardware meaning of the sample (port voltage, word(AB = c | 4) >> 2) is I; every threshold is C. */
#ifdef POE_FW_SEQ
#include "seq_detect.h"

/* stock record at 0x8104 + 7c: +0 state .. +6 (field names ours) */
struct seq_rec {
	u8 state;
	u8 cnt_a;    /* +1 */
	u8 cnt_b;    /* +2 */
	u8 sample;   /* +3 */
	u8 base;     /* +4 */
	u8 rise_a;   /* +5 */
	u8 rise_b;   /* +6 */
};

static __xdata struct seq_rec rec[NCH];

/* stock 0x49BD..0x49DF (D5 / D6 part and state = 1; arm 0x3A30 + timer 100 are the caller's) */
void seq_begin(u8 ch)
{
	hal_class_start(ch);
	rec[ch].state = SEQ_ARMED;
}

/* stock 0x34D0 0x35EC..0x3616, every pass for every channel; cnt_b is kept (C). Stock also sets the channel delay byte to 6 (caller). */
void seq_prepare(void)
{
	u8 c;

	for (c = 0; c < NCH; c++) {
		struct seq_rec __xdata *r = &rec[c];

		if (r->state != SEQ_ARMED)
			continue;
		r->state = SEQ_START;
		r->cnt_a = 0;
		r->base = 0;
		r->rise_a = 0;
		r->rise_b = 0;
	}
}

/* 0x3292: restart the ramp, D6 = 0x20 | c << 6 */
static void restart(u8 c, struct seq_rec __xdata *r)
{
	r->state = SEQ_RAMP_B;
	hal_class_select(c);
}

/* stock 0x321F..0x3242: track the baseline, leave the ramp above 0x28 (state + 2) */
static void track(struct seq_rec __xdata *r, u8 x)
{
	r->cnt_b = 0;
	r->base = x;
	if (x > 0x28)
		r->state += 2;
}

u8 seq_step(u8 c)
{
	struct seq_rec __xdata *r = &rec[c];
	u8 x, d;

	x = (u8)(hal_ind_word((u8)(c | 4)) >> 2);   /* stock 0x3180..0x3195, word cached by 0x34D0 earlier in the pass */
	r->sample = x;
	if ((u16)x + 6 < r->base)                    /* 0x319F..0x31B2, 9-bit sum */
		r->state = SEQ_LOST;

	switch (r->state) {
	case SEQ_START:
		r->state = SEQ_PRE;
		break;
	case SEQ_PRE:                                /* 0x31EB: state is set before the test */
		r->state = SEQ_RAMP_A;
		if (x > 0x2A)
			r->state = SEQ_LOST;
		break;
	case SEQ_RAMP_A:                             /* 0x3205 */
		r->cnt_a++;
		if (r->cnt_a >= 0x1F) {
			restart(c, r);
			break;
		}
		track(r, x);
		break;
	case SEQ_RAMP_B:                             /* 0x321F */
		track(r, x);
		break;
	case SEQ_SETT_A:
	case SEQ_SETT_B:                             /* 0x3245 */
		r->cnt_a = 0;
		r->cnt_b++;
		d = (u8)(x - r->base);                   /* wraps: x < base is >= 0x48 and fails */
		if (d >= 0x48) {
			r->state = SEQ_LOST;
			break;
		}
		r->rise_a = d;
		if (r->state == SEQ_SETT_A) {
			if (r->cnt_b == 0x0A && d < 0x0C) {
				restart(c, r);
				break;
			}
			if (x <= 0x35 || r->cnt_b < 2)
				break;
		} else if (x <= 0x35 && r->cnt_b < 0x41) {
			break;
		}
		r->base = x;                             /* 0x32CD */
		r->state += 2;
		break;
	case SEQ_HOLD_A:
	case SEQ_HOLD_B:                             /* 0x32DB: wait as many passes as the settle phase took */
		r->cnt_a++;
		if (r->cnt_a == r->cnt_b) {
			r->rise_b = (u8)(x - r->base);
			r->state += 2;
		}
		break;
	default:                                     /* 0x0A.. 0x0D: not in the 0x31C5 table */
		break;
	}

	if (r->state >= SEQ_DONE_A) {                /* 0x3311: stock also sets delay byte 7, timer 0x14 (caller) and clears 80C0 bits 7:4 */
		hal_class_stop();
		return 1;
	}
	return 0;
}

/* stock 0x3A30 0x3A70..0x3ADE + 0x3AE1 test: class-valid bit (W1 bit 7) of the finished probe; W0 / W1 / next-state writes are the caller's */
u8 seq_eval(u8 c)
{
	struct seq_rec __xdata *r = &rec[c];
	u8 ok = 0, p, q, diff;

	if (r->state == SEQ_LOST)
		return 0;
	p = r->rise_a;
	q = r->rise_b;
	diff = (p > q) ? (u8)(p - q) : (u8)(q - p);
	if (r->cnt_b == 2)
		ok = (p < 0x14);
	else if (r->cnt_b < 0x41)
		ok = 1;
	else
		ok = (p >= 0x16);
	if (diff >= (u8)(p >> 1))
		ok = 0;
	return ok;
}

u8 seq_state(u8 c)
{
	return rec[c].state;
}
#endif
