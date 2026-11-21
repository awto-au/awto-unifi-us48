/* Analog / power SFR accessors for seq_power.c (cluster 3). Same rules as hal_analog.c: copy the stock access, name the
 * stock address. Kept in its own file so the parallel clusters do not conflict in hal_analog.c; fold in at merge. */
#include "common.h"
#include "seq_power.h"

__sfr __at(0xF3) PG_STAT;     /* read-only in stock: only 0x5074 touches it; bit per channel (power good, I) */

static const __code u8 bit_of[4] = { 0x01, 0x02, 0x04, 0x08 };   /* stock tbl 0x4C23 */

/* stock leaf_5074 0x5074: bit(ch) of SFR F3. */
u8 hal3_pg(u8 ch)
{
	return (PG_STAT & bit_of[ch]) != 0;
}

/* stock set_chan_ctrl_bit7 0x493F: on -> B(ch) |= 0x80, off -> B(ch) &= 0x7F. */
void hal3_ctrl_bit7(u8 ch, u8 on)
{
	switch (ch) {
	case 0: if (on) CH0_CTRL |= 0x80; else CH0_CTRL &= 0x7F; break;
	case 1: if (on) CH1_CTRL |= 0x80; else CH1_CTRL &= 0x7F; break;
	case 2: if (on) CH2_CTRL |= 0x80; else CH2_CTRL &= 0x7F; break;
	default: if (on) CH3_CTRL |= 0x80; else CH3_CTRL &= 0x7F; break;
	}
}

/* stock set_chan_ctrl_bit2 0x497E: clr != 0 -> B(ch) &= 0xFB (the auto trigger), clr == 0 -> B(ch) |= 0x04. */
void hal3_ctrl_bit2(u8 ch, u8 clr)
{
	switch (ch) {
	case 0: if (clr) CH0_CTRL &= 0xFB; else CH0_CTRL |= 0x04; break;
	case 1: if (clr) CH1_CTRL &= 0xFB; else CH1_CTRL |= 0x04; break;
	case 2: if (clr) CH2_CTRL &= 0xFB; else CH2_CTRL |= 0x04; break;
	default: if (clr) CH3_CTRL &= 0xFB; else CH3_CTRL |= 0x04; break;
	}
}

/* stock set_chan_ctrl_field 0x4AA1: B(ch) &= 0xCF; B(ch) |= (v << 4) & 0xF0 (v = 0 in every stock caller but the class one). */
void hal3_ctrl_field(u8 ch, u8 v)
{
	u8 o = (u8)(v << 4);

	switch (ch) {
	case 0: CH0_CTRL &= 0xCF; CH0_CTRL |= o; break;
	case 1: CH1_CTRL &= 0xCF; CH1_CTRL |= o; break;
	case 2: CH2_CTRL &= 0xCF; CH2_CTRL |= o; break;
	default: CH3_CTRL &= 0xCF; CH3_CTRL |= o; break;
	}
}

/* stock st_fault_shutdown 0x4D5C..0x4D6A (via 0x2306 / 0x2154): EA off, F1 &= ~bit(ch) (low nibble only), EA on. */
void hal3_f1_off_low(u8 ch)
{
	EA = 0;
	PWR_EN_SET &= (u8)~bit_of[ch];
	EA = 1;
}

/* stock 0x4D5C / 0x2DF5: same SFR write as above is the only power-off in the fault and host-shutdown paths. */
