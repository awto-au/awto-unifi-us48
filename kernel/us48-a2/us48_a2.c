// SPDX-License-Identifier: GPL-2.0
/*
 * us48_a2: remoteproc driver for the unit 1 Cortex-A9 (a2), aux device us48_pci.a2.
 * Firmware us48-a2.elf runs from a DT reserved-memory carveout in host DDR (design: docs/a2-design.md).
 */
#include <linux/auxiliary_bus.h>
#include <linux/firmware.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/regmap.h>
#include <linux/remoteproc.h>
#include <linux/sizes.h>
#include "remoteproc_internal.h"
#include "us48_hwdb.h"
#include "us48_pci.h"
#include "us48_a2_boot.h"

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

/* Payload mode (Linux on a2): after the stub ELF, zImage + DTB (+ initramfs) are copied into the carveout. */
static bool payload;
module_param(payload, bool, 0644);
MODULE_PARM_DESC(payload, "load zImage/DTB/initramfs into the carveout after the (boot stub) ELF");
static char *kernel_fw = "us48-a2-zImage";
static char *dtb_fw = "us48-a2.dtb";
static char *initrd_fw = "us48-a2-initrd";	/* empty string: none */
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

/* Copy one firmware file to carveout + off; the offset must be past the stub MiB and the file must fit. */
static int a2_blob(struct us48_a2 *a, const char *name, u32 off, const char *what)
{
	const struct firmware *fw;
	int ret;

	if (off == U32_MAX)
		return dev_err_probe(a->dev, -EINVAL, "payload: no %s offset (DT ubnt,%s-offset or module parameter)\n", what, what);
	ret = request_firmware(&fw, name, a->dev);
	if (ret)
		return dev_err_probe(a->dev, ret, "payload: %s %s\n", what, name);
	if (off < HW_A2_STUB_SIZE || off > a->size || fw->size > a->size - off) {
		dev_err(a->dev, "payload: %s %s (%zu B) at +%#x does not fit the carveout\n", what, name, fw->size, off);
		ret = -EFBIG;
	} else {
		memcpy(a->va + off, fw->data, fw->size);
		dev_info(a->dev, "payload: %s %s at a2 %#x, %zu B\n", what, name, a->da + off, fw->size);
	}
	release_firmware(fw);
	return ret;
}

static u32 a2_off(u32 param, u32 dt)
{
	return param != U32_MAX ? param : dt;
}

/* Standard ELF load (stub or self-test); in payload mode also the kernel, DTB, initramfs and the stub's boot args. */
static int us48_a2_load(struct rproc *rproc, const struct firmware *fw)
{
	struct us48_a2 *a = rproc->priv;
	struct us48_a2_boot_args *args = a->va + US48_A2_BOOT_ARGS_OFF;
	u32 koff = a2_off(kernel_off, a->dt_off[0]), doff = a2_off(dtb_off, a->dt_off[1]);
	u32 ioff = a2_off(initrd_off, a->dt_off[2]);
	int ret = rproc_elf_load_segments(rproc, fw);

	if (ret || !payload)
		return ret;
	ret = a2_blob(a, kernel_fw, koff, "kernel") ?: a2_blob(a, dtb_fw, doff, "dtb");
	if (!ret && initrd_fw && *initrd_fw)
		ret = a2_blob(a, initrd_fw, ioff, "initrd");
	if (ret)
		return ret;
	args->kernel = a->da + koff;
	args->dtb = a->da + doff;
	args->status = 0;
	wmb();
	args->magic = US48_A2_BOOT_MAGIC;	/* last: the stub parks without it */
	return 0;
}

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

	ret = ret ?: a2_halt(a);
	ret = ret ?: a2_set_pc(a, rproc->bootaddr);
	ret = ret ?: a2_release(a);
	if (ret)
		a2_reset(a);	/* never leave a2 halted */
	return ret;
}

/* Halt is best effort (a stalled a2 never halts); the reset is what stops it. */
static int us48_a2_stop(struct rproc *rproc)
{
	struct us48_a2 *a = rproc->priv;

	if (a2_halt(a))
		dev_warn(a->dev, "a2 did not halt, resetting\n");
	return a2_reset(a);
}

static const struct rproc_ops us48_a2_ops = {
	.prepare		= us48_a2_prepare,
	.unprepare		= us48_a2_unprepare,
	.start			= us48_a2_start,
	.stop			= us48_a2_stop,
	.load			= us48_a2_load,
	.parse_fw		= rproc_elf_load_rsc_table,
	.find_loaded_rsc_table	= rproc_elf_find_loaded_rsc_table,
	.get_boot_addr		= rproc_elf_get_boot_addr,
	.sanity_check		= rproc_elf_sanity_check,
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
	return devm_rproc_add(dev, rproc);
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
