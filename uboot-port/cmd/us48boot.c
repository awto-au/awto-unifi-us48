// SPDX-License-Identifier: GPL-2.0+
/*
 * us48boot: boot the kernel slot of kernel0 from flash (#63, #73): header check, exact-length read, bootm.
 * On any failure: print the reason, return failure (stay at the prompt). No fallback, no retry.
 */

#include <command.h>
#include <image.h>
#include <vsprintf.h>
#include <linux/sizes.h>

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
	snprintf(cmd, sizeof(cmd), "bootm 0x%x", US48_LOADBUF);
	ret = run_command(cmd, 0);	/* returns only on failure: data CRC, unsupported image */
	return us48_fail("bootm returned", ret);
}

U_BOOT_CMD(us48boot, 1, 0, do_us48boot, "boot the kernel slot (0x2e0000) from flash",
	   "- uImage header magic + CRC, exact-length read, bootm; stops at the prompt on any failure");
