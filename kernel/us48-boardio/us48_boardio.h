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

#endif
