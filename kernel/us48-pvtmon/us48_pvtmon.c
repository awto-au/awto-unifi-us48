// SPDX-License-Identifier: GPL-2.0
/*
 * us48_pvtmon: die temperature (TOP PVTMON) of the BCM5334x chips as thermal sensor + hwmon (#19).
 * Unit 0: platform device (DT, CMC1 S-channel of the AXI CMIC). Unit 1: aux device of us48_pci (its locked S-channel).
 * Sensor facts and formula: hwdb (top_pvtmon, PVTMON_CALC_*), docs/clock-audit.md s3. Any read problem is -EIO.
 */
#include <linux/auxiliary_bus.h>
#include <linux/bitfield.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/thermal.h>
#include "bcm_cmic.h"
#include "us48_hwdb.h"
#include "us48_pci.h"
#include "thermal_hwmon.h"
/* Shared S-channel core, single source in common/bcm-cmic/ (included, not copied) */
#include "../../common/bcm-cmic/bcm_cmic_schan.c"

#define PVT_CTRL_0	(HW_TOP_PVTMON_BASE + HW_TOP_PVTMON_CTRL_0)
#define PVT_CTRL_1	(HW_TOP_PVTMON_BASE + HW_TOP_PVTMON_CTRL_1)
#define PVT_RESULT	(HW_TOP_PVTMON_BASE + HW_TOP_PVTMON_RESULT_0)
#define PVT_CTRL_0_INIT	0x001d2003	/* docs/clock-audit.md s3 */
/* Plausible die range; outside it the code is garbage (seen: unit 1 reading -150.3 C = code 1023), so -EIO */
#define PVT_MIN_MC	(-40000)
#define PVT_MAX_MC	150000

struct pvt {
	struct device *dev;
	struct bcm_cmic_unit u;		/* unit 0 only */
	struct mutex lock;		/* unit 0: one S-channel user of CMC1 */
	struct us48_pci *pci;		/* unit 1 only */
	bool starting;			/* sensor restarted, first valid code not seen yet */
};

static int pvt_op(struct pvt *p, u32 op, u32 addr, u32 *v)
{
	int ret;

	if (p->pci) {
		ret = us48_pci_schan_poll(p->pci, op, HW_CONST_TOP_SBUS_BLK, addr, v, 1, NULL);
	} else {
		mutex_lock(&p->lock);
		ret = bcm_cmic_schan_op(&p->u, op, HW_CONST_TOP_SBUS_BLK, addr, v, 1, NULL);
		mutex_unlock(&p->lock);
	}
	return ret == -EAGAIN ? ret : ret ? -EIO : 0;
}

/* Sensor is in reset after power-on until something runs the init sequence (clock-audit s3). */
static int pvt_start(struct pvt *p, bool force)
{
	u32 v, z = 0, run = HW_TOP_PVTMON_CTRL_1_RESET_N_MASK, ctrl0 = PVT_CTRL_0_INIT;
	int ret = pvt_op(p, BCM_SCHAN_READ_REG, PVT_CTRL_1, &v);

	if (ret || (!force && (v & HW_TOP_PVTMON_CTRL_1_RESET_N_MASK)))
		return ret;
	ret = pvt_op(p, BCM_SCHAN_WRITE_REG, PVT_CTRL_0, &ctrl0);
	if (!ret)
		ret = pvt_op(p, BCM_SCHAN_WRITE_REG, PVT_CTRL_1, &z);
	if (!ret)
		ret = pvt_op(p, BCM_SCHAN_WRITE_REG, PVT_CTRL_1, &run);
	return ret;
}

static int pvt_get_temp(struct thermal_zone_device *tz, int *temp)
{
	struct pvt *p = thermal_zone_device_priv(tz);
	u32 v, code;
	int mc, ret = pvt_op(p, BCM_SCHAN_READ_REG, PVT_RESULT, &v);

	if (ret)
		return ret;
	code = FIELD_GET(HW_TOP_PVTMON_RESULT_0_TEMP_DATA_MASK, v);
	/* 0 = in reset / not running; 0x3ff = saturated, seen after a chip reset that left RESET_N set (CPS_RESET, #106) */
	if (!code || code == FIELD_MAX(HW_TOP_PVTMON_RESULT_0_TEMP_DATA_MASK)) {
		bool first = !p->starting;

		p->starting = true;	/* first bad code after a reset: restart + -EAGAIN (not logged); a repeat is -EIO */
		ret = pvt_start(p, code);
		return ret == -EAGAIN || (!ret && first) ? -EAGAIN : -EIO;
	}
	mc = (HW_CONST_PVTMON_CALC_OFFSET - HW_CONST_PVTMON_CALC_SLOPE * (int)code) / HW_CONST_PVTMON_CALC_DIV;
	if (mc < PVT_MIN_MC || mc > PVT_MAX_MC) {
		dev_warn_ratelimited(p->dev, "implausible die code %u (%d mC): -EIO\n", code, mc);
		return -EIO;
	}
	p->starting = false;
	*temp = mc;
	return 0;
}

static const struct thermal_zone_device_ops pvt_ops = { .get_temp = pvt_get_temp };

static int pvt_register(struct device *dev, struct pvt *p)
{
	struct thermal_zone_device *tz;
	int ret;

	p->dev = dev;
	ret = pvt_start(p, false);
	if (ret)
		return dev_err_probe(dev, ret, "PVTMON init failed\n");
	tz = devm_thermal_of_zone_register(dev, 0, p, &pvt_ops);
	if (IS_ERR(tz))
		return dev_err_probe(dev, PTR_ERR(tz), "no thermal zone for this sensor in the DT\n");
	return devm_thermal_add_hwmon_sysfs(dev, tz);
}

static int pvt_plat_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct pvt *p = devm_kzalloc(dev, sizeof(*p), GFP_KERNEL);
	void __iomem *cmc1;

	if (!p)
		return -ENOMEM;
	mutex_init(&p->lock);
	/* reg = CMC1 S-channel window; the shared core addresses CMC0, so base = window - CMC0 ctrl offset */
	cmc1 = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(cmc1))
		return PTR_ERR(cmc1);
	p->u.base = cmc1 - BCM_CMIC_SCHAN_CTRL;
	return pvt_register(dev, p);
}

static const struct of_device_id pvt_of_match[] = {
	{ .compatible = "ubnt,us48-pvtmon" },
	{ }
};
MODULE_DEVICE_TABLE(of, pvt_of_match);

static struct platform_driver pvt_plat_driver = {
	.driver = { .name = "us48_pvtmon", .of_match_table = pvt_of_match },
	.probe = pvt_plat_probe,
};

static int pvt_aux_probe(struct auxiliary_device *adev, const struct auxiliary_device_id *id)
{
	struct pvt *p = devm_kzalloc(&adev->dev, sizeof(*p), GFP_KERNEL);

	if (!p)
		return -ENOMEM;
	p->pci = us48_pci_from_adev(adev);
	return pvt_register(&adev->dev, p);
}

static const struct auxiliary_device_id pvt_aux_ids[] = {
	{ .name = US48_PCI_AUX_ID(US48_PCI_AUX_PVTMON) },
	{ }
};
MODULE_DEVICE_TABLE(auxiliary, pvt_aux_ids);

static struct auxiliary_driver pvt_aux_driver = {
	.name = "pvt",
	.id_table = pvt_aux_ids,
	.probe = pvt_aux_probe,
};

static int __init pvt_init(void)
{
	int ret = platform_driver_register(&pvt_plat_driver);

	if (ret)
		return ret;
	ret = auxiliary_driver_register(&pvt_aux_driver);
	if (ret)
		platform_driver_unregister(&pvt_plat_driver);
	return ret;
}

static void __exit pvt_exit(void)
{
	auxiliary_driver_unregister(&pvt_aux_driver);
	platform_driver_unregister(&pvt_plat_driver);
}

module_init(pvt_init);
module_exit(pvt_exit);

MODULE_DESCRIPTION("UniFi US-48-500W BCM5334x die temperature (PVTMON over S-channel): thermal sensor + hwmon");
MODULE_LICENSE("GPL");
