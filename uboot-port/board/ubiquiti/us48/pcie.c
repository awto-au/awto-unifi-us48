// SPDX-License-Identifier: GPL-2.0+
/*
 * Unit 0 PCIe root complex bring-up so unit 1 (BCM53346) is reachable from a cold power-up.
 * uboot-1 never writes CLK_CONTROL: cold 0x4 = PERST# asserted, so it never trains the link.
 * Sequence, sources and evidence: docs/pcie-bringup.md (#76). Constants: hwdb/gen/us48_hwdb.h.
 */

#include <linux/delay.h>
#include <linux/kernel.h>
#include <linux/types.h>
#include <linux/errno.h>
#include <time.h>
#include <vsprintf.h>
#include <asm/io.h>
#include "us48_hwdb.h"
#include "us48_pcie.h"

#define PAXB(r)		((void __iomem *)(HW_PAXB0_BASE + (r)))

#define LINK_UP		(HW_PAXB0_PCIE_LINK_STATUS_PCIE_PHYLINKUP_MASK | HW_PAXB0_PCIE_LINK_STATUS_PCIE_DL_ACTIVE_MASK)
#define PERST_ALL	(HW_PAXB0_CLK_CONTROL_RC_PCIE_RST_OUTPUT_MASK | HW_PAXB0_CLK_CONTROL_EP_MODE_SURVIVE_PERST_MASK | \
			 HW_PAXB0_CLK_CONTROL_EP_PERST_SOURCE_SELECT_MASK)

/* PCIe CEM Tperst = 100 us min, minus 10 % */
#define PERST_ASSERT_US		90
/* PCIe Base r6 6.6.1: 100 ms after PERST# release before the first config request (<= 5 GT/s), minus 10 % */
#define CFG_WAIT_MS		90
/* Wait for PHYLINKUP + DL_ACTIVE: expected <= 120 ms from release (100 ms + 20 ms Detect entry, PCIe Base r6 6.6.1), x1.25; expiry = fail + regs */
#define LINK_WAIT_MS		150
/* Wait for a vendor-ID read that is not RRS: expected 100 ms after the CFG_WAIT_MS (PCIe Base r6 6.6.1, I), x1.25; expiry = fail */
#define EP_WAIT_MS		125
#define POLL_US			100

#define CFG_RRS			0xffff0001U
#define EP_VID_DID		0x834614e4U	/* BCM53346 */

/* Unit 1 function 0 BARs as uboot-1's enumeration places them (bottom-up from 0x08000000, docs/pcie-bringup.md) */
#define EP_BAR0			(HW_PCIE0_WIN_BASE)			/* 32 KiB */
#define EP_BAR2			(HW_PCIE0_WIN_BASE + 0x40000)		/* 256 KiB CMIC, what us48_cmic_eth and fan.c use */
#define EP_BAR4			(HW_PCIE0_WIN_BASE + 0x1000000)	/* 16 MiB */
#define EP_CMD			0x146U	/* MEM + bus master + SERR as uboot-1 */

static u32 rc_r(u32 off)
{
	writel(off & 0xffc, PAXB(HW_PAXB0_CONFIG_IND_ADDR));
	return readl(PAXB(HW_PAXB0_CONFIG_IND_DATA));
}

static void rc_w(u32 off, u32 v)
{
	writel(off & 0xffc, PAXB(HW_PAXB0_CONFIG_IND_ADDR));
	writel(v, PAXB(HW_PAXB0_CONFIG_IND_DATA));
}

static u32 ep_r(u32 reg)
{
	writel((1 << 20) | (reg & 0xffc) | 1, PAXB(HW_PAXB0_CFG_ADDR));	/* bus 1 dev 0 fn 0, type 1 */
	return readl(PAXB(HW_PAXB0_CFG_DATA));
}

static void ep_w(u32 reg, u32 v)
{
	writel((1 << 20) | (reg & 0xffc) | 1, PAXB(HW_PAXB0_CFG_ADDR));
	writel(v, PAXB(HW_PAXB0_CFG_DATA));
}

static void dump(const char *tag)
{
	printf("pcie: %s CLK_CTRL %#x LINK_STATUS %#x RESET_STATUS %#x STRAP %#x LINKSTA %#x (no LTSSM reg known)\n",
	       tag, readl(PAXB(HW_PAXB0_CLK_CONTROL)), readl(PAXB(HW_PAXB0_PCIE_LINK_STATUS)),
	       readl(PAXB(HW_PAXB0_RESET_STATUS)), readl(PAXB(HW_PAXB0_STRAP_STATUS)),
	       rc_r(HW_RCCFG0_LNKSTA) >> 16);
}

static bool link_up(void)
{
	return (readl(PAXB(HW_PAXB0_PCIE_LINK_STATUS)) & LINK_UP) == LINK_UP;
}

/* PERST# assert + release as pcie-iproc.c:731-755; returns the timer_get_us() stamp of the release */
static ulong perst_cycle(void)
{
	u32 v = readl(PAXB(HW_PAXB0_CLK_CONTROL));

	writel(v & ~PERST_ALL, PAXB(HW_PAXB0_CLK_CONTROL));
	udelay(PERST_ASSERT_US);
	writel((v & ~PERST_ALL) | HW_PAXB0_CLK_CONTROL_RC_PCIE_RST_OUTPUT_MASK, PAXB(HW_PAXB0_CLK_CONTROL));
	return timer_get_us();
}

/* Outbound 0x08000000 window (64 MiB x2) and inbound window 1, as stock soc_pcie_init 0xc033eb60-0xc033ebac; write only on a difference */
static void windows(void)
{
	static const struct { u32 off, val; } w[] = {
		{ HW_PAXB0_OMAP_0_LO, HW_PCIE0_WIN_BASE },
		{ HW_PAXB0_OARR_0, HW_PCIE0_WIN_BASE | 1 },
		{ HW_PAXB0_OMAP_1_LO, HW_PCIE0_WIN_BASE + 0x4000000 },
		{ HW_PAXB0_OARR_1, (HW_PCIE0_WIN_BASE + 0x4000000) | 1 },
		{ HW_PAXB0_IMAP1, 0x80 },
		{ HW_PAXB0_IARR_1_LOWER, 1 },
	};
	int i;

	for (i = 0; i < ARRAY_SIZE(w); i++)
		if (readl(PAXB(w[i].off)) != w[i].val)
			writel(w[i].val, PAXB(w[i].off));
}

static bool pcie_ok;

bool us48_pcie_up(void)
{
	return pcie_ok && link_up();
}

static int pcie_init(void)
{
	ulong t0, t;
	u32 v;

	dump("entry");
	if (!(readl(PAXB(HW_PAXB0_CLK_CONTROL)) & HW_PAXB0_CLK_CONTROL_RC_PCIE_RST_OUTPUT_MASK) || !link_up()) {
		t0 = perst_cycle();
		printf("pcie: PERST# cycled (assert %d us)\n", PERST_ASSERT_US);
		windows();
		while (!link_up()) {
			t = timer_get_us() - t0;
			if (t > LINK_WAIT_MS * 1000UL) {
				printf("pcie: FAIL no link %lu us after PERST# release (limit %d ms)\n", t, LINK_WAIT_MS);
				dump("fail");
				return -ETIMEDOUT;
			}
			udelay(POLL_US);
		}
		printf("pcie: link up %lu us after PERST# release\n", timer_get_us() - t0);
		/* config requests only 100 ms after release */
		while ((t = timer_get_us() - t0) < CFG_WAIT_MS * 1000UL)
			udelay(POLL_US);
		t0 += CFG_WAIT_MS * 1000UL;
	} else {
		printf("pcie: link already up (warm), PERST# untouched\n");
		windows();
		t0 = timer_get_us();
	}

	if (((rc_r(0x18) >> 8) & 0xff) != 1) {
		rc_w(0x18, 0x00040100);			/* pri 0, sec 1, sub 4, latency 0 as uboot-1 */
		rc_w(0x20, (0x1000 << 16) | 0x0800);	/* mem base 0x08000000, limit 0x1000 as uboot-1 */
		rc_w(0x04, EP_CMD);
	}

	while ((v = ep_r(0)) == CFG_RRS) {
		t = timer_get_us() - t0;
		if (t > EP_WAIT_MS * 1000UL) {
			printf("pcie: FAIL unit 1 answers RRS %lu us after the config wait (limit %d ms)\n", t, EP_WAIT_MS);
			dump("fail");
			return -ETIMEDOUT;
		}
		udelay(POLL_US);
	}
	if (v != EP_VID_DID) {
		printf("pcie: FAIL unit 1 id %#x, want %#x\n", v, EP_VID_DID);
		dump("fail");
		return -ENODEV;
	}

	if ((ep_r(0x18) & ~0xfU) != EP_BAR2 || (ep_r(0x10) & ~0xfU) != EP_BAR0 || (ep_r(0x20) & ~0xfU) != EP_BAR4) {
		ep_w(0x10, EP_BAR0);
		ep_w(0x14, 0);
		ep_w(0x18, EP_BAR2);
		ep_w(0x1c, 0);
		ep_w(0x20, EP_BAR4);
		ep_w(0x24, 0);
		ep_w(0x04, EP_CMD);
	}
	if ((ep_r(0x18) & ~0xfU) != EP_BAR2) {
		printf("pcie: FAIL BAR2 reads %#x, want %#x\n", ep_r(0x18), EP_BAR2);
		return -EIO;
	}
	dump("up");
	printf("pcie: unit 1 %#x BAR0 %#x BAR2 %#x BAR4 %#x\n", ep_r(0), ep_r(0x10) & ~0xfU, ep_r(0x18) & ~0xfU,
	       ep_r(0x20) & ~0xfU);
	return 0;
}

int us48_pcie_init(void)
{
	int ret = pcie_init();

	pcie_ok = !ret;
	if (ret)
		printf("pcie: unit 1 unreachable, all unit 1 access skipped (fans, xe1)\n");
	return ret;
}
