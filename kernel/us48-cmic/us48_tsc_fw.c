// SPDX-License-Identifier: GPL-2.0
/*
 * TSC SerDes bring-up of unit 1 (xe49/xe50, TSC0 lanes 0 and 2, 1-lane SFI) over MIIM bus 1: stage 0, microcode
 * download, stage 1 check, stage 2. Steps = OpenMDK bcmi_tsc_xgxs_drv.c via us48_tsc_init.h (scripts/gen_tsc.py).
 * Microcode = request_firmware("us48-tsc.bin"). docs/driver-internals.md 9.
 */
#include <linux/delay.h>
#include <linux/firmware.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/sched.h>

#include "us48_cmic.h"

enum { TSC_RMW, TSC_DELAY, TSC_LANE_RESET, TSC_UC_RX, TSC_FW, TSC_PLL, TSC_CRC, TSC_UC_CMD };
enum { TSC_LANE, TSC_SHARED, TSC_PAIR, TSC_ALL };
struct tsc_step {
	u8 op, scope;
	u16 reg, mask, val;
	u8 devad;
};

#include "us48_tsc_init.h"

#define TSC_AER			0xffde
#define TSC_COMMAND		0xffc2
#define TSC_COMMAND2		0xffca
#define TSC_RAMWORD		0xffc0
#define TSC_ADDRESS		0xffc1
#define TSC_WRDATA		0xffc3
#define TSC_DLSTAT		0xffc5
#define TSC_CRC_REG		0xf015
#define TSC_VERSION_REG		0xf010
#define  CMD_INIT		BIT(15)
#define  CMD_WRITE_RUN		(BIT(3) | BIT(0))
#define  CMD_STOP		BIT(1)
#define  CMD_UC_RESET_N		BIT(4)
#define  DL_INIT_DONE		BIT(15)
#define  DL_ERRS		(BIT(0) | BIT(1))
/* OM limits (bcmi_tsc_xgxs_firmware_set.c / drv.c, not measured here), 1 ms steps: expiry logs op + limit and aborts. */
#define DL_INIT_MS		200
#define CRC_CALC_MS		100
#define PLL_LOCK_MS		200
/* Stage 0 resets the analog of our two lanes; OM resets the primary lane only (I: lanes 0 and 2 share one load). */
#define STAGE0_LANE_MASK	0x5
#define NLANES			4

/* Datasheet-less delays: OM value - 10 % (CLAUDE.md delay rule). */
static void tsc_delay(unsigned int us)
{
	us = us * 9 / 10;
	usleep_range(us, us + us / 4);
}

static int tsc_set_aer(struct us48_cmic *u, u8 aer)
{
	return us48_tsc_wr(u, TSC_AER, aer);
}

static int tsc_rmw_one(struct us48_cmic *u, const struct tsc_step *s, u8 aer)
{
	u32 reg = s->devad << 16 | s->reg;
	u16 v = 0;

	if (tsc_set_aer(u, aer))
		return -EIO;
	if (s->mask != 0xffff && us48_tsc_rd(u, reg, &v))
		return -EIO;
	return us48_tsc_wr(u, reg, (v & ~s->mask) | s->val);
}

#define UC_CTRL_REG		0xc20e
#define DSC_DIAG_CTRL0_REG	0xc20d
/* OM _tsc_uc_cmd_seq polls READY_FOR_CMD 2000 reads without a time base; one MIIM read ~65 us, so 130 ms. Expiry = error. */
#define UC_READY_MS		130

/* Graceful uC stop (0) / resume (2) of one lane, OM _tsc_uc_cmd_seq (rev < A2): UC_CTRL = cmd << 8 | GP_UC_REQ. */
static int tsc_uc_cmd(struct us48_cmic *u, u8 lane, u8 cmd)
{
	ktime_t t0 = ktime_get();
	u16 v = 0;

	if (tsc_set_aer(u, lane) || us48_tsc_rd(u, DSC_DIAG_CTRL0_REG, &v))
		return -EIO;
	if (v & BIT(0))		/* DIAGNOSTICS_EN: uC busy with diagnostics, OM skips */
		return 0;
	do {
		if (us48_tsc_rd(u, UC_CTRL_REG, &v))
			return -EIO;
		if (v & BIT(7))
			break;
		tsc_delay(1000);
	} while (ktime_ms_delta(ktime_get(), t0) < UC_READY_MS);
	if (!(v & BIT(7))) {
		dev_err(u->dev, "tsc uC cmd %u lane %u: UC_CTRL %04x not ready after %lld ms (limit %u ms)\n", cmd, lane, v,
			ktime_ms_delta(ktime_get(), t0), UC_READY_MS);
		return -ETIMEDOUT;
	}
	if (us48_tsc_wr(u, UC_CTRL_REG, v & ~(BIT(7) | BIT(6) | GENMASK(5, 4))) ||
	    us48_tsc_wr(u, UC_CTRL_REG, cmd << 8 | 1))
		return -EIO;
	tsc_delay(1000);
	return 0;
}

static int tsc_rmw(struct us48_cmic *u, const struct tsc_step *s, u8 lane)
{
	int l, ret = 0;

	switch (s->scope) {
	case TSC_ALL:
		for (l = 0; !ret && l < NLANES; l++)
			ret = tsc_rmw_one(u, s, l);
		return ret;
	case TSC_SHARED:
		return tsc_rmw_one(u, s, 0);
	case TSC_PAIR:
		return tsc_rmw_one(u, s, lane & ~1);
	default:
		return tsc_rmw_one(u, s, lane);
	}
}

static int tsc_poll(struct us48_cmic *u, u16 reg, u16 mask, bool set, unsigned int limit_ms, const char *what)
{
	ktime_t t0 = ktime_get();
	u16 v = 0;
	unsigned int i;

	if (tsc_set_aer(u, 0))
		return -EIO;
	for (i = 0; i < limit_ms; i++) {
		if (us48_tsc_rd(u, reg, &v))
			return -EIO;
		if (!!(v & mask) == set)
			return 0;
		tsc_delay(1000);
	}
	dev_err(u->dev, "tsc %s: reg %04x = %04x after %lld ms (limit %u ms)\n", what, reg, v,
		ktime_ms_delta(ktime_get(), t0), limit_ms);
	return -ETIMEDOUT;
}

/* bcmi_tsc_xgxs_firmware_set: init RAM, write the image as LE 16-bit words, check, start the 8051. */
static int tsc_download(struct us48_cmic *u, const struct firmware *fw)
{
	size_t n = ALIGN(fw->size, 16), i;
	ktime_t t0 = ktime_get();
	u16 v = 0;
	int ret;

	ret = tsc_set_aer(u, 0) ?: us48_tsc_wr(u, TSC_COMMAND, CMD_INIT) ?:
	      tsc_poll(u, TSC_DLSTAT, DL_INIT_DONE, true, DL_INIT_MS, "RAM init");
	if (ret)
		return ret;
	ret = us48_tsc_rd(u, TSC_COMMAND2, &v) ?: us48_tsc_wr(u, TSC_COMMAND2, v | 0xf) ?:	/* TMR_EN */
	      us48_tsc_wr(u, TSC_RAMWORD, n - 1) ?: us48_tsc_wr(u, TSC_ADDRESS, 0) ?:
	      us48_tsc_wr(u, TSC_COMMAND, CMD_WRITE_RUN);
	for (i = 0; !ret && i < n; i += 2) {
		u16 w = (i < fw->size ? fw->data[i] : 0) | (i + 1 < fw->size ? fw->data[i + 1] << 8 : 0);

		ret = us48_tsc_wr(u, TSC_WRDATA, w);
		if (ret)
			dev_err(u->dev, "tsc download: word write failed at byte %zu of %zu\n", i, n);
		if (!(i % 1024))
			cond_resched();
	}
	ret = ret ?: us48_tsc_wr(u, TSC_COMMAND, CMD_STOP) ?: us48_tsc_rd(u, TSC_DLSTAT, &v);
	if (ret)
		return ret;
	if (v & DL_ERRS) {
		dev_err(u->dev, "tsc download: DOWNLOAD_STATUS %04x (ERR0/ERR1)\n", v);
		return -EIO;
	}
	ret = us48_tsc_wr(u, TSC_CRC_REG, 0) ?: us48_tsc_wr(u, TSC_COMMAND, CMD_UC_RESET_N);	/* CRC calc on, start uC */
	dev_info(u->dev, "tsc: %zu bytes downloaded in %lld ms\n", n, ktime_ms_delta(ktime_get(), t0));
	return ret;
}

static int tsc_run(struct us48_cmic *u, const struct tsc_step *st, int n, u8 lane, u8 lane_mask,
		   const struct firmware *fw)
{
	int i, ret = 0;
	u16 v = 0, m;

	for (i = 0; !ret && i < n; i++) {
		switch (st[i].op) {
		case TSC_RMW:
			ret = tsc_rmw(u, &st[i], lane);
			break;
		case TSC_DELAY:
			tsc_delay(st[i].val);
			break;
		case TSC_LANE_RESET:
			m = (lane_mask << TSC_RESET_RX_SHIFT) | (lane_mask << TSC_RESET_TX_SHIFT);
			ret = tsc_rmw_one(u, &(struct tsc_step){ .reg = TSC_RESET_REG, .mask = m, .val = st[i].val ? m : 0 }, 0);
			break;
		case TSC_UC_CMD:
			ret = tsc_uc_cmd(u, lane, st[i].val);
			break;
		case TSC_UC_RX:
			ret = us48_tsc_uc_ram_wr(u, lane, 0x04d0 + 0x100 * lane, st[i].val ? 0x0e00 : 0x0d00);
			break;
		case TSC_FW:
			ret = tsc_download(u, fw);
			break;
		case TSC_PLL:
			ret = tsc_poll(u, TSC_PLLSTAT_REG, TSC_PLLSTAT_PASS, true, PLL_LOCK_MS, "PLL lock");
			break;
		case TSC_CRC:
			ret = tsc_poll(u, TSC_CRC_REG, 0xffff, true, CRC_CALC_MS, "ucode CRC");
			if (!ret)
				ret = us48_tsc_rd(u, TSC_CRC_REG, &m) ?: us48_tsc_rd(u, TSC_VERSION_REG, &v);
			if (!ret) {
				dev_info(u->dev, "tsc: ucode version %04x CRC %04x\n", v, m);
				if (v != TSC_FW_VERSION || m != TSC_FW_CRC)	/* I: CRC reg = header CRC */
					dev_warn(u->dev, "tsc: expected version %04x CRC %04x\n", TSC_FW_VERSION, TSC_FW_CRC);
			}
			break;
		}
		if (ret)
			dev_err(u->dev, "tsc: step %d (op %d reg %04x) failed: %d\n", i, st[i].op, st[i].reg, ret);
	}
	return ret;
}

/*
 * Module in cage <front> (SFF-8472 Table 4-1/5-3, A0h: byte 0 id, byte 8 bit 2 = passive cable). I2C = PCA9548
 * channel front-49 (board.md), adapter found by mux name, no bus numbers. Read-only.
 * Returns 1 passive cable, 0 optic/other, -errno unreadable (logged; the optic path is then left as is).
 */
static int tsc_sfp_passive_chan(struct device *dev, int chan)
{
	char want[40];
	int nr;

	snprintf(want, sizeof(want), "-mux (chan_id %d)", chan);
	for (nr = 0; nr < 32; nr++) {
		struct i2c_adapter *ad = i2c_get_adapter(nr);
		u8 off = 0, a0[11];
		struct i2c_msg m[2] = { { .addr = 0x50, .len = 1, .buf = &off },
					{ .addr = 0x50, .flags = I2C_M_RD, .len = sizeof(a0), .buf = a0 } };
		int ret;

		if (!ad)
			continue;
		if (strncmp(ad->name, "i2c-", 4) || !strstr(ad->name, want)) {
			i2c_put_adapter(ad);
			continue;
		}
		ret = i2c_transfer(ad, m, 2);
		if (ret != 2) {
			dev_err(dev, "sfp chan %d: A0h read failed on %s: %d\n", chan, ad->name, ret);
			i2c_put_adapter(ad);
			return ret < 0 ? ret : -EIO;
		}
		dev_info(dev, "sfp chan %d (%s): id %02x conn %02x b3 %02x b8 %02x\n", chan, ad->name, a0[0], a0[2], a0[3], a0[8]);
		i2c_put_adapter(ad);
		return a0[0] == 0x03 && (a0[8] & BIT(2));
	}
	dev_err(dev, "sfp chan %d: no PCA9548 channel adapter\n", chan);
	return -ENODEV;
}

static int tsc_sfp_passive(struct us48_cmic *u, int front)
{
	return tsc_sfp_passive_chan(u->dev, front - 49);
}

int us48_tsc_init(struct us48_cmic *u, const struct firmware *fw)
{
	static const struct tsc_step mode = { .reg = TSC_SETUP_REG, .mask = TSC_SETUP_MODE_MASK | BIT(3), .val = 0 };
	int i, ret;

	if (!fw) {
		dev_err(u->dev, "us48-tsc.bin not loaded: 10G SerDes microcode missing, xe links stay down\n");
		return -ENOENT;
	}
	mutex_lock(&u->mdio);
	/* PhyConfig_Mode: 4 x 1-lane ports (PORT_MODE_SEL 0, SINGLE_PORT_MODE 0). */
	ret = tsc_rmw_one(u, &mode, 0) ?: tsc_run(u, tsc_stage0, ARRAY_SIZE(tsc_stage0), 0, STAGE0_LANE_MASK, fw);
	for (i = 0; !ret && i < u->nports; i++) {
		if (u->cfgs[i].copper)
			continue;
		ret = tsc_run(u, tsc_stage2, ARRAY_SIZE(tsc_stage2), u->cfgs[i].lane, BIT(u->cfgs[i].lane), fw);
		ret = ret ?: tsc_run(u, tsc_stage3, ARRAY_SIZE(tsc_stage3), u->cfgs[i].lane, BIT(u->cfgs[i].lane), fw);
		/* Passive cable in the cage: OM CR mode (FIRMWARE_MODE 2) + RX restart; optic = OM SFI = FIRMWARE_MODE 0 (stage 0 value). */
		if (!ret && tsc_sfp_passive(u, u->cfgs[i].front) == 1) {
			u8 l = u->cfgs[i].lane;

			dev_info(u->dev, "tsc %s: passive cable (SFF-8472 A0h byte 8 bit 2): FIRMWARE_MODE 2\n", u->cfgs[i].name);
			ret = tsc_uc_cmd(u, l, 0) ?: tsc_rmw_one(u, &(struct tsc_step){ .reg = 0xc260, .mask = 0xffff, .val = 2 }, l) ?:
			      tsc_uc_cmd(u, l, 2) ?: us48_tsc_uc_ram_wr(u, l, 0x04d0 + 0x100 * l, 0x0d00) ?:
			      tsc_rmw_one(u, &(struct tsc_step){ .reg = 0xc137, .mask = BIT(0) }, l) ?:	/* RSTB_LANE 0 */
			      tsc_rmw_one(u, &(struct tsc_step){ .reg = 0xc21e, .mask = BIT(15), .val = BIT(15) }, l) ?:	/* RXSEQSTART */
			      us48_tsc_uc_ram_wr(u, l, 0x04d0 + 0x100 * l, 0x0e00);
		}
		if (!ret)
			dev_info(u->dev, "tsc %s: stage 2 + speed set (10G SFI) done\n", u->cfgs[i].name);
	}
	mutex_unlock(&u->mdio);
	return ret;
}
MODULE_FIRMWARE("us48-tsc.bin");
