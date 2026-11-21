/* I2C slave ISR body (hook 1, register bank 1). Spec: docs/poe-fw.md section 9; stock isr_i2c_slave 0x3F10 (C).
 * SFR DD status, F5 ack control, F6 TX data, F7 RX byte / status code. The F5 / F6 value sequence is copied from stock (U meaning). */
#include "common.h"

__data u8 i2c_phase;   /* stock IRAM 0x30: next byte after SLA+W is the register pointer */
__data u8 i2c_rx;      /* stock 0x34 */
__data u8 i2c_ptr;     /* stock 0x35 */

void isr_i2c_body(void) __using(1)
{
	if ((I2C_STATUS & 0x11) == 0x11) {
		I2C_CTRL = 0x03;
		i2c_rx = I2C_RX;
		if (i2c_rx == 0xA8 || i2c_rx == 0xB8) {          /* SLA+R / data ack: read byte */
			I2C_CTRL = 0x01;
			I2C_TX = HREG(i2c_ptr);                  /* MOVX @(0x8000 + ptr): not masked, like stock */
			I2C_CTRL = 0x02;
			I2C_TX = 0xC4;
			host_rd_dispatch(i2c_ptr);               /* post-process, then ptr++ */
			i2c_ptr++;
			i2c_phase = 1;
		} else if (i2c_rx == 0x80) {                     /* data received */
			I2C_CTRL = 0x01;
			i2c_wdata = I2C_RX;
			if (i2c_phase) {
				i2c_ptr = i2c_wdata;             /* first byte after SLA+W: register pointer */
				i2c_phase = 0;
			} else {
				host_wr_dispatch(i2c_ptr);       /* write handler, then ptr++ (auto-increment) */
				i2c_ptr++;
			}
			I2C_CTRL = 0x02;
			I2C_TX = 0xC4;
		} else {
			if (i2c_rx == 0xA0 && i2c_phase) {       /* stop */
				i2c_phase = 0;
				i2c_ptr = 0;
			}
			I2C_CTRL = 0x02;
			I2C_TX = 0xC4;
			if (i2c_rx == 0x60)                      /* SLA+W */
				i2c_phase = 1;
		}
	}
	if ((I2C_STATUS & 0x22) == 0x22)
		I2C_STATUS = (I2C_STATUS & 0xF0) | 0x02;
	if ((I2C_STATUS & 0x88) == 0x88) {                       /* bus error: main loop reloads */
		HREG(R_WDT_TIME) |= 0x01;
		HREG(R_REG_4A) = 0x01;
		I2C_STATUS = (I2C_STATUS & 0xF0) | 0x08;
	}
}
