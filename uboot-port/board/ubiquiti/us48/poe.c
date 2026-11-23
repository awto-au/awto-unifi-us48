// SPDX-License-Identifier: GPL-2.0+
/*
 * US-48 PoE: reset and broadcast firmware download of the 12 BCM59111 at boot (docs/poe.md 4 + 8, #189).
 * Same sequence as the Linux driver (kernel/bcm59111 bcm_fw_load_bcast): reset line CCA GPIO 9 (active low), PCA9548 channels 4+5
 * together (0x30), 512 blocks [0x70][0x80][off BE16][32 data] to broadcast 0x30, then 0x70 <- 0x40, then reg 0x75 == 0xaa per chip.
 * Every chip gets the stock cold config and the marker 0x01 = 0x66, so Linux sees it configured. Ports stay off.
 * A key press aborts (the load is the boot abort window). Any other failure: report, boot continues, Linux loads the firmware.
 */
#include <command.h>
#include <console.h>
#include <linux/errno.h>
#include <linux/delay.h>
#include <string.h>
#include <time.h>
#include <vsprintf.h>
#include <asm/io.h>
#include "us48_fan.h"
#include "us48_hwdb.h"

#define CCA_BASE	0x18000000
#define RST_BIT		BIT(9)		/* CCA GPIO 9, all 12 chips, low = reset (bench 2026-11-23, #26) */
#define RST_PULSE_MS	10		/* stock host pulse (docs/poe.md 8) */
#define RST_SETTLE_MS	1000		/* stock waits 1 s before the load */
#define PCA9548		0x70
#define BCAST		0x30
#define MUX_BOTH	0x30
#define MUX_CARD0	0x10
#define MUX_CARD1	0x20
#define CHIP0		0x28
#define NCHIP_CARD	6
#define FW_SIZE		0x4000
#define FW_BLOCK	32
#define REG_FW_DWN	0x70
#define REG_CRC		0x75
#define CRC_RUNNING	0xaa
#define FW_WAIT_MS	2500		/* stock sleeps 2 s then checks once, x1.25 (docs/poe.md 4) */
#define KEY_EVERY	16		/* blocks between console polls: 16 x 3.3 ms = 53 ms */
/* Polls of 10 us per transfer: 1.25 x wire time at 100 kHz (9 bits per byte incl. address) + margin */
#define POLLS(n)	(11 * ((n) + 1) + 10)

__asm__(".pushsection .rodata\n.balign 4\nus48_poe_fw:\n.incbin \"bcm59111-v21.bin\"\n.popsection\n");
extern const u8 us48_poe_fw[];

static bool poe_key(void)
{
	if (tstc()) {
		getchar();
		return true;
	}
	return false;
}

static int mux(u8 v)
{
	u32 t[2] = { PCA9548 << 1, BIT(31) | v };

	return us48_smb_xfer(t, 2, 7 << 9, POLLS(2));
}

static int wr(u8 a, u8 reg, u8 val)
{
	u32 t[3] = { a << 1, reg, BIT(31) | val };

	return us48_smb_xfer(t, 3, 7 << 9, POLLS(3));
}

static int rd(u8 a, u8 reg)
{
	u32 t[3] = { a << 1, reg, BIT(31) | (a << 1 | 1) };
	int ret = us48_smb_xfer(t, 3, 0xa << 9 | 1, POLLS(3));

	return ret ? ret : (int)(us48_smb_rx() & 0xff);
}

/* Reset pulse, then the abortable settle. 0 ok, -EINTR key */
static int poe_reset(void)
{
	void __iomem *g = (void __iomem *)CCA_BASE;
	u32 dout = readl(g + HW_CCA_GPIO_DOUT), oen = readl(g + HW_CCA_GPIO_OUTEN);
	int i;

	writel(dout & ~RST_BIT, g + HW_CCA_GPIO_DOUT);
	writel(oen | RST_BIT, g + HW_CCA_GPIO_OUTEN);
	mdelay(RST_PULSE_MS);
	writel(oen & ~RST_BIT, g + HW_CCA_GPIO_OUTEN);	/* released: pulled up */
	writel(dout, g + HW_CCA_GPIO_DOUT);
	for (i = 0; i < RST_SETTLE_MS / 10; i++) {
		if (poe_key())
			return -EINTR;
		mdelay(10);
	}
	return 0;
}

static int poe_download(void)
{
	const u8 *fw = us48_poe_fw;
	u32 t[1 + 4 + FW_BLOCK];
	unsigned int off, blk = 0;
	int ret, i;

	ret = mux(MUX_BOTH);
	if (ret)
		return ret;
	for (off = 0; off < FW_SIZE; off += FW_BLOCK, blk++) {
		if (blk % KEY_EVERY == 0 && poe_key()) {
			mux(0);
			return -EINTR;
		}
		t[0] = BCAST << 1;
		t[1] = REG_FW_DWN;
		t[2] = 0x80;
		t[3] = off >> 8;
		t[4] = off & 0xff;
		for (i = 0; i < FW_BLOCK; i++)
			t[5 + i] = fw[off + i];
		t[4 + FW_BLOCK] |= BIT(31);
		ret = us48_smb_xfer(t, 1 + 4 + FW_BLOCK, 7 << 9, POLLS(1 + 4 + FW_BLOCK));
		if (ret) {
			printf("poe: broadcast block at 0x%04x failed (%d)\n", off, ret);
			break;
		}
	}
	if (!ret) {
		t[0] = BCAST << 1;
		t[1] = REG_FW_DWN;
		t[2] = BIT(31) | 0x40;
		ret = us48_smb_xfer(t, 3, 7 << 9, POLLS(3));
	}
	mux(0);
	return ret;
}

/* Stock cold config, same values as kernel/bcm59111 bcm_cold_config + marker 0x01 = 0x66 (INT mask, stock init value) */
static int poe_config(u8 a)
{
	int c, ret = wr(a, 0x12, 0);	/* ports off */

	ret = ret ?: wr(a, 0x13, 0x0f);
	ret = ret ?: wr(a, 0x14, 0xff);
	ret = ret ?: wr(a, 0x44, 0x0f);
	for (c = 0; c < 4 && !ret; c++) {
		ret = wr(a, 0x46 + 5 * c, 0x01);
		ret = ret ?: wr(a, 0x47 + 5 * c, 0xe2);
		ret = ret ?: wr(a, 0x48 + 5 * c, 0xc0);
	}
	return ret ?: wr(a, 0x01, 0x66);
}

/* Every chip: wait for 0x75 == 0xaa (one deadline from the stop write), then configure. Returns the count of failed chips. */
static int poe_finish(void)
{
	ulong t0 = get_timer(0);
	int card, n, bad = 0, v, ret;

	for (card = 0; card < 2; card++) {
		mux(card ? MUX_CARD1 : MUX_CARD0);
		for (n = 0; n < NCHIP_CARD; n++) {
			u8 a = CHIP0 + n;

			for (;;) {
				v = rd(a, REG_CRC);
				if (v == CRC_RUNNING || get_timer(t0) > FW_WAIT_MS)
					break;
				mdelay(10);
			}
			ret = v == CRC_RUNNING ? poe_config(a) : -ETIMEDOUT;
			if (ret) {
				printf("poe: PoE-%02d (0x%02x on card %d): reg 0x75 = %d, config %d (limit %d ms)\n",
				       card * NCHIP_CARD + n + 1, a, card, v, ret, FW_WAIT_MS);
				bad++;
			}
		}
	}
	mux(0);
	return bad;
}

int us48_poe_load(void)
{
	ulong t0 = get_timer(0);
	int ret, bad;

	us48_smb_init();
	puts("poe: reset + broadcast firmware download (any key aborts)\n");
	ret = poe_reset();
	ret = ret ?: poe_download();
	if (ret == -EINTR) {
		puts("poe: aborted by key press\n");
		return ret;
	}
	if (ret) {
		printf("poe: download failed (%d), Linux will load the firmware\n", ret);
		return ret;
	}
	bad = poe_finish();
	printf("poe: %d of 12 chips running and configured, %lu ms\n", 12 - bad, get_timer(t0));
	return bad ? -EIO : 0;
}

static int do_poe(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	int card, n, v;

	if (argc == 2 && !strcmp(argv[1], "load"))
		return us48_poe_load() ? CMD_RET_FAILURE : CMD_RET_SUCCESS;
	if (argc != 1)
		return CMD_RET_USAGE;
	us48_smb_init();
	for (card = 0; card < 2; card++) {
		mux(card ? MUX_CARD1 : MUX_CARD0);
		for (n = 0; n < NCHIP_CARD; n++) {
			v = rd(CHIP0 + n, REG_CRC);
			printf("poe: PoE-%02d reg 0x75 = %d (0xaa = firmware running), reg 0x01 = %d\n", card * NCHIP_CARD + n + 1, v,
			       v == CRC_RUNNING ? rd(CHIP0 + n, 0x01) : -1);
		}
	}
	mux(0);
	return CMD_RET_SUCCESS;
}

U_BOOT_CMD(poe, 2, 0, do_poe, "PoE chips: status / reset + load firmware",
	   "      - reg 0x75 of the 12 BCM59111\n"
	   "poe load - reset line, broadcast download, config (ports stay off); any key aborts");
