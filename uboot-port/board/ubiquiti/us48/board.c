// SPDX-License-Identifier: GPL-2.0+
/*
 * Ubiquiti UniFi Switch 48 PoE 500W (Broadcom Hurricane 2), second stage.
 * Entered from the stock U-Boot via bootm: DDR, PLLs and UART are set up.
 */

#include <clk-uclass.h>
#include <dm.h>
#include <fdtdec.h>
#include <init.h>
#include <log.h>
#include <vsprintf.h>
#include <asm/global_data.h>
#include <asm/io.h>
#include "us48_sram_clk.h"

DECLARE_GLOBAL_DATA_PTR;

/* RAM = bank 0 (0x0, 128 MiB) + bank 1 (0x68000000, 128 MiB), both from the DT */
int dram_init(void)
{
	return fdtdec_setup_mem_size_base();
}

int dram_init_banksize(void)
{
	return fdtdec_setup_memory_banksize();
}

/* Relocate to the top of bank 1: the low bank holds image, kernel and stock leftovers */
phys_addr_t board_get_usable_ram_top(phys_size_t total_size)
{
	return 0x70000000;
}

/* ARM PLL / CCU, layout and sequences: docs/clocks.md (register map, E1, E2); docs/uboot-cpu-clock.md */
#define SP805_BASE	0x18039000

/*
 * 0 = not yet run, 1 = at 400 (no switch asked), 2 = switched, <0 = refused/failed.
 * .data: set in board_early_init_f before relocation, where .bss overlays .rel.dyn (u-boot.lds).
 */
static int cpu_status __section(".data") = 0;

/* Mainline clk-iproc-armpll.c recalc: bypass, lock, offset mode, 0 = max divider. fid 2 (sys clk) -> 0 */
static ulong us48_arm_hz(void)
{
	u32 a = readl(CCU_PLLARMA);
	u32 fid = (readl(CCU_POLICY_DBG) >> 12) & 7;
	u64 ndiv = (a >> 8) & 0x3ff;
	u32 pdiv = (a >> 24) & 7, mdiv;

	if (fid == 0)
		return 25000000;
	if ((fid != 6 && fid != 7) || (readl(CCU_PLLARMC) & BIT(8)) || !(a & BIT(28)) ||
	    (readl(CCU_PLLARM_OFFSET) & BIT(29)))
		return 0;
	mdiv = readl(fid == 6 ? CCU_PLLARMC : CCU_PLLARMCTL5) & 0xff;
	ndiv = ((ndiv ? ndiv : 1024) << 20) | (readl(CCU_PLLARMB) & 0xfffff);
	return (ulong)(((25000000ULL * ndiv) >> 20) / (pdiv ? pdiv : 8) / (mdiv ? mdiv : 256));
}

/* Wait for GO to self-clear. Expected ~10 us (few hundred 25 MHz cycles, I); 4096 reads >> that, expiry = fail */
static int ccu_go(u32 policy)
{
	int n;

	writel(policy, CCU_POLICY_FREQ);
	writel(3, CCU_POLICY_CTL);
	for (n = 0; n < 4096; n++)
		if (!(readl(CCU_POLICY_CTL) & 1))
			return 0;
	return -ETIMEDOUT;
}

static int us48_cpu_switch(int mhz)
{
	int n, ret = 0;
	u32 b;

	/* E0 gate: ch1 mdiv 2 (else fid 7 = 1600), fid 6, and we really are at 400 */
	if ((readl(CCU_PLLARMCTL5) & 0xff) != 2 || ((readl(CCU_POLICY_DBG) >> 12) & 7) != 6 ||
	    us48_arm_hz() / 1000000 != 400)	/* ndiv_frac 1 (stock) adds ~0.4 ppm */
		return -EINVAL;

	/* SP805 normally armed by dev.py before bootm; arm 10 s if not (APB clock, unchanged by this) */
	if ((readl(SP805_BASE + 8) & 3) != 3) {
		writel(0x1acce551, SP805_BASE + 0xc00);
		writel(50000000 * 10, SP805_BASE);
		writel(3, SP805_BASE + 8);
	}

#ifdef CONFIG_US48_SRAM_CLK
	return us48_sram_clk_cpu(mhz);
#endif
	if (mhz != 800 && !US48_RELOCK_OK(mhz))
		return -EINVAL;
	writel(0xa5a501, CCU_WR_ACCESS);
	if (mhz == 800) {
		ret = ccu_go(0x87070707);	/* E1: fid 7, no relock */
	} else {
		b = (readl(CCU_PLLARMB) & 0xfff00000) | 1;
		ret = ccu_go(0x82020202);	/* E2: sys clk while relocking */
		if (ret) {
			ccu_go(0x86060606);
			goto out;
		}
		writel(b, CCU_PLLARMB);
		writel(US48_PLLARMA(mhz, 0), CCU_PLLARMA);	/* ndiv, pdiv 1, soft_resetb */
		/* lock: expected tens of us (I, unmeasured), 2000 reads at ~100 MHz sys clk; expiry = back to 400 */
		for (n = 0; n < 2000 && (readl(CCU_PLLARMA) & CCU_PLLARMA_LOCKED) != CCU_PLLARMA_LOCKED; n++)
			;
		if (n == 2000) {
			writel(0x11004003, CCU_PLLARMA);	/* VCO back to 1600 */
			ccu_go(0x86060606);
			ret = -ETIMEDOUT;
			goto out;
		}
		writel(US48_PLLARMA(mhz, 1), CCU_PLLARMA);	/* soft_post_resetb */
		ret = ccu_go(0x87070707);
		if (ret) {
			writel(0x11004003, CCU_PLLARMA);
			ccu_go(0x86060606);
		}
	}
out:
	writel(0, CCU_WR_ACCESS);
	return ret;
}

/* Before timer_init: the global timer reads its rate from periph_clk (below) once at probe */
int board_early_init_f(void)
{
	int mhz = CONFIG_US48_CPU_MHZ;

	if (mhz == 400) {
		cpu_status = 1;
		return 0;
	}
	cpu_status = us48_cpu_switch(mhz);
	if (!cpu_status)
		cpu_status = 2;
	return 0;
}

/* Console is up here */
int board_init(void)
{
	ulong hz = us48_arm_hz();

	printf("cpu: US48_CPU_MHZ=%d status=%d ARM=%lu MHz PERIPHCLK=%lu MHz\n", CONFIG_US48_CPU_MHZ,
	       cpu_status, hz / 1000000, hz / 2000000);
	if (cpu_status < 0)
		printf("cpu: switch refused/failed (%d), staying at 400\n", cpu_status);
	return 0;
}

/* PERIPHCLK = ARM / 2, from the live PLL registers; 0 reads fall back to the 400 MHz value */
static ulong us48_periph_get_rate(struct clk *clk)
{
	ulong hz = us48_arm_hz();

	return hz ? hz / 2 : CFG_SYS_HZ_CLOCK;
}

static const struct clk_ops us48_periph_ops = {
	.get_rate = us48_periph_get_rate,
};

static const struct udevice_id us48_periph_ids[] = {
	{ .compatible = "awto,us48-periph-clk" },
	{}
};

U_BOOT_DRIVER(us48_periph_clk) = {
	.name = "us48_periph_clk",
	.id = UCLASS_CLK,
	.of_match = us48_periph_ids,
	.ops = &us48_periph_ops,
};
