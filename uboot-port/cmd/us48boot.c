// SPDX-License-Identifier: GPL-2.0+
/*
 * us48boot: boot the kernel slot of kernel0 from flash (#63, #73): header check, exact-length read, bootm.
 * On any failure: print the reason, return failure (stay at the prompt). No fallback, no retry.
 */

#include <command.h>
#include <image.h>
#include <vsprintf.h>
#include <linux/sizes.h>
#include <linux/errno.h>
#ifdef CONFIG_US48_POE
#include "../board/ubiquiti/us48/us48_fan.h"
#endif

#define US48_KSLOT_OFF	0x2e0000	/* kernel0 slot after the 2 MiB awto-uboot slot (dev.py SLOTS, docs/autochain.md) */
#define US48_KSLOT_SIZE	0xd00000	/* 13 MiB, to the end of kernel0 */
#define US48_LOADBUF	0x01000000	/* same RAM address as every other path (CONFIG_SYS_LOAD_ADDR) */

static int us48_fail(const char *what, int ret)
{
	printf("us48boot: %s (%d)\n", what, ret);
	return CMD_RET_FAILURE;
}

static int do_us48boot(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	struct legacy_img_hdr *hdr = (struct legacy_img_hdr *)US48_LOADBUF;
	char cmd[64];
	u32 len;
	int ret;

#ifdef CONFIG_US48_POE
	/* The PoE firmware load is also the abort window: a key press stops here, any other failure boots on (Linux loads it, #189) */
	ret = us48_poe_load();
	if (ret == -EINTR)
		return us48_fail("aborted by a key press during the PoE load", ret);
	if (ret)
		printf("us48boot: PoE load failed (%d), booting anyway, Linux loads the firmware\n", ret);
#endif
	ret = run_command("sf probe 0", 0);
	if (ret)
		return us48_fail("sf probe", ret);
	snprintf(cmd, sizeof(cmd), "sf read 0x%x 0x%x 0x%x", US48_LOADBUF, US48_KSLOT_OFF, (u32)sizeof(*hdr));
	ret = run_command(cmd, 0);
	if (ret)
		return us48_fail("sf read header", ret);
	if (!image_check_magic(hdr))
		return us48_fail("bad uImage magic in the kernel slot", be32_to_cpu(hdr->ih_magic));
	if (!image_check_hcrc(hdr))
		return us48_fail("uImage header CRC mismatch", 0);
	len = image_get_data_size(hdr);
	if (!len || len > US48_KSLOT_SIZE - sizeof(*hdr))
		return us48_fail("uImage data length outside the slot", len);
	printf("us48boot: header ok, %u B\n", len);
	snprintf(cmd, sizeof(cmd), "sf read 0x%x 0x%x 0x%x", US48_LOADBUF, US48_KSLOT_OFF, (u32)sizeof(*hdr) + len);
	ret = run_command(cmd, 0);
	if (ret)
		return us48_fail("sf read image", ret);
	puts("us48boot: image read\n");
	/* Same arming as dev.py _arm_sp805: 10 s boot WDT is shorter than a debug kernel's time to sp805-wdt probe. */
	ret = run_command("mw.l 18039c00 1acce551; mw.l 18039000 448b9b80; mw.l 18039008 3", 0);
	if (ret)
		return us48_fail("SP805 arm", ret);
	snprintf(cmd, sizeof(cmd), "bootm 0x%x", US48_LOADBUF);
	ret = run_command(cmd, 0);	/* returns only on failure: data CRC, unsupported image */
	return us48_fail("bootm returned", ret);
}

U_BOOT_CMD(us48boot, 1, 0, do_us48boot, "boot the kernel slot (0x2e0000) from flash",
	   "- uImage header magic + CRC, exact-length read, bootm; stops at the prompt on any failure");

#ifdef CONFIG_US48_STOCKBOOT
/*
 * us48stock (#78): RAM-load variant only. Read stock kernel1 (never written), stock's bootargs and machine ID, stop the SP805, bootm.
 * Stock facts: docs/uboot.md section 4 (cmdline, machid 3000, load/entry 0x18000), autochain.md (kernel1 = 0xfe0000, record 0x1ee0000 not read).
 */
#include <env.h>
#include <asm/global_data.h>
#include <asm/io.h>

DECLARE_GLOBAL_DATA_PTR;

#define US48_K1_OFF	0xfe0000
#define US48_K1_SIZE	0xf00000	/* kernel1 without the bootsel record (last 64 KiB), as uboot-1 reads it */
#define US48_SP805	0x18039000	/* DT us48-uboot.dts watchdog; unlock 0x1acce551 at +0xc00, control at +8 */

static int do_us48stock(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	struct legacy_img_hdr *hdr = (struct legacy_img_hdr *)US48_LOADBUF;
	char cmd[64];
	u32 len;
	int ret;

	ret = run_command("sf probe 0", 0);
	if (ret)
		return us48_fail("sf probe", ret);
	snprintf(cmd, sizeof(cmd), "sf read 0x%x 0x%x 0x%x", US48_LOADBUF, US48_K1_OFF, (u32)sizeof(*hdr));
	ret = run_command(cmd, 0);
	if (ret)
		return us48_fail("sf read kernel1 header", ret);
	if (!image_check_magic(hdr) || !image_check_hcrc(hdr))
		return us48_fail("kernel1: bad uImage magic or header CRC", be32_to_cpu(hdr->ih_magic));
	len = image_get_data_size(hdr);
	if (!len || len > US48_K1_SIZE - sizeof(*hdr))
		return us48_fail("kernel1: data length outside the slot", len);
	printf("us48stock: kernel1 '%s' %u B, load %08x\n", image_get_name(hdr), len, image_get_load(hdr));
	snprintf(cmd, sizeof(cmd), "sf read 0x%x 0x%x 0x%x", US48_LOADBUF, US48_K1_OFF, (u32)sizeof(*hdr) + len);
	ret = run_command(cmd, 0);
	if (ret)
		return us48_fail("sf read kernel1", ret);
	env_set("bootargs", "console=ttyS0,115200 mem=128M@0x0 mem=128M@0x68000000 "
		"mtdparts=spi1.0:768k(u-boot),64k(u-boot-env),64k(shmoo),15360k(kernel0),15424k(kernel1),1024k(cfg),64k(EEPROM) "
		"ubntbootid=1 ubnt_active_slot=1 ubnt_current_slot=1 ubootver=usw-v1.1.6.122-gd2310f3e");
	env_set("machid", "bb8");
	gd->bd->bi_boot_params = 0x8000;	/* ATAG list address uboot-1 uses (docs/uboot.md section 4); unset here = no cmdline, stock then guesses its mtd map */
	puts("us48stock: image read, SP805 off, bootm\n");
	snprintf(cmd, sizeof(cmd), "bootm 0x%x", US48_LOADBUF);
	run_command("wdt stop", 0);
	writel(0x1acce551, US48_SP805 + 0xc00);
	writel(0, US48_SP805 + 0x8);
	ret = run_command(cmd, 0);	/* returns only on failure */
	return us48_fail("bootm returned (SP805 is off: power cycle if the console hangs)", ret);
}

U_BOOT_CMD(us48stock, 1, 0, do_us48stock, "RAM-only: read kernel1 and boot stock",
	   "- #78 stock-warm capture variant; never in the flash image");
#endif
