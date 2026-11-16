// SPDX-License-Identifier: GPL-2.0
/*
 * us48_a2: remoteproc driver for the unit 1 Cortex-A9 (a2), aux device us48_pci.a2.
 * Firmware us48-a2.elf runs from a reusable CMA pool in host DDR, allocated whole while a2 is on (docs/a2-design.md).
 */
#include <linux/auxiliary_bus.h>
#include <linux/debugfs.h>
#include <linux/delay.h>
#include <linux/dma-mapping.h>
#include <linux/firmware.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/kthread.h>
#include <linux/ktime.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/of_reserved_mem.h>
#include <linux/reboot.h>
#include <linux/regmap.h>
#include <linux/remoteproc.h>
#include <linux/seq_file.h>
#include <linux/sizes.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/uaccess.h>
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
 * Debug handshakes: a few A9 cycles + a few PCIe round trips (~us each). 1 ms backstop, not measured on the bus
 * (a2_halt has its own measured limit). Expiry: log op + DSCR + limit, -ETIMEDOUT. A stalled a2 never halts: stop resets it.
 */
#define DBG_WAIT_US	1000
/* a2_halt wait: 1.25 x the worst of 220 measured halts (2.9 ms), typical 15-20 us; see #102 */
#define HALT_WAIT_US	3700

/*
 * a2 ARM clock: same sequence as awto-uboot us48_cpu_switch (uboot-port/board/ubiquiti/us48/board.c, docs/clock-audit.md 2),
 * written through the unit 1 regmap before the release. 0 = leave the CCU as the ROM left it.
 * GO and PLL lock: ~10 us / tens of us expected (I, unmeasured on unit 1); 1 ms limit, expiry = log + restore.
 */
/* Default 100 (sys clk, ROM state): a2 corrupts memory above 100 MHz, Dan 2026-11-15, #79 */
static uint cpu_mhz = 1600;	/* Dan 2026-11-16 (#88); highest clean step is tracked in #103/#88 */
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
static bool dcc_poll = true;
module_param(dcc_poll, bool, 0644);
MODULE_PARM_DESC(dcc_poll, "a1 DCC reader polls a2's DSCR/DTRTX while a2 runs (0 = never touch a2 debug regs after the release, #79)");
/* #79: a2's line fill passes its own eviction write on the way to host DDR: stale clean lines above ~700 MHz */
static uint dcc_late_us;
module_param(dcc_late_us, uint, 0644);
MODULE_PARM_DESC(dcc_late_us, "start the DCC poll this many us after the release instead of before it (0 = before; #103 test, a2 hvc_dcc gives up after 100 ms)");

/* #103 capture only: kernel symbol VAs of the running a2 image (System.map `l2x0_base`, `mem_map`); 0 = that analysis skipped */
static uint cap_l2x0;
module_param(cap_l2x0, uint, 0644);
MODULE_PARM_DESC(cap_l2x0, "capture: VA of the kernel variable l2x0_base in the a2 image (System.map), enables the PL310 clean/invalidate analysis");
static uint cap_memmap;
module_param(cap_memmap, uint, 0644);
MODULE_PARM_DESC(cap_memmap, "capture: VA of the kernel variable mem_map in the a2 image (System.map), dumps the struct page of the faulting node");

static bool paxb_order = true;
module_param(paxb_order, bool, 0644);
MODULE_PARM_DESC(paxb_order, "unit 1 PAXB TX_DEBUG_CFG.EP_TX_WR_RD_ORDER: a2 reads wait for its earlier writes (reset 0 = a read may pass a write)");
/*
 * #103: a2 line fill returned data older than the a2's own earlier write to the same line (first line of a new maple node read as zeros,
 * DDR held the data), 11/250 boots at 1000 MHz with only EP_TX_WR_RD_ORDER + rx_arid=0. Depth-1 command buffers on unit 1 PAXB TX
 * (named fields, PAXB_0_PAXB_TX_DEBUG_CFG bits 0/1, regdb/hr2_regs.tsv:5690): 0/250 each. Counts: docs/a2-linux.md "#103".
 */
static bool paxb_wr_depth1 = true;
module_param(paxb_wr_depth1, bool, 0644);
MODULE_PARM_DESC(paxb_wr_depth1, "unit 1 TX_DEBUG_CFG.EP_TX_WR_CMD_BUF_DEPTH_1 at the release (#103)");
static bool paxb_rd_depth1;
module_param(paxb_rd_depth1, bool, 0644);
MODULE_PARM_DESC(paxb_rd_depth1, "unit 1 TX_DEBUG_CFG.EP_TX_RD_CMD_BUF_DEPTH_1 at the release (#103; also 0/250, but a2 boot 1.9 s instead of 1.45 s)");
static int rx_arid;
module_param(rx_arid, int, 0644);
MODULE_PARM_DESC(rx_arid, "unit 0 PAXB RX_FREE_ARID_COUNT while a2 runs (default 0 = one inbound read at a time, #79; 1..3 fault under a1 DDR load; -1 = leave the reset value 3)");
static bool l2_aux = true;
MODULE_PARM_DESC(l2_aux, "boot stub sets the unit 1 PL310 way size (stock uboot-1 aux) with L2 off before the kernel (default 1; a1 cannot reach the PL310)");
module_param(l2_aux, bool, 0644);
static bool stub_dcc = true;
module_param(stub_dcc, bool, 0644);
MODULE_PARM_DESC(stub_dcc, "boot stub writes DCC marker characters (proves the console path before the kernel runs)");
module_param(kernel_fw, charp, 0644);
module_param(dtb_fw, charp, 0644);
module_param(initrd_fw, charp, 0644);
/* Offsets in the carveout: DT properties of the ubnt,us48-a2-rproc node, a module parameter overrides. */
static uint kernel_off = U32_MAX, dtb_off = U32_MAX, initrd_off = U32_MAX;
module_param(kernel_off, uint, 0644);
module_param(dtb_off, uint, 0644);
module_param(initrd_off, uint, 0644);

enum a2_cap_state { CAP_IDLE, CAP_ARMED, CAP_DONE, CAP_FAILED };

struct us48_a2 {
	struct device *dev;
	struct rproc *rproc;
	struct mutex cap_lock;		/* cap_task and arming; cap_txt is valid only in CAP_DONE/CAP_FAILED */
	struct task_struct *cap_task;
	enum a2_cap_state cap_state;
	bool cap_vc;
	bool cap_skip;			/* vc: resume past catches that look like normal page faults */
	u32 cap_extra[24];		/* inspect: extra VAs (from the oops registers) to compare */
	int cap_nextra;
	bool cap_null;			/* vc: resume everything except a kernel-mode data abort at DFAR < 0x1000 with the PC in the kernel image (#103) */
	u32 cap_nskip, cap_skip_user, cap_last[4];
	bool cap_pre;			/* armed while a2 is offline: DBGVCR set in start() before the release, thread spawned after */
	uint cap_ms;
	char cap_txt[65536];
	size_t cap_len;
	struct us48_pci *p;
	struct regmap *map;
	struct us48_a2_dcc *dcc;	/* console reader: runs from before the release until stop */
	struct notifier_block reboot_nb;
	phys_addr_t phys;
	size_t size;
	void *va;	/* WC buffer of the carveout, valid between prepare and unprepare */
	dma_addr_t dma;
	u32 da;		/* a2 AXI address of the carveout start */
	u32 dt_off[3];	/* kernel, dtb, initrd offsets from the DT, U32_MAX if absent */
	u32 halt_n, halt_fail, halt_last_us, halt_max_us;	/* a2_halt wait statistics (debugfs halt_us) */
};

/* Pool mapping for the bus test (us48_a2_region_get): region_lock is held between get and put and by prepare/unprepare. */
static DEFINE_MUTEX(region_lock);
static struct us48_a2 *region_owner;

void *us48_a2_region_get(size_t *size)
{
	struct us48_a2 *a;

	mutex_lock(&region_lock);
	a = region_owner;
	if (!a || !a->va) {
		mutex_unlock(&region_lock);
		return ERR_PTR(-ENODEV);
	}
	*size = a->size;
	return a->va;
}
EXPORT_SYMBOL_GPL(us48_a2_region_get);

void us48_a2_region_put(void)
{
	mutex_unlock(&region_lock);
}
EXPORT_SYMBOL_GPL(us48_a2_region_put);

/* For rproc subdevices (us48_vnet): the rproc, and the pool mapping, which is valid only between a subdev's prepare and unprepare. */
struct rproc *us48_a2_rproc(void)
{
	return region_owner ? region_owner->rproc : NULL;
}
EXPORT_SYMBOL_GPL(us48_a2_rproc);

void *us48_a2_pool_va(size_t *size)
{
	struct us48_a2 *a = region_owner;

	if (!a || !a->va)
		return NULL;
	*size = a->size;
	return a->va;
}
EXPORT_SYMBOL_GPL(us48_a2_pool_va);

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
	ktime_t t0;
	u32 d;
	int ret = regmap_write(a->map, A2_DBG(LAR), LAR_KEY);

	ret = ret ?: regmap_set_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_HDBGEN_MASK);
	ret = ret ?: regmap_write(a->map, A2_DBG(DRCR), HW_CS_DEBUG0_DRCR_REQ_HALT_MASK);
	if (ret)
		return ret;
	t0 = ktime_get();
	ret = regmap_read_poll_timeout(a->map, A2_DBG(DSCR), d, d & HW_CS_DEBUG0_DSCR_HALTED_MASK, 0, HALT_WAIT_US);
	a->halt_last_us = ktime_to_us(ktime_sub(ktime_get(), t0));
	a->halt_n++;
	if (!ret && a->halt_last_us > a->halt_max_us)
		a->halt_max_us = a->halt_last_us;
	if (ret) {
		a->halt_fail++;
		dev_err(a->dev, "a2 halt: DSCR %#x, limit %d us, elapsed %u us, ret %d\n", d, HALT_WAIT_US, a->halt_last_us, ret);
	}
	return ret;
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
	} else if (off < A2_STUB_KEEP || off > a->size - HW_A2_VNET_SIZE - HW_A2_SHARED_SIZE || fw->size > a->size - HW_A2_VNET_SIZE - HW_A2_SHARED_SIZE - off) {
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
	if (cpu_mhz > 1600)
		dev_warn(a->dev, "Linux payload at cpu_mhz %u: above the 1600 MHz run value, 1650 MHz hangs (#79, #88)\n", cpu_mhz);
	ret = a2_blob(a, kernel_fw, koff, "kernel", false) ?: a2_blob(a, dtb_fw, doff, "dtb", false) ?:
	      a2_blob(a, initrd_fw, ioff, "initrd", true);
	if (ret || ub)
		return ret;
	args->kernel = a->da + koff;
	args->dtb = a->da + doff;
	args->status = 0;
	args->flags = (stub_dcc ? US48_A2_BOOT_F_DCC : 0) | (l2_aux ? US48_A2_BOOT_F_L2AUX : 0);
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

/* debugfs paxb_tx: unit 1 PAXB_TX_DEBUG_CFG (AXI -> PCIe ordering/depth), read / write hex (#79 board experiments) */
static ssize_t a2_paxb_tx_read(struct file *f, char __user *u, size_t n, loff_t *pos)
{
	struct us48_a2 *a = f->private_data;
	char buf[24];
	u32 v;
	int ret = regmap_read(a->map, HW_PAXB0_BASE + HW_PAXB0_TX_DEBUG_CFG, &v);

	return ret ?: simple_read_from_buffer(u, n, pos, buf, scnprintf(buf, sizeof(buf), "%#x\n", v));
}

static ssize_t a2_paxb_tx_write(struct file *f, const char __user *u, size_t n, loff_t *pos)
{
	struct us48_a2 *a = f->private_data;
	u32 v;
	int ret = kstrtou32_from_user(u, n, 0, &v);

	ret = ret ?: v & ~0xfu ? -EINVAL : 0;	/* only the four defined bits */
	ret = ret ?: regmap_write(a->map, HW_PAXB0_BASE + HW_PAXB0_TX_DEBUG_CFG, v);
	return ret ?: n;
}

static const struct file_operations a2_paxb_tx_fops = {
	.open = simple_open,
	.read = a2_paxb_tx_read,
	.write = a2_paxb_tx_write,
};

/* Before the firmware load: window to the carveout, a2 held in ROM so it cannot run stale code over the load. */
static int us48_a2_prepare(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;
	struct rproc_mem_entry *mem;
	u32 axi;
	int ret;

	/* The whole pool: cma_alloc fails (logged, no fallback) while movable pages in it are pinned. */
	mutex_lock(&region_lock);
	a->va = dma_alloc_wc(a->dev, a->size, &a->dma, GFP_KERNEL);
	mutex_unlock(&region_lock);
	if (!a->va)
		return dev_err_probe(a->dev, -ENOMEM, "dma_alloc_wc %zu B from the a2 pool failed (pinned pages?)\n", a->size);
	if (a->dma != a->phys) {
		dev_err(a->dev, "pool allocation at %pad, want %pa\n", &a->dma, &a->phys);
		ret = -EINVAL;
		goto free;
	}
	ret = us48_pci_outbound_map(a->p, a->phys, a->size, &axi);
	if (ret)
		goto free;
	if (axi != a->da) {
		ret = -EINVAL;
		goto unmap;
	}
	ret = a2_reset(a);
	if (ret)
		goto unmap;
	mem = rproc_mem_entry_init(a->dev, a->va, a->dma, a->size, a->da, NULL, NULL, A2_CARVEOUT);
	if (!mem) {
		ret = -ENOMEM;
		goto unmap;
	}
	rproc_add_carveout(rproc, mem);
	return 0;
unmap:
	us48_pci_outbound_unmap(a->p);
free:
	mutex_lock(&region_lock);
	dma_free_wc(a->dev, a->size, a->va, a->dma);
	a->va = NULL;
	mutex_unlock(&region_lock);
	return ret;
}

static int us48_a2_unprepare(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;

	us48_pci_outbound_unmap(a->p);
	mutex_lock(&region_lock);
	dma_free_wc(a->dev, a->size, a->va, a->dma);
	a->va = NULL;
	mutex_unlock(&region_lock);
	return 0;
}

/* unit 0 is a1's own AXI: direct mapping (unit 1 goes through the regmap) */
static int a2_rx_arid(int cnt)
{
	void __iomem *va;
	u32 v;

	if (cnt < 0)
		return 0;
	if (cnt > 3)
		return -EINVAL;
	va = ioremap(HW_PAXB0_BASE + HW_PAXB0_RX_DEBUG_CONTROL_0, 4);
	if (!va)
		return -ENOMEM;
	v = readl(va);
	writel((v & ~HW_PAXB0_RX_DEBUG_CONTROL_0_RX_FREE_ARID_COUNT_MASK) | cnt, va);
	iounmap(va);
	return 0;
}

/* Read-back of the #79 bits, no writes: unit 1 TX_DEBUG_CFG.EP_TX_WR_RD_ORDER, unit 0 RX_DEBUG_CONTROL_0.RX_FREE_ARID_COUNT (#103). */
static int a2_paxb_state(struct us48_a2 *a, u32 *tx, u32 *rx)
{
	void __iomem *va;
	int ret = regmap_read(a->map, HW_PAXB0_BASE + HW_PAXB0_TX_DEBUG_CFG, tx);

	if (ret)
		return ret;
	va = ioremap(HW_PAXB0_BASE + HW_PAXB0_RX_DEBUG_CONTROL_0, 4);
	if (!va)
		return -ENOMEM;
	*rx = readl(va);
	iounmap(va);
	return 0;
}

static void a2_paxb_log(struct us48_a2 *a, const char *when)
{
	u32 tx = 0, rx = 0;
	int ret = a2_paxb_state(a, &tx, &rx);

	dev_info(a->dev, "paxb %s: ret %d wr_rd_order %u rx_arid %u (tx_cfg %#x rx_ctl0 %#x)\n", when, ret,
		 !!(tx & HW_PAXB0_TX_DEBUG_CFG_EP_TX_WR_RD_ORDER_MASK), rx & HW_PAXB0_RX_DEBUG_CONTROL_0_RX_FREE_ARID_COUNT_MASK, tx, rx);
}

/* sysfs paxb_state: live read-back (normal image has no debugfs) */
static int a2_paxb_state_get(char *buf, const struct kernel_param *kp)
{
	u32 tx = 0, rx = 0;
	int ret;

	if (!region_owner)
		return -ENODEV;
	ret = a2_paxb_state(region_owner, &tx, &rx);
	if (ret)
		return ret;
	return sysfs_emit(buf, "wr_rd_order=%u rx_arid=%u\n", !!(tx & HW_PAXB0_TX_DEBUG_CFG_EP_TX_WR_RD_ORDER_MASK),
			  rx & HW_PAXB0_RX_DEBUG_CONTROL_0_RX_FREE_ARID_COUNT_MASK);
}

static const struct kernel_param_ops a2_paxb_state_ops = { .get = a2_paxb_state_get };
module_param_cb(paxb_state, &a2_paxb_state_ops, NULL, 0444);

/* Before the release: AXI -> PCIe write-before-read order of unit 1, inbound read concurrency of unit 0 (restored in stop). */
static int a2_paxb_cfg(struct us48_a2 *a)
{
	int ret = regmap_update_bits(a->map, HW_PAXB0_BASE + HW_PAXB0_TX_DEBUG_CFG, HW_PAXB0_TX_DEBUG_CFG_EP_TX_WR_RD_ORDER_MASK | HW_PAXB0_TX_DEBUG_CFG_EP_TX_WR_CMD_BUF_DEPTH_1_MASK |
				     HW_PAXB0_TX_DEBUG_CFG_EP_TX_RD_CMD_BUF_DEPTH_1_MASK,
				     (paxb_order ? HW_PAXB0_TX_DEBUG_CFG_EP_TX_WR_RD_ORDER_MASK : 0) |
				     (paxb_wr_depth1 ? HW_PAXB0_TX_DEBUG_CFG_EP_TX_WR_CMD_BUF_DEPTH_1_MASK : 0) |
				     (paxb_rd_depth1 ? HW_PAXB0_TX_DEBUG_CFG_EP_TX_RD_CMD_BUF_DEPTH_1_MASK : 0));

	return ret ?: a2_rx_arid(rx_arid);
}

/* ROM loop -> halt -> PC = entry -> release (sequence of docs/a2.md). */
static int a2_cap_pre_vcr(struct us48_a2 *a);
static int a2_cap_pre_spawn(struct us48_a2 *a);

static int us48_a2_start(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;
	int ret = a2_reset(a);

	ret = ret ?: a2_set_axi(a, axi_mhz);
	ret = ret ?: a2_set_cpu(a, cpu_mhz);
	ret = ret ?: a2_paxb_cfg(a);
	ret = ret ?: a2_halt(a);
	ret = ret ?: a2_set_pc(a, rproc->bootaddr);
	ret = ret ?: a2_cap_pre_vcr(a);
	if (!ret && dcc_poll && !dcc_late_us)
		us48_a2_dcc_start(a->dcc);	/* before the release: a2's hvc_dcc gives up after 100 ms */
	ret = ret ?: a2_release(a);
	if (!ret && dcc_poll && dcc_late_us) {
		fsleep(dcc_late_us);
		us48_a2_dcc_start(a->dcc);
	}
	if (!ret)
		a2_paxb_log(a, "at release");
	if (!ret && hdbgen_off && !a->cap_pre)
		ret = regmap_clear_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_HDBGEN_MASK);
	if (!ret)
		ret = a2_cap_pre_spawn(a);
	if (ret) {
		us48_a2_dcc_stop(a->dcc);
		a2_reset(a);	/* never leave a2 halted */
		if (rx_arid >= 0)
			a2_rx_arid(3);
	}
	return ret;
}

static void a2_cap_disarm(struct us48_a2 *a);

/* Halt is best effort (a stalled a2 never halts); the reset is what stops it. */
static int us48_a2_stop(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;

	int ret;

	a2_cap_disarm(a);
	us48_a2_dcc_stop(a->dcc);	/* before the halt: DSCR is shared with the debug sequences */
	if (a2_halt(a))
		dev_warn(a->dev, "a2 did not halt, resetting\n");
	ret = a2_reset(a);
	if (rx_arid >= 0)
		a2_rx_arid(3);	/* reset value, after a2 is stopped: changing it with a2 reads in flight (#102) */
	return ret;
}

/*
 * Reboot: a running a2 (DCC poller, reads in flight) must be stopped before pcie-iproc shutdown asserts PERST#;
 * a read in flight at PERST# never completes and stalls a1 until the SP805 bites, after which uboot-1 hangs (#90, #91).
 */
static int us48_a2_reboot(struct notifier_block *nb, unsigned long action, void *data)
{
	struct us48_a2 *a = container_of(nb, struct us48_a2, reboot_nb);

	if (a->rproc->state == RPROC_RUNNING) {
		dev_info(a->dev, "reboot: stopping a2 before PERST#\n");
		rproc_shutdown(a->rproc);
	}
	return NOTIFY_DONE;
}

static void a2_reboot_exit(void *data)
{
	unregister_reboot_notifier(data);
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

/*
 * debugfs capture: halt a2 at the first fetch from the vector page and dump its state over CoreSight,
 * no code on a2 (#79). Needs dcc_poll=0. Ends with a2 reset: the corrupted a2 is not resumed.
 * Regs: DDI0388I Table 10-1 (p10-5) offsets, DBGDSCR p8-9.
 */
#define A2_VEC_PCSR_LO	0xffff0000u	/* PCSR = address + 8 in ARM state: vectors 0x00-0x1c sample as 0x08-0x24 */
#define A2_VEC_PCSR_LEN	0x30u
#define A2_DBGVCR	0x01c		/* DDI0388I Table 10-1: DBGVCR; bits 1 undef, 3 prefetch abort, 4 data abort (DDI0406C C11) */
#define A2_VCR_S	(BIT(1) | BIT(3) | BIT(4))	/* secure bits [7:0]; non-secure copy at [31:24] (DDI0406C C11.11.38) */
#define A2_VCR_CATCH	(A2_VCR_S | A2_VCR_S << 24)
#define A2_CAP_DEF_MS	30000	/* backstop only: one rawtest run lasts seconds; expiry logs the limit and elapsed time */
#define ARM_MCR_DTRTX	0xee000e15	/* mcr p14, 0, Rt, c0, c5, 0 (DBGDTRTXint, DDI0388I Table 10-1); Rt in bits 15:12 */
#define ARM_MOV_R0_PC	0xe1a0000f
#define ARM_MRC_DFAR_R0	0xee160f10
#define ARM_MRS_R0_CPSR	0xe10f0000
#define ARM_MRS_R0_SPSR	0xe14f0000
#define ARM_MSR_CPSR_C(imm)	(0xe321f000 | (imm))	/* msr cpsr_c, #imm (imm8, I and F set) */
#define ARM_MSR_CPSR_C_R0	0xe121f000
#define ARM_LDR_R0_R1	0xe5910000
#define ARM_MCR_ATS1CPR_R1	0xee071f18	/* mcr p15, 0, r1, c7, c8, 0 */
#define ARM_MCR_DCCMVAC_R1	0xee071f3a	/* mcr p15, 0, r1, c7, c10, 1: clean line by MVA to PoC */
#define ARM_MCR_DCIMVAC_R1	0xee071f36	/* mcr p15, 0, r1, c7, c6, 1: invalidate line by MVA */
#define ARM_MCR_DCCSW_R0	0xee070f5a	/* mcr p15, 0, r0, c7, c10, 2: clean line by set/way */
#define ARM_STR_R0_R1		0xe5810000	/* str r0, [r1] */
#define ARM_MCR_DSB		0xee070f9a	/* mcr p15, 0, r0, c7, c10, 4 */
#define ARM_MRC_PAR_R0	0xee170f14	/* mrc p15, 0, r0, c7, c4, 0 */

static void cap_add(struct us48_a2 *a, const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	a->cap_len += vscnprintf(a->cap_txt + a->cap_len, sizeof(a->cap_txt) - a->cap_len, fmt, ap);
	va_end(ap);
}

/* Halted a2: run an ITR instruction that moves a value to DTRTX, return it. */
static int a2_rd(struct us48_a2 *a, u32 ins, u32 *v, const char *what)
{
	int ret = a2_itr(a, ins, what);

	ret = ret ?: a2_wait(a, HW_CS_DEBUG0_DSCR_DTRTXFULL_MASK, HW_CS_DEBUG0_DSCR_DTRTXFULL_MASK, what);
	return ret ?: regmap_read(a->map, A2_DBG(DTRTX), v);
}

/* Halted a2: run an ITR instruction that leaves its result in r0, return it. */
static int a2_rd0(struct us48_a2 *a, u32 ins, u32 *v, const char *what)
{
	return a2_itr(a, ins, what) ?: a2_rd(a, ARM_MCR_DTRTX, v, what);
}

/* a2 view (ldr through its caches) vs memory (a1's WC mapping) of 8 words at base; va -> PA by ATS1CPR/PAR */
static int a2_cap_mem(struct us48_a2 *a, u32 base)
{
	u32 i, va, par, w, pa, mem;
	int ret;

	cap_add(a, "mem at %#x (va, par, pa, a2 view, memory):\n", base);
	for (i = 0; i < 8; i++) {
		va = base + 4 * i;
		ret = a2_set_reg(a, 1, va) ?: a2_itr(a, ARM_MCR_ATS1CPR_R1, "ats1cpr");
		ret = ret ?: a2_rd0(a, ARM_MRC_PAR_R0, &par, "par");
		if (!ret)
			ret = a2_set_reg(a, 1, va) ?: a2_rd0(a, ARM_LDR_R0_R1, &w, "ldr");
		if (ret)
			return ret;
		pa = (par & ~0xfffu) | (va & 0xfff);
		if ((par & 1) || pa < a->da || pa - a->da >= a->size) {
			cap_add(a, "  %08x %08x -      %08x -\n", va, par, w);
			continue;
		}
		mem = READ_ONCE(*(u32 *)(a->va + (pa - a->da)));
		cap_add(a, "  %08x %08x %08x %08x %08x%s\n", va, par, pa, w, mem, w == mem ? "" : "  DIFF");
	}
	return 0;
}

/* PL310 by-PA ops (cache-l2x0.h: CLEAN_LINE_PA 0x7b0, CLEAN_INV_LINE_PA 0x7f0, CACHE_SYNC 0x730); l2 = VA of the PL310 in the a2 image */
#define PL310_CACHE_SYNC	0x730
#define PL310_CLEAN_INV_PA	0x7f0
#define PL310_CLEAN_WAY		0x7bc
#define A2_CAP_CLEAN_US	50	/* PCIe posted write a2 -> DDR: sub-us, 50 us = well over 1.25x any path latency seen */
static int a2_cap_l2op(struct us48_a2 *a, u32 l2, u32 reg, u32 val)
{
	u32 sync;
	int ret = a2_set_reg(a, 0, val) ?: a2_set_reg(a, 1, l2 + reg) ?: a2_itr(a, ARM_STR_R0_R1, "pl310 op");

	ret = ret ?: a2_set_reg(a, 0, 0) ?: a2_set_reg(a, 1, l2 + PL310_CACHE_SYNC) ?: a2_itr(a, ARM_STR_R0_R1, "pl310 sync");
	ret = ret ?: a2_set_reg(a, 1, l2 + PL310_CACHE_SYNC) ?: a2_rd0(a, ARM_LDR_R0_R1, &sync, "pl310 sync rd");
	udelay(A2_CAP_CLEAN_US);
	return ret;
}

/*
 * One word that differs between the a2 view and memory: where does the data sit? Steps (a2 L1 D, PL310, DDR as a1 reads it):
 * L1 clean, then L1 invalidate + reread (= L2 view when L2 holds the line), then L2 clean+inv by PA, memory reread, a2 refetch.
 * w = a2 view, mem = DDR before, pa = the word's PA (a1 maps the pool at a->va).
 */
static int a2_cap_l2(struct us48_a2 *a, u32 va, u32 pa, u32 w, u32 mem)
{
	u32 *p = (u32 *)(a->va + (pa - a->da));
	u32 m1, m2, l2v = 0, a2w = 0, l2 = 0;
	int ret;

	ret = a2_set_reg(a, 1, va) ?: a2_itr(a, ARM_MCR_DCCMVAC_R1, "dccmvac") ?: a2_itr(a, ARM_MCR_DSB, "dsb");
	udelay(A2_CAP_CLEAN_US);
	m1 = READ_ONCE(*p);
	ret = ret ?: a2_set_reg(a, 1, va) ?: a2_itr(a, ARM_MCR_DCIMVAC_R1, "dcimvac") ?: a2_itr(a, ARM_MCR_DSB, "dsb2");
	ret = ret ?: a2_set_reg(a, 1, va) ?: a2_rd0(a, ARM_LDR_R0_R1, &l2v, "ldr l2view");
	if (!ret && cap_l2x0) {
		ret = a2_set_reg(a, 1, cap_l2x0) ?: a2_rd0(a, ARM_LDR_R0_R1, &l2, "l2x0_base");
		if (!ret && l2)
			ret = a2_cap_l2op(a, l2, PL310_CLEAN_INV_PA, pa & ~31u);
	}
	m2 = READ_ONCE(*p);
	ret = ret ?: a2_set_reg(a, 1, va) ?: a2_itr(a, ARM_MCR_DCIMVAC_R1, "dcimvac2") ?: a2_itr(a, ARM_MCR_DSB, "dsb3");
	ret = ret ?: a2_set_reg(a, 1, va) ?: a2_rd0(a, ARM_LDR_R0_R1, &a2w, "ldr refetch");
	cap_add(a, "    L2 analysis %08x pa %08x (pl310 va %08x ret %d): a2 view %08x, DDR %08x, DDR after L1 clean %08x, a2 after L1 inv (L2 view) %08x, DDR after L2 clean+inv %08x, a2 refetch %08x (DDR now %08x) => %s\n",
		va, pa, l2, ret, w, mem, m1, l2v, m2, a2w, READ_ONCE(*p),
		m2 == w ? "dirty, written back (normal)" : m2 == mem ? "DDR unchanged: a2 line differs from DDR and is not dirty (stale read or lost write)" : "DDR changed to a third value");
	return ret;
}

/* a2 L1 D clean by set/way (32 kB, 4 ways, 32 B lines) then PL310 clean all ways: the printk ring etc. reach DDR before the reset */
static int a2_cap_flush(struct us48_a2 *a)
{
	u32 l2 = 0, v = 0;
	int ret = 0, way, set;

	for (way = 0; !ret && way < 4; way++)
		for (set = 0; !ret && set < 256; set++)
			ret = a2_set_reg(a, 0, way << 30 | set << 5) ?: a2_itr(a, ARM_MCR_DCCSW_R0, "dccsw");
	ret = ret ?: a2_itr(a, ARM_MCR_DSB, "dsb flush");
	if (ret || !cap_l2x0)
		return ret;
	ret = a2_set_reg(a, 1, cap_l2x0) ?: a2_rd0(a, ARM_LDR_R0_R1, &l2, "l2x0_base");
	if (ret || !l2)
		return ret;
	ret = a2_cap_l2op(a, l2, PL310_CLEAN_WAY, 0xff);
	ret = ret ?: a2_set_reg(a, 1, l2 + PL310_CLEAN_WAY) ?: a2_rd0(a, ARM_LDR_R0_R1, &v, "clean way rd");
	udelay(A2_CAP_CLEAN_US);
	cap_add(a, "flush: L1 clean by set/way, PL310 clean by way (reg %#x, ret %d)\n", v, ret);
	return ret;
}

/*
 * Compare 'nw' words at va (a2 view = ldr through its caches, vs the carveout in a1's WC mapping). Differing words: clean the line
 * by MVA (DCCMVAC + DSB), wait for the posted write, read memory again. mem == a2 view afterwards = line was dirty (normal);
 * memory unchanged = clean line holding data that differs from DDR = wrong or stale read on a2.
 */
#define A2_CAP_WIN_DIFFS	64
static int a2_cap_win(struct us48_a2 *a, u32 va, u32 nw, const char *tag)
{
	struct { u32 va, w, mem; } d[A2_CAP_WIN_DIFFS];
	u32 i, par, w, pa, nd = 0, ndiff = 0, mem, nl2 = 0;
	int ret;

	ret = a2_set_reg(a, 1, va) ?: a2_itr(a, ARM_MCR_ATS1CPR_R1, "ats1cpr");
	ret = ret ?: a2_rd0(a, ARM_MRC_PAR_R0, &par, "par");
	if (ret)
		return ret;
	pa = (par & ~0xfffu) | (va & 0xfff);
	if ((par & 1) || pa < a->da || pa - a->da + 4 * nw > a->size) {
		cap_add(a, "win %s %08x: not in the carveout (par %08x)\n", tag, va, par);
		return 0;
	}
	for (i = 0; i < nw; i++) {
		ret = a2_set_reg(a, 1, va + 4 * i) ?: a2_rd0(a, ARM_LDR_R0_R1, &w, "ldr");
		if (ret)
			return ret;
		mem = READ_ONCE(*(u32 *)(a->va + (pa - a->da) + 4 * i));
		if (w == mem)
			continue;
		if (nd < ARRAY_SIZE(d))
			d[nd++] = (typeof(d[0])){ va + 4 * i, w, mem };
		ndiff++;
	}
	cap_add(a, "win %s %08x (pa %08x) %u words: %u differ\n", tag, va, pa, nw, ndiff);
	for (i = 0; i < nd; i++) {
		ret = a2_set_reg(a, 1, d[i].va) ?: a2_itr(a, ARM_MCR_DCCMVAC_R1, "dccmvac") ?: a2_itr(a, ARM_MCR_DSB, "dsb");
		if (ret)
			return ret;
	}
	if (nd)
		udelay(A2_CAP_CLEAN_US);
	for (i = 0; i < nd; i++) {
		mem = READ_ONCE(*(u32 *)(a->va + (pa - a->da) + (d[i].va - va)));
		cap_add(a, "  %08x a2 %08x mem %08x -> after clean mem %08x: %s\n", d[i].va, d[i].w, d[i].mem, mem,
			mem == d[i].w ? "dirty line (normal)" : mem == d[i].mem ? "CLEAN LINE DIFFERS FROM DDR (wrong/stale read)" : "memory changed, not to the a2 value");
		if (mem == d[i].w || nl2 >= 16 || (i && (d[i].va ^ d[i - 1].va) < 32 && (d[i].va & ~31u) == (d[i - 1].va & ~31u)))
			continue;
		nl2++;
		ret = a2_cap_l2(a, d[i].va, pa + (d[i].va - va), d[i].w, d[i].mem);
		if (ret)
			return ret;
	}
	return 0;
}

/* 4 KiB page at b (a2 view vs DDR) and its struct page (FLATMEM mem_map, 32 B per page on ARM32) */
static int a2_cap_page(struct us48_a2 *a, u32 b)
{
	u32 mm = 0, par = 0;
	int ret = a2_cap_win(a, b, 1024, "page");

	if (ret || !cap_memmap)
		return ret;
	ret = a2_set_reg(a, 1, cap_memmap) ?: a2_rd0(a, ARM_LDR_R0_R1, &mm, "mem_map");
	ret = ret ?: a2_set_reg(a, 1, b) ?: a2_itr(a, ARM_MCR_ATS1CPR_R1, "ats1cpr") ?: a2_rd0(a, ARM_MRC_PAR_R0, &par, "par");
	if (!ret && mm && !(par & 1)) {
		u32 pfn = par >> 12, sp = mm + 32 * (pfn - (a->da >> 12));	/* ARCH_PFN_OFFSET = PHYS_OFFSET >> 12 (a2 RAM = 0x08000000) */

		cap_add(a, "struct page of %08x: pfn %#x, mem_map %08x, va %08x\n", b, pfn, mm, sp);
		ret = a2_cap_win(a, sp & ~31u, 8, "struct page");
	}
	return ret;
}

static int a2_cap_regs(struct us48_a2 *a, u32 pcsr)
{
	static const struct { const char *n; u32 ins; } cp[] = {
		{ "DFSR", 0xee150f10 }, { "DFAR", 0xee160f10 }, { "IFSR", 0xee150f30 }, { "IFAR", 0xee160f50 },
		{ "SCTLR", 0xee110f10 }, { "TTBR0", 0xee120f10 },
	};
	static const struct { const char *n; u32 mode; } md[] = { { "und", 0xdb }, { "abt", 0xd7 }, { "svc", 0xd3 }, { "sys", 0xdf } };
	u32 v, cpsr, lr_und = 0, lr_abt = 0, rg[13] = {}, svc_sp = 0;
	int i, ret;

	ret = regmap_set_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_ITREN_MASK | HW_CS_DEBUG0_DSCR_HDBGEN_MASK);
	ret = ret ?: regmap_read(a->map, A2_DBG(DSCR), &v);
	if (ret)
		return ret;
	cap_add(a, "trigger pcsr %#x, dscr %#x (moe %u)\n", pcsr, v, (v >> 2) & 15);
	for (i = 0; i < 13; i++) {
		ret = a2_rd(a, ARM_MCR_DTRTX | i << 12, &v, "mcr r");
		if (ret)
			return ret;
		rg[i] = v;
		cap_add(a, "r%-2d %08x%s", i, v, i % 4 == 3 ? "\n" : "  ");
	}
	ret = a2_rd(a, ARM_MCR_DTRTX | 13 << 12, &v, "mcr sp");
	cap_add(a, "sp  %08x  ", v);
	ret = ret ?: a2_rd(a, ARM_MCR_DTRTX | 14 << 12, &v, "mcr lr");
	cap_add(a, "lr  %08x\n", v);
	ret = ret ?: a2_rd0(a, ARM_MRS_R0_CPSR, &cpsr, "mrs cpsr");
	if (ret)
		return ret;
	cap_add(a, "cpsr %08x\n", cpsr);
	for (i = 0; i < ARRAY_SIZE(cp); i++) {
		ret = a2_rd0(a, cp[i].ins, &v, cp[i].n);
		if (ret)
			return ret;
		cap_add(a, "%s %08x\n", cp[i].n, v);
	}
	/* banked sp, lr, spsr: switch mode through CPSR, restore after (DDI0388I: ITR may change mode) */
	for (i = 0; i < ARRAY_SIZE(md); i++) {
		u32 sp, lr, spsr = 0;

		ret = a2_itr(a, ARM_MSR_CPSR_C(md[i].mode), "msr mode");
		ret = ret ?: a2_rd(a, ARM_MCR_DTRTX | 13 << 12, &sp, "mcr sp");
		ret = ret ?: a2_rd(a, ARM_MCR_DTRTX | 14 << 12, &lr, "mcr lr");
		if (!ret && i < 3)
			ret = a2_rd0(a, ARM_MRS_R0_SPSR, &spsr, "mrs spsr");
		if (ret)
			return ret;
		cap_add(a, "%s: sp %08x lr %08x spsr %08x\n", md[i].n, sp, lr, spsr);
		svc_sp = i == 2 ? sp : svc_sp;
		lr_und = i == 0 ? lr : lr_und;
		lr_abt = i == 1 ? lr : lr_abt;
	}
	ret = a2_set_reg(a, 0, cpsr) ?: a2_itr(a, ARM_MSR_CPSR_C_R0, "msr restore");
	if (ret)
		return ret;
	ret = a2_cap_mem(a, ((cpsr & 0x1f) == 0x17 ? lr_abt : lr_und) - 8);
	/* every register that looks like a kernel pointer: its 256 B block (maple nodes, stack frames, slab objects) */
	for (i = 0; !ret && i <= 13; i++) {
		u32 b = (i == 13 ? svc_sp : rg[i]) & ~0xffu;
		int j;

		if ((b >> 28) != 0xc)
			continue;
		for (j = 0; j < i; j++)
			if (((j == 13 ? svc_sp : rg[j]) & ~0xffu) == b)
				break;
		if (j == i) {
			char tag[8];

			snprintf(tag, sizeof(tag), i == 13 ? "svcsp" : "r%d", i);
			ret = a2_cap_win(a, b, 64, tag);
		}
	}
	/* faulting instruction, then the 4 KiB page (and its struct page, FLATMEM mem_map) of up to 4 lowmem data pointers */
	if (!ret && (cpsr & 0x1f) == 0x17) {
		u32 ins = 0, pg[4], np = 0;

		ret = a2_set_reg(a, 1, lr_abt - 8) ?: a2_rd0(a, ARM_LDR_R0_R1, &ins, "ldr ins");
		cap_add(a, "fault pc %08x, instruction %08x (Rn = r%u = %08x)\n", lr_abt - 8, ins, (ins >> 16) & 15, ((ins >> 16) & 15) < 13 ? rg[(ins >> 16) & 15] : 0);
		for (i = 0; !ret && i < 13 && np < 4; i++) {
			u32 b = rg[i] & ~0xfffu, k;

			if (b < 0xc0f00000 || b >= 0xef000000)
				continue;
			for (k = 0; k < np && pg[k] != b; k++)
				;
			if (k < np)
				continue;
			pg[np++] = b;
			cap_add(a, "page of r%d:\n", i);
			ret = a2_cap_page(a, b);
		}
	}
	for (i = 0; !ret && i < a->cap_nextra; i++) {
		cap_add(a, "extra va %08x:\n", a->cap_extra[i]);
		ret = a2_cap_mem(a, a->cap_extra[i] & ~0xffu);	/* the node's first line: parent word + 7 slots, a2 view vs DDR, before any maintenance */
		ret = ret ?: a2_cap_win(a, a->cap_extra[i] & ~0xffu, 64, "extra");
		ret = ret ?: a2_cap_page(a, a->cap_extra[i] & ~0xfffu);
	}
	return ret;
}

/*
 * vc skip mode: a2 halted at a vector fetch. Normal Linux page faults (user-mode origin, or a kernel access to a user
 * address 0x1000..0xbfffffff) are resumed; anything else (und, kernel-address or NULL faults, kernel prefetch aborts) is the capture.
 * PC read in debug state = vector + 8 (ARM state).
 */
static int a2_cap_classify(struct us48_a2 *a, bool *legit)
{
	u32 r0, cpsr, spsr, pc, far, org, lr = 0;
	int ret;

	*legit = false;
	ret = regmap_set_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_ITREN_MASK | HW_CS_DEBUG0_DSCR_HDBGEN_MASK);
	ret = ret ?: a2_rd(a, ARM_MCR_DTRTX, &r0, "save r0");
	ret = ret ?: a2_rd0(a, ARM_MRS_R0_CPSR, &cpsr, "mrs cpsr");
	ret = ret ?: a2_rd0(a, ARM_MRS_R0_SPSR, &spsr, "mrs spsr");
	ret = ret ?: a2_rd0(a, ARM_MOV_R0_PC, &pc, "mov r0,pc");
	ret = ret ?: a2_rd0(a, ARM_MRC_DFAR_R0, &far, "dfar");
	ret = ret ?: a2_rd(a, ARM_MCR_DTRTX | 14 << 12, &lr, "lr");
	if (ret)
		return ret;
	org = spsr & 0x1f;
	if (a->cap_null) {
		/* only a kernel-mode data abort at a NULL-class address from kernel image code is the capture (fault PC = lr_abt - 8) */
		*legit = !((cpsr & 0x1f) == 0x17 && pc == 0xffff0018 && org == 0x13 && far < 0x1000 && lr - 8 >= 0xc0008000 && lr - 8 < 0xc1000000);
	} else if ((cpsr & 0x1f) == 0x17 && (pc == 0xffff0018 || pc == 0xffff0014)) {
		if (org == 0x10)
			*legit = true, a->cap_skip_user++;
		else if (pc == 0xffff0018 && org == 0x13 && far >= 0x1000 && far < 0xc0000000)
			*legit = true;
	}
	a->cap_last[0] = cpsr, a->cap_last[1] = spsr, a->cap_last[2] = pc, a->cap_last[3] = far;
	if (!*legit)
		return 0;
	/* the restart refetches the vector, which would catch again: catch off for the restart, back on right after */
	ret = a2_set_reg(a, 0, r0);
	regmap_clear_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_ITREN_MASK);
	ret = ret ?: regmap_write(a->map, (HW_CS_DEBUG0_BASE + A2_DBGVCR), 0);
	ret = ret ?: a2_release(a);
	return ret ?: regmap_write(a->map, (HW_CS_DEBUG0_BASE + A2_DBGVCR), A2_VCR_CATCH);
}

static void a2_cap_done(struct us48_a2 *a, u32 pcsr)
{
	int ret = a2_halt(a);

	a->cap_len = 0;
	if (a->cap_skip)
		cap_add(a, "skipped %u normal faults (%u from user mode); last seen cpsr %#x spsr %#x pc %#x far %#x\n", a->cap_nskip,
			a->cap_skip_user, a->cap_last[0], a->cap_last[1], a->cap_last[2], a->cap_last[3]);
	ret = ret ?: a2_cap_regs(a, pcsr);
	ret = ret ?: a2_cap_flush(a);	/* dirty lines (printk ring) to DDR so a2_dmesg.py can read them after the reset */
	if (ret)
		cap_add(a, "capture failed: %d\n", ret);
	regmap_clear_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_ITREN_MASK);
	regmap_write(a->map, (HW_CS_DEBUG0_BASE + A2_DBGVCR), 0);
	a2_reset(a);	/* never leave a2 halted */
	smp_store_release(&a->cap_state, ret ? CAP_FAILED : CAP_DONE);
}

/* Fastest poll = back-to-back PCSR reads (one PCIe read each); vc mode polls DSCR.halted after DBGVCR halts at the fetch. */
static int a2_cap_thread(void *arg)
{
	struct us48_a2 *a = arg;
	ktime_t t0 = ktime_get();
	u32 v = 0;
	int ret = 0;

	if (a->cap_vc) {
		ret = regmap_write(a->map, A2_DBG(LAR), LAR_KEY);
		ret = ret ?: regmap_set_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_HDBGEN_MASK);
		ret = ret ?: regmap_write(a->map, (HW_CS_DEBUG0_BASE + A2_DBGVCR), A2_VCR_CATCH);
		ret = ret ?: regmap_read(a->map, (HW_CS_DEBUG0_BASE + A2_DBGVCR), &v);
		dev_info(a->dev, "capture: DBGVCR reads %#x after write %#lx, ret %d\n", v, A2_VCR_CATCH, ret);
	}
	while (!ret && !kthread_should_stop()) {
		ret = regmap_read(a->map, a->cap_vc ? A2_DBG(DSCR) : A2_DBG(ITR_PCSR), &v);
		if (ret)
			break;
		if (a->cap_vc ? !!(v & HW_CS_DEBUG0_DSCR_HALTED_MASK) : (v - A2_VEC_PCSR_LO < A2_VEC_PCSR_LEN)) {
			bool legit = false;

			if (a->cap_vc && a->cap_skip) {
				ret = a2_cap_classify(a, &legit);
				if (ret)
					break;
				if (legit) {
					a->cap_nskip++;
					continue;
				}
			}
			a2_cap_done(a, v);
			return 0;
		}
		if (ktime_to_ms(ktime_sub(ktime_get(), t0)) >= a->cap_ms) {
			dev_err(a->dev, "capture: no vector fetch in %u ms limit (elapsed %lld ms, last %#x)\n", a->cap_ms,
				ktime_to_ms(ktime_sub(ktime_get(), t0)), v);
			ret = -ETIMEDOUT;
		}
		cond_resched();
	}
	if (a->cap_vc)
		regmap_write(a->map, (HW_CS_DEBUG0_BASE + A2_DBGVCR), 0);
	a->cap_len = 0;
	cap_add(a, "capture ended: %d\n", ret);
	if (a->cap_skip)
		cap_add(a, "skipped %u normal faults (%u from user mode); last seen cpsr %#x spsr %#x pc %#x far %#x\n", a->cap_nskip,
			a->cap_skip_user, a->cap_last[0], a->cap_last[1], a->cap_last[2], a->cap_last[3]);
	smp_store_release(&a->cap_state, CAP_FAILED);
	return ret;
}

static void a2_cap_reap(struct us48_a2 *a)
{
	if (a->cap_task) {
		kthread_stop(a->cap_task);
		put_task_struct(a->cap_task);
		a->cap_task = NULL;
	}
}

static void a2_cap_disarm(struct us48_a2 *a)
{
	mutex_lock(&a->cap_lock);
	a2_cap_reap(a);
	if (a->cap_pre) {
		a->cap_pre = false;
		a->cap_state = CAP_IDLE;
	}
	mutex_unlock(&a->cap_lock);
}

static void a2_cap_exit(void *data)
{
	a2_cap_disarm(data);
}

/* cap_lock held */
static int a2_cap_spawn(struct us48_a2 *a)
{
	struct task_struct *t = kthread_create(a2_cap_thread, a, "a2-capture");

	if (IS_ERR(t)) {
		a->cap_state = CAP_IDLE;
		return PTR_ERR(t);
	}
	get_task_struct(t);
	a->cap_task = t;
	wake_up_process(t);
	return 0;
}

/* start(), a2 halted before the release: vector catch set before a2 executes anything */
static int a2_cap_pre_vcr(struct us48_a2 *a)
{
	int ret = 0;

	if (a->cap_pre && a->cap_vc)
		ret = regmap_write(a->map, (HW_CS_DEBUG0_BASE + A2_DBGVCR), A2_VCR_CATCH);
	return ret;
}

/* start(), after the release */
static int a2_cap_pre_spawn(struct us48_a2 *a)
{
	int ret = 0;

	mutex_lock(&a->cap_lock);
	if (a->cap_pre) {
		a->cap_pre = false;
		ret = a2_cap_spawn(a);
	}
	mutex_unlock(&a->cap_lock);
	return ret;
}

/* a2 running: thread now. a2 offline: armed for the next start (DBGVCR before the release, thread after). */
static int a2_cap_arm(struct us48_a2 *a, bool vc, bool skip, bool null, uint ms)
{
	int ret = 0;

	mutex_lock(&a->cap_lock);
	if (dcc_poll || a->cap_state == CAP_ARMED || (a->rproc->state != RPROC_RUNNING && a->rproc->state != RPROC_OFFLINE)) {
		ret = -EBUSY;	/* the DCC poller shares DSCR */
	} else {
		a2_cap_reap(a);
		a->cap_vc = vc;
		a->cap_skip = skip || null;
		a->cap_null = null;
		a->cap_nskip = a->cap_skip_user = 0;
		a->cap_ms = ms;
		a->cap_len = 0;
		a->cap_state = CAP_ARMED;
		if (a->rproc->state == RPROC_OFFLINE)
			a->cap_pre = true;
		else
			ret = a2_cap_spawn(a);
	}
	mutex_unlock(&a->cap_lock);
	return ret;
}

/* #103: a2 already oopsed and sits in the panic loop (console text seen by the DCC reader): halt it, dump, flush its caches to DDR, reset. */
static int a2_inspect(struct us48_a2 *a, const u32 *va, int n)
{
	int ret, i;

	mutex_lock(&a->cap_lock);
	a2_cap_reap(a);
	if (a->rproc->state != RPROC_RUNNING) {
		mutex_unlock(&a->cap_lock);
		return -ENODEV;
	}
	us48_a2_dcc_stop(a->dcc);	/* the poller shares DSCR */
	a->cap_len = 0;
	a->cap_nextra = n;
	for (i = 0; i < n; i++)
		a->cap_extra[i] = va[i];
	ret = a2_halt(a);
	ret = ret ?: a2_cap_regs(a, 0);
	ret = ret ?: a2_cap_flush(a);
	if (ret)
		cap_add(a, "inspect failed: %d\n", ret);
	regmap_clear_bits(a->map, A2_DBG(DSCR), HW_CS_DEBUG0_DSCR_ITREN_MASK);
	a2_reset(a);	/* never leave a2 halted; the rproc state stays running until stop */
	a->cap_nextra = 0;
	smp_store_release(&a->cap_state, ret ? CAP_FAILED : CAP_DONE);
	mutex_unlock(&a->cap_lock);
	return ret;
}

static int a2_cap_show(struct seq_file *m, void *unused)
{
	static const char *const names[] = { "idle", "armed", "done", "failed" };
	struct us48_a2 *a = m->private;
	enum a2_cap_state st = smp_load_acquire(&a->cap_state);

	mutex_lock(&a->cap_lock);
	seq_printf(m, "state %s\n", names[st]);
	if (st == CAP_DONE || st == CAP_FAILED)
		seq_write(m, a->cap_txt, a->cap_len);
	mutex_unlock(&a->cap_lock);
	return 0;
}

static int a2_cap_open(struct inode *inode, struct file *f)
{
	return single_open(f, a2_cap_show, inode->i_private);
}

/* write "arm [vc] [skip|null] [ms]" or "disarm"; null = skip everything but a kernel NULL-class data abort (#103) */
static ssize_t a2_cap_write(struct file *f, const char __user *u, size_t n, loff_t *pos)
{
	struct us48_a2 *a = ((struct seq_file *)f->private_data)->private;
	char buf[320], *p = buf, *tok;
	bool vc = false, skip = false, null = false;
	uint ms = A2_CAP_DEF_MS;
	int ret = 0;

	if (n >= sizeof(buf))
		return -EINVAL;
	if (copy_from_user(buf, u, n))
		return -EFAULT;
	buf[n] = 0;
	p = strim(buf);
	tok = strsep(&p, " ");
	if (sysfs_streq(tok, "disarm")) {
		a2_cap_disarm(a);
		return n;
	}
	if (sysfs_streq(tok, "inspect")) {
		u32 v[24];
		int nv = 0;

		while (!ret && nv < ARRAY_SIZE(v) && (tok = strsep(&p, " ")))
			ret = kstrtou32(tok, 16, &v[nv++]);
		return ret ?: a2_inspect(a, v, nv) ?: n;
	}
	if (!sysfs_streq(tok, "arm"))
		return -EINVAL;
	while (!ret && (tok = strsep(&p, " "))) {
		if (!strcmp(tok, "vc"))
			vc = true;
		else if (!strcmp(tok, "skip"))
			skip = true;
		else if (!strcmp(tok, "null"))
			null = true;
		else
			ret = kstrtouint(tok, 0, &ms) ?: (ms ? 0 : -EINVAL);
	}
	ret = ret ?: a2_cap_arm(a, vc, skip, null, ms);
	return ret ?: n;
}

static const struct file_operations a2_cap_fops = {
	.owner = THIS_MODULE,
	.open = a2_cap_open,
	.read = seq_read,
	.write = a2_cap_write,
	.llseek = seq_lseek,
	.release = single_release,
};

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

/*
 * sysfs a2_ram: raw read of the a2 pool (host DDR) while a2 is prepared (running or stopped-after-oops, not offline).
 * No a2 debug-register access: lets a1 read a2's printk ring / stall evidence after the fact (#103).
 */
static ssize_t a2_ram_read(struct file *f, struct kobject *kobj, const struct bin_attribute *attr, char *buf, loff_t off, size_t count)
{
	ssize_t n = -ENODEV;

	mutex_lock(&region_lock);
	if (region_owner && region_owner->va) {
		n = 0;
		if (off < region_owner->size) {
			n = min_t(size_t, count, region_owner->size - off);
			memcpy(buf, region_owner->va + off, n);
		}
	}
	mutex_unlock(&region_lock);
	return n;
}
static BIN_ATTR_RO(a2_ram, 0);

static void a2_ram_exit(void *dev)
{
	sysfs_remove_bin_file(&((struct device *)dev)->kobj, &bin_attr_a2_ram);
}

static void a2_rmem_release(void *dev)
{
	of_reserved_mem_device_release(dev);
}

static void a2_region_exit(void *data)
{
	mutex_lock(&region_lock);
	region_owner = NULL;
	mutex_unlock(&region_lock);
}

static int us48_a2_probe(struct auxiliary_device *adev, const struct auxiliary_device_id *id)
{
	struct device *dev = &adev->dev;
	struct device_node *np, *pool;
	struct resource res;
	u32 dt_off[3];
	struct us48_a2 *a;
	struct rproc *rproc;
	int ret;

	np = of_find_compatible_node(NULL, NULL, "ubnt,us48-a2-rproc");
	if (!np)
		return dev_err_probe(dev, -ENODEV, "no node ubnt,us48-a2-rproc\n");
	pool = of_parse_phandle(np, "memory-region", 0);
	if (!pool) {
		of_node_put(np);
		return dev_err_probe(dev, -ENODEV, "ubnt,us48-a2-rproc: no memory-region\n");
	}
	ret = of_address_to_resource(pool, 0, &res);
	of_node_put(pool);
	dt_off[0] = dt_off[1] = dt_off[2] = U32_MAX;
	of_property_read_u32(np, "ubnt,kernel-offset", &dt_off[0]);
	of_property_read_u32(np, "ubnt,dtb-offset", &dt_off[1]);
	of_property_read_u32(np, "ubnt,initrd-offset", &dt_off[2]);
	if (ret) {
		of_node_put(np);
		return dev_err_probe(dev, ret, "memory-region reg\n");
	}
	ret = dma_set_coherent_mask(dev, DMA_BIT_MASK(32));
	if (!ret)
		ret = of_reserved_mem_device_init_by_idx(dev, np, 0);
	of_node_put(np);
	if (ret)
		return dev_err_probe(dev, ret, "a2 memory pool\n");
	ret = devm_add_action_or_reset(dev, a2_rmem_release, dev);
	if (ret)
		return ret;

	rproc = devm_rproc_alloc(dev, "us48-a2", &us48_a2_ops, A2_FW, sizeof(*a));
	if (!rproc)
		return -ENOMEM;
	rproc->auto_boot = false;
	a = rproc->priv;
	a->dev = dev;
	a->rproc = rproc;
	mutex_init(&a->cap_lock);
	ret = devm_add_action_or_reset(dev, a2_cap_exit, a);
	if (ret)
		return ret;
	a->p = us48_pci_from_adev(adev);
	a->map = us48_pci_regmap(a->p);
	memcpy(a->dt_off, dt_off, sizeof(dt_off));
	a->phys = res.start;
	a->size = resource_size(&res);
	/* one 64 MiB outbound granule: the carveout must not straddle it (checked again by the map call) */
	if ((a->phys & (SZ_64M - 1)) + a->size > SZ_64M)
		return dev_err_probe(dev, -EINVAL, "carveout %pa+%zx crosses a 64 MiB boundary\n", &a->phys, a->size);
	a->da = HW_A2_HOST_WIN_BASE + (a->phys & (SZ_64M - 1));
	region_owner = a;
	ret = devm_add_action_or_reset(dev, a2_region_exit, NULL);
	if (ret)
		return ret;
	dev_info(dev, "carveout %pa+%zx, a2 AXI %#x\n", &a->phys, a->size, a->da);
	ret = sysfs_create_bin_file(&dev->kobj, &bin_attr_a2_ram);
	if (!ret)
		ret = devm_add_action_or_reset(dev, a2_ram_exit, dev);
	if (ret)
		return ret;
	/* created before rproc_add: devres releases it after rproc_del, which may still call stop() */
	a->dcc = us48_a2_dcc_create(dev, a->map);
	if (IS_ERR(a->dcc))
		return PTR_ERR(a->dcc);
	a->reboot_nb.notifier_call = us48_a2_reboot;
	ret = register_reboot_notifier(&a->reboot_nb);
	if (ret)
		return ret;
	ret = devm_add_action_or_reset(dev, a2_reboot_exit, &a->reboot_nb);
	if (ret)
		return ret;
	ret = devm_rproc_add(dev, rproc);
	if (!ret) {
		us48_a2_dcc_debugfs(a->dcc, rproc->dbg_dir);
		if (rproc->dbg_dir)
			debugfs_create_file("pcsr", 0444, rproc->dbg_dir, a, &a2_pcsr_fops);
		if (rproc->dbg_dir)
			debugfs_create_file("ccu", 0444, rproc->dbg_dir, a, &a2_ccu_fops);
		if (rproc->dbg_dir)
			debugfs_create_file("capture", 0644, rproc->dbg_dir, a, &a2_cap_fops);
		if (rproc->dbg_dir)
			debugfs_create_file("paxb_tx", 0644, rproc->dbg_dir, a, &a2_paxb_tx_fops);
		if (rproc->dbg_dir) {
			debugfs_create_u32("halt_n", 0444, rproc->dbg_dir, &a->halt_n);
			debugfs_create_u32("halt_fail", 0444, rproc->dbg_dir, &a->halt_fail);
			debugfs_create_u32("halt_last_us", 0444, rproc->dbg_dir, &a->halt_last_us);
			debugfs_create_u32("halt_max_us", 0444, rproc->dbg_dir, &a->halt_max_us);
		}
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
