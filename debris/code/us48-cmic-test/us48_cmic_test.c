// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic_test - bring-up probe for unit 1 (BCM53346, PCIe): enable front port 50
 * (xe1, docs/driver-internals.md 13.5) and receive one frame by CMIC packet DMA (section 5).
 * Test scaffold for #4; the real driver follows the sparx5 model.
 */
#include <linux/module.h>
#include <linux/pci.h>
#include <linux/dma-mapping.h>
#include <linux/delay.h>
#include <linux/io.h>

#define CMC0		0x31000
#define SCHAN_CTRL	(CMC0 + 0x000)
#define SCHAN_MSG(n)	(CMC0 + 0x00c + 4 * (n))
#define MIIM_PARAM	(CMC0 + 0x080)
#define MIIM_RDATA	(CMC0 + 0x084)
#define MIIM_ADDR	(CMC0 + 0x088)
#define MIIM_CTRL	(CMC0 + 0x08c)
#define MIIM_STAT	(CMC0 + 0x090)
#define DMA_CTRL(ch)	(CMC0 + 0x140 + 4 * (ch))
#define DMA_STAT	(CMC0 + 0x150)
#define DMA_DESC(ch)	(CMC0 + 0x158 + 4 * (ch))
#define COS_RX0(ch)	(CMC0 + 0x168 + 8 * (ch))
#define COS_RX1(ch)	(CMC0 + 0x16c + 8 * (ch))
#define DMA_STAT_CLR	(CMC0 + 0x1a4)
#define HOSTMEM_REMAP(n) (CMC0 + 0x474 + 4 * (n))
#define HOSTMEM_REMAP3	(CMC0 + 0x4ac)
#define RXBUF_RLS_CRED	0x1a000

#define TSC_A		2	/* TSC0 core on internal MIIM bus 1 */
#define XE1_LANE	2
#define RX_CH		1
#define BUF_LEN		2048

/* One MIIM or S-channel op: < 30 us on the wire; 1000 x 1 us polls = 1 ms cap, logged on expiry. */
#define OP_POLLS	1000
/* Wait for one LAN frame: broadcasts arrive every ~2 s on this segment (#4); 5 s cap, logged. */
#define RX_WAIT_MS	5000

static void __iomem *cmic;

static int op_wait(u32 reg, u32 done, const char *what)
{
	int i;

	for (i = 0; i < OP_POLLS; i++) {
		u32 v = readl(cmic + reg);

		if (v & done)
			return v;
		udelay(1);
	}
	pr_err("us48_cmic: %s: no done bit after %d us\n", what, OP_POLLS);
	return -ETIMEDOUT;
}

/*