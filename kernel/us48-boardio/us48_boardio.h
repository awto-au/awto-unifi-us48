/* SPDX-License-Identifier: GPL-2.0 */
/* us48_boardio: CMIC MSPI board-IO chains (docs/board.md "MSPI board IO"). Exported to us48_fan. */
#ifndef US48_BOARDIO_H
#define US48_BOARDIO_H

#include <linux/types.h>

struct device;
struct spi_device;

/* Chains by select pins (A = unit 0 GP0, B = unit 1 GP0); the length is fixed per chain */
enum us48_mspi_chain {
	US48_MSPI_POE_LED0,	/* (0,0) 6 B: PoE LEDs front 1-24 */
	US48_MSPI_POE_LED1,	/* (0,1) 6 B: PoE LEDs front 25-48 */
	US48_MSPI_FAN0,		/* (1,0) 7 B: byte 0 unit 0 fans, bytes 1-6 PoE mode select */
	US48_MSPI_FAN1,		/* (1,1) 7 B: byte 0 unit 1 fans, bytes 1-6 PoE mode select */
	US48_MSPI_NCHAIN
};

int us48_boardio_attach(struct spi_device *spi);
int us48_mspi_frame(enum us48_mspi_chain ch, const u8 *buf, size_t len);
int us48_poe_leds_attach(struct device *dev);

/* Frame state of the two (1,x) chains is owned here: byte 0 = fan level (bits 5:4), bytes 1..6 = 2-bit PoE mode field per port */
#define US48_POE_FIELD_NONE	0
#define US48_POE_FIELD_PASSIVE	1	/* 24 V on the spare pairs, no detection: the caller owns the safety checks (bcm59111 portN_mode) */
#define US48_POE_FIELD_8023	2
int us48_boardio_fan_level(unsigned int level);			/* both chains */
int us48_boardio_poe_field(unsigned int port, unsigned int field);	/* port 0..47 = front port - 1 (I: order not bench-verified) */
int us48_boardio_poe_field_get(unsigned int port);

#endif
