// SPDX-License-Identifier: GPL-2.0
/*
 * us48_a2: remoteproc driver for the unit 1 Cortex-A9 (a2), aux device us48_pci.a2.
 * Firmware us48-a2.elf runs from a DT reserved-memory carveout in host DDR (design: docs/a2-design.md).
 */
#include <linux/auxiliary_bus.h>
#include <linux/debugfs.h>
#include <linux/firmware.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/ktime.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/regmap.h>
#include <linux/remoteproc.h>
#include <linux/seq_file.h>
#include <linux/sizes.h>
#include <linux/string.h>
#include "remoteproc_internal.h"
#include "us48_hwdb.h"
#include "us48_pci.h"
#include "us48_a2_boot.h"
#include "us48_a2_dcc.h"

#define A2_FW		"us48-a2.elf"
#define A2_CARVEOUT	"a2-carveout"	/* must match the firmware resource table */
#define A2_DBG(r)	(HW_CS_DEBUG0_BASE + HW_CS_DEBUG0_##r)
#define A2_CRU_CONTROL	(HW_CRU_BASE + HW_CRU_CONTROL)
#define LAR_KEY		0xc5acce55	/* CoreSight unlock, hwdb cs_debug0.LAR */
#define ARM_MRC_DTRRX	0xee100e15	/* mrc p14, 0, r0, c0, c5, 0; Rt in bits 15:12 */
#define ARM_MOV_PC_R0	0xe1a0f000
/*
 * Debug handshakes: a few A9 cycles + a few PCIe round trips (~us each). 1 ms backstop, not measured on the bus.
 * Expiry: log op + DSCR + limit, -ETIMEDOUT. A stalled a2 (bad AXI read) never halts: stop resets it.
 */
#define DBG_WAIT_US	1000

/*
 * a2 ARM clock: same sequence as awto-uboot us48_cpu_switch (uboot-port/board/ubiquiti/us48/board.c, docs/clock-audit.md 2),
 * written through the unit 1 regmap before the release. 0 = leave the CCU as the ROM left it.
 * GO and PLL lock: ~10 us / tens of us expected (I, unmeasured on unit 1); 1 ms limit, expiry = log + restore.
 */
/* Default 100 (sys clk, ROM state): a2 corrupts memory above 100 MHz, Dan 2026-11-15, #79 */
static uint cpu_mhz = 100;
module_param(cpu_mhz, uint, 0644);
MODULE_PARM_DESC(cpu_mhz, "a2 ARM clock set at start: 100 (sys clk), 400..600 (VCO 4x) or 700..1999 (VCO 2x), VCO a multiple of 25 MHz; 0 = leave");
/*
 * unit 1 AXI (GENPLL ch3, APB = AXI/4): power-on mdiv 5 = 400 MHz, uboot-1 runs unit 0 at 200 (mode 4) (#79).
 * Sequence = D-Link genpll.c iproc_config_genpll (docs/clocks.md E5). 0 = leave.
 */
static uint axi_mhz = 200;
module_param(axi_mhz, uint, 0644);
MODULE_PARM_DESC(axi_mhz, "unit 1 AXI clock set at start: 100, 200, 250, 285 or 400 (GENPLL table of genpll.c); 0 = leave");
/*
 * GENPLL lock after a ch3 load. Waits for: STATUS bit 0. VCO is unchanged (NDIV same), so lock stays set;
 * expected = one PCIe read. Limit 190 us = 1.25 x 149 us, the only PLL lock measured here (ARM PLL, GO + lock).
 * Expiry: log STATUS + limit, -ETIMEDOUT, a2 not started.
 */
#define GENPLL_WAIT_US	190
#define CCU(r)		(HW_CCU_BASE + HW_CCU_##r)
#define CCU_UNLOCK	0xa5a501
#define CCU_LOCKED	(HW_CCU_PLLARMA_LOCK_MASK | HW_CCU_PLLARMA_LOCK_RAW_MASK)
#define CCU_WAIT_US	1000
/* up to 600 MHz: VCO 4 x MHz through ch0 (mdiv 4, fid 6; 400 = stock); above: VCO 2 x MHz through ch1 (mdiv 2, fid 7) */
#define CCU_FID6(mhz)	((mhz) <= 600)
#define CCU_VCO_MHZ(mhz)	(CCU_FID6(mhz) ? (mhz) * 4 : (mhz) * 2)
#define CCU_POLICY(mhz)	(CCU_FID6(mhz) ? 0x86060606 : 0x87070707)
#define CCU_PLLARMA_VAL(mhz, post) (HW_CCU_PLLARMA_LOCK_MASK | 1 << HW_CCU_PLLARMA_PDIV_SHIFT | \
				    (CCU_VCO_MHZ(mhz) / 25) << HW_CCU_PLLARMA_NDIV_INT_SHIFT | ((post) ? 3 : 1))

/* Stub text + boot args (+0x200) end below this; an uncompressed Image goes at +0x8000, a zImage at +0x1000000 */
#define A2_STUB_KEEP	0x1000

/* Payload mode (Linux on a2): after the stub ELF, zImage + DTB (+ initramfs) are copied into the carveout. */
static bool payload;
module_param(payload, bool, 0644);
MODULE_PARM_DESC(payload, "load zImage/DTB/initramfs into the carveout after the (boot stub) ELF");
/* "uboot": firmware = raw us48-a2-uboot.bin (awto-uboot, load = entry = carveout base), no ELF, no resource table (docs/a2-uboot.md) */
static char *mode = "elf";
module_param(mode, charp, 0644);
MODULE_PARM_DESC(mode, "elf (stub or self-test ELF) or uboot (raw awto-uboot at the carveout base; payload=1 also preloads zImage + DTB)");
static char *kernel_fw = "us48-a2-zImage";
static char *dtb_fw = "us48-a2.dtb";
static char *initrd_fw = "us48-a2-initrd";	/* optional: empty, whitespace-only or a missing file = none */
static bool hdbgen_off;
module_param(hdbgen_off, bool, 0644);
MODULE_PARM_DESC(hdbgen_off, "clear DSCR.HDBGEN after the release: a2 runs without halting-debug enabled (bring-up experiment, #79)");
static bool stub_dcc = true;
module_param(stub_dcc, bool, 0644);
MODULE_PARM_DESC(stub_dcc, "boot stub writes DCC marker characters (proves the console path before the kernel runs)");
module_param(kernel_fw, charp, 0644);
module_param(dtb_fw, charp, 0644);
module_param(initrd_fw, charp, 0644);
/* Offsets in the carveout: DT properties of the reserved-memory node, a module parameter overrides. */
static uint kernel_off = U32_MAX, dtb_off = U32_MAX, initrd_off = U32_MAX;
module_param(kernel_off, uint, 0644);
module_param(dtb_off, uint, 0644);
module_param(initrd_off, uint, 0644);

struct us48_a2 {
	struct device *dev;
	struct us48_pci *p;
	struct regmap *map;
	struct us48_a2_dcc *dcc;	/* console reader: runs from before the release until stop */
	phys_addr_t phys;
	size_t size;
	void *va;	/* WC mapping of the carveout */
	u32 da;		/* a2 AXI address of the carveout start */
	u32 dt_off[3];	/* kernel, dtb, initrd offsets from the DT, U32_MAX if absent */
};

static int a2_wait(struct us48_a2 *a, u32 mask, u32 want, const char *what)
{
	u32 d;
	int ret = regmap_read_poll_timeout(a->map, A2_DBG(DSCR), d, (d & mask) == want, 0, DBG_WAIT_US);

	if (ret)
		dev_err(a->dev, "a2 %s: DSCR %#x, limit %d us, ret %d\n", what, d, DBG_WAIT_US, ret);
	return ret;
}

/* Pulse CRU_CONTROL.ARM_RST_N (RMW: unit 1 has other bits set): a2 returns to the boot ROM loop. */
static int a2_reset(struct us48_a2 *a)
{
	int ret = regmap_clear_bits(a->map, A2_CRU_CONTROL, HW_CRU_CONTROL_ARM_RST_N_MASK);

	return ret ?: regmap_set_bits(a->map, A2_CRU_CONTROL, HW_CRU_CONTROL_ARM_RST_N_MASK);
}

static int a2_halt(struct us48_a2 *a)
{
	int ret = regmap_write(a->map, A2_DBG(LAR), LAR_KEY);

	ret = ret ?: regmap_set_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_HDBGEN_MASK);
	ret = ret ?: regmap_write(a->map, A2_DBG(DRCR), HW_CS_DEBUG0_DRCR_REQ_HALT_MASK);
	return ret ?: a2_wait(a, HW_CS_DEBUG0_DSCR_HALTED_MASK, HW_CS_DEBUG0_DSCR_HALTED_MASK, "halt");
}

static int a2_itr(struct us48_a2 *a, u32 ins, const char *what)
{
	int ret = regmap_write(a->map, A2_DBG(ITR_PCSR), ins);

	return ret ?: a2_wait(a, HW_CS_DEBUG0_DSCR_INSTRCOMPL_MASK, HW_CS_DEBUG0_DSCR_INSTRCOMPL_MASK, what);
}

/* Halted a2: Rn = val through DTRRX. Rn > 0 is for a boot stub's r0-r2 later (not used yet). */
static int a2_set_reg(struct us48_a2 *a, unsigned int rn, u32 val)
{
	return regmap_write(a->map, A2_DBG(DTRRX), val) ?: a2_itr(a, ARM_MRC_DTRRX | rn << 12, "mrc DTRRX");
}

static int a2_set_pc(struct us48_a2 *a, u32 pc)
{
	u32 en = HW_CS_DEBUG0_DSCR_ITREN_MASK | HW_CS_DEBUG0_DSCR_HDBGEN_MASK;
	int ret = regmap_set_bits(a->map, A2_DBG(DSCR), en);

	ret = ret ?: a2_set_reg(a, 0, pc);
	ret = ret ?: a2_itr(a, ARM_MOV_PC_R0, "mov pc,r0");
	regmap_clear_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_ITREN_MASK);
	return ret;
}

static int a2_release(struct us48_a2 *a)
{
	u32 drcr = HW_CS_DEBUG0_DRCR_REQ_REST_MASK | HW_CS_DEBUG0_DRCR_CLR_STK_EXC_MASK;
	int ret = regmap_write(a->map, A2_DBG(DRCR), drcr);

	return ret ?: a2_wait(a, HW_CS_DEBUG0_DSCR_RESTARTED_MASK | HW_CS_DEBUG0_DSCR_HALTED_MASK,
			      HW_CS_DEBUG0_DSCR_RESTARTED_MASK, "restart");
}

/*
 * Copy one firmware file to carveout + off: past the stub MiB, below a2_shared (resource table, trace).
 * optional: an empty name (sysfs stores a trailing newline) or a missing file is skipped, not an error.
 */
static int a2_blob(struct us48_a2 *a, const char *name, u32 off, const char *what, bool optional)
{
	const struct firmware *fw;
	char *n = kstrdup(name ?: "", GFP_KERNEL);
	int ret;

	if (!n)
		return -ENOMEM;
	strim(n);
	if (optional && !*n) {
		dev_info(a->dev, "payload: no %s configured, skipped\n", what);
		ret = 0;
		goto out;
	}
	ret = optional ? firmware_request_nowarn(&fw, n, a->dev) : request_firmware(&fw, n, a->dev);
	if (ret == -ENOENT && optional) {
		dev_info(a->dev, "payload: %s %s not present, skipped\n", what, n);
		ret = 0;
		goto out;
	}
	if (ret) {
		ret = dev_err_probe(a->dev, ret, "payload: %s %s\n", what, n);
		goto out;
	}
	if (off == U32_MAX) {
		dev_err(a->dev, "payload: no %s offset (DT ubnt,%s-offset or module parameter)\n", what, what);
		ret = -EINVAL;
	} else if (off < A2_STUB_KEEP || off > a->size - HW_A2_SHARED_SIZE || fw->size > a->size - HW_A2_SHARED_SIZE - off) {
		dev_err(a->dev, "payload: %s %s (%zu B) at +%#x does not fit the carveout\n", what, n, fw->size, off);
		ret = -EFBIG;
	} else {
		memcpy(a->va + off, fw->data, fw->size);
		dev_info(a->dev, "payload: %s %s at a2 %#x, %zu B\n", what, n, a->da + off, fw->size);
	}
	release_firmware(fw);
out:
	kfree(n);
	return ret;
}

static u32 a2_off(u32 param, u32 dt)
{
	return param != U32_MAX ? param : dt;
}

static bool a2_uboot(void)
{
	return mode && sysfs_streq(mode, "uboot");
}

/* uboot mode: the image is raw, so none of the ELF hooks apply; no resource table */
static int a2_sanity_check(struct rproc *rproc, const struct firmware *fw)
{
	if (!a2_uboot())
		return rproc_elf_sanity_check(rproc, fw);
	if (!fw->size || fw->size > HW_A2_STUB_SIZE) {
		dev_err(&rproc->dev, "uboot image %zu B: want 1..%u B (the first carveout MiB)\n", fw->size, HW_A2_STUB_SIZE);
		return -EFBIG;
	}
	return 0;
}

static int a2_parse_fw(struct rproc *rproc, const struct firmware *fw)
{
	return a2_uboot() ? 0 : rproc_elf_load_rsc_table(rproc, fw);
}

static struct resource_table *a2_find_loaded_rsc_table(struct rproc *rproc, const struct firmware *fw)
{
	return a2_uboot() ? NULL : rproc_elf_find_loaded_rsc_table(rproc, fw);
}

static u64 a2_get_boot_addr(struct rproc *rproc, const struct firmware *fw)
{
	struct us48_a2 *a = rproc->priv;

	return a2_uboot() ? a->da : rproc_elf_get_boot_addr(rproc, fw);
}

/* Standard ELF load (stub or self-test) or the raw awto-uboot; in payload mode also the kernel, DTB, initramfs and (ELF only) the stub's boot args. */
static int us48_a2_load(struct rproc *rproc, const struct firmware *fw)
{
	struct us48_a2 *a = rproc->priv;
	struct us48_a2_boot_args *args = a->va + US48_A2_BOOT_ARGS_OFF;
	u32 koff = a2_off(kernel_off, a->dt_off[0]), doff = a2_off(dtb_off, a->dt_off[1]);
	u32 ioff = a2_off(initrd_off, a->dt_off[2]);
	bool ub = a2_uboot();
	int ret = 0;

	if (ub) {
		memcpy(a->va, fw->data, fw->size);
		dev_info(a->dev, "uboot: %zu B at a2 %#x\n", fw->size, a->da);
	} else {
		ret = rproc_elf_load_segments(rproc, fw);
	}
	if (ret || !payload)
		return ret;
	if (cpu_mhz > 100)
		dev_warn(a->dev, "Linux payload at cpu_mhz %u: memory corruption seen above 100 MHz (#70, #79)\n", cpu_mhz);
	ret = a2_blob(a, kernel_fw, koff, "kernel", false) ?: a2_blob(a, dtb_fw, doff, "dtb", false) ?:
	      a2_blob(a, initrd_fw, ioff, "initrd", true);
	if (ret || ub)
		return ret;
	args->kernel = a->da + koff;
	args->dtb = a->da + doff;
	args->status = 0;
	args->flags = stub_dcc ? US48_A2_BOOT_F_DCC : 0;
	args->dcc_fail = 0;
	args->stage = 0;
	wmb();
	args->magic = US48_A2_BOOT_MAGIC;	/* last: the stub parks without it */
	return 0;
}

/* clk-iproc-armpll.c recalc: bypass, lock, offset mode, 0 = max divider; 0 returned when not a plain PLL rate */
static ulong a2_arm_hz(struct us48_a2 *a, u32 *fid_out)
{
	u32 dbg, pa, pb, pc, c5, off;
	u64 ndiv;
	u32 fid, pdiv, mdiv;

	if (regmap_read(a->map, CCU(POLICY_DBG), &dbg) || regmap_read(a->map, CCU(PLLARMA), &pa) ||
	    regmap_read(a->map, CCU(PLLARMB), &pb) || regmap_read(a->map, CCU(PLLARMC), &pc) ||
	    regmap_read(a->map, CCU(PLLARMCTL5), &c5) || regmap_read(a->map, CCU(PLLARM_OFFSET), &off))
		return 0;
	fid = (dbg & HW_CCU_POLICY_DBG_ACT_FREQ_MASK) >> HW_CCU_POLICY_DBG_ACT_FREQ_SHIFT;
	if (fid_out)
		*fid_out = fid;
	if (fid == 0)
		return 25000000;
	if (fid == 2)
		return 100000000;	/* sys clk row of the Broadcom table (armpll.c:26); unit 1 ROM default */
	if ((fid != 6 && fid != 7) || (pc & HW_CCU_PLLARMC_CH0_BYPASS_MASK) || !(pa & HW_CCU_PLLARMA_LOCK_MASK) ||
	    (off & HW_CCU_PLLARM_OFFSET_SW_CTL_MASK))
		return 0;
	mdiv = (fid == 6 ? pc : c5) & 0xff;
	ndiv = (pa & HW_CCU_PLLARMA_NDIV_INT_MASK) >> HW_CCU_PLLARMA_NDIV_INT_SHIFT;
	pdiv = (pa & HW_CCU_PLLARMA_PDIV_MASK) >> HW_CCU_PLLARMA_PDIV_SHIFT;
	ndiv = ((ndiv ?: 1024) << 20) | (pb & HW_CCU_PLLARMB_NDIV_FRAC_MASK);
	return div_u64(div_u64(div_u64(25000000ULL * ndiv, 1 << 20), pdiv ?: 8), mdiv ?: 256);
}

static int a2_ccu_go(struct us48_a2 *a, u32 policy)
{
	u32 v;
	int ret = regmap_write(a->map, CCU(POLICY_FREQ), policy) ?: regmap_write(a->map, CCU(POLICY_CTL), 3);

	if (ret)
		return ret;
	ret = regmap_read_poll_timeout(a->map, CCU(POLICY_CTL), v, !(v & HW_CCU_POLICY_CTL_GO_MASK), 0, CCU_WAIT_US);
	if (ret)
		dev_err(a->dev, "ccu GO (policy %#x): POLICY_CTL %#x, limit %d us, ret %d\n", policy, v, CCU_WAIT_US, ret);
	return ret;
}

/* E2 relock (sys clk while the PLL relocks, then fid 7). Failure: PLL and policy restored to what was read. */
static int a2_set_cpu(struct us48_a2 *a, uint mhz)
{
	u32 fid = 0, old_pa, old_pol, pb, v;
	ulong hz = a2_arm_hz(a, &fid);
	ktime_t t0 = ktime_get();
	int ret;

	if (!mhz)
		return 0;
	if (mhz == 100 && fid == 2)
		return 0;
	if (mhz != 100 && hz / 1000000 == mhz && fid == (CCU_FID6(mhz) ? 6 : 7))
		return 0;
	if (mhz != 100 && !(mhz >= 400 && mhz < 2000 && CCU_VCO_MHZ(mhz) % 25 == 0 && CCU_VCO_MHZ(mhz) >= 1600 && CCU_VCO_MHZ(mhz) <= 4000))
		return dev_err_probe(a->dev, -EINVAL, "cpu_mhz %u: 100, 400..600 (VCO 4x) or 700..1999 (VCO 2x), VCO 1600..4000 and a multiple of 25\n", mhz);
	ret = regmap_read(a->map, CCU(PLLARMCTL5), &v);
	if (ret)
		return ret;
	if ((v & 0xff) != 2 || (fid != 2 && fid != 6 && fid != 7))
		return dev_err_probe(a->dev, -EINVAL, "ccu gate: ch1 mdiv %u (want 2), active fid %u (want 2/6/7)\n", v & 0xff, fid);
	ret = regmap_read(a->map, CCU(PLLARMA), &old_pa);
	ret = ret ?: regmap_read(a->map, CCU(POLICY_FREQ), &old_pol);
	ret = ret ?: regmap_read(a->map, CCU(PLLARMB), &pb);
	ret = ret ?: regmap_write(a->map, CCU(WR_ACCESS), CCU_UNLOCK);
	if (ret)
		return ret;
	ret = a2_ccu_go(a, 0x82020202);
	if (ret)
		goto restore;
	if (mhz == 100)
		goto out;	/* sys clk policy already selected above; PLL left as is */
	ret = regmap_write(a->map, CCU(PLLARMB), (pb & ~HW_CCU_PLLARMB_NDIV_FRAC_MASK) | 1);
	ret = ret ?: regmap_write(a->map, CCU(PLLARMA), CCU_PLLARMA_VAL(mhz, 0));
	ret = ret ?: regmap_read_poll_timeout(a->map, CCU(PLLARMA), v, (v & CCU_LOCKED) == CCU_LOCKED, 0, CCU_WAIT_US);
	if (ret) {
		dev_err(a->dev, "ccu PLL lock: PLLARMA %#x, limit %d us, ret %d\n", v, CCU_WAIT_US, ret);
		goto restore;
	}
	ret = regmap_write(a->map, CCU(PLLARMA), CCU_PLLARMA_VAL(mhz, 1)) ?: a2_ccu_go(a, CCU_POLICY(mhz));
	if (!ret)
		goto out;
restore:
	regmap_write(a->map, CCU(PLLARMA), old_pa);
	a2_ccu_go(a, old_pol);
out:
	regmap_write(a->map, CCU(WR_ACCESS), 0);
	hz = a2_arm_hz(a, &fid);
	dev_info(a->dev, "a2 ARM %lu MHz (fid %u), requested %u, ret %d, %lld us\n", hz / 1000000, fid, mhz, ret,
		 ktime_to_us(ktime_sub(ktime_get(), t0)));
	return ret;
}

static int a2_set_axi(struct us48_a2 *a, uint mhz)
{
	static const u16 mdiv_of[][2] = { { 100, 20 }, { 200, 10 }, { 250, 8 }, { 285, 7 }, { 400, 5 } };
	u32 c0, c1, c2, c3, st;
	int i, ret;

	if (!mhz)
		return 0;
	for (i = 0; i < ARRAY_SIZE(mdiv_of) && mdiv_of[i][0] != mhz; i++)
		;
	if (i == ARRAY_SIZE(mdiv_of))
		return dev_err_probe(a->dev, -EINVAL, "axi_mhz %u: want 100, 200, 250, 285 or 400\n", mhz);
	ret = regmap_read(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL1, &c1);
	ret = ret ?: regmap_read(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL2, &c2);
	if (ret)
		return ret;
	if ((c1 & HW_GENPLL_CTRL1_NDIV_MASK) != 80)
		return dev_err_probe(a->dev, -EINVAL, "genpll gate: NDIV %u (want 80)\n", c1 & HW_GENPLL_CTRL1_NDIV_MASK);
	if (((c2 & HW_GENPLL_CTRL2_CH3_MDIV_MASK) >> HW_GENPLL_CTRL2_CH3_MDIV_SHIFT) == mdiv_of[i][1])
		return 0;
	/* genpll.c: clear ch3/ch4 load-enables, FAST_LOCK, MDIV, SW_TO_GEN_PLL_LOAD, load-enable 0x18, poll lock */
	ret = regmap_read(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL3, &c3);
	ret = ret ?: regmap_write(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL3, c3 & ~HW_GENPLL_CTRL3_LOAD_EN_MASK);
	ret = ret ?: regmap_read(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL0, &c0);
	ret = ret ?: regmap_write(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL0, c0 | HW_GENPLL_CTRL0_FAST_LOCK_MASK);
	c2 = (c2 & ~HW_GENPLL_CTRL2_CH3_MDIV_MASK) | mdiv_of[i][1] << HW_GENPLL_CTRL2_CH3_MDIV_SHIFT;
	ret = ret ?: regmap_write(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL2, c2);
	c3 = (c3 & ~HW_GENPLL_CTRL3_LOAD_EN_MASK) | HW_GENPLL_CTRL3_SW_TO_GEN_PLL_LOAD_MASK;
	ret = ret ?: regmap_write(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL3, c3);
	c3 |= 0x18 << HW_GENPLL_CTRL3_LOAD_EN_SHIFT;
	ret = ret ?: regmap_write(a->map, HW_GENPLL_BASE + HW_GENPLL_CTRL3, c3);
	ret = ret ?: regmap_read_poll_timeout(a->map, HW_GENPLL_BASE + HW_GENPLL_STATUS, st,
					      st & HW_GENPLL_STATUS_LOCK_MASK, 0, GENPLL_WAIT_US);
	if (ret)
		dev_err(a->dev, "genpll lock: STATUS %#x, limit %d us, ret %d\n", st, GENPLL_WAIT_US, ret);
	else
		dev_info(a->dev, "unit 1 AXI %u MHz (ch3 mdiv %u), APB %u MHz\n", mhz, mdiv_of[i][1], mhz / 4);
	return ret;
}

static int a2_ccu_show(struct seq_file *m, void *unused)
{
	static const struct { const char *n; u32 r; } regs[] = {
		{ "ARM_DIV", HW_CCU_ARM_DIV }, { "POLICY_FREQ", HW_CCU_POLICY_FREQ }, { "POLICY_CTL", HW_CCU_POLICY_CTL },
		{ "POLICY_DBG", HW_CCU_POLICY_DBG }, { "PLLARMA", HW_CCU_PLLARMA }, { "PLLARMB", HW_CCU_PLLARMB },
		{ "PLLARMC", HW_CCU_PLLARMC }, { "PLLARMCTL5", HW_CCU_PLLARMCTL5 }, { "PLLARM_OFFSET", HW_CCU_PLLARM_OFFSET },
		{ "PLL_DEBUG", HW_CCU_PLL_DEBUG },
	};
	struct us48_a2 *a = m->private;
	u32 v, fid = 0;
	ulong hz;
	int i, ret;

	for (i = 0; i < ARRAY_SIZE(regs); i++) {
		ret = regmap_read(a->map, HW_CCU_BASE + regs[i].r, &v);
		if (ret)
			return ret;
		seq_printf(m, "%-14s %#010x\n", regs[i].n, v);
	}
	hz = a2_arm_hz(a, &fid);
	seq_printf(m, "fid %u ARM %lu Hz (%lu MHz) PERIPHCLK %lu Hz (conf I: ARM/2)\n", fid, hz, hz / 1000000, hz / 2);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(a2_ccu);

/* Before the firmware load: window to the carveout, a2 held in ROM so it cannot run stale code over the load. */
static int us48_a2_prepare(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;
	struct rproc_mem_entry *mem;
	u32 axi;
	int ret;

	ret = us48_pci_outbound_map(a->p, a->phys, a->size, &axi);
	if (ret)
		return ret;
	if (axi != a->da) {
		ret = -EINVAL;
		goto unmap;
	}
	ret = a2_reset(a);
	if (ret)
		goto unmap;
	mem = rproc_mem_entry_init(a->dev, a->va, a->phys, a->size, a->da, NULL, NULL, A2_CARVEOUT);
	if (!mem) {
		ret = -ENOMEM;
		goto unmap;
	}
	rproc_add_carveout(rproc, mem);
	return 0;
unmap:
	us48_pci_outbound_unmap(a->p);
	return ret;
}

static int us48_a2_unprepare(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;

	us48_pci_outbound_unmap(a->p);
	return 0;
}

/* ROM loop -> halt -> PC = entry -> release (sequence of docs/a2.md). */
static int us48_a2_start(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;
	int ret = a2_reset(a);

	ret = ret ?: a2_set_axi(a, axi_mhz);
	ret = ret ?: a2_set_cpu(a, cpu_mhz);
	ret = ret ?: a2_halt(a);
	ret = ret ?: a2_set_pc(a, rproc->bootaddr);
	if (!ret)
		us48_a2_dcc_start(a->dcc);	/* before the release: a2's hvc_dcc gives up after 100 ms */
	ret = ret ?: a2_release(a);
	if (!ret && hdbgen_off)
		ret = regmap_clear_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_HDBGEN_MASK);
	if (ret) {
		us48_a2_dcc_stop(a->dcc);
		a2_reset(a);	/* never leave a2 halted */
	}
	return ret;
}

/* Halt is best effort (a stalled a2 never halts); the reset is what stops it. */
static int us48_a2_stop(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;

	us48_a2_dcc_stop(a->dcc);	/* before the halt: DSCR is shared with the debug sequences */
	if (a2_halt(a))
		dev_warn(a->dev, "a2 did not halt, resetting\n");
	return a2_reset(a);
}

/*
 * debugfs pcsr: DSCR + 8 DBGPCSR samples (PCSR = instruction address + 8 in ARM state; 0xffffffff = not
 * sampled). Read-only on the CoreSight window: safe next to the DCC poller. Map to System.map / the boot args.
 */
#define PCSR_SAMPLES	8
static int a2_pcsr_show(struct seq_file *m, void *unused)
{
	struct us48_a2 *a = m->private;
	u32 v, i;
	int ret = regmap_read(a->map, A2_DBG(DSCR), &v);

	if (ret)
		return ret;
	seq_printf(m, "dscr %#x\n", v);
	for (i = 0; i < PCSR_SAMPLES; i++) {
		ret = regmap_read(a->map, A2_DBG(ITR_PCSR), &v);
		if (ret)
			return ret;
		seq_printf(m, "pcsr %#010x\n", v);
	}
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(a2_pcsr);

static const struct rproc_ops us48_a2_ops = {
	.prepare		= us48_a2_prepare,
	.unprepare		= us48_a2_unprepare,
	.start			= us48_a2_start,
	.stop			= us48_a2_stop,
	.load			= us48_a2_load,
	.parse_fw		= a2_parse_fw,
	.find_loaded_rsc_table	= a2_find_loaded_rsc_table,
	.get_boot_addr		= a2_get_boot_addr,
	.sanity_check		= a2_sanity_check,
};

static int us48_a2_probe(struct auxiliary_device *adev, const struct auxiliary_device_id *id)
{
	struct device *dev = &adev->dev;
	struct device_node *np;
	struct resource res;
	u32 dt_off[3];
	struct us48_a2 *a;
	struct rproc *rproc;
	int ret;

	np = of_find_compatible_node(NULL, NULL, "ubnt,us48-a2-carveout");
	if (!np)
		return dev_err_probe(dev, -ENODEV, "no reserved-memory node ubnt,us48-a2-carveout\n");
	ret = of_address_to_resource(np, 0, &res);
	dt_off[0] = dt_off[1] = dt_off[2] = U32_MAX;
	of_property_read_u32(np, "ubnt,kernel-offset", &dt_off[0]);
	of_property_read_u32(np, "ubnt,dtb-offset", &dt_off[1]);
	of_property_read_u32(np, "ubnt,initrd-offset", &dt_off[2]);
	of_node_put(np);
	if (ret)
		return dev_err_probe(dev, ret, "carveout reg\n");

	rproc = devm_rproc_alloc(dev, "us48-a2", &us48_a2_ops, A2_FW, sizeof(*a));
	if (!rproc)
		return -ENOMEM;
	rproc->auto_boot = false;
	a = rproc->priv;
	a->dev = dev;
	a->p = us48_pci_from_adev(adev);
	a->map = us48_pci_regmap(a->p);
	memcpy(a->dt_off, dt_off, sizeof(dt_off));
	a->phys = res.start;
	a->size = resource_size(&res);
	/* one 64 MiB outbound granule: the carveout must not straddle it (checked again by the map call) */
	if ((a->phys & (SZ_64M - 1)) + a->size > SZ_64M)
		return dev_err_probe(dev, -EINVAL, "carveout %pa+%zx crosses a 64 MiB boundary\n", &a->phys, a->size);
	a->da = HW_A2_HOST_WIN_BASE + (a->phys & (SZ_64M - 1));
	a->va = devm_memremap(dev, a->phys, a->size, MEMREMAP_WC);
	if (IS_ERR(a->va))
		return PTR_ERR(a->va);
	dev_info(dev, "carveout %pa+%zx, a2 AXI %#x\n", &a->phys, a->size, a->da);
	/* created before rproc_add: devres releases it after rproc_del, which may still call stop() */
	a->dcc = us48_a2_dcc_create(dev, a->map);
	if (IS_ERR(a->dcc))
		return PTR_ERR(a->dcc);
	ret = devm_rproc_add(dev, rproc);
	if (!ret) {
		us48_a2_dcc_debugfs(a->dcc, rproc->dbg_dir);
		if (rproc->dbg_dir)
			debugfs_create_file("pcsr", 0444, rproc->dbg_dir, a, &a2_pcsr_fops);
		if (rproc->dbg_dir)
			debugfs_create_file("ccu", 0444, rproc->dbg_dir, a, &a2_ccu_fops);
	}
	return ret;
}

static const struct auxiliary_device_id us48_a2_ids[] = {
	{ .name = US48_PCI_AUX_ID(US48_PCI_AUX_A2) },
	{ }
};
MODULE_DEVICE_TABLE(auxiliary, us48_a2_ids);

static struct auxiliary_driver us48_a2_driver = {
	.name		= "a2",
	.id_table	= us48_a2_ids,
	.probe		= us48_a2_probe,
};
module_auxiliary_driver(us48_a2_driver);

MODULE_FIRMWARE(A2_FW);
MODULE_DESCRIPTION("UniFi US-48-500W unit 1 Cortex-A9 (a2) remoteproc driver");
MODULE_LICENSE("GPL");
