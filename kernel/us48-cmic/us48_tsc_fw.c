// SPDX-License-Identifier: GPL-2.0
/*
 * TSC SerDes bring-up of unit 1 (xe49/xe50, TSC0 lanes 0 and 2, 1-lane SFI) over MIIM bus 1: stage 0, microcode
 * download, stage 1 check, stage 2. Steps = OpenMDK bcmi_tsc_xgxs_drv.c via us48_tsc_init.h (scripts/gen_tsc.py).
 * Microcode = request_firmware("us48-tsc.bin"). docs/driver-internals.md 9.
 */
#include <linux/bitfield.h>
#include <linux/delay.h>
#include <linux/firmware.h>
#include <linux/module.h>
#include <linux/sched.h>
#include <linux/slab.h>

#include "us48_cmic.h"
#include "us48_hwdb.h"

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

/*
 * 1000BASE-X (8b/10b, CL37) values for the stage 2 rows of the same reg + mask; PLL stays div66 (10.3125 G), the lane
 * runs OS 8.25 (0xc137 OS_MODE 7). Sources: OM bcmi_tsc_xgxs_drv.c _tsc_credit/encode/decode_set FV_adr_1000M (plldiv 66);
 * stock-warm unit 0 TSC0 lanes 0/2 (tmp/higig/stock.txt, a1/l0 = a1/l2): c100 40a5, c103 0029, c111 000c, c113 01cb,
 * c114 0004, c130 2cb0, c134 0073, c136 0001, c137 003a, c254 0220, c181 0016, c184 0000. docs/driver-internals.md 9.4.
 */
static const struct { u16 reg, mask, val; } tsc_1g_patch[] = {
	{ 0xc100, 0xffff, 0x40a5 },	/* TX_X4_CREDIT0: CLOCKCNT0 0xa5, CREDITENABLE, SGMII_SPD_SWITCH 0 */
	{ 0xc101, 0xffff, 0x8200 },	/* TX_X4_CREDIT1: OM rsv 0x82 (10G 0x84); reads back 0 on stock */
	{ 0xc103, 0x1fff, 0x0029 },	/* TX_X4_MAC_CREDITGENCNT */
	{ 0xc111, 0x1e1c, 0x000c },	/* TX_X4_ENCODE_0 ENCODEMODE 3 = CL36 8b/10b */
	{ 0xc113, 0xc000, 0x0000 },	/* TX_X4_MISC SCR_MODE 0 = no scrambler */
	{ 0xc114, 0x0004, 0x0004 },	/* TX_X4_CL36_TX_0 DISABLE_PACKET_MISALIGN */
	{ 0xc130, 0xfff8, 0x2cb0 },	/* RX_X4_PCS_CONTROL_0: decode 5, deskew 4, desc2 5, byte delete 2 (CL36) */
	{ 0xc134, 0xbe07, 0x0003 },	/* RX_X4_DECODE_CONTROL_0 BLOCK_SYNC_MODE 3 (10b8b), no CL48 sync acquire */
	{ 0xc136, 0x0007, 0x0001 },	/* RX_X4_CL36_RX_0 CL36_EN */
	{ 0xc137, 0x007a, 0x003a },	/* RX_X4_PMA_CONTROL_0 OS_MODE 7 (8.25), RX_GBOX_AFRST_EN */
	{ 0xc181, 0x02c3, 0x0002 },	/* CL37_BASE_ABIL: SGMII_SPEED 2 (1G), no pause advertised (MAC pause not programmed) */
	{ 0xc184, 0xffff, 0x0000 },	/* OVER1G_ABIL_0: no BAM 2.5G */
	{ 0xc254, 0x3f00, 0x0200 },	/* CL72_MISC2_CONTROL SW_ACTUAL_SPEED 0x02 = 1000M (FV_adr_1000M) */
};

/* Stage 3 (speed_set 10G) rows that do not apply at 1G: SW_ACTUAL_SPEED, ANATX drive, TX FIR (OM txdrv 6GOS2 = stage 2 values). */
static bool tsc_1g_skip(const struct tsc_step *s)
{
	return s->op == TSC_RMW && !s->devad && (s->reg == 0xc254 || s->reg == 0xc017 || s->reg == 0xc252);
}

static int tsc_run(struct us48_cmic *u, const struct tsc_step *st, int n, u8 lane, u8 lane_mask,
		   const struct firmware *fw, bool g1)
{
	int i, j, ret = 0;
	u16 v = 0, m;

	for (i = 0; !ret && i < n; i++) {
		switch (st[i].op) {
		case TSC_RMW: {
			struct tsc_step s = st[i];

			if (g1 && tsc_1g_skip(&s))
				break;
			for (j = 0; g1 && j < ARRAY_SIZE(tsc_1g_patch); j++)
				if (s.reg == tsc_1g_patch[j].reg && s.mask == tsc_1g_patch[j].mask && s.scope == TSC_LANE)
					s.val = tsc_1g_patch[j].val;
			ret = tsc_rmw(u, &s, lane);
			break;
		}
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
 * Lane media mode (caller holds mdio): FIRMWARE_MODE 0xc260 = 2 (DAC, OM PHY_IF_CR) or 0 (optic, OM SFI = stage-0
 * value) inside a uC stop/resume, then the per-lane RX restart (13.3b).
 */
static int tsc_fw_mode_set(struct us48_cmic *u, u8 l, u16 mode)
{
	return tsc_uc_cmd(u, l, 0) ?: tsc_rmw_one(u, &(struct tsc_step){ .reg = 0xc260, .mask = 0xffff, .val = mode }, l) ?:
	       tsc_uc_cmd(u, l, 2) ?: us48_tsc_uc_ram_wr(u, l, 0x04d0 + 0x100 * l, 0x0d00) ?:
	       tsc_rmw_one(u, &(struct tsc_step){ .reg = 0xc137, .mask = BIT(0) }, l) ?:	/* RSTB_LANE 0 */
	       tsc_rmw_one(u, &(struct tsc_step){ .reg = 0xc21e, .mask = BIT(15), .val = BIT(15) }, l) ?:	/* RXSEQSTART */
	       us48_tsc_uc_ram_wr(u, l, 0x04d0 + 0x100 * l, 0x0e00);
}

static struct us48_port *xe_port(struct us48_cmic *u, int i)
{
	return u->port[i] && u->port[i]->cfg == &u->cfgs[i] ? u->port[i] : NULL;
}

/* Module type from the sfp caps (pcs_config): set the lane mode; one RX restart after the next link up (#87). */
int us48_tsc_fw_mode(struct us48_port *p, u16 mode)
{
	struct us48_cmic *u = p->u;
	int ret;

	mutex_lock(&u->mdio);
	ret = tsc_fw_mode_set(u, p->cfg->lane, mode);
	mutex_unlock(&u->mdio);
	netdev_info(p->ndev, "sfp: FIRMWARE_MODE %d -> %d (%s): %d\n", p->fw_mode, mode, mode ? "DAC" : "optic", ret);
	if (!ret) {
		p->fw_mode = mode;
		p->rx_tuned = false;
	}
	return ret;
}
EXPORT_SYMBOL_GPL(us48_tsc_fw_mode);

static int xlport_up(struct us48_cmic *u, u32 blk, u32 mode, u32 cfg);

/* Unit 1 internal-bus MDC divisor: chip reset value 0x10006 (regdb CMIC_RATE_ADJUST_INT_MDIO); cold init sets 40. #108 */
static uint tsc_mdc_div = 6;
module_param(tsc_mdc_div, uint, 0644);

int us48_tsc_init(struct us48_cmic *u, const struct firmware *fw)
{
	static const struct tsc_step mode = { .reg = TSC_SETUP_REG, .mask = TSC_SETUP_MODE_MASK | BIT(3), .val = 0 };
	int i, ret;

	if (!fw) {
		dev_err(u->dev, "us48-tsc.bin not loaded: 10G SerDes microcode missing, xe links stay down\n");
		return -ENOENT;
	}
	/* Unit 0 (SFP 1/2, #143): our cold init leaves XLPORT0 powered down (XGXS 0xe0); also the pwrsave power-up. */
	if (!u->modid) {
		ret = xlport_up(u, BLK_XLPORT, 0, 0);
		if (ret)
			return ret;
	}
	if (u->modid)
		iowrite32(1 << 16 | max(tsc_mdc_div, 2u), u->cmic + HW_CMIC0_RATE_ADJUST_INT_MDIO);
	mutex_lock(&u->mdio);
	/* PhyConfig_Mode: 4 x 1-lane ports (PORT_MODE_SEL 0, SINGLE_PORT_MODE 0). */
	ret = tsc_rmw_one(u, &mode, 0) ?: tsc_run(u, tsc_stage0, ARRAY_SIZE(tsc_stage0), 0, STAGE0_LANE_MASK, fw, false);
	for (i = 0; !ret && i < u->nports; i++) {
		const struct port_cfg *c = &u->cfgs[i];
		bool g1;

		if (c->copper)
			continue;
		g1 = us48_cfg_1g(c);
		ret = tsc_run(u, tsc_stage2, ARRAY_SIZE(tsc_stage2), c->lane, BIT(c->lane), fw, g1);
		ret = ret ?: tsc_run(u, tsc_stage3, ARRAY_SIZE(tsc_stage3), c->lane, BIT(c->lane), fw, g1);
		/* Board lane polarity (hwdb, #78): ANATX_ACONTROL0 TXPOL_FLIP b5, ANARX_ACONTROL0 RXPOL_FLIP b15; analog, live */
		if (!ret && c->tx_pol)
			ret = tsc_rmw_one(u, &(struct tsc_step){ .reg = 0xc011, .mask = BIT(5), .val = BIT(5) }, c->lane);
		if (!ret && c->rx_pol)
			ret = tsc_rmw_one(u, &(struct tsc_step){ .reg = 0xc021, .mask = BIT(15), .val = BIT(15) }, c->lane);
		/* FIRMWARE_MODE 0 (OM SFI, stage 0 value) until pcs_config sees a DAC in the cage */
		if (!ret) {
			dev_info(u->dev, "tsc %s: stage 2 + speed set (%s) done\n", c->name, g1 ? "1000BASE-X" : "10G SFI");
			if (xe_port(u, i)) {
				xe_port(u, i)->fw_mode = 0;
				xe_port(u, i)->an_1g = -1;
			}
		}
		ret = ret ?: (g1 ? us48_xe_mac_1g(u, c) : 0);
	}
	mutex_unlock(&u->mdio);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_tsc_init);
MODULE_FIRMWARE("us48-tsc.bin");

/*
 * ---- HiGig links between the chips (#57): logical 28/29 = phys 30/32 = XLPORT1 (SBUS block 6) ports 0/2, TSC1 at MIIM
 * bus 1 address 5, two dual-lane ports (lanes 0-1, 2-3) at 13000 Mb/s = SDK TSCMOD_SPD_12773_DXGXS, HiGig2.
 * Sources per value: docs/driver-internals.md section 15 (stock trace, stock-warm TSC1 capture, OpenBCM 6.5.16).
 */
#define BLK_XLPORT1		6
#define HG_NLANES		4
#define XGXS0_CTRL		0x2021400
#define XLPORT_MAC_CONTROL	0x2021000
#define XLPORT_SOFT_RESET	0x2020c00
#define XLPORT_MODE_REG		0x2020a00
#define XLPORT_ENABLE_REG	0x2020b00
#define XLPORT_POWER_SAVE	0x2020d00
#define XLPORT_CONFIG(l)	(0x20000 | (l))
#define XLPORT_TXFIFO_CTRL(l)	(0x20300 | (l))
#define XLMAC_CTRL(l)		(0x60000 | (l))
#define XLMAC_MODE(l)		(0x60100 | (l))
#define XLMAC_TX_CTRL(l)	(0x60400 | (l))
#define XLMAC_RX_CTRL(l)	(0x60600 | (l))
#define XLMAC_RX_MAX_SIZE(l)	(0x60800 | (l))
#define XLMAC_RX_LSS_CTRL(l)	(0x60a00 | (l))
#define XLMAC_RX_LSS_STATUS(l)	(0x60b00 | (l))
#define XLMAC_CLR_RX_LSS(l)	(0x60c00 | (l))
#define XLMAC_RPKT(l)		(0xb00 | (l))
#define XLMAC_TPKT(l)		(0x4c00 | (l))
/* SDK _soc_xgxs_reset_single_tsc sleep_usec 1100 (common/drv.c) - 10 %; 9 ms after power-up as the cold-init replay. */
#define XGXS_STEP_US		990
#define XGXS_PLL_US		9000

static int hg_wr(struct us48_cmic *u, u32 addr, u32 lo, u32 hi, int words)
{
	u32 d[2] = { lo, hi };

	return us48_sbus(u, 0x0d, BLK_XLPORT1, addr, d, words);
}

/* Stage-2 field values that differ for 12.73G HiGig2 dual-lane (stock-warm TSC1 = SDK cfg_seq for SPD_12773_DXGXS). */
static const struct { u16 reg, mask, val; } hg_patch[] = {
	{ 0xc111, 0x1e1c, 0x1e18 },	/* ENCODE_0: brcm 64b/66b (encode 6), HG2 enable/codec */
	{ 0xc113, 0xc000, 0x4000 },	/* TX_X4_MISC SCR_MODE 1 */
	{ 0xc130, 0xfff8, 0x5280 },	/* RX_X4_PCS_CONTROL_0 */
	{ 0xc136, 0x0007, 0x0006 },	/* RX_X4_CL36_RX_0 */
	{ 0xc134, 0xbe07, 0xbe05 },	/* RX_X4_DECODE_CONTROL_0 */
	{ 0xc253, 0x00f0, 0x00a0 },	/* CL72_MISC1_CONTROL TAP_DEFAULT_MUXSEL */
	{ 0xc254, 0x3f00, 0x1900 },	/* CL72_MISC2_CONTROL SW_ACTUAL_SPEED 0x19 */
	{ 0xc103, 0x1fff, 0x0002 },	/* TX_X4_MAC_CREDITGENCNT */
	{ 0xa003, 0x00ff, 0x0066 },	/* TX_X2_BRCM_MODE ACOL_SWAP_COUNT64B66B */
	{ 0xa023, 0x0400, 0x0000 },	/* RX_X2_MISC_0 CHK_END_EN */
};

/* Written before the analog reset release (stock-warm values; 0xc135 = cfg_seq SPD_12773_DXGXS 0x900f). */
static const struct tsc_step hg_extra[] = {
	{ TSC_RMW, TSC_LANE, 0xc135, 0xffff, 0x900f },	/* RX_X4_DECODE_CONTROL_1 */
	{ TSC_RMW, TSC_PAIR, 0xa031, 0xffff, 0xc920 },
	{ TSC_RMW, TSC_PAIR, 0xa081, 0xffff, 0x2000 },
};

static struct tsc_step *hg_build(const struct tsc_step *st, int n, int *out_n, bool stage0)
{
	struct tsc_step *s = kcalloc(n + ARRAY_SIZE(hg_extra), sizeof(*s), GFP_KERNEL);
	int i, j, k = 0;

	if (!s)
		return NULL;
	for (i = 0; i < n; i++) {
		if (!stage0 && st[i].op == TSC_LANE_RESET && !st[i].val)
			for (j = 0; j < ARRAY_SIZE(hg_extra); j++)
				s[k++] = hg_extra[j];
		s[k] = st[i];
		/* SETUP DEFAULT_PLL_MODE_AFE div42 (cfg_seq:1653, SPD_12773_DXGXS plldiv 42; stock 0x9000 = 0x73b0) */
		if (stage0 && s[k].op == TSC_RMW && s[k].reg == TSC_SETUP_REG && s[k].mask == 0x1f00)
			s[k].val = 0x1300;
		for (j = 0; !stage0 && s[k].op == TSC_RMW && j < ARRAY_SIZE(hg_patch); j++)
			if (s[k].reg == hg_patch[j].reg && s[k].mask == hg_patch[j].mask)
				s[k].val = hg_patch[j].val;
		k++;
	}
	*out_n = k;
	return s;
}

static int hg_tsc(struct us48_cmic *u, const struct firmware *fw)
{
	/* PORT_MODE_SEL 3 = 2 x 2-lane (driver-internals s9 step 1; stock 0x9000 [6:4] = 3) */
	static const struct tsc_step mode = { .reg = TSC_SETUP_REG, .mask = TSC_SETUP_MODE_MASK | BIT(3), .val = 0x30 };
	struct tsc_step *s0, *s2;
	int n0, n2, l, ret;

	s0 = hg_build(tsc_stage0, ARRAY_SIZE(tsc_stage0), &n0, true);
	s2 = hg_build(tsc_stage2, ARRAY_SIZE(tsc_stage2), &n2, false);
	if (!s0 || !s2) {
		kfree(s0);
		kfree(s2);
		return -ENOMEM;
	}
	mutex_lock(&u->mdio);
	u->tsc_phy = TSC1_PHY;
	ret = tsc_rmw_one(u, &mode, 0) ?: tsc_run(u, s0, n0, 0, 0xf, fw, false);
	/* lane map 0x9003 = 0x1b1b and RX_X1 DECODE_CONTROL_0 0x9220 = 0x0323 (stock-warm; cfg_seq:3217 for 12773) */
	ret = ret ?: tsc_rmw_one(u, &(struct tsc_step){ .reg = 0x9003, .mask = 0xffff, .val = 0x1b1b }, 0);
	ret = ret ?: tsc_rmw_one(u, &(struct tsc_step){ .reg = 0x9220, .mask = 0xffff, .val = 0x0323 }, 0);
	for (l = 0; !ret && l < HG_NLANES; l++)
		ret = tsc_run(u, s2, n2, l, BIT(l), fw, false);
	u->tsc_phy = 0;
	mutex_unlock(&u->mdio);
	kfree(s0);
	kfree(s2);
	return ret;
}

/*
 * XLPORT block power-up in stock trace order (init-phases A2/A8): XGXS reset release, MAC reset pulse, port mode.
 * HiGig (XLPORT1): CONFIG 0x400, MODE 0x1b (2 x dual); unit 0 SFP (XLPORT0): MODE 0 (quad), as stock's unit 0 trace.
 */
static int xlport_up(struct us48_cmic *u, u32 blk, u32 mode, u32 cfg)
{
	static const u32 xgxs[] = { 0xe0, 0xa0, 0xa0, 0xa4, 0xac, 0xae };
	u32 v;
	int i, ret = 0;

#define XW(a, x)	(v = (x), us48_sbus(u, 0x0d, blk, (a), &v, 1))
	ret = XW(XLPORT_POWER_SAVE, 0);		/* undo pwrsave (us48_cmic_pwr.c); xgxs[0] clears IDDQ, sets TSCCLK_EN */
	for (i = 0; !ret && i < ARRAY_SIZE(xgxs); i++) {
		ret = XW(XGXS0_CTRL, xgxs[i]);
		tsc_delay(i == 2 ? XGXS_PLL_US : XGXS_STEP_US);
	}
	ret = ret ?: XW(XLPORT_MAC_CONTROL, 1);
	udelay(9);		/* hurricane2.c soc_hurricane2_tsc_reset sal_udelay(10) - 10 % */
	ret = ret ?: XW(XLPORT_MAC_CONTROL, 0);
	if (cfg)
		ret = ret ?: XW(XLPORT_CONFIG(0), cfg) ?: XW(XLPORT_CONFIG(2), cfg);
	ret = ret ?: XW(XLPORT_SOFT_RESET, 5) ?: XW(XLPORT_MODE_REG, mode) ?: XW(XLPORT_SOFT_RESET, 0) ?:
	      XW(XLPORT_ENABLE_REG, 5);
#undef XW
	return ret;
}

/* EPIPE EGR_PORT_CREDIT_RESET[phys]: EP->port credits; without the pulse the MMU queue never drains (#57). */
#define EGR_PORT_CREDIT_RESET(phys)	(0x340c0000 + (phys))
#define HG_PHYS(l)		(30 + (l))	/* XLPORT1 lane 0/2 = phys 30/32 */

static int ep_credit(struct us48_cmic *u, u8 l, u32 v)
{
	return us48_sbus(u, 0x09, BLK_EPIPE, EGR_PORT_CREDIT_RESET(HG_PHYS(l)), &v, 1);
}

/* Stock trace order (unit 0 ords 11919-11922; SDK xlmac.c "Reset EP credit before de-assert SOFT_RESET"). */
static int hg_mac(struct us48_cmic *u, u8 l)
{
	return hg_wr(u, XLPORT_ENABLE_REG, 5 & ~BIT(l), 0, 1) ?: ep_credit(u, l, 1) ?:
	       hg_wr(u, XLPORT_TXFIFO_CTRL(l), 7, 0, 1) ?: ep_credit(u, l, 0) ?: hg_wr(u, XLPORT_TXFIFO_CTRL(l), 4, 0, 1) ?:
	       hg_wr(u, XLPORT_ENABLE_REG, 5, 0, 1) ?:
	       hg_wr(u, XLMAC_CTRL(l), 0x1800, 0, 2) ?: hg_wr(u, XLMAC_RX_CTRL(l), 0x400, 0, 2) ?:
	       hg_wr(u, XLMAC_TX_CTRL(l), 0x8802, 0x10, 2) ?: hg_wr(u, XLMAC_RX_MAX_SIZE(l), 0x3fe8, 0, 2) ?:
	       hg_wr(u, XLMAC_MODE(l), 0x42, 0, 2) ?:		/* HDR_MODE 2 = HiGig2, SPEED_MODE 4 */
	       hg_wr(u, XLMAC_RX_LSS_CTRL(l), 0x70, 0, 2) ?:
	       hg_wr(u, XLPORT_CONFIG(l), 0xe00, 0, 1) ?:		/* HIGIG_MODE + HIGIG2_MODE + HONOR_PAUSE_FOR_E2E */
	       hg_wr(u, XLMAC_CTRL(l), 0x1803, 0, 2) ?:		/* TX_EN RX_EN, SOFT_RESET 0, SW_LINK_STATUS */
	       hg_wr(u, XLMAC_CLR_RX_LSS(l), 3, 0, 2) ?: hg_wr(u, XLMAC_CLR_RX_LSS(l), 0, 0, 2);
}

/*
 * ---- 1000BASE-X on unit 0 xe51/xe52 (#78). Sources: OM bcmi_tsc_xgxs_drv.c (_tsc_autoneg_control_set, _tsc_xgxs_autoneg_set,
 * speed_set 1000M), stock unit 0 trace docs/init-phases-u0.md B1, SDK xlmac.c mac_xl_speed_set. docs/driver-internals.md 9.4.
 */
#define XLMAC_SPEED_MODE_MASK	GENMASK(6, 4)	/* XLMAC_MODE SPEED_MODE; init-phases-u0 XLMAC_MODE 0x20 = 2, 0x40 = 4 */
#define XLMAC_SPEED_1000	2		/* SDK xlmac.c:55 SOC_XLMAC_SPEED_1000 */
#define XLMAC_RX_LSS_1G		0x73		/* stock unit 0 ord 57787: 0x70 + LOCAL/REMOTE_FAULT_DISABLE (SDK xlmac.c:1243-1248) */

/* XLMAC of the lane to 1G, MAC parked (probe / power-up, before port_enable): SPEED_MODE 2, LSS as stock. */
int us48_xe_mac_1g(struct us48_cmic *u, const struct port_cfg *c)
{
	u32 v[2] = {};
	int ret = us48_sbus(u, 0x0b, BLK_XLPORT, XLMAC_MODE(c->lane), v, 2);

	v[0] = (v[0] & ~XLMAC_SPEED_MODE_MASK) | FIELD_PREP(XLMAC_SPEED_MODE_MASK, XLMAC_SPEED_1000);
	ret = ret ?: us48_sbus(u, 0x0d, BLK_XLPORT, XLMAC_MODE(c->lane), v, 2);
	v[0] = XLMAC_RX_LSS_1G;
	v[1] = 0;
	ret = ret ?: us48_sbus(u, 0x0d, BLK_XLPORT, XLMAC_RX_LSS_CTRL(c->lane), v, 2);
	if (ret)
		dev_err(u->dev, "%s: XLMAC 1G mode failed: %d\n", c->name, ret);
	return ret;
}

#define ENABLES_REG		0xc180
#define  EN_AN_SETUP		BIT(14)
#define  EN_AN_TYPES		GENMASK(11, 6)	/* CL37 BAM, CL73 BAM/HPAM/CL73, CL37 SGMII, CL37 (OM: all but CL37 cleared) */
#define  EN_CL37		BIT(6)
#define  EN_CL37_RESTART	BIT(1)
#define CL37_BASE_ABIL_REG	0xc181
#define CONTROLS_REG		0xc188
#define  CTRL_PD_KX_EN		BIT(1)
#define PMA_CONTROL_REG		0xc137
#define  PMA_OVERRIDE_OS_MODE	BIT(7)

static int tsc_one(struct us48_cmic *u, u8 lane, u16 reg, u16 mask, u16 val)
{
	return tsc_rmw_one(u, &(struct tsc_step){ .reg = reg, .mask = mask, .val = val }, lane);
}

/*
 * CL37 AN on/off for one 1G lane (OM autoneg_control_set CL37_10G = 1000BASE-X at PLL div66: OS mode overridden to 8.25 so
 * the base page is received at 1.25 Gbaud; CL37 base ability FD only; PD_KX_EN so a forced-1G partner still links).
 */
int us48_tsc_1g_an(struct us48_port *p, bool an)
{
	struct us48_cmic *u = p->u;
	u8 l = p->cfg->lane;
	int ret;

	mutex_lock(&u->mdio);
	if (an)
		ret = tsc_one(u, l, PMA_CONTROL_REG, PMA_OVERRIDE_OS_MODE, PMA_OVERRIDE_OS_MODE) ?:
		      tsc_one(u, l, ENABLES_REG, EN_AN_SETUP | EN_AN_TYPES | GENMASK(13, 12) | EN_CL37_RESTART, EN_AN_SETUP) ?:
		      tsc_one(u, l, CL37_BASE_ABIL_REG, BIT(8) | BIT(4) | BIT(2), BIT(4)) ?:
		      tsc_one(u, l, CONTROLS_REG, CTRL_PD_KX_EN, CTRL_PD_KX_EN) ?:
		      tsc_one(u, l, ENABLES_REG, EN_AN_TYPES | EN_CL37_RESTART, EN_CL37 | EN_CL37_RESTART) ?:
		      tsc_one(u, l, ENABLES_REG, EN_CL37_RESTART, 0);
	else
		ret = tsc_one(u, l, PMA_CONTROL_REG, PMA_OVERRIDE_OS_MODE, 0) ?:
		      tsc_one(u, l, ENABLES_REG, EN_AN_TYPES | EN_CL37_RESTART, 0) ?:
		      tsc_one(u, l, CONTROLS_REG, CTRL_PD_KX_EN, 0);
	mutex_unlock(&u->mdio);
	netdev_info(p->ndev, "1000BASE-X: CL37 AN %s: %d\n", an ? "on" : "off (forced 1G FD)", ret);
	if (!ret)
		p->an_1g = an;
	return ret;
}

void us48_tsc_1g_an_restart(struct us48_port *p)
{
	struct us48_cmic *u = p->u;

	mutex_lock(&u->mdio);
	tsc_one(u, p->cfg->lane, ENABLES_REG, EN_CL37_RESTART, EN_CL37_RESTART);
	tsc_one(u, p->cfg->lane, ENABLES_REG, EN_CL37_RESTART, 0);
	mutex_unlock(&u->mdio);
}

/* c154 PCS_LIVE (b1 link), c1ac AN_MISC_STATUS (b15 AN_COMPLETE), c198 LP_BASE_PAGE1 (raw CL37 base page word). */
int us48_tsc_1g_status(struct us48_port *p, u16 *live, u16 *an_misc, u16 *lp)
{
	struct us48_cmic *u = p->u;
	int ret;

	mutex_lock(&u->mdio);
	ret = us48_tsc_wr(u, TSC_AER, p->cfg->lane) ?: us48_tsc_rd(u, 0xc154, live) ?: us48_tsc_rd(u, 0xc1ac, an_misc) ?:
	      us48_tsc_rd(u, 0xc198, lp);
	mutex_unlock(&u->mdio);
	return ret;
}

int us48_higig_up(struct us48_cmic *u, const struct firmware *fw)
{
	static const u16 en[][2] = {
		{ 0xc138, 0x0009 },			/* RX_SIGDET: no forced signal-detect */
		{ 0xc100, 0x0000 }, { 0xc100, 0x4021 },	/* TX credits re-armed */
		{ 0xc017, 0x0990 },			/* ANATX_DRIVER: ELEC_IDLE 0 */
		{ 0xc01a, 0x0000 },			/* ANATX_CONTROL6: TX enabled */
	};
	/* Both lanes of a port at once: AER 4 = lanes 0-1, 5 = lanes 2-3 (SDK tscmod_phyreg.c:509-516 dual broadcast). */
	static const u16 pair[][2] = {
		{ 0xc113, 0x41c8 }, { 0xc113, 0x41cb },	/* TX lane reset pulse, after credits */
		{ 0xc137, 0x0001 },			/* RX lane reset release */
	};
	int i, l, ret;

	if (!fw)
		return -ENOENT;
	ret = xlport_up(u, BLK_XLPORT1, 0x1b, 0x400) ?: hg_tsc(u, fw) ?: hg_mac(u, 0) ?: hg_mac(u, 2);
	/* Lane enable after the MAC, port_enable order (driver-internals 13.5/13.7), values = stock-warm TSC1 per lane. */
	mutex_lock(&u->mdio);
	u->tsc_phy = TSC1_PHY;
	for (l = 0; !ret && l < HG_NLANES; l++) {
		ret = tsc_set_aer(u, l);
		for (i = 0; !ret && i < ARRAY_SIZE(en); i++)
			ret = us48_tsc_wr(u, en[i][0], en[i][1]);
	}
	for (l = 4; !ret && l <= 5; l++) {
		ret = tsc_set_aer(u, l);
		for (i = 0; !ret && i < ARRAY_SIZE(pair); i++)
			ret = us48_tsc_wr(u, pair[i][0], pair[i][1]);
	}
	u->tsc_phy = 0;
	mutex_unlock(&u->mdio);
	dev_info(u->dev, "higig: bring-up %s (%d)\n", ret ? "FAILED" : "done", ret);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_higig_up);

void us48_higig_status(struct us48_cmic *u)
{
	u16 pll = 0, crc = 0, pmd[HG_NLANES] = {}, pcs[HG_NLANES] = {};
	u32 ctrl[2], lss[2], rp[2], tp[2], cfg;
	int l;

	mutex_lock(&u->mdio);
	u->tsc_phy = TSC1_PHY;
	tsc_set_aer(u, 0);
	us48_tsc_rd(u, TSC_PLLSTAT_REG, &pll);
	us48_tsc_rd(u, TSC_CRC_REG, &crc);
	for (l = 0; l < HG_NLANES; l++)
		if (tsc_set_aer(u, l) || us48_tsc_rd(u, 0xc150, &pmd[l]) || us48_tsc_rd(u, 0xc154, &pcs[l]))
			pmd[l] = pcs[l] = 0xffff;
	u->tsc_phy = 0;
	mutex_unlock(&u->mdio);
	dev_info(u->dev, "higig tsc1: PLL %04x CRC %04x PMD_LIVE %04x %04x %04x %04x PCS_LIVE %04x %04x %04x %04x\n",
		 pll, crc, pmd[0], pmd[1], pmd[2], pmd[3], pcs[0], pcs[1], pcs[2], pcs[3]);
	for (l = 0; l < 3; l += 2) {
		hg_wr(u, XLMAC_CLR_RX_LSS(l), 3, 0, 2);	/* LSS is latched: clear, then read the current state */
		hg_wr(u, XLMAC_CLR_RX_LSS(l), 0, 0, 2);
		if (us48_sbus(u, 0x0b, BLK_XLPORT1, XLMAC_CTRL(l), ctrl, 2) ||
		    us48_sbus(u, 0x0b, BLK_XLPORT1, XLMAC_RX_LSS_STATUS(l), lss, 2) ||
		    us48_sbus(u, 0x0b, BLK_XLPORT1, XLMAC_RPKT(l), rp, 2) ||
		    us48_sbus(u, 0x0b, BLK_XLPORT1, XLMAC_TPKT(l), tp, 2) ||
		    us48_sbus(u, 0x0b, BLK_XLPORT1, XLPORT_CONFIG(l), &cfg, 1))
			continue;
		dev_info(u->dev, "higig port %d: XLPORT_CONFIG %x XLMAC_CTRL %x LSS %x RPKT %u TPKT %u\n",
			 28 + l / 2, cfg, ctrl[0], lss[0], rp[0], tp[0]);
	}
}
EXPORT_SYMBOL_GPL(us48_higig_status);
