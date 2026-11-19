// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic: switch ports of the UniFi US-48-500W: copper ge1-ge24 (unit 0, AXI CMIC), ge25-ge48 and SFP+ xe49/xe50
 * (unit 1, PCIe; us48_cmic_xe.ko). Port tables: hwdb/gen/us48_ports.h.
 * One CMIC DMA ring pair per unit: RX is demuxed on the source port, TX goes straight to a port through the module
 * header. Each port sits in its own private VLAN with the CPU only, so the chip never forwards between ports or into
 * the HiGig links, unless it joins a Linux bridge (switchdev offload, us48_cmic_sw.c, docs/switchdev.md).
 * Copper bring-up at the first ndo_open. Polled. docs/driver-internals.md 1, 4, 5, 12, 13.5, 14.
 */
#include <linux/delay.h>
#include <linux/etherdevice.h>
#include <linux/hrtimer.h>
#include <linux/dma-mapping.h>
#include <linux/if_vlan.h>
#include <linux/iopoll.h>
#include <linux/firmware.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/pci.h>
#include <linux/platform_device.h>
#include <linux/spinlock.h>
#include <linux/unaligned.h>
#include <linux/workqueue.h>

#include "us48_cmic.h"
#include "bcm_cmic.h"
#include "us48_hwdb.h"
#include "us48_ports.h"

/* Port tables from hwdb/board/ports.toml (scripts/hwdb_gen.py ports_c): name, vid, logical port, GPORT block/lane, PHY. */
const struct port_cfg us48_unit1_cfgs[] = { US48_PORTS_UNIT1 };
/* Unit 0 (host SoC, AXI CMIC): front 1 = ge1 = management. */
static const struct port_cfg unit0_cfgs[] = { US48_PORTS_UNIT0 };
const int us48_unit1_nports = ARRAY_SIZE(us48_unit1_cfgs);
EXPORT_SYMBOL_GPL(us48_unit1_cfgs);
EXPORT_SYMBOL_GPL(us48_unit1_nports);

/* Log the module header + status words of the next N received frames (RX bring-up, #4). */
static uint rx_dump;
module_param(rx_dump, uint, 0644);

static u32 rd(struct us48_cmic *u, u32 off) { return ioread32(u->cmic + off); }
static void wr(struct us48_cmic *u, u32 off, u32 v) { iowrite32(v, u->cmic + off); }

/*
 * S-channel (section 1): every op of a unit runs under u->schan_seq, then the owner's lock (unit 1: us48_pci_schan, which
 * us48_pvtmon shares; unit 0: CMC0 has no other kernel user). Lock order: link_lock -> mdio -> schan_seq -> owner (#125).
 */
static int schan_exec(struct bcm_cmic_unit *cu, us48_schan_fn fn, void *ctx, u32 op, u32 blk, u32 addr, u32 *data,
		      int words, u32 *ack)
{
	return fn ? fn(ctx, op, blk, addr, data, words, ack) : bcm_cmic_schan_raw(cu, op, blk, addr, data, words, ack);
}

/* bcm_cmic_unit hook of a probed unit: the shared core's ops take the same locks as sbus(). */
static int cu_schan_locked(struct bcm_cmic_unit *cu, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	struct us48_cmic *u = cu->priv;
	int ret;

	mutex_lock(&u->schan_seq);
	ret = schan_exec(cu, u->schan_fn, u->schan_ctx, op, blk, addr, data, words, ack);
	mutex_unlock(&u->schan_seq);
	return ret;
}

/* SBUS op: reg read 0x0b / write 0x0d, mem read 0x07 / write 0x09, insert 0x24, delete 0x26. Section 1. */
static int sbus(struct us48_cmic *u, u32 op, u32 blk, u32 addr, u32 *data, int words)
{
	return bcm_cmic_schan_op(&u->cu, op, blk, addr, data, words, NULL);
}

static void ebit(u32 *e, int n) { e[n / 32] |= BIT(n % 32); }

static void set_even_parity(u32 *e, int par)
{
	int i, ones = 0;

	e[par / 32] &= ~BIT(par % 32);
	for (i = 0; i <= par / 32; i++)
		ones += hweight32(e[i]);
	if (ones & 1)
		e[par / 32] |= BIT(par % 32);
}

/* Set bits (bit numbers into the entry) and keep EVEN_PARITY at bit par consistent. */
static void smem_set_bits(struct us48_cmic *u, u32 blk, u32 addr, int words, int par, const int *bits, int n)
{
	u32 e[4];
	int i;

	if (sbus(u, 0x07, blk, addr, e, words))
		return;
	for (i = 0; i < n; i++)
		ebit(e, bits[i]);
	set_even_parity(e, par);
	sbus(u, 0x09, blk, addr, e, words);
}

int us48_sbus(struct us48_cmic *u, u32 op, u32 blk, u32 addr, u32 *data, int words)
{
	return sbus(u, op, blk, addr, data, words);
}
EXPORT_SYMBOL_GPL(us48_sbus);

/* ---- TSC SerDes over MIIM (clause 45, devad 0) ---- */

int us48_tsc_wr(struct us48_cmic *u, u32 reg, u16 val)
{
	u32 st;
	int ret;

	wr(u, MIIM_CTRL, 0);
	wr(u, MIIM_PARAM, MIIM_TSC_C45 | (u->tsc_phy ?: TSC0_PHY) << 16 | val);
	wr(u, MIIM_ADDRESS, reg);
	wr(u, MIIM_CTRL, BIT(0));			/* WR_START */
	ret = readl_poll_timeout_atomic(u->cmic + MIIM_STAT, st, st & BIT(0), 1, MIIM_TIMEOUT_US);
	wr(u, MIIM_CTRL, 0);
	if (ret)
		dev_err(u->dev, "tsc write %04x <- %04x: no OPN_DONE in %u us\n", reg, val, MIIM_TIMEOUT_US);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_tsc_wr);

int us48_tsc_rd(struct us48_cmic *u, u32 reg, u16 *val)
{
	u32 st;
	int ret;

	wr(u, MIIM_CTRL, 0);
	wr(u, MIIM_PARAM, MIIM_TSC_C45 | (u->tsc_phy ?: TSC0_PHY) << 16);
	wr(u, MIIM_ADDRESS, reg);
	wr(u, MIIM_CTRL, BIT(1));			/* RD_START */
	ret = readl_poll_timeout_atomic(u->cmic + MIIM_STAT, st, st & BIT(0), 1, MIIM_TIMEOUT_US);
	if (!ret)
		*val = rd(u, MIIM_READ_DATA) & 0xffff;
	wr(u, MIIM_CTRL, 0);
	return ret;
}
EXPORT_SYMBOL_GPL(us48_tsc_rd);

static uint phy_dbg;
module_param(phy_dbg, uint, 0644);

/* Log the read-only TSC / XLMAC status of every xe port at probe (cheap: ~30 MIIM + 8 SBUS reads per port). */
static uint tsc_log = 1;
module_param(tsc_log, uint, 0644);

struct tsc_reg {
	const char *n;
	u16 reg;
};
/* Core registers, AER 0 (driver-internals 13.2 checks 1-2). */
static const struct tsc_reg tsc_core[] = {
	{ "SERDESID", 0x900e }, { "ANAPLL", 0x9010 }, { "VERSION", 0xf010 }, { "CRC", 0xf015 },
	{ "DLSTAT", 0xffc5 }, { "UCCMD", 0xffc2 },
};
/* Per-lane registers, AER = lane (checks 3-8). PMD_LATCH / PCS_LATCH are latched: this read clears them. */
static const struct tsc_reg tsc_lane[] = {
	{ "UC_CTRL", 0xc20e }, { "PMD_LIVE", 0xc150 }, { "PMD_LATCH", 0xc151 }, { "ANARX1", 0xc02b },
	{ "SIGDET", 0xc138 }, { "PMACTL0", 0xc137 }, { "PCS_LIVE", 0xc154 }, { "PCS_LATCH0", 0xc153 },
	{ "CL49FSM", 0xc145 }, { "ANATX_DRV", 0xc017 }, { "ANATX6", 0xc01a }, { "TXMISC", 0xc113 },
	{ "CRED0", 0xc100 }, { "CRED1", 0xc101 }, { "TXPCS", 0xc121 },
};

static void tsc_dump_one(struct us48_cmic *u, const struct port_cfg *c)
{
	char b[200];
	u16 v;
	u32 d[2];
	int i, n = 0;

	mutex_lock(&u->mdio);
	if (us48_tsc_wr(u, 0xffde, 0))
		goto out;
	for (i = 0; i < ARRAY_SIZE(tsc_core); i++)
		n += scnprintf(b + n, sizeof(b) - n, " %s=%04x", tsc_core[i].n, us48_tsc_rd(u, tsc_core[i].reg, &v) ? 0xffff : v);
	dev_info(u->dev, "tsc %s core:%s\n", c->name, b);
	if (us48_tsc_wr(u, 0xffde, c->lane))
		goto out;
	n = 0;
	for (i = 0; i < ARRAY_SIZE(tsc_lane); i++) {
		n += scnprintf(b + n, sizeof(b) - n, " %s=%04x", tsc_lane[i].n, us48_tsc_rd(u, tsc_lane[i].reg, &v) ? 0xffff : v);
		if (i == 6 || i == 11) {
			dev_info(u->dev, "tsc %s lane %d:%s\n", c->name, c->lane, b);
			n = 0;
		}
	}
	dev_info(u->dev, "tsc %s lane %d:%s\n", c->name, c->lane, b);
out:
	mutex_unlock(&u->mdio);
	/* XLMAC / XLPORT, block 5, lane in the low address byte. */
	n = 0;
	for (i = 0; i < 4; i++) {
		static const struct { const char *n; u32 a; int w; } m[] = {
			{ "XLMAC_CTRL", 0x60000, 2 }, { "RX_LSS", 0x60b00, 2 }, { "MODE", 0x60100, 2 }, { "TX_CTRL", 0x60400, 2 },
		};

		d[0] = d[1] = 0;
		n += scnprintf(b + n, sizeof(b) - n, " %s=%08x", m[i].n,
			       sbus(u, 0x0b, BLK_XLPORT, m[i].a | c->lane, d, m[i].w) ? 0xffffffff : d[0]);
	}
	d[0] = 0;
	n += scnprintf(b + n, sizeof(b) - n, " ENABLE=%x", sbus(u, 0x0b, BLK_XLPORT, 0x2020b00, d, 1) ? 0xf : d[0]);
	d[0] = 0;
	n += scnprintf(b + n, sizeof(b) - n, " FAULT=%x", sbus(u, 0x0b, BLK_XLPORT, 0x20600 | c->lane, d, 1) ? 0xf : d[0]);
	dev_info(u->dev, "tsc %s mac:%s\n", c->name, b);
	n = 0;
	for (i = 0; i < 3; i++) {
		static const struct { const char *n; u32 a; } m[] = { { "RPKT", 0xb00 }, { "TPKT", 0x4c00 }, { "RFCS", 0xf00 } };

		d[0] = d[1] = 0;
		n += scnprintf(b + n, sizeof(b) - n, " %s=%u", m[i].n,
			       sbus(u, 0x0b, BLK_XLPORT, m[i].a | c->lane, d, 2) ? 0 : d[0]);
	}
	dev_info(u->dev, "tsc %s mib:%s\n", c->name, b);
}

void us48_tsc_dump(struct us48_cmic *u)
{
	int i;

	for (i = 0; i < u->nports; i++)
		if (!u->cfgs[i].copper)
			tsc_dump_one(u, &u->cfgs[i]);
}
EXPORT_SYMBOL_GPL(us48_tsc_dump);

/* Log TSC registers lo..hi (AER = lane) of one port: lane A/B comparison (#46). */
/* lo/hi = devad << 16 | reg16 (devad 1 = PMD regs); aer >= 0 overrides the port's lane. */
void us48_tsc_range(struct us48_cmic *u, const struct port_cfg *c, u32 lo, u32 hi, int aer)
{
	char b[160];
	u16 v;
	u32 r;
	int n = 0;

	mutex_lock(&u->mdio);
	us48_tsc_wr(u, 0xffde, aer >= 0 ? aer : c->lane);
	for (r = lo; r <= hi; r++) {
		n += scnprintf(b + n, sizeof(b) - n, " %x=%04x", r, us48_tsc_rd(u, r, &v) ? 0xffff : v);
		if (n > 100 || r == hi) {
			dev_info(u->dev, "tsc %s range:%s\n", c->name, b);
			n = 0;
		}
	}
	mutex_unlock(&u->mdio);
}
EXPORT_SYMBOL_GPL(us48_tsc_range);

/* RMW of a lane register: AER first (it is sticky per PHY address). Caller holds u->mdio. */
static int tsc_rmw(struct us48_cmic *u, u8 lane, u16 reg, u16 clr, u16 set)
{
	u16 v;

	if (us48_tsc_wr(u, 0xffde, lane) || us48_tsc_rd(u, reg, &v))
		return -EIO;
	return us48_tsc_wr(u, reg, (v & ~clr) | set);
}

/* uC mailbox proxy write of one RAM word (OM phy_tsc_iblk_proxy_write, driver-internals 13.3b). Caller holds u->mdio. */
int us48_tsc_uc_ram_wr(struct us48_cmic *u, u8 lane, u16 ram, u16 data)
{
	ktime_t t0 = ktime_get();
	u16 v = 0;

	if (us48_tsc_wr(u, 0xffde, 0) || us48_tsc_wr(u, 0xffc8, 0) || us48_tsc_wr(u, 0xffc0, ram) || us48_tsc_wr(u, 0xffc1, lane) ||
	    us48_tsc_wr(u, 0xffc3, data) || us48_tsc_wr(u, 0xffc8, 0x0019))	/* REQ | WR | RAM */
		return -EIO;
	/* uC services a mailbox request in its main loop (not measured): 10 ms first-run bound, elapsed logged. */
	while (ktime_us_delta(ktime_get(), t0) < 10000) {
		if (us48_tsc_rd(u, 0xffc8, &v))
			return -EIO;
		if (v & BIT(15))
			break;
		usleep_range(100, 200);
	}
	dev_info(u->dev, "tsc uC mailbox ram %04x <- %04x lane %d: %s after %lld us (mailbox %04x)\n", ram, data, lane,
		 (v & BIT(15)) ? "done" : "TIMEOUT", ktime_us_delta(ktime_get(), t0), v);
	us48_tsc_wr(u, 0xffc8, 0);
	return (v & BIT(15)) ? 0 : -ETIMEDOUT;
}
EXPORT_SYMBOL_GPL(us48_tsc_uc_ram_wr);

/*
 * Per-lane RX restart without a firmware download (driver-internals 13.3b, from OM _tsc_serdes_stop /
 * _tsc_uc_rx_lane_control; not verified on hardware): use when signal is present (PMD_LIVE b1) but the PCS never locks.
 */
int us48_tsc_rx_restart(struct us48_cmic *u, const struct port_cfg *c)
{
	u16 ram = 0x04d0 + 0x100 * c->lane, pmd = 0, pcs = 0;
	ktime_t t0;
	int ret;

	mutex_lock(&u->mdio);
	ret = tsc_rmw(u, c->lane, 0xc138, BIT(2) | BIT(1), 0);
	ret = ret ?: us48_tsc_uc_ram_wr(u, c->lane, ram, 0x0d00);		/* uC lane stop */
	ret = ret ?: tsc_rmw(u, c->lane, 0xc137, BIT(0), 0);		/* RSTB_LANE = 0 */
	ret = ret ?: tsc_rmw(u, c->lane, 0xc21e, 0, BIT(15));		/* RXSEQSTART */
	ret = ret ?: us48_tsc_uc_ram_wr(u, c->lane, ram, 0x0e00);		/* uC lane start (forced SFI, non-AN) */
	if (ret) {
		mutex_unlock(&u->mdio);
		dev_err(u->dev, "%s: rx restart failed: %d\n", c->name, ret);
		return ret;
	}
	/* RX adaptation has no OM bound: 1 s first-run backstop, first-lock time logged for tightening. */
	t0 = ktime_get();
	do {
		us48_tsc_wr(u, 0xffde, c->lane);
		if (!us48_tsc_rd(u, 0xc150, &pmd) && !us48_tsc_rd(u, 0xc154, &pcs) && (pmd & BIT(3)) && (pcs & BIT(1)))
			break;
		mutex_unlock(&u->mdio);
		msleep(10);
		mutex_lock(&u->mdio);
	} while (ktime_ms_delta(ktime_get(), t0) < 1000);
	if (!(pmd & BIT(3)))
		tsc_rmw(u, c->lane, 0xc137, 0, BIT(0));			/* RSTB_LANE by host if the uC did not */
	mutex_unlock(&u->mdio);
	dev_info(u->dev, "%s: rx restart: PMD_LIVE %04x PCS_LIVE %04x after %lld ms\n", c->name, pmd, pcs,
		 ktime_ms_delta(ktime_get(), t0));
	return 0;
}
EXPORT_SYMBOL_GPL(us48_tsc_rx_restart);

static struct us48_cmic *dbg_u;
static const struct port_cfg *dbg_cfg;
static DEFINE_MUTEX(dbg_lock);	/* the mib param vs unbind */

/* echo 1 > /sys/module/us48_cmic/parameters/mib: log the GPORT RX counters of the first copper port (#56). */
static int mib_set(const char *val, const struct kernel_param *kp)
{
	static const struct { const char *n; u32 a; } r[] = {
		{ "GRPKT", 0x1b00 }, { "GRBYT", 0x1c00 }, { "GRFCS", 0x0 }, { "GRUND", 0xc00 },
		{ "GROVR", 0x800 }, { "GRFRG", 0xd00 }, { "GRXPF", 0x200 }, { "GTPKT", 0x3800 },
	};
	u32 v[2];
	int i;

	mutex_lock(&dbg_lock);
	if (!dbg_u || !dbg_cfg) {
		mutex_unlock(&dbg_lock);
		return -ENODEV;
	}
	for (i = 0; i < ARRAY_SIZE(r); i++) {
		v[0] = v[1] = 0;
		if (sbus(dbg_u, 0x0b, dbg_cfg->blk, r[i].a | dbg_cfg->lane, v, 2))
			dev_info(dbg_u->dev, "mib %s: read failed\n", r[i].n);
		else
			dev_info(dbg_u->dev, "mib %s = %u (hi %u)\n", r[i].n, v[0], v[1]);
	}
	/* CPU port (MMU port 0): addr = base + (cos << 8) + port, validated vs stock trace by scripts/mmu_addr.py (#56) */
	for (i = 0; i < 8; i++) {
		u32 d[2] = { 0, 0 }, h[2] = { 0, 0 }, c[2] = { 0, 0 };

		sbus(dbg_u, 0x0b, 0xc, 0x101000 + (i << 8), d, 1);
		sbus(dbg_u, 0x0b, 0xc, 0x800 + (i << 8), h, 1);
		sbus(dbg_u, 0x0b, 0xc, 0xed00 + (i << 8), c, 1);
		dev_info(dbg_u->dev, "mib cpu cos%d: EGRDROPPKT %u HOLPKTSETLIMIT %u CELLMAXLIMIT %08x\n", i, d[0],
			 h[0] & 0x7ff, c[0]);
	}
	mutex_unlock(&dbg_lock);
	return 0;
}

static const struct kernel_param_ops mib_ops = { .set = mib_set };
module_param_cb(mib, &mib_ops, NULL, 0200);

/* ---- schan param: host-tool S-channel access under the driver's locks (#125; replaces devmem S-channel) ---- */

static struct us48_cmic *schan_units[2];	/* by unit = modid; set while probed */
static DEFINE_MUTEX(schan_lock);		/* schan_units, schan_out; unbind waits for a running request */
static char schan_out[PAGE_SIZE];		/* result of the last request */
static size_t schan_len;
#define SCHAN_LINE_MAX	400			/* longest result line: header + 2 x 20 words */

static void schan_unit_set(struct us48_cmic *u, bool on)
{
	mutex_lock(&schan_lock);
	if (on)
		schan_units[u->modid] = u;
	else if (schan_units[u->modid] == u)
		schan_units[u->modid] = NULL;
	mutex_unlock(&schan_lock);
}

static void schan_put(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	schan_len += vscnprintf(schan_out + schan_len, sizeof(schan_out) - schan_len, fmt, ap);
	va_end(ap);
}

static void schan_words(const char *tag, const u32 *d, int n)
{
	int i;

	schan_put(" %s", tag);
	for (i = 0; i < n; i++)
		schan_put(" %08x", d[i]);
}

/* Words as hex text for the log line of a write. */
static char *words_str(char *b, size_t sz, const u32 *d, int n)
{
	int i, k = 0;

	b[0] = 0;
	for (i = 0; i < n; i++)
		k += scnprintf(b + k, sz - k, "%s%08x", i ? " " : "", d[i]);
	return b;
}

/*
 * One request line "<unit> <op> <blk> <addr> <words> [data...]", op: rreg rmem (read), wreg wmem (read, write, read back),
 * lookup delete (L2-style key = data). Runs under that unit's schan_seq: no driver op in between. Writes are logged.
 */
static void schan_line(char *ln)
{
	static const struct { const char *n; u32 op, rd; } ops[] = {
		{ "rreg", BCM_SCHAN_READ_REG }, { "rmem", BCM_SCHAN_READ_MEM },
		{ "wreg", BCM_SCHAN_WRITE_REG, BCM_SCHAN_READ_REG }, { "wmem", BCM_SCHAN_WRITE_MEM, BCM_SCHAN_READ_MEM },
		{ "lookup", BCM_SCHAN_TABLE_LOOKUP }, { "delete", BCM_SCHAN_TABLE_DELETE },
	};
	u32 data[BCM_CMIC_SCHAN_MSG_WORDS] = { }, old[BCM_CMIC_SCHAN_MSG_WORDS] = { }, back[BCM_CMIC_SCHAN_MSG_WORDS] = { };
	u32 ack = 0;
	unsigned int unit, blk, addr, words;
	char name[8];
	int i, k, n, pos, ret;
	struct us48_cmic *u;

	if (sscanf(ln, "%u %7s %u %x %u%n", &unit, name, &blk, &addr, &words, &pos) != 5) {
		schan_put("err parse \"%.40s\"\n", ln);
		return;
	}
	for (k = 0; k < ARRAY_SIZE(ops) && strcmp(name, ops[k].n); k++)
		;
	if (unit > 1 || k == ARRAY_SIZE(ops) || blk > 63 || words < 1 || words > BCM_CMIC_SCHAN_MSG_WORDS - 3) {
		schan_put("err args %u %s %u 0x%08x %u\n", unit, name, blk, addr, words);
		return;
	}
	for (i = 0; ops[k].op != BCM_SCHAN_READ_REG && ops[k].op != BCM_SCHAN_READ_MEM && i < words; i++) {
		if (sscanf(ln + pos, "%x%n", &data[i], &n) != 1) {
			schan_put("err data %u %s %u 0x%08x: %d of %u words\n", unit, name, blk, addr, i, words);
			return;
		}
		pos += n;
	}
	u = schan_units[unit];
	if (!u) {
		schan_put("err nodev %u %s %u 0x%08x\n", unit, name, blk, addr);
		return;
	}
	mutex_lock(&u->schan_seq);
	if (ops[k].rd) {
		ret = schan_exec(&u->cu, u->schan_fn, u->schan_ctx, ops[k].rd, blk, addr, old, words, NULL);
		ret = ret ?: schan_exec(&u->cu, u->schan_fn, u->schan_ctx, ops[k].op, blk, addr, data, words, NULL);
		ret = ret ?: schan_exec(&u->cu, u->schan_fn, u->schan_ctx, ops[k].rd, blk, addr, back, words, NULL);
	} else {
		ret = schan_exec(&u->cu, u->schan_fn, u->schan_ctx, ops[k].op, blk, addr, data, words, &ack);
	}
	mutex_unlock(&u->schan_seq);

	if (ret && ret != -ENOENT) {
		schan_put("err %d %u %s %u 0x%08x\n", ret, unit, name, blk, addr);
	} else if (ops[k].rd) {
		bool same = !memcmp(data, back, 4 * words);

		schan_put("%s %u %s %u 0x%08x", same ? "ok" : "mismatch", unit, name, blk, addr);
		schan_words("old", old, words);
		schan_words("back", back, words);
		schan_put("\n");
		char b0[9 * BCM_CMIC_SCHAN_MSG_WORDS], b1[9 * BCM_CMIC_SCHAN_MSG_WORDS];

		dev_info(u->dev, "schan param: %s blk %u 0x%08x: %s -> %s (%s)\n", name, blk, addr,
			 words_str(b0, sizeof(b0), old, words), words_str(b1, sizeof(b1), back, words),
			 same ? "read back ok" : "READ BACK MISMATCH");
	} else {
		schan_put("%s %u %s %u 0x%08x ack %08x", ret ? "notfound" : "ok", unit, name, blk, addr, ack);
		if (!ret && ops[k].op != BCM_SCHAN_TABLE_DELETE)
			schan_words(":", data, ops[k].op == BCM_SCHAN_TABLE_LOOKUP ? words + 1 : words);
		schan_put("\n");
		if (ops[k].op == BCM_SCHAN_TABLE_DELETE) {
			char b0[9 * BCM_CMIC_SCHAN_MSG_WORDS];

			dev_info(u->dev, "schan param: delete blk %u 0x%08x key %s: %s\n", blk, addr,
				 words_str(b0, sizeof(b0), data, words), ret ? "not found" : "deleted");
		}
	}
}

/* echo "<line>\n<line>..." > /sys/module/us48_cmic/parameters/schan; cat it for one result line per request line. */
static int schan_set(const char *val, const struct kernel_param *kp)
{
	char *buf = kstrdup(val, GFP_KERNEL), *s, *ln;
	int left = 0;

	if (!buf)
		return -ENOMEM;
	mutex_lock(&schan_lock);
	schan_len = 0;
	schan_out[0] = 0;
	for (s = buf; (ln = strsep(&s, "\n;")); ) {
		ln = strim(ln);
		if (!*ln)
			continue;
		if (sizeof(schan_out) - schan_len < SCHAN_LINE_MAX) {
			left++;
			continue;
		}
		schan_line(ln);
	}
	if (left)
		schan_put("err full: %d request lines not run\n", left);
	mutex_unlock(&schan_lock);
	kfree(buf);
	return 0;
}

static int schan_get(char *buf, const struct kernel_param *kp)
{
	int n;

	mutex_lock(&schan_lock);
	n = scnprintf(buf, PAGE_SIZE, "%s", schan_out);
	mutex_unlock(&schan_lock);
	return n;
}

static const struct kernel_param_ops schan_ops = { .set = schan_set, .get = schan_get };
module_param_cb(schan, &schan_ops, NULL, 0600);

static void octal_phy_diag(struct us48_cmic *u, struct bcm_cmic_unit *cu, u8 phy)
{
	u16 aux, mode, slave, detect, isolate, dsp, reset;
	int ret;

	ret = bcm_cmic_miim_read(cu, false, 0, phy, 0x18, &aux);
	if (!ret)
		ret = bcm_cmic_miim_shadow_read(cu, false, 0, phy, 0x1f, &mode);
	if (!ret)
		ret = bcm_cmic_miim_shadow_read(cu, false, 0, phy, 0x15, &slave);
	if (!ret)
		ret = bcm_cmic_miim_shadow_read(cu, false, 0, phy, 0x1e, &detect);
	if (!ret)
		ret = bcm_cmic_miim_rdb_read(cu, false, 0, phy, 0x02a, &isolate);
	if (!ret)
		ret = bcm_cmic_miim_rdb_read(cu, false, 0, phy, 0x125, &dsp);
	if (!ret)
		ret = bcm_cmic_miim_rdb_read(cu, false, 0, phy, 0x82b, &reset);
	if (ret) {
		dev_warn(u->dev, "octal phy %02x vendor-state read failed: %d\n", phy, ret);
		return;
	}

	dev_info(u->dev, "octal phy %02x AUX %04x MODE %04x SGMII %04x DETECT %04x "
		 "POWER_MII %04x DSP_TAP10 %04x GLOBAL_RESET %04x\n",
		 phy, aux, mode, slave, detect, isolate, dsp, reset);
}

/* Read both copper MDIO buses. No configuration writes; the shadow/RDB reads write selectors and the RDB mode enter/exit. */
void us48_mdio_scan(struct us48_cmic *u)
{
	static const u8 regs[] = { 2, 3, 0, 1, 1, 4, 5, 9, 10 };
	struct bcm_cmic_unit cu = u->cu;
	int bus, phy, i, ret;

	mutex_lock(&u->mdio);
	for (bus = 0; bus < 2; bus++) {
		bool internal = !bus;

		for (phy = 0; phy < 32; phy++) {
			u16 v[ARRAY_SIZE(regs)];

			for (i = 0; i < ARRAY_SIZE(regs); i++) {
				ret = bcm_cmic_miim_read(&cu, internal, 0, phy, regs[i], &v[i]);
				if (ret) {
					dev_warn(u->dev, "mdio %s0 phy %02x reg %u read failed: %d\n",
						 internal ? "int" : "ext", phy, regs[i], ret);
					break;
				}
			}
			if (ret)
				continue;
			if ((!v[0] && !v[1]) || v[0] == 0xffff || v[1] == 0xffff)
				continue;
			dev_info(u->dev, "mdio %s0 phy %02x id %04x:%04x BMCR %04x BMSR %04x link %u "
				 "ANAR %04x ANLPAR %04x GBCR %04x GBSR %04x\n",
				 internal ? "int" : "ext", phy, v[0], v[1], v[2], v[4], !!(v[4] & BIT(2)),
				 v[5], v[6], v[7], v[8]);
			if (!internal && v[0] == 0x600d && v[1] == 0x845b)
				octal_phy_diag(u, &cu, phy);
		}
	}
	mutex_unlock(&u->mdio);
}
EXPORT_SYMBOL_GPL(us48_mdio_scan);

static int phy_scan_set(const char *val, const struct kernel_param *kp)
{
	unsigned int scan;
	int ret = kstrtouint(val, 0, &scan);

	if (ret || !scan)
		return ret;
	mutex_lock(&dbg_lock);
	if (!dbg_u)
		ret = -ENODEV;
	else {
		us48_mdio_scan(dbg_u);
		ret = 0;
	}
	mutex_unlock(&dbg_lock);
	return ret;
}

static const struct kernel_param_ops phy_scan_ops = { .set = phy_scan_set };
module_param_cb(phy_scan, &phy_scan_ops, NULL, 0200);
MODULE_PARM_DESC(phy_scan, "1 = read copper status and octal-PHY vendor state on both MIIM bus-0s");

static struct bcm_hr2_ge ge_of(const struct us48_port *p)
{
	return (struct bcm_hr2_ge){ .blk = p->cfg->blk, .lane = p->cfg->lane, .phy = p->cfg->phy, .lport = p->cfg->lport,
				    .phys = p->cfg->phys, .modid = p->u->modid, .vid = p->vid, .external_phy = p->cfg->external_phy };
}

/* Shared core (common/bcm-cmic): copper MAC init, PHY AN, private VLAN, PORT_TAB, link bitmap. First ndo_open, link_lock held. */
static int copper_enable(struct us48_port *p)
{
	struct us48_cmic *u = p->u;
	struct bcm_cmic_unit cu = u->cu;
	struct bcm_hr2_ge ge = ge_of(p);
	int ret;

	ge.blk_ready = u->blk_ready & BIT(p->cfg->blk);
	mutex_lock(&u->mdio);
	ret = bcm_hr2_ge_bringup(&cu, &ge);
	mutex_unlock(&u->mdio);
	if (!ret) {
		u->blk_ready |= BIT(p->cfg->blk);
		p->copper_ready = true;
	}
	if (p->cfg->front == 1) {
		mutex_lock(&dbg_lock);
		dbg_u = u;
		dbg_cfg = p->cfg;
		mutex_unlock(&dbg_lock);
	}
	dev_info(u->dev, "%s: copper bring-up %s (block %d lane %d%s)\n", p->cfg->name, ret ? "FAILED" : "done",
		 p->cfg->blk, p->cfg->lane, ge.blk_ready ? "" : ", block init");
	return ret;
}

/* Copper PHY power follows the netdev (#133): down = POWER_DOWN, the peer loses carrier; up = AN restart. */
int us48_copper_power(struct us48_port *p, bool on)
{
	struct bcm_cmic_unit cu = p->u->cu;
	struct bcm_hr2_ge ge = ge_of(p);
	int ret;

	mutex_lock(&p->u->mdio);
	ret = bcm_hr2_ge_phy_power(&cu, &ge, on);
	mutex_unlock(&p->u->mdio);
	if (ret)
		netdev_warn(p->ndev, "PHY power %s failed: %d\n", on ? "up" : "down", ret);
	return ret;
}

/* BMSR link (latched low: read twice) and negotiated speed from the 1000BASE-T / LPA registers. */
static bool copper_link(struct us48_port *p, int *speed)
{
	struct us48_cmic *u = p->u;
	struct bcm_cmic_unit cu = u->cu;
	struct bcm_hr2_ge ge = ge_of(p);
	bool up;

	mutex_lock(&u->mdio);
	up = bcm_hr2_ge_link(&cu, &ge, speed);
	if (phy_dbg) {
		u16 bmcr = 0, aux = 0;

		bcm_cmic_miim_read(&cu, !p->cfg->external_phy, 0, p->cfg->phy, 0, &bmcr);
		bcm_cmic_miim_read(&cu, !p->cfg->external_phy, 0, p->cfg->phy, 0x19, &aux);
		netdev_info(p->ndev, "phy %d: BMCR %04x AUX19 %04x speed %d\n", p->cfg->phy, bmcr, aux, *speed);
	}
	mutex_unlock(&u->mdio);
	return up;
}

/* One copper port, link_lock held: PHY link -> MAC up (speed from the PHY) / MAC down, carrier. */
static void copper_poll(struct us48_port *p)
{
	struct bcm_cmic_unit cu = p->u->cu;
	struct bcm_hr2_ge ge = ge_of(p);
	int speed = 0;
	bool up = copper_link(p, &speed);

	if (up == netif_carrier_ok(p->ndev))
		return;
	if (up) {
		bcm_hr2_ge_mac_up(&cu, &ge, speed);
		WRITE_ONCE(p->speed, speed);
		netif_carrier_on(p->ndev);
	} else {
		bcm_hr2_ge_mac_down(&cu, &ge);
		netif_carrier_off(p->ndev);
	}
	netdev_info(p->ndev, "link %s%s\n", up ? "up" : "down",
		    up ? (speed == 1000 ? " 1000M" : speed == 100 ? " 100M" : " 10M") : "");
}

/* Copper only, open ports only (never-opened ones have no GPORT/VLAN init). xe carrier: phylink (us48_cmic_phylink.c). */
static void link_work_fn(struct work_struct *w)
{
	struct us48_cmic *u = container_of(to_delayed_work(w), struct us48_cmic, link_work);
	int i;

	for (i = 0; i < u->nports; i++) {
		struct us48_port *p = u->port[i];

		if (!p->cfg->copper)
			continue;
		mutex_lock(&u->link_lock);
		if (p->open && p->copper_ready)
			copper_poll(p);
		mutex_unlock(&u->link_lock);
	}
	schedule_delayed_work(&u->link_work, msecs_to_jiffies(LINK_POLL_MS));
}

/*
 * TX credits (TX_X4_CREDIT0.CREDITENABLE) take effect only when re-armed once the MAC is up: the TX PCS then
 * encodes (ENCODE_STATUS_0 != 0) and the far end stops sending RF on the optic lane (#78, driver-internals 13.7).
 */
static void tx_credit_cycle(struct us48_cmic *u, u32 lane)
{
	int ret;

	mutex_lock(&u->mdio);
	ret = us48_tsc_wr(u, 0xffde, lane) ?: us48_tsc_wr(u, 0xc100, 0x0000) ?: us48_tsc_wr(u, 0xc100, 0x4021);
	mutex_unlock(&u->mdio);
	if (ret)
		dev_warn(u->dev, "tsc lane %u: TX credit cycle failed (%d)\n", lane, ret);
}

/* Undo stock's port disable on one xe lane: verified order, driver-internals 13.5. */
static void port_enable(struct us48_port *p)
{
	static const u16 seq[][2] = {
		{ 0xc138, 0x0009 },			/* RX_SIGDET: drop the forced signal-detect bits */
		{ 0xc101, 0x0084 }, { 0xc100, 0x4021 },	/* TX credits, CREDITENABLE */
		{ 0xc017, 0x2020 },			/* ANATX_DRIVER: clear ELEC_IDLE (stock's xe value) */
		{ 0xc01a, 0x0000 },			/* ANATX_CONTROL6: clear TX disable */
		{ 0xc113, 0xc1c8 }, { 0xc113, 0xc1cb },	/* TX lane reset pulse, after credits */
		{ 0xc137, 0x0001 },			/* RX lane reset release */
	};
	struct us48_cmic *u = p->u;
	u32 a = p->cfg->lane, v[2];
	int i, ret = 0;

	if (p->cfg->copper)		/* deferred to the first ndo_open */
		return;
	/* Only from stock's parked state: re-crediting a running port is not known safe. */
	if (!sbus(u, 0x0b, BLK_XLPORT, 0x60000 | a, v, 2) && (v[0] & 0x43) == 0x03) {
		dev_info(u->dev, "%s: MAC already enabled (XLMAC_CTRL %08x): skip enable\n", p->cfg->name, v[0]);
		tx_credit_cycle(u, a);
		return;
	}
	mutex_lock(&u->mdio);
	ret = us48_tsc_wr(u, 0xffde, a);			/* AER: lane select */
	for (i = 0; !ret && i < ARRAY_SIZE(seq); i++)
		ret = us48_tsc_wr(u, seq[i][0], seq[i][1]);
	mutex_unlock(&u->mdio);
	if (ret)
		return;
	v[0] = 7;
	sbus(u, 0x0d, BLK_XLPORT, 0x20300 | a, v, 1);	/* XLPORT_TXFIFO_CTRL clear counts */
	v[0] = 4;
	sbus(u, 0x0d, BLK_XLPORT, 0x20300 | a, v, 1);
	v[0] = 0x1003; v[1] = 0;			/* XLMAC_CTRL: TX_EN, RX_EN, SOFT_RESET 0 */
	sbus(u, 0x0d, BLK_XLPORT, 0x60000 | a, v, 2);
	v[0] = 7;					/* XLMAC_CLEAR_RX_LSS_STATUS pulse */
	sbus(u, 0x0d, BLK_XLPORT, 0x60c00 | a, v, 2);
	v[0] = 0;
	sbus(u, 0x0d, BLK_XLPORT, 0x60c00 | a, v, 2);
	tx_credit_cycle(u, a);
	dev_info(u->dev, "%s: enable sequence done\n", p->cfg->name);
}

void us48_xe_port_enable(struct us48_port *p)
{
	port_enable(p);
}

/*
 * Private VLAN (id = front number) with only the CPU and this port: VLAN 1 also holds the HiGig links
 * to unit 0, and two ports in one VLAN would be bridged by the chip. Flood pointers, STG and profile are
 * cloned from VLAN 1; only the bitmaps differ.
 */
static void port_vlan_init(struct us48_port *p)
{
	static const int cpu_stg[] = { 0, 1 };
	struct us48_cmic *u = p->u;
	int lp = p->cfg->lport, stg_bits[] = { 2 * lp, 2 * lp + 1 };
	u32 e[4], g[3], one = 1;
	u32 stg;

	if (sbus(u, 0x07, BLK_IPIPE, VLAN_TAB(p->vid), e, 4) || (e[1] & BIT(6))) {	/* VALID b38 */
		dev_warn(u->dev, "%s: VLAN %d not free (or unreadable): not touching\n", p->cfg->name, p->vid);
		return;
	}
	if (sbus(u, 0x07, BLK_IPIPE, VLAN_TAB(1), e, 4) || sbus(u, 0x07, BLK_EPIPE, EGR_VLAN(1), g, 3))
		return;
	stg = (e[1] >> 7) & 0x1ff;			/* STG b39..47 */
	e[0] &= ~0x3fffffffu;				/* PORT_BITMAP 0:30 */
	ebit(e, 0);
	ebit(e, lp);
	set_even_parity(e, 126);
	sbus(u, 0x09, BLK_IPIPE, VLAN_TAB(p->vid), e, 4);
	g[0] = 0;					/* UT_BITMAP 0:30 + PORT_BITMAP 30:30 */
	g[1] &= ~0x0fffffffu;
	ebit(g, 0);
	ebit(g, lp);
	ebit(g, 30);
	ebit(g, 30 + lp);
	set_even_parity(g, 76);
	sbus(u, 0x09, BLK_EPIPE, EGR_VLAN(p->vid), g, 3);
	/* SP_TREE state 3 = forwarding, for the CPU and this port, in the cloned STG. */
	smem_set_bits(u, BLK_IPIPE, STG_TAB(stg), 2, 60, cpu_stg, 2);
	smem_set_bits(u, BLK_IPIPE, STG_TAB(stg), 2, 60, stg_bits, 2);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN_STG(stg), 2, 60, cpu_stg, 2);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN_STG(stg), 2, 60, stg_bits, 2);
	/* Stock's port disable leaves EGR_ENABLE = 0: every frame to the port dies before the MAC. Index = physical (#124). */
	sbus(u, 0x09, BLK_EPIPE, EGR_ENABLE(p->cfg->phys), &one, 1);
}

/*
 * PORT_TAB[port]: PORT_VID (24:12) puts untagged ingress into our VLAN. CML_FLAGS_NEW/MOVE (121:4,
 * 125:4) stay 0: stock's 8 ('do HW learn') drops every unicast frame here (ING_EVENT_DEBUG CML+RDROP).
 */
static void port_tab_set(struct us48_cmic *u, int lport, u32 vid, bool cml_off)
{
	u32 e[8];

	if (sbus(u, 0x07, BLK_IPIPE, PORT_TAB(lport), e, 8))
		return;
	e[0] = (e[0] & 0x00ffffff) | (vid << 24);
	e[1] = (e[1] & ~0xfu) | (vid >> 8);
	if (cml_off) {
		e[3] &= ~(0xfu << 25 | 0x7u << 29);
		e[4] &= ~1u;
	}
	sbus(u, 0x09, BLK_IPIPE, PORT_TAB(lport), e, 8);
}

static void set_field(u32 *e, int lsb, int width, u64 v)
{
	int i;

	for (i = 0; i < width; i++)
		if (v & BIT_ULL(i))
			e[(lsb + i) / 32] |= BIT((lsb + i) % 32);
}

/* Static L2X entry: the port's MAC in its VLAN -> CPU (modid 1, port 0). TX bypasses learning. */
static int l2_op_cpu(struct us48_port *p, u32 op)
{
	u32 e[4] = { };
	int i, ones = 0;

	if (p->cfg->copper) {
		struct bcm_cmic_unit cu = p->u->cu;
		struct bcm_hr2_ge ge = ge_of(p);

		return bcm_hr2_ge_l2_cpu(&cu, &ge, p->ndev->dev_addr, op == 0x24);
	}

	set_field(e, 2, 12, p->vid);			/* VLAN_ID */
	set_field(e, 14, 48, ether_addr_to_u64(p->ndev->dev_addr));	/* MAC_ADDR */
	set_field(e, 68, 7, p->u->modid);			/* MODULE_ID, PORT_NUM 0 = CPU */
	set_field(e, 91, 1, 1);				/* CPU: copy to CPU */
	set_field(e, 97, 1, 1);				/* STATIC_BIT */
	set_field(e, 99, 1, 1);				/* VALID */
	for (i = 0; i < 4; i++)
		ones += hweight32(e[i]);
	set_field(e, 100, 1, ones & 1);			/* EVEN_PARITY */
	return sbus(p->u, op, BLK_IPIPE, L2X, e, 4);
}

static void dma_abort(struct us48_cmic *u, int ch)
{
	u32 ctrl = rd(u, DMA_CTRL(ch)), stat;

	if (rd(u, DMA_STAT) & DMA_ACTIVE(ch)) {
		wr(u, DMA_CTRL(ch), ctrl | DMA_EN | DMA_ABORT);
		if (readl_poll_timeout_atomic(u->cmic + DMA_STAT, stat, !(stat & DMA_ACTIVE(ch)),
					      1, ABORT_TIMEOUT_US))
			dev_err(u->dev, "ch%d abort: still active after %u us, DMA_STAT %08x\n",
				ch, ABORT_TIMEOUT_US, stat);
	}
	wr(u, DMA_CTRL(ch), rd(u, DMA_CTRL(ch)) & ~(DMA_EN | DMA_ABORT));
	wr(u, DMA_STAT_CLR, BIT(ch));
}

struct us48_buf {
	struct device *dev;
	size_t size;
	void *cpu;
	dma_addr_t dma;
	enum dma_data_direction dir;
};

static void us48_free_buf(void *data)
{
	struct us48_buf *b = data;

	dma_free_pages(b->dev, b->size, virt_to_page(b->cpu), b->dma, b->dir);
}

/* Cached DMA pages below 128 MiB (the CMIC inbound window); freed with the device. */
static void *us48_alloc_buf(struct device *dev, struct device *dma_dev, size_t size, dma_addr_t *dma, enum dma_data_direction dir)
{
	struct us48_buf *b = devm_kzalloc(dev, sizeof(*b), GFP_KERNEL);
	struct page *pg;

	if (!b)
		return NULL;
	size = PAGE_ALIGN(size);
	pg = dma_alloc_pages(dma_dev, size, dma, dir, GFP_KERNEL);
	if (!pg)
		return NULL;
	b->dev = dma_dev;
	b->size = size;
	b->cpu = page_address(pg);
	b->dma = *dma;
	b->dir = dir;
	if (devm_add_action_or_reset(dev, us48_free_buf, b))
		return NULL;
	return b->cpu;
}

struct us48_coh {
	struct device *dev;
	size_t size;
	void *cpu;
	dma_addr_t dma;
};

static void us48_free_coh(void *data)
{
	struct us48_coh *c = data;

	dma_free_coherent(c->dev, c->size, c->cpu, c->dma);
}

/* dmam_alloc_coherent() with the DMA device apart from the devres owner (aux device vs PCI device). */
static void *us48_alloc_coh(struct device *dev, struct device *dma_dev, size_t size, dma_addr_t *dma)
{
	struct us48_coh *c = devm_kzalloc(dev, sizeof(*c), GFP_KERNEL);

	if (!c)
		return NULL;
	c->cpu = dma_alloc_coherent(dma_dev, size, dma, GFP_KERNEL);
	if (!c->cpu)
		return NULL;
	c->dev = dma_dev;
	c->size = size;
	c->dma = *dma;
	if (devm_add_action_or_reset(dev, us48_free_coh, c))
		return NULL;
	return c->cpu;
}

static void rx_arm(struct us48_cmic *u)
{
	int i;

	/* RX_RING data DCBs + a RELOAD DCB back to DCB 0: the engine never stops (shared code: bcm_hr2_rx_arm). */
	memset(u->rx_dcb, 0, (RX_RING + 1) * 64);
	for (i = 0; i < RX_RING; i++) {
		u->rx_dcb[16 * i] = cpu_to_le32(u->rx_buf_dma + i * BUF_SZ);
		u->rx_dcb[16 * i + 1] = cpu_to_le32(BUF_SZ | DCB_W1_CHAIN);
	}
	u->rx_dcb[16 * RX_RING] = cpu_to_le32(u->rx_dcb_dma);
	u->rx_dcb[16 * RX_RING + 1] = cpu_to_le32(DCB_W1_CHAIN | DCB_W1_RELOAD);
	u->rx_next = 0;
	dma_sync_single_for_device(u->dma_dev, u->rx_buf_dma, RX_RING * BUF_SZ, DMA_FROM_DEVICE);
	wmb();
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & ~DMA_EN);
	wr(u, DMA_STAT_CLR, BIT(RX_CH));
	wr(u, DMA_DESC(RX_CH), u->rx_dcb_dma);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) | DMA_EN);
}

static struct us48_port *port_by_lport(struct us48_cmic *u, unsigned int lport)
{
	int i;

	for (i = 0; i < u->nports; i++)
		if (u->port[i]->cfg->lport == lport)
			return u->port[i];
	return NULL;
}

/* One received DCB -> skb on the port it came from: drop the 4 CRC bytes and our VLAN tag if present. */
static void rx_one(struct us48_cmic *u, int i)
{
	u32 w15 = le32_to_cpu(u->rx_dcb[16 * i + 15]);
	u32 w12 = le32_to_cpu(u->rx_dcb[16 * i + 12]);
	u8 *d = u->rx_buf + i * BUF_SZ;
	unsigned int len = w15 & 0xffff;
	struct us48_port *p = port_by_lport(u, w12 & 0xff);
	struct net_device *nd;
	struct sk_buff *skb;

	dma_sync_single_for_cpu(u->dma_dev, u->rx_buf_dma + i * BUF_SZ, BUF_SZ, DMA_FROM_DEVICE);
	if (rx_dump) {
		rx_dump--;
		dev_info(u->dev, "RX dcb%d len %u src %u w15 %08x da %pM et %04x\n", i, len, w12 & 0xff, w15, d,
			 get_unaligned_be16(d + 12));
	}
	if (!p || !p->open)
		return;
	nd = p->ndev;
	if ((w15 & DCB_W15_ERR) || len < ETH_ZLEN + ETH_FCS_LEN || len > BUF_SZ) {
		nd->stats.rx_errors++;
		return;
	}
	len -= ETH_FCS_LEN;
	skb = len <= SKB_POOL_LEN ? skb_dequeue(&u->skb_pool) : NULL;
	if (!skb)
		skb = napi_alloc_skb(&u->napi, len);
	if (!skb) {		/* slot is recycled by the caller either way */
		nd->stats.rx_dropped++;
		return;
	}
	if (get_unaligned_be16(d + 12) == ETH_P_8021Q &&
	    (get_unaligned_be16(d + 14) & VLAN_VID_MASK) == READ_ONCE(p->rx_vid)) {
		skb_put_data(skb, d, 12);
		skb_put_data(skb, d + 16, len - 16);
	} else {
		skb_put_data(skb, d, len);
	}
	skb->protocol = eth_type_trans(skb, nd);
#if IS_ENABLED(CONFIG_NET_SWITCHDEV)
	/* Offloaded bridge port: the ASIC already flooded/forwarded it to the unit's other members (docs/switchdev.md). */
	if (READ_ONCE(p->br))
		skb->offload_fwd_mark = 1;
#endif
	nd->stats.rx_packets++;
	nd->stats.rx_bytes += skb->len;
	napi_gro_receive(&u->napi, skb);
}

static void tx_wake_all(struct us48_cmic *u)
{
	int i;

	for (i = 0; i < u->nports; i++)
		netif_wake_queue(u->port[i]->ndev);
}

static __le32 *tx_slot_dcb(struct us48_cmic *u, unsigned int n)
{
	return u->tx_dcb + 32 * (n % TX_RING);
}

/* Start the next chain of queued frames (contiguous slots) if the engine is idle; tx_lock held. */
static void tx_launch(struct us48_cmic *u)
{
	unsigned int s = u->tx_run, end, i;

	if (u->tx_tail != u->tx_run || s == u->tx_head)
		return;
	end = min(u->tx_head, s + TX_RING - s % TX_RING);
	for (i = s; i < end - 1; i++)
		tx_slot_dcb(u, i)[17] |= cpu_to_le32(DCB_W1_CHAIN);
	wmb();
	wr(u, DMA_DESC(TX_CH), u->tx_dcb_dma + 128 * (s % TX_RING));
	wr(u, DMA_CTRL(TX_CH), rd(u, DMA_CTRL(TX_CH)) | DMA_EN);
	u->tx_run = end;
}

/* Complete finished frames in order, then launch what is queued; tx_lock held. */
static void __tx_reap(struct us48_cmic *u)
{
	bool reaped = false;

	while (u->tx_tail != u->tx_run) {
		unsigned int n = u->tx_tail;
		struct net_device *nd = u->port[u->tx_meta[n % TX_RING] >> 16]->ndev;

		if (!(le32_to_cpu(READ_ONCE(tx_slot_dcb(u, n)[31])) & DCB_W15_DONE))
			break;
		nd->stats.tx_packets++;
		nd->stats.tx_bytes += u->tx_meta[n % TX_RING] & 0xffff;
		u->tx_tail++;
		reaped = true;
	}
	if (!reaped)
		return;
	if (u->tx_tail == u->tx_run) {		/* chain finished: stop the channel before the next one */
		wr(u, DMA_CTRL(TX_CH), rd(u, DMA_CTRL(TX_CH)) & ~DMA_EN);
		wr(u, DMA_STAT_CLR, BIT(TX_CH));
		tx_launch(u);
	}
	tx_wake_all(u);
}

static void tx_reap(struct us48_cmic *u)
{
	spin_lock(&u->tx_lock);
	__tx_reap(u);
	spin_unlock(&u->tx_lock);
}

/* Top up the RX skb pool with sleeping allocations: NAPI-context atomic allocs failed under load (#56). */
static void us48_pool_fill(struct work_struct *w)
{
	struct us48_cmic *u = container_of(w, struct us48_cmic, pool_work);

	while (skb_queue_len(&u->skb_pool) < SKB_POOL) {
		struct sk_buff *skb = __netdev_alloc_skb(NULL, SKB_POOL_LEN, GFP_KERNEL);

		if (!skb)
			return;
		skb_queue_tail(&u->skb_pool, skb);
		cond_resched();
	}
}

static int us48_poll(struct napi_struct *napi, int budget)
{
	struct us48_cmic *u = container_of(napi, struct us48_cmic, napi);
	int done = 0;

	tx_reap(u);
	while (done < budget) {
		int i = u->rx_next;

		if (!(le32_to_cpu(READ_ONCE(u->rx_dcb[16 * i + 15])) & DCB_W15_DONE))
			break;
		dma_rmb();
		rx_one(u, i);
		dma_sync_single_for_device(u->dma_dev, u->rx_buf_dma + i * BUF_SZ, BUF_SZ, DMA_FROM_DEVICE);
		u->rx_dcb[16 * i + 15] = 0;	/* hand the slot back */
		if (i == 0)	/* RELOAD DONE is written before slot 0 fills; clearing at slot 255 raced it (#56) */
			u->rx_dcb[16 * RX_RING + 15] = 0;
		wmb();
		u->rx_next = (i + 1) % RX_RING;
		done++;
	}
	/* Engine halted with every filled slot consumed: restart it from DCB 0. */
	if (!done && !READ_ONCE(u->quiesced) && !(le32_to_cpu(READ_ONCE(u->rx_dcb[16 * u->rx_next + 15])) & DCB_W15_DONE) &&
	    !(rd(u, DMA_STAT) & DMA_ACTIVE(RX_CH)))
		rx_arm(u);
	if (skb_queue_len(&u->skb_pool) < SKB_POOL / 2)
		schedule_work(&u->pool_work);
	if (done < budget)
		napi_complete_done(napi, done);
	return done;
}

static enum hrtimer_restart us48_poll_timer(struct hrtimer *t)
{
	struct us48_cmic *u = container_of(t, struct us48_cmic, poll_timer);

	napi_schedule(&u->napi);
	hrtimer_forward_now(t, ns_to_ktime(POLL_NS));
	return HRTIMER_RESTART;
}

/* TX to a port with the module header (OM bcm56150_a0_bmd_tx): DCB0 = DA+SA (12 B), DCB1 = rest at +16, HG on both. */
static netdev_tx_t us48_xmit(struct sk_buff *skb, struct net_device *nd)
{
	struct us48_port *p = netdev_priv(nd);
	struct us48_cmic *u = p->u;
	unsigned int len = max_t(unsigned int, skb->len, ETH_ZLEN);
	unsigned int n;
	__le32 *dcb;
	u8 *buf;
	int i;

	if (len + 16 > BUF_SZ || skb->len < 12) {
		nd->stats.tx_dropped++;
		dev_kfree_skb_any(skb);
		return NETDEV_TX_OK;
	}
	spin_lock(&u->tx_lock);
	__tx_reap(u);
	if (u->tx_head - u->tx_tail >= TX_RING) {
		for (i = 0; i < u->nports; i++)
			netif_stop_queue(u->port[i]->ndev);
		spin_unlock(&u->tx_lock);
		return NETDEV_TX_BUSY;
	}
	n = u->tx_head % TX_RING;
	buf = u->tx_buf + n * BUF_SZ;
	dcb = tx_slot_dcb(u, n);
	memset(buf, 0, len + 16);		/* DCB1 reads buf[16, len + 8) */
	skb_copy_bits(skb, 0, buf, 12);
	skb_copy_bits(skb, 12, buf + 16, skb->len - 12);
	memset(dcb, 0, 128);
	dcb[0] = cpu_to_le32(u->tx_buf_dma + n * BUF_SZ);
	dcb[1] = cpu_to_le32(12 | DCB_W1_CHAIN | DCB_W1_SG | DCB_W1_STAT | DCB_W1_HG);
	dcb[2] = cpu_to_le32(0x81000000);
	dcb[3] = cpu_to_le32(p->cfg->lport);
	dcb[4] = cpu_to_le32(0x00040000);		/* txq 0 */
	dcb[16] = cpu_to_le32(u->tx_buf_dma + n * BUF_SZ + 16);
	/* +4: XLMAC_TX_CTRL CRC_MODE is 'replace' (stock): the last 4 bytes are overwritten with the FCS, not added. */
	dcb[17] = cpu_to_le32((len + ETH_FCS_LEN - 12) | DCB_W1_STAT | DCB_W1_HG);
	memcpy(&dcb[18], &dcb[2], 16);		/* OM _dcb_init: header on both DCBs */
	for (i = 0; i < u->nports; i++)
		if (u->port[i] == p)
			u->tx_meta[n] = i << 16 | len;
	dma_sync_single_for_device(u->dma_dev, u->tx_buf_dma + n * BUF_SZ, len + 16, DMA_TO_DEVICE);
	u->tx_head++;
	if (u->tx_head - u->tx_tail >= TX_RING)	/* stopped only when the ring is full; __tx_reap wakes */
		for (i = 0; i < u->nports; i++)
			netif_stop_queue(u->port[i]->ndev);
	tx_launch(u);
	spin_unlock(&u->tx_lock);
	dev_consume_skb_any(skb);
	return NETDEV_TX_OK;
}

static void us48_tx_timeout(struct net_device *nd, unsigned int txq)
{
	struct us48_port *p = netdev_priv(nd);
	struct us48_cmic *u = p->u;

	netdev_err(nd, "TX stuck: DMA_STAT %08x CTRL %08x dcb1 w15 %08x\n", rd(u, DMA_STAT),
		   rd(u, DMA_CTRL(TX_CH)), le32_to_cpu(tx_slot_dcb(u, u->tx_tail)[31]));
	spin_lock_bh(&u->tx_lock);
	dma_abort(u, TX_CH);
	nd->stats.tx_errors += u->tx_head - u->tx_tail;
	u->tx_tail = u->tx_run = u->tx_head;
	tx_wake_all(u);
	spin_unlock_bh(&u->tx_lock);
}

static int us48_set_mac(struct net_device *nd, void *a)
{
	struct us48_port *p = netdev_priv(nd);
	int ret;

	if (p->open)
		l2_op_cpu(p, 0x26);
	ret = eth_mac_addr(nd, a);
	if (!ret && p->open && l2_op_cpu(p, 0x24))
		netdev_warn(nd, "L2 insert for %pM failed\n", nd->dev_addr);
	return ret;
}

static int us48_open(struct net_device *nd)
{
	struct us48_port *p = netdev_priv(nd);
	struct us48_cmic *u = p->u;

	if (p->cfg->copper && !p->copper_ready) {
		int ret;

		mutex_lock(&u->link_lock);
		ret = us48_pwr_octal_up(p) ?: copper_enable(p);
		mutex_unlock(&u->link_lock);
		if (ret)
			return ret;
		us48_sw_port_reapply(p);	/* joined a bridge before the first open */
	} else if (p->cfg->copper) {
		int ret = us48_copper_power(p, true);

		if (ret)
			return ret;
	}
	if (l2_op_cpu(p, 0x24))
		netdev_warn(nd, "L2 insert for %pM failed: unicast to us will not arrive\n", nd->dev_addr);
	mutex_lock(&u->link_lock);
	if (!p->cfg->copper) {
		int ret = us48_pwr_xe_up(u);	/* XLPORT0 gated by pwrsave (#143) */

		if (ret) {
			mutex_unlock(&u->link_lock);
			l2_op_cpu(p, 0x26);
			return ret;
		}
	}
	p->open = true;
	mutex_unlock(&u->link_lock);
	if (u->nopen++ == 0) {
		us48_pool_fill(&u->pool_work);
		rx_arm(u);
		napi_enable(&u->napi);
		hrtimer_start(&u->poll_timer, ns_to_ktime(POLL_NS), HRTIMER_MODE_REL);
	}
	netif_start_queue(nd);
	if (p->pl)
		phylink_start(p->pl);	/* sfp TX enable; pcs_config sets the lane mode for the module in the cage */
	return 0;
}

static int us48_stop(struct net_device *nd)
{
	struct us48_port *p = netdev_priv(nd);
	struct us48_cmic *u = p->u;

	if (p->pl)
		phylink_stop(p->pl);
	netif_stop_queue(nd);
	mutex_lock(&u->link_lock);
	p->open = false;
	/* Copper: MAC off and PHY powered down with the netdev; the poll skips the port until the next open. */
	if (p->cfg->copper && netif_carrier_ok(nd)) {
		struct bcm_cmic_unit cu = u->cu;
		struct bcm_hr2_ge ge = ge_of(p);

		bcm_hr2_ge_mac_down(&cu, &ge);
		netif_carrier_off(nd);
	}
	if (p->cfg->copper && p->copper_ready)
		us48_copper_power(p, false);
	mutex_unlock(&u->link_lock);
	l2_op_cpu(p, 0x26);
	if (--u->nopen == 0) {
		hrtimer_cancel(&u->poll_timer);
		napi_disable(&u->napi);
		dma_abort(u, RX_CH);
		spin_lock_bh(&u->tx_lock);
		dma_abort(u, TX_CH);
		u->tx_tail = u->tx_run = u->tx_head;
		spin_unlock_bh(&u->tx_lock);
		cancel_work_sync(&u->pool_work);
		skb_queue_purge(&u->skb_pool);
	}
	return 0;
}

/* Per-port copper state for scripts/copper_check.py (#126): bring-up runs once per load, block init once per block. */
static ssize_t copper_ready_show(struct device *d, struct device_attribute *attr, char *buf)
{
	struct us48_port *p = netdev_priv(to_net_dev(d));

	return sysfs_emit(buf, "%d\n", READ_ONCE(p->copper_ready));
}
static DEVICE_ATTR_RO(copper_ready);

static ssize_t block_ready_show(struct device *d, struct device_attribute *attr, char *buf)
{
	struct us48_port *p = netdev_priv(to_net_dev(d));

	return sysfs_emit(buf, "%d\n", !!(READ_ONCE(p->u->blk_ready) & BIT(p->cfg->blk)));
}
static DEVICE_ATTR_RO(block_ready);

/* Mb/s the MAC was set to at the last link up; 0 without carrier (#130). */
static ssize_t link_speed_show(struct device *d, struct device_attribute *attr, char *buf)
{
	struct net_device *nd = to_net_dev(d);
	struct us48_port *p = netdev_priv(nd);

	return sysfs_emit(buf, "%d\n", netif_carrier_ok(nd) ? READ_ONCE(p->speed) : 0);
}
static DEVICE_ATTR_RO(link_speed);

static struct attribute *us48_copper_attrs[] = {
	&dev_attr_copper_ready.attr,
	&dev_attr_block_ready.attr,
	&dev_attr_link_speed.attr,
	NULL
};
static const struct attribute_group us48_copper_group = { .attrs = us48_copper_attrs };

static const struct net_device_ops us48_netdev_ops = {
	.ndo_open		= us48_open,
	.ndo_stop		= us48_stop,
	.ndo_start_xmit		= us48_xmit,
	.ndo_tx_timeout		= us48_tx_timeout,
	.ndo_set_mac_address	= us48_set_mac,
	.ndo_validate_addr	= eth_validate_addr,
#if IS_ENABLED(CONFIG_NET_SWITCHDEV)
	.ndo_get_port_parent_id	= us48_sw_parent_id,
	.ndo_fdb_dump		= us48_sw_fdb_dump,
#endif
};

bool us48_is_port(const struct net_device *nd)
{
	return nd->netdev_ops == &us48_netdev_ops;
}

static void us48_free_napi_dev(void *data)
{
	struct us48_cmic *u = data;

	cancel_delayed_work_sync(&u->link_work);
	netif_napi_del(&u->napi);
	free_netdev(u->napi_dev);
}

/* Both channels off and their descriptor pointers cleared: nothing can fetch a freed DCB, even if EN is set later. */
static void dma_stop_all(struct us48_cmic *u)
{
	int ch;

	for (ch = 0; ch < 2; ch++) {
		dma_abort(u, ch);
		wr(u, DMA_DESC(ch), 0);
	}
}

/* Self-check at unbind: a DMA left running into freed rings is the classic reload crash. Expiry-free, read-only. */
static void us48_selfcheck(struct us48_cmic *u)
{
	int ch, bad = 0;

	for (ch = 0; ch < 2; ch++)
		bad += !!(rd(u, DMA_CTRL(ch)) & (DMA_EN | DMA_ABORT)) + !!(rd(u, DMA_STAT) & DMA_ACTIVE(ch));
	bad += u->nopen != 0;
	bad += u->tx_head != u->tx_tail || u->tx_tail != u->tx_run;
	bad += !skb_queue_empty(&u->skb_pool);
	if (bad)
		dev_err(u->dev, "unbind self-check: %d problems: DMA_CTRL %08x/%08x DMA_STAT %08x nopen %d tx %u/%u/%u pool %u\n",
			bad, rd(u, DMA_CTRL(0)), rd(u, DMA_CTRL(1)), rd(u, DMA_STAT), u->nopen, u->tx_tail, u->tx_run,
			u->tx_head, skb_queue_len(&u->skb_pool));
	WARN_ON(bad);
}

void us48_remove_common(struct us48_cmic *u)
{
	int i;

	schan_unit_set(u, false);
	us48_led_unit_exit(u);
	us48_pwr_xe_stop(u);
	cancel_delayed_work_sync(&u->link_work);
	/* ndo_stop of an open port stops the poll timer, NAPI and both channels. */
	for (i = 0; i < u->nports; i++) {
		if (u->port[i] && u->port[i]->registered) {
			unregister_netdev(u->port[i]->ndev);
			u->port[i]->registered = false;
		}
		if (u->port[i])
			us48_xe_phylink_destroy(u->port[i]);
	}
	us48_sw_unit_exit(u);
	/* Stopped for good: ports that never opened still get the explicit abort. */
	hrtimer_cancel(&u->poll_timer);
	WRITE_ONCE(u->quiesced, true);
	spin_lock_bh(&u->tx_lock);
	dma_stop_all(u);
	spin_unlock_bh(&u->tx_lock);
	cancel_work_sync(&u->pool_work);
	skb_queue_purge(&u->skb_pool);
	us48_selfcheck(u);
}
EXPORT_SYMBOL_GPL(us48_remove_common);

/*
 * Stock's CMIC_CPS_RESET (init-phases-u0.md), run between TOP_SOFT_RESET_REG_2 and _REG release. Without it a second
 * full init leaves CPU TX hung (#78). The reset also clears the GP pins and the MSPI strap override the fan/board-IO
 * chain uses and the kernel's SMBus master (xfer fails after 50 s): carry them over, then redo ring map/timeout/rate.
 */
#define CPS_RESET_WAIT_MS	9		/* OpenMDK _bmd_reset 10 ms, delay rule -10 % */

static void us48_cps_reset(struct bcm_cmic_unit *cu)
{
	void __iomem *b = cu->base;
	u32 gp_out = readl(b + HW_CMIC0_GP_DATA_OUT), gp_en = readl(b + HW_CMIC0_GP_OUT_EN);
	u32 strap = readl(b + HW_CMIC0_OVERRIDE_STRAP);
	static const u32 i2c_regs[] = { HW_CMIC0_I2CM_CFG, HW_CMIC0_I2CM_TIM_CFG, HW_CMIC0_I2CM_S_CFG_SMBUS_ADDR,
					HW_CMIC0_I2CM_M_FIFO_CTRL, HW_CMIC0_I2CM_S_FIFO_CTRL, HW_CMIC0_I2CM_IE };
	u32 i2c[ARRAY_SIZE(i2c_regs)];
	int i;

	for (i = 0; i < ARRAY_SIZE(i2c_regs); i++)
		i2c[i] = readl(b + i2c_regs[i]);

	writel(0, b + HW_CMIC0_RXBUF_EP_RLS_CRED);
	writel(1, b + HW_CMIC0_RXBUF_EP_RLS_CRED);
	writel(1, b + HW_CMIC0_CPS_RESET);
	mdelay(CPS_RESET_WAIT_MS);
	writel(0, b + HW_CMIC0_PCIE_PIO_ENDIANESS);
	writel(0, b + HW_CMIC0_UC0_PIO_ENDIANESS);
	writel(gp_out, b + HW_CMIC0_GP_DATA_OUT);
	writel(gp_en, b + HW_CMIC0_GP_OUT_EN);
	writel(strap, b + HW_CMIC0_OVERRIDE_STRAP);
	for (i = 0; i < ARRAY_SIZE(i2c_regs); i++)
		writel(i2c[i], b + i2c_regs[i]);
	bcm_hr2_cmic_setup(cu);
}

/* Cold init runs before the unit's us48_cmic exists: ops through the owner's lock only (nothing else of ours runs yet). */
struct cold_ctx {
	us48_schan_fn fn;
	void *ctx;
};

static int cu_schan_cold(struct bcm_cmic_unit *cu, u32 op, u32 blk, u32 addr, u32 *data, int words, u32 *ack)
{
	struct cold_ctx *c = cu->priv;

	return schan_exec(cu, c->fn, c->ctx, op, blk, addr, data, words, ack);
}

int us48_chip_cold_init(struct device *dev, void __iomem *cmic, us48_schan_fn schan_fn, void *schan_ctx, int unit)
{
	struct cold_ctx c = { .fn = schan_fn, .ctx = schan_ctx };
	struct bcm_cmic_unit cu = { .base = cmic, .pre_top_release = us48_cps_reset, .schan_op = cu_schan_cold, .priv = &c };
	int ch, ops = 0, errs = 0;

	/* The bootloader may leave a channel running into RAM Linux now owns: log it, abort + wait (bounded), then reset. */
	for (ch = 0; ch < 2; ch++) {
		u32 ctrl = ioread32(cmic + DMA_CTRL(ch));

		if ((ctrl & DMA_EN) || (ioread32(cmic + DMA_STAT) & DMA_ACTIVE(ch)))
			dev_info(dev, "left by bootloader: ch%d CTRL %08x DESC %08x STAT %08x\n", ch, ctrl,
				 ioread32(cmic + DMA_DESC(ch)), ioread32(cmic + DMA_STAT));
		bcm_hr2_dma_abort(&cu, ch);
	}
	/* Every load: full cold init (chip reset via the TOP resets of the list), whatever state the chip is in. */
	if (unit)
		bcm_hr2_u1_cold_init(&cu, BCM_HR2_INIT_ALL | BCM_HR2_INIT_FORCE | BCM_HR2_INIT_CPS, &ops, &errs);
	else
		bcm_hr2_u0_cold_init(&cu, BCM_HR2_INIT_ALL | BCM_HR2_INIT_FORCE | BCM_HR2_INIT_CPS, &ops, &errs);
	/* The RX credit release must follow the pipe reset (#46, #34). */
	bcm_hr2_dma_init(&cu);
	if (errs)
		dev_err(dev, "unit %d cold init: %d ops, %d FAILED\n", unit, ops, errs);
	else
		dev_info(dev, "unit %d cold init: %d ops\n", unit, ops);
	return 0;
}
EXPORT_SYMBOL_GPL(us48_chip_cold_init);

int us48_probe_common(struct device *dev, struct device *dma_dev, void __iomem *cmic, us48_schan_fn schan_fn,
		      void *schan_ctx, const struct port_cfg *cfgs, int nports, u8 modid, const struct firmware *fw,
		      us48_serdes_init_t serdes_init, struct us48_cmic **out)
{
	struct us48_cmic *u;
	u32 link[2];
	int i, ret;

	u = devm_kzalloc(dev, sizeof(*u), GFP_KERNEL);
	if (!u)
		return -ENOMEM;
	u->dev = dev;
	u->dma_dev = dma_dev;
	u->cfgs = cfgs;
	u->nports = nports;
	u->modid = modid;
	u->cmic = cmic;
	u->schan_fn = schan_fn;
	u->schan_ctx = schan_ctx;
	mutex_init(&u->schan_seq);
	u->cu = (struct bcm_cmic_unit){ .base = cmic, .schan_op = cu_schan_locked, .priv = u };
	mutex_init(&u->mdio);
	mutex_init(&u->link_lock);
	spin_lock_init(&u->tx_lock);
	skb_queue_head_init(&u->skb_pool);
	INIT_WORK(&u->pool_work, us48_pool_fill);
	INIT_DELAYED_WORK(&u->link_work, link_work_fn);
	ret = dma_set_mask_and_coherent(dma_dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;
	*out = u;

	/* The caller ran us48_chip_cold_init: channels are idle. Init (section 5): keep endian bits, ch0 TX / ch1 RX. */
	wr(u, DMA_CTRL(TX_CH), (rd(u, DMA_CTRL(TX_CH)) & DMA_ENDIAN) | DMA_DIR_TX);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & DMA_ENDIAN);
	wr(u, COS_CTRL_RX_0(RX_CH), 0xffffffff);
	wr(u, COS_CTRL_RX_1(RX_CH), 0xffff);

	u->rx_dcb = us48_alloc_coh(dev, dma_dev, (RX_RING + 1) * 64, &u->rx_dcb_dma);
	/* Data buffers are cached pages + streaming syncs: CPU copies out of uncached coherent memory cost ~1.4 us/byte (#46). */
	u->rx_buf = us48_alloc_buf(dev, dma_dev, RX_RING * BUF_SZ, &u->rx_buf_dma, DMA_FROM_DEVICE);
	u->tx_dcb = us48_alloc_coh(dev, dma_dev, TX_RING * 128, &u->tx_dcb_dma);
	u->tx_buf = us48_alloc_buf(dev, dma_dev, TX_RING * BUF_SZ, &u->tx_buf_dma, DMA_TO_DEVICE);
	if (!u->rx_dcb || !u->rx_buf || !u->tx_dcb || !u->tx_buf)
		return -ENOMEM;
	/* Endpoint inbound window covers bus < 0x08000000 only (section 14.3). */
	if (u->rx_buf_dma + RX_RING * BUF_SZ > SZ_128M || u->tx_buf_dma + TX_RING * BUF_SZ > SZ_128M ||
	    u->rx_dcb_dma > SZ_128M || u->tx_dcb_dma + TX_RING * 128 > SZ_128M) {
		dev_err(dev, "DMA memory above 128 MiB (rx %pad tx %pad)\n", &u->rx_buf_dma, &u->tx_buf_dma);
		return -ENOMEM;
	}

	u->napi_dev = alloc_netdev_dummy(0);
	if (!u->napi_dev)
		return -ENOMEM;
	netif_napi_add(u->napi_dev, &u->napi, us48_poll);
	ret = devm_add_action_or_reset(dev, us48_free_napi_dev, u);
	if (ret)
		return ret;
	hrtimer_setup(&u->poll_timer, us48_poll_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	us48_sw_unit_init(u);

	for (i = 0; i < u->nports; i++) {
		struct net_device *nd = devm_alloc_etherdev(dev, sizeof(struct us48_port));
		struct us48_port *p;

		if (!nd)
			return -ENOMEM;
		p = netdev_priv(nd);
		p->u = u;
		p->ndev = nd;
		p->cfg = &u->cfgs[i];
		p->vid = u->cfgs[i].vid;
		p->rx_vid = p->vid;
		p->learning = true;		/* bridge default BR_LEARNING */
		p->fw_mode = -1;
		u->port[i] = p;
		SET_NETDEV_DEV(nd, dev);
		nd->netdev_ops = &us48_netdev_ops;
		nd->watchdog_timeo = HZ;	/* one frame completes in us; 1 s = stuck, logged + aborted */
		nd->min_mtu = ETH_MIN_MTU;
		nd->max_mtu = BUF_SZ - ETH_HLEN - VLAN_HLEN - ETH_FCS_LEN - 4;
		/* Fixed locally administered MACs: one L2 entry per port, no growth across loads. */
		eth_hw_addr_set(nd, (const u8[ETH_ALEN]){ US48_MAC_PREFIX, p->cfg->front });
		strscpy(nd->name, p->cfg->name, IFNAMSIZ);
		if (p->cfg->copper)
			nd->sysfs_groups[0] = &us48_copper_group;
		netif_carrier_off(nd);
	}

	if (serdes_init) {
		ret = serdes_init(u, fw);
		if (ret)
			dev_err(dev, "SerDes bring-up failed (%d): 10G links stay down\n", ret);
	}
	us48_pwr_restore(u);
	for (i = 0; i < u->nports; i++) {
		struct us48_port *p = u->port[i];

		port_enable(p);
		/* Down until opened, as SDK _bcm_port_probe ("leave port disabled"); copper_enable() does VLAN + PORT_TAB. */
		if (p->cfg->copper) {
			us48_copper_power(p, false);
			continue;
		}
		port_vlan_init(p);
		port_tab_set(u, p->cfg->lport, p->vid, true);
		bcm_hr2_umc_cpu_unblock(&u->cu, p->cfg->lport);	/* #127 */
	}
	us48_led_chain_cfg(u);	/* SDK order: _hurricane2_ledup_init before _soc_hu2_power_Optimize */
	us48_pwr_save(u);
	if (!sbus(u, 0x0b, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2)) {
		for (i = 0; i < u->nports; i++)
			link[0] |= BIT(u->port[i]->cfg->lport);
		link[0] |= BIT(0);
		sbus(u, 0x0d, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2);
	}

	for (i = 0; i < u->nports; i++)
		if (!u->port[i]->cfg->copper)
			us48_xe_phylink_create(u->port[i]);	/* failure logged: that port has no link */
	for (i = 0; i < u->nports; i++) {
		ret = register_netdev(u->port[i]->ndev);
		if (ret) {
			us48_remove_common(u);
			return ret;
		}
		u->port[i]->registered = true;
		netdev_info(u->port[i]->ndev, "front %d (logical port %d), MAC %pM, VLAN %d\n", u->port[i]->cfg->front,
			    u->port[i]->cfg->lport, u->port[i]->ndev->dev_addr, u->port[i]->vid);
	}
	us48_led_unit_init(u);
	us48_pwr_xe_start(u);
	schedule_delayed_work(&u->link_work, msecs_to_jiffies(LINK_POLL_MS));
	schan_unit_set(u, true);
	if (tsc_log)
		us48_tsc_dump(u);
	return 0;
}
EXPORT_SYMBOL_GPL(us48_probe_common);

void us48_quiesce(struct us48_cmic *u)
{
	int i;

	schan_unit_set(u, false);
	us48_pwr_xe_stop(u);
	cancel_delayed_work_sync(&u->link_work);
	hrtimer_cancel(&u->poll_timer);
	WRITE_ONCE(u->quiesced, true);
	dma_stop_all(u);
	for (i = 0; i < u->nports; i++)
		port_tab_set(u, u->cfgs[i].lport, 1, false);
}
EXPORT_SYMBOL_GPL(us48_quiesce);

/* Unit 0: the host SoC's own CMIC on the AXI bus (HW_CMIC0_BASE), management port front 1. */
static uint unit0 = 1;
module_param(unit0, uint, 0444);
module_param_named(gaps, bcm_hr2_init_gaps, uint, 0644);
MODULE_PARM_DESC(gaps, "1 = sleep the stock trace gaps in the cold init (#34)");
MODULE_PARM_DESC(unit0, "1 (default) = drive unit 0 front 1 (ge1) over AXI; 0 = core only (us48_cmic_xe.ko needs the core loaded)");

#define UNIT0_CMIC_BASE	HW_CMIC0_BASE
#define UNIT0_CMIC_LEN	0x40000

static struct platform_device *unit0_pdev;
static struct us48_cmic *unit0_u;

struct us48_cmic *us48_unit0(void)
{
	return READ_ONCE(unit0_u);
}
EXPORT_SYMBOL_GPL(us48_unit0);

static bool sfp10g;
module_param(sfp10g, bool, 0444);
MODULE_PARM_DESC(sfp10g, "deprecated, no effect: xe51/xe52 (SFP 1/2, TSC0 lanes 0/2) always exist (#143)");

static int us48_cmic_plat_probe(struct platform_device *pdev)
{
	const struct firmware *fw = NULL;
	struct us48_cmic *u = NULL;
	void __iomem *cmic = devm_ioremap(&pdev->dev, UNIT0_CMIC_BASE, UNIT0_CMIC_LEN);
	int ret;

	if (!cmic)
		return -ENOMEM;
	if (sfp10g)
		dev_warn(&pdev->dev, "sfp10g is deprecated and has no effect: xe51/xe52 are always created (#143)\n");
	if (request_firmware(&fw, "us48-tsc.bin", &pdev->dev))
		fw = NULL;		/* us48_tsc_init logs it: xe51/xe52 stay down */
	ret = us48_chip_cold_init(&pdev->dev, cmic, NULL, NULL, 0);
	ret = ret ?: us48_probe_common(&pdev->dev, &pdev->dev, cmic, NULL, NULL, unit0_cfgs, ARRAY_SIZE(unit0_cfgs), 0, fw,
				       us48_tsc_init, &u);
	release_firmware(fw);
	if (!ret) {
		platform_set_drvdata(pdev, u);
		WRITE_ONCE(unit0_u, u);
	}
	return ret;
}

static void us48_cmic_plat_remove(struct platform_device *pdev)
{
	struct us48_cmic *u = platform_get_drvdata(pdev);

	WRITE_ONCE(unit0_u, NULL);
	mutex_lock(&dbg_lock);
	dbg_u = NULL;
	mutex_unlock(&dbg_lock);
	us48_remove_common(u);
}

static struct platform_driver us48_cmic_plat_driver = {
	.driver		= { .name = "us48_cmic_u0" },
	.probe		= us48_cmic_plat_probe,
	.remove		= us48_cmic_plat_remove,
};

static int __init us48_cmic_init(void)
{
	int ret = us48_sw_init();	/* unit 1 (us48_cmic_xe.ko) ports use it too */

	if (ret)
		return ret;
	ret = us48_led_init();
	if (ret)
		goto err_sw;
	if (!unit0)
		return 0;
	ret = platform_driver_register(&us48_cmic_plat_driver);
	if (ret)
		goto err_led;
	unit0_pdev = platform_device_register_simple("us48_cmic_u0", -1, NULL, 0);
	if (IS_ERR(unit0_pdev)) {
		ret = PTR_ERR(unit0_pdev);
		platform_driver_unregister(&us48_cmic_plat_driver);
		goto err_led;
	}
	return 0;
err_led:
	us48_led_exit();
err_sw:
	us48_sw_exit();
	return ret;
}

static void __exit us48_cmic_exit(void)
{
	if (unit0_pdev && !IS_ERR(unit0_pdev)) {
		platform_device_unregister(unit0_pdev);
		platform_driver_unregister(&us48_cmic_plat_driver);
	}
	us48_led_exit();
	us48_sw_exit();
}

module_init(us48_cmic_init);
module_exit(us48_cmic_exit);

MODULE_DESCRIPTION("UniFi US-48-500W BCM5334x switch port core + unit 0 front 1 (ge1, polled DMA)");
MODULE_LICENSE("GPL");
