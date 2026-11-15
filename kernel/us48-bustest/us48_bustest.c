// SPDX-License-Identifier: GPL-2.0
/*
 * us48_bustest: a1 side of the PCIe bus performance + integrity harness (#80), aux device us48_pci.bustest.
 * debugfs us48_bustest/: cmd (write a test), result (last result line), stats (link counters + temps), a2 (a2 results
 * struct), mismatch (log). Tests, units, stop rules: docs/pcie-harness.md. Driven by scripts/pcie_harness.py.
 */
#include <linux/auxiliary_bus.h>
#include <linux/bitfield.h>
#include <linux/bitops.h>
#include <linux/debugfs.h>
#include <linux/io.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/math64.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/pci.h>
#include <linux/random.h>
#include <linux/regmap.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include "bcm_cmic.h"
#include "bustest.h"
#include "us48_hwdb.h"
#include "us48_pci.h"
/* S-channel core is linked into each module that needs it (as us48_pci and us48_cmic do) */
#include "../../common/bcm-cmic/bcm_cmic_schan.c"

#define SRAM_PAGE	0x1000U
#define SRAM_WORDS	(HW_A2_SRAM_SIZE / 4)
#define CHUNK		1000	/* accesses per timed batch (IRQs off): ~2 ms at 2 us per read */
#define DEFAULT_LIMIT_MS 2000
#define MISS_LOG	32
#define POLL_MS		100
/* TOP PVTMON via S-channel block 16 (docs/clock-audit.md 3; not in hwdb yet): result 0x02050800 TEMP_DATA [9:0] */
#define PVT_BLK		16
#define PVT_CTRL0	0x02050000U
#define PVT_CTRL1	0x02050400U
#define PVT_RESULT	0x02050800U
#define PVT_CTRL0_INIT	0x001d2003U
#define PVT_CTRL1_RUN	0x80000000U
#define PVT_U0_CMC1	0x32000U	/* unit 0 CMC1 S-channel (hwdb HW_CMIC1_CMC1_SCHAN_CTRL offset), shared with us48-pvtmon */

struct bt_miss {
	u64 t_ns;
	char what[12];
	u32 addr, exp, got;
};

struct us48_bt {
	struct device *dev;
	struct us48_pci *p;
	struct pci_dev *ep, *rc;
	struct dentry *dir;
	struct mutex run_lock;		/* one test at a time, protects res/miss */
	char res[256];
	struct bt_miss miss[MISS_LOG];
	atomic_t miss_n;
	atomic64_t mismatches;
	struct delayed_work poll;
	spinlock_t st_lock;		/* link counters */
	struct bcm_cmic_unit u0;
	void __iomem *u0_map;
	void *a2_res, *a2_xy;	/* results page, x/y buffers in the a2 pool; valid only between a2_get and a2_put (run_lock held) */
	/* link counters */
	u64 polls, link_down, ep_chg, rc_chg, lt_seen, dl_inactive, perm_err;
	u64 ep_cor, ep_unc, rc_cor, rc_unc, ep_stat_err, rc_stat_err, rc_sec_err;
	u16 ep_lnk, rc_lnk;
	u32 paxb_link, paxb_rst, a2_y_seq_pushed;
	bool alive;
};

static bool u0_pvt = true;
module_param(u0_pvt, bool, 0444);
MODULE_PARM_DESC(u0_pvt, "read the unit 0 PVTMON through its CMC1 S-channel (shared with us48-pvtmon, retried)");

static void log_miss(struct us48_bt *b, const char *what, u32 addr, u32 exp, u32 got)
{
	int n = atomic_inc_return(&b->miss_n) - 1;
	struct bt_miss *m = &b->miss[n % MISS_LOG];

	atomic64_inc(&b->mismatches);
	m->t_ns = ktime_get_real_ns();
	strscpy(m->what, what, sizeof(m->what));
	m->addr = addr;
	m->exp = exp;
	m->got = got;
}

static void set_res(struct us48_bt *b, const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vscnprintf(b->res, sizeof(b->res), fmt, ap);
	va_end(ap);
}

/* --- link health poll --- */

static u32 popc_clear(struct pci_dev *d, int reg, u32 w1c_mask)
{
	u32 v;

	if (pci_read_config_dword(d, reg, &v) || v == ~0U)
		return 0;
	v &= w1c_mask;
	if (v)
		pci_write_config_dword(d, reg, v);
	return hweight32(v);
}

static u32 stat_err_clear(struct pci_dev *d, int reg)
{
	u16 v;

	if (pci_read_config_word(d, reg, &v) || v == 0xffff)
		return 0;
	v &= PCI_STATUS_DETECTED_PARITY | PCI_STATUS_SIG_SYSTEM_ERROR | PCI_STATUS_REC_MASTER_ABORT |
	     PCI_STATUS_REC_TARGET_ABORT | PCI_STATUS_SIG_TARGET_ABORT | PCI_STATUS_PARITY;
	if (v)
		pci_write_config_word(d, reg, v);
	return hweight16(v);
}

static void poll_fn(struct work_struct *w)
{
	struct us48_bt *b = container_of(to_delayed_work(w), struct us48_bt, poll);
	u16 ep = 0, rc = 0;
	u32 pl = 0, pr = 0;
	int aer;

	spin_lock(&b->st_lock);
	b->polls++;
	spin_unlock(&b->st_lock);
	if (us48_pci_link_ok(b->p)) {
		spin_lock(&b->st_lock);
		b->link_down++;
		spin_unlock(&b->st_lock);
		goto again;
	}
	pcie_capability_read_word(b->ep, PCI_EXP_LNKSTA, &ep);
	if (b->rc)
		pcie_capability_read_word(b->rc, PCI_EXP_LNKSTA, &rc);
	regmap_read(us48_pci_regmap(b->p), HW_PAXB0_BASE + HW_PAXB0_PCIE_LINK_STATUS, &pl);
	regmap_read(us48_pci_regmap(b->p), HW_PAXB0_BASE + HW_PAXB0_RESET_STATUS, &pr);
	spin_lock(&b->st_lock);
	if (b->ep_lnk && (ep & (PCI_EXP_LNKSTA_CLS | PCI_EXP_LNKSTA_NLW)) != (b->ep_lnk & (PCI_EXP_LNKSTA_CLS | PCI_EXP_LNKSTA_NLW)))
		b->ep_chg++;
	if (b->rc_lnk && (rc & (PCI_EXP_LNKSTA_CLS | PCI_EXP_LNKSTA_NLW)) != (b->rc_lnk & (PCI_EXP_LNKSTA_CLS | PCI_EXP_LNKSTA_NLW)))
		b->rc_chg++;
	if ((ep | rc) & PCI_EXP_LNKSTA_LT)
		b->lt_seen++;
	if (!(pl & HW_PAXB0_PCIE_LINK_STATUS_PCIE_DL_ACTIVE_MASK) || !(pl & HW_PAXB0_PCIE_LINK_STATUS_PCIE_PHYLINKUP_MASK))
		b->dl_inactive++;
	b->ep_lnk = ep;
	b->rc_lnk = rc;
	b->paxb_link = pl;
	b->paxb_rst = pr;
	spin_unlock(&b->st_lock);
	/* PCI_STATUS error bits (master/target abort, parity, SERR) = what PAXB reports on a failed access; W1C counted */
	{
		u32 e = stat_err_clear(b->ep, PCI_STATUS), r = b->rc ? stat_err_clear(b->rc, PCI_STATUS) : 0;
		u32 rs = b->rc ? stat_err_clear(b->rc, PCI_SEC_STATUS) : 0;
		u32 ec = 0, eu = 0, rcc = 0, ru = 0;

		aer = pci_find_ext_capability(b->ep, PCI_EXT_CAP_ID_ERR);
		if (aer) {
			ec = popc_clear(b->ep, aer + PCI_ERR_COR_STATUS, ~0U);
			eu = popc_clear(b->ep, aer + PCI_ERR_UNCOR_STATUS, ~0U);
		}
		aer = b->rc ? pci_find_ext_capability(b->rc, PCI_EXT_CAP_ID_ERR) : 0;
		if (aer) {
			rcc = popc_clear(b->rc, aer + PCI_ERR_COR_STATUS, ~0U);
			ru = popc_clear(b->rc, aer + PCI_ERR_UNCOR_STATUS, ~0U);
		}
		spin_lock(&b->st_lock);
		b->ep_stat_err += e;
		b->rc_stat_err += r;
		b->rc_sec_err += rs;
		b->ep_cor += ec;
		b->ep_unc += eu;
		b->rc_cor += rcc;
		b->rc_unc += ru;
		spin_unlock(&b->st_lock);
	}
again:
	if (b->alive)
		schedule_delayed_work(&b->poll, msecs_to_jiffies(POLL_MS));
}

/* --- temperatures: PVTMON through S-channel --- */

static int pvt_op(struct us48_bt *b, int unit, u32 op, u32 addr, u32 *d)
{
	return unit ? us48_pci_schan(b->p, op, PVT_BLK, addr, d, 1, NULL) : bcm_cmic_schan_op(&b->u0, op, PVT_BLK, addr, d, 1, NULL);
}

/* mC, or -1; start the sensor if CTRL_1 says it is stopped (same sequence as us48-pvtmon); 3 tries for the shared CMC1 */
static int pvt_mc(struct us48_bt *b, int unit)
{
	int tries;

	if (!unit && (!b->u0_map || !u0_pvt))
		return -1;
	for (tries = 0; tries < 3; tries++) {
		u32 d = 0, code;

		if (pvt_op(b, unit, BCM_SCHAN_READ_REG, PVT_CTRL1, &d))
			continue;
		if (!(d & PVT_CTRL1_RUN)) {
			d = PVT_CTRL0_INIT;
			pvt_op(b, unit, BCM_SCHAN_WRITE_REG, PVT_CTRL0, &d);
			d = 0;
			pvt_op(b, unit, BCM_SCHAN_WRITE_REG, PVT_CTRL1, &d);
			d = PVT_CTRL1_RUN;
			pvt_op(b, unit, BCM_SCHAN_WRITE_REG, PVT_CTRL1, &d);
		}
		if (pvt_op(b, unit, BCM_SCHAN_READ_REG, PVT_RESULT, &d))
			continue;
		code = d & 0x3ff;
		if (!code || (4180000 - 5556 * (int)code) / 10 < -20000 || (4180000 - 5556 * (int)code) / 10 > 150000)
			continue;
		return (4180000 - 5556 * (int)code) / 10;
	}
	return -1;
}

/* --- a1 -> unit 1 tests --- */

struct tctx {
	ktime_t t0;
	u32 limit_ms;
};

static bool over(struct tctx *c)
{
	return ktime_ms_delta(ktime_get(), c->t0) > c->limit_ms;
}

static const char *kind_name[] = { "walk1", "walk0", "addr", "prbs31" };

static u32 pat_next(int kind, u32 i, u32 addr, u32 *s)
{
	switch (kind) {
	case 0:
		return 1U << (i & 31);
	case 1:
		return ~(1U << (i & 31));
	case 2:
		return addr + i * 4;
	default:
		*s = ((*s << 1) | (((*s >> 30) ^ (*s >> 27)) & 1)) & 0x7fffffffU;	/* x^31 + x^28 + 1 */
		return *s ^ (*s << 31);
	}
}

/* SRAM save/restore: the a2 stub only uses it outside payload/bustest modes, content is put back after each test */
static u32 *sram_save(struct us48_bt *b)
{
	u32 *buf = kmalloc(HW_A2_SRAM_SIZE, GFP_KERNEL);
	u32 pg;

	if (!buf)
		return NULL;
	for (pg = 0; pg < HW_A2_SRAM_SIZE; pg += SRAM_PAGE) {
		void __iomem *va = us48_pci_win_get(b->p, HW_A2_SRAM_BASE + pg);

		if (IS_ERR(va)) {
			kfree(buf);
			return NULL;
		}
		__ioread32_copy(buf + pg / 4, va, SRAM_PAGE / 4);
		us48_pci_win_put(b->p);
	}
	return buf;
}

static void sram_restore(struct us48_bt *b, u32 *buf)
{
	u32 pg;

	for (pg = 0; pg < HW_A2_SRAM_SIZE; pg += SRAM_PAGE) {
		void __iomem *va = us48_pci_win_get(b->p, HW_A2_SRAM_BASE + pg);

		if (IS_ERR(va))
			break;
		__iowrite32_copy(va, buf + pg / 4, SRAM_PAGE / 4);
		us48_pci_win_put(b->p);
	}
	kfree(buf);
}

static void t_lat(struct us48_bt *b, unsigned long n, struct tctx *c)
{
	u64 rd_sum = 0, wr_sum = 0, rd_min = ~0ULL, rd_max = 0, wr_min = ~0ULL, wr_max = 0, done = 0;
	const char *st = "ok";
	u32 *save = sram_save(b);

	if (!save) {
		set_res(b, "test=lat status=fail err=link_or_nomem");
		return;
	}
	while (done < n) {
		void __iomem *va = us48_pci_win_get(b->p, HW_A2_SRAM_BASE + 0x1000);	/* page 1; restored below */
		unsigned long flags;
		u64 t0, t1, t2;
		u32 i, s = 0;

		if (IS_ERR(va)) {
			st = "link";
			break;
		}
		local_irq_save(flags);
		t0 = ktime_get_ns();
		for (i = 0; i < CHUNK; i++)
			s += readl_relaxed(va + (i & 0x3ff) * 4);
		t1 = ktime_get_ns();
		for (i = 0; i < CHUNK; i++)
			writel_relaxed(i, va + (i & 0x3ff) * 4);
		(void)readl(va);	/* a posted write is done when a read behind it returns */
		t2 = ktime_get_ns();
		local_irq_restore(flags);
		us48_pci_win_put(b->p);
		if (s == 0xdeadbeef)
			pr_debug("x");
		rd_sum += t1 - t0;
		wr_sum += t2 - t1;
		rd_min = min(rd_min, t1 - t0);
		rd_max = max(rd_max, t1 - t0);
		wr_min = min(wr_min, t2 - t1);
		wr_max = max(wr_max, t2 - t1);
		done += CHUNK;
		if (over(c)) {
			st = "timeout";
			break;
		}
	}
	sram_restore(b, save);
	set_res(b, "test=lat status=%s n=%llu rd_ns=%llu rd_ns_min=%llu rd_ns_max=%llu wr_ns=%llu wr_ns_min=%llu wr_ns_max=%llu",
		st, done, div64_u64(rd_sum, done ?: 1), div_u64(rd_min, CHUNK), div_u64(rd_max, CHUNK), div64_u64(wr_sum, done ?: 1),
		div_u64(wr_min, CHUNK), div_u64(wr_max, CHUNK));
}

static void t_burst(struct us48_bt *b, unsigned long n, struct tctx *c)
{
	u64 rd_ns = 0, wr_ns = 0, bytes = 0, k;
	const char *st = "ok";
	u32 *save = sram_save(b), *tmp = kmalloc(SRAM_PAGE, GFP_KERNEL);

	if (!save || !tmp) {
		kfree(save);
		kfree(tmp);
		set_res(b, "test=burst status=fail err=link_or_nomem");
		return;
	}
	for (k = 0; k < n; k++) {
		void __iomem *va = us48_pci_win_get(b->p, HW_A2_SRAM_BASE + 0x1000);
		u64 t0, t1, t2;

		if (IS_ERR(va)) {
			st = "link";
			break;
		}
		t0 = ktime_get_ns();
		__ioread32_copy(tmp, va, SRAM_PAGE / 4);
		t1 = ktime_get_ns();
		__iowrite32_copy(va, tmp, SRAM_PAGE / 4);
		(void)readl(va);
		t2 = ktime_get_ns();
		us48_pci_win_put(b->p);
		rd_ns += t1 - t0;
		wr_ns += t2 - t1;
		bytes += SRAM_PAGE;
		if (over(c)) {
			st = "timeout";
			break;
		}
	}
	sram_restore(b, save);
	kfree(tmp);
	/* bytes / ns = GB/s; x1000 = MB/s */
	set_res(b, "test=burst status=%s n=%llu bytes=%llu rd_MBps=%llu wr_MBps=%llu", st, k, bytes,
		div64_u64(bytes * 1000, rd_ns ?: 1), div64_u64(bytes * 1000, wr_ns ?: 1));
}

static void t_schan(struct us48_bt *b, unsigned long n, struct tctx *c)
{
	u64 t0 = ktime_get_ns(), ns, k, err = 0;
	const char *st = "ok";
	u32 d;

	for (k = 0; k < n; k++) {
		if (us48_pci_schan(b->p, BCM_SCHAN_READ_REG, PVT_BLK, PVT_RESULT, &d, 1, NULL))
			err++;
		if (over(c)) {
			st = "timeout";
			break;
		}
	}
	ns = ktime_get_ns() - t0;
	if (err)
		st = "fail";
	set_res(b, "test=schan status=%s n=%llu err=%llu ops_per_s=%llu us_per_op=%llu", st, k, err,
		div64_u64(k * 1000000000ULL, ns ?: 1), div64_u64(ns, (k ?: 1) * 1000));
}

static void t_pat(struct us48_bt *b, int kind, unsigned long rounds, struct tctx *c)
{
	u64 words = 0, bad = 0, r;
	const char *st = "ok";
	u32 *save = sram_save(b);

	if (!save) {
		set_res(b, "test=pat status=fail err=link_or_nomem");
		return;
	}
	for (r = 0; r < rounds && !strcmp(st, "ok"); r++) {
		u32 s = 0x1234567U + r, pg, i;

		for (pg = 0; pg < HW_A2_SRAM_SIZE; pg += SRAM_PAGE) {
			void __iomem *va = us48_pci_win_get(b->p, HW_A2_SRAM_BASE + pg);

			if (IS_ERR(va)) {
				st = "link";
				break;
			}
			for (i = 0; i < SRAM_PAGE / 4; i++)
				writel_relaxed(pat_next(kind, pg / 4 + i, HW_A2_SRAM_BASE, &s), va + i * 4);
			us48_pci_win_put(b->p);
		}
		s = 0x1234567U + r;
		for (pg = 0; pg < HW_A2_SRAM_SIZE && !strcmp(st, "ok"); pg += SRAM_PAGE) {
			void __iomem *va = us48_pci_win_get(b->p, HW_A2_SRAM_BASE + pg);

			if (IS_ERR(va)) {
				st = "link";
				break;
			}
			for (i = 0; i < SRAM_PAGE / 4; i++) {
				u32 e = pat_next(kind, pg / 4 + i, HW_A2_SRAM_BASE, &s), g = readl_relaxed(va + i * 4);

				if (e != g) {
					log_miss(b, kind_name[kind], HW_A2_SRAM_BASE + pg + i * 4, e, g);
					bad++;
				}
			}
			us48_pci_win_put(b->p);
		}
		words += SRAM_WORDS;
		if (bad)
			st = "mismatch";
		else if (over(c))
			st = "timeout";
	}
	sram_restore(b, save);
	set_res(b, "test=pat_%s status=%s rounds=%llu words=%llu mismatches=%llu", kind_name[kind], st, r, words, bad);
}

/* --- a1 <-> a2 carveout --- */

/* a2 pool from us48_a2 (soft dependency: symbol_get); the pool exists only while a2 is prepared. */
static void *(*a2_region_get)(size_t *size);
static void (*a2_region_put)(void);

static int a2_get(struct us48_bt *b)
{
	size_t size;
	void *va;

	a2_region_get = symbol_get(us48_a2_region_get);
	if (!a2_region_get)
		return -ENODEV;
	a2_region_put = symbol_get(us48_a2_region_put);
	va = a2_region_get(&size);
	if (IS_ERR(va) || !a2_region_put || size < BT_XBUF_OFF + 2 * (BT_YBUF_OFF - BT_XBUF_OFF)) {
		symbol_put(us48_a2_region_get);
		if (a2_region_put)
			symbol_put(us48_a2_region_put);
		return IS_ERR(va) ? PTR_ERR(va) : -ENODEV;
	}
	b->a2_res = va + BT_RES_OFF;
	b->a2_xy = va + BT_XBUF_OFF;
	return 0;
}

static void a2_put(struct us48_bt *b)
{
	if (!b->a2_res)
		return;
	b->a2_res = b->a2_xy = NULL;
	a2_region_put();
	symbol_put(us48_a2_region_put);
	symbol_put(us48_a2_region_get);
}

static struct bt_results *a2r(struct us48_bt *b)
{
	return b->a2_res;
}

static int a2_magic_ok(struct us48_bt *b)
{
	return b->a2_res && READ_ONCE(a2r(b)->magic) == BT_MAGIC && a2r(b)->version == BT_VERSION;
}

static void t_a2push(struct us48_bt *b)
{
	struct bt_results *r = a2r(b);
	volatile u32 *y = b->a2_xy + (BT_YBUF_OFF - BT_XBUF_OFF);
	u32 seed, i;

	if (!a2_magic_ok(b)) {
		set_res(b, "test=a2push status=fail err=a2_not_running");
		return;
	}
	if (READ_ONCE(r->y_ack) != READ_ONCE(r->y_seq)) {
		set_res(b, "test=a2push status=busy seq=%u ack=%u", r->y_seq, r->y_ack);
		return;
	}
	seed = get_random_u32();
	for (i = 0; i < BT_XY_SIZE / 4; i++)
		y[i] = bt_ypat(seed, i);
	wmb();
	WRITE_ONCE(r->y_seed, seed);
	wmb();
	WRITE_ONCE(r->y_seq, r->y_seq + 1);
	set_res(b, "test=a2push status=ok seq=%u seed=%08x y_mismatch=%u", r->y_seq, seed, r->y_mismatch);
}

static void t_a2xverify(struct us48_bt *b)
{
	volatile u32 *x = b->a2_xy;
	u32 i, bad = 0;

	if (!a2_magic_ok(b) || !READ_ONCE(a2r(b)->x_ready)) {
		set_res(b, "test=a2xverify status=fail err=a2_not_running");
		return;
	}
	for (i = 0; i < BT_XY_SIZE / 4; i++) {
		u32 g = x[i];

		if (g != bt_xpat(i)) {
			log_miss(b, "a2->a1 x", HW_A2_CARVEOUT_BASE + BT_XBUF_OFF + i * 4, bt_xpat(i), g);
			bad++;
		}
	}
	set_res(b, "test=a2xverify status=%s words=%u mismatches=%u", bad ? "mismatch" : "ok", BT_XY_SIZE / 4, bad);
}

/* --- debugfs --- */

static ssize_t cmd_write(struct file *f, const char __user *ubuf, size_t len, loff_t *pos)
{
	struct us48_bt *b = file_inode(f)->i_private;
	char buf[96], name[24];
	unsigned long n = 0, limit = DEFAULT_LIMIT_MS;
	struct tctx c;
	int got;

	if (len >= sizeof(buf))
		return -EINVAL;
	if (copy_from_user(buf, ubuf, len))
		return -EFAULT;
	buf[len] = 0;
	got = sscanf(buf, "%23s %lu %lu", name, &n, &limit);
	if (got < 1)
		return -EINVAL;
	mutex_lock(&b->run_lock);
	c.t0 = ktime_get();
	c.limit_ms = limit;
	if (!strcmp(name, "lat"))
		t_lat(b, n ? n : 20000, &c);
	else if (!strcmp(name, "burst"))
		t_burst(b, n ? n : 200, &c);
	else if (!strcmp(name, "schan"))
		t_schan(b, n ? n : 500, &c);
	else if (!strncmp(name, "pat_", 4) && (!strcmp(name + 4, "walk1") || !strcmp(name + 4, "walk0") ||
					      !strcmp(name + 4, "addr") || !strcmp(name + 4, "prbs31"))) {
		int k;

		for (k = 0; strcmp(kind_name[k], name + 4); k++)
			;
		t_pat(b, k, n ? n : 1, &c);
	} else if (!strcmp(name, "a2push")) {
		a2_get(b);
		t_a2push(b);
		a2_put(b);
	} else if (!strcmp(name, "a2xverify")) {
		a2_get(b);
		t_a2xverify(b);
		a2_put(b);
	} else if (!strcmp(name, "a2clear")) {
		if (!a2_get(b))
			memset(b->a2_res, 0, sizeof(struct bt_results));
		a2_put(b);
		set_res(b, "test=a2clear status=ok");
	} else {
		set_res(b, "test=%s status=fail err=unknown_test", name);
	}
	set_res(b, "%s dur_ms=%lld", b->res, ktime_ms_delta(ktime_get(), c.t0));
	mutex_unlock(&b->run_lock);
	return len;
}

static const struct file_operations cmd_fops = { .write = cmd_write, .open = simple_open, .llseek = noop_llseek };

static ssize_t result_read(struct file *f, char __user *ubuf, size_t len, loff_t *pos)
{
	struct us48_bt *b = f->private_data;
	char line[300];
	int n;

	mutex_lock(&b->run_lock);
	n = scnprintf(line, sizeof(line), "%s\n", b->res);
	mutex_unlock(&b->run_lock);
	return simple_read_from_buffer(ubuf, len, pos, line, n);
}

static const struct file_operations result_fops = { .read = result_read, .open = simple_open, .llseek = default_llseek };

static int stats_show(struct seq_file *m, void *unused)
{
	struct us48_bt *b = m->private;
	u16 ep, rc;
	u32 pl, pr;
	u64 v[12];
	int t0, t1;

	t1 = pvt_mc(b, 1);
	t0 = pvt_mc(b, 0);
	spin_lock(&b->st_lock);
	ep = b->ep_lnk;
	rc = b->rc_lnk;
	pl = b->paxb_link;
	pr = b->paxb_rst;
	v[0] = b->polls;
	v[1] = b->link_down;
	v[2] = b->ep_chg;
	v[3] = b->rc_chg;
	v[4] = b->lt_seen;
	v[5] = b->dl_inactive;
	v[6] = b->ep_cor;
	v[7] = b->ep_unc;
	v[8] = b->rc_cor;
	v[9] = b->rc_unc;
	v[10] = b->ep_stat_err + b->rc_stat_err + b->rc_sec_err;
	v[11] = atomic64_read(&b->mismatches);
	spin_unlock(&b->st_lock);
	seq_printf(m, "t_ns=%llu ep_width=%u ep_speed=%u rc_width=%u rc_speed=%u ep_lnksta=%04x rc_lnksta=%04x ", ktime_get_real_ns(),
		   FIELD_GET(PCI_EXP_LNKSTA_NLW, ep), FIELD_GET(PCI_EXP_LNKSTA_CLS, ep),
		   FIELD_GET(PCI_EXP_LNKSTA_NLW, rc), FIELD_GET(PCI_EXP_LNKSTA_CLS, rc), ep, rc);
	seq_printf(m, "paxb_link=%08x paxb_reset=%08x polls=%llu link_down=%llu ep_link_chg=%llu rc_link_chg=%llu lt_seen=%llu dl_inactive=%llu ",
		   pl, pr, v[0], v[1], v[2], v[3], v[4], v[5]);
	seq_printf(m, "aer_ep_cor=%llu aer_ep_unc=%llu aer_rc_cor=%llu aer_rc_unc=%llu pci_status_err=%llu mismatches=%llu t0_mc=%d t1_mc=%d\n",
		   v[6], v[7], v[8], v[9], v[10], v[11], t0, t1);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(stats);

static int a2_show(struct seq_file *m, void *unused)
{
	struct us48_bt *b = m->private;
	struct bt_results r;

	mutex_lock(&b->run_lock);
	a2_get(b);
	if (!a2_magic_ok(b)) {
		a2_put(b);
		mutex_unlock(&b->run_lock);
		seq_puts(m, "a2=absent\n");
		return 0;
	}
	memcpy(&r, b->a2_res, sizeof(r));
	a2_put(b);
	mutex_unlock(&b->run_lock);
	seq_printf(m, "a2=running tick_hz=%u", BT_TICK_HZ);
#define BT_P(n) seq_printf(m, " " #n "=%u", r.n);
	BT_FIELDS(BT_P)
#undef BT_P
	seq_putc(m, '\n');
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(a2);

static int miss_show(struct seq_file *m, void *unused)
{
	struct us48_bt *b = m->private;
	int n = atomic_read(&b->miss_n), i, from = n > MISS_LOG ? n - MISS_LOG : 0;

	seq_printf(m, "total=%d\n", n);
	for (i = from; i < n; i++) {
		struct bt_miss *e = &b->miss[i % MISS_LOG];

		seq_printf(m, "t_ns=%llu what=%s addr=%08x exp=%08x got=%08x\n", e->t_ns, e->what, e->addr, e->exp, e->got);
	}
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(miss);

/* --- aux --- */

static int bt_probe(struct auxiliary_device *adev, const struct auxiliary_device_id *id)
{
	struct device *dev = &adev->dev;
	struct us48_bt *b;

	b = devm_kzalloc(dev, sizeof(*b), GFP_KERNEL);
	if (!b)
		return -ENOMEM;
	b->dev = dev;
	b->p = us48_pci_from_adev(adev);
	b->ep = to_pci_dev(us48_pci_dev(b->p));
	b->rc = pci_upstream_bridge(b->ep);
	mutex_init(&b->run_lock);
	spin_lock_init(&b->st_lock);
	INIT_DELAYED_WORK(&b->poll, poll_fn);
	set_res(b, "none");
	if (u0_pvt) {
		/* CMC1 S-channel ctrl at CMIC0 + 0x32000 = base + BCM_CMIC_SCHAN_CTRL (0x31000) */
		b->u0_map = devm_ioremap(dev, HW_CMIC0_BASE + 0x1000, PVT_U0_CMC1 + 0x100);
		b->u0.base = b->u0_map;
	}
	b->dir = debugfs_create_dir("us48_bustest", NULL);
	debugfs_create_file("cmd", 0200, b->dir, b, &cmd_fops);
	debugfs_create_file("result", 0444, b->dir, b, &result_fops);
	debugfs_create_file("stats", 0444, b->dir, b, &stats_fops);
	debugfs_create_file("a2", 0444, b->dir, b, &a2_fops);
	debugfs_create_file("mismatch", 0444, b->dir, b, &miss_fops);
	auxiliary_set_drvdata(adev, b);
	b->alive = true;
	schedule_delayed_work(&b->poll, msecs_to_jiffies(POLL_MS));
	return 0;
}

static void bt_remove(struct auxiliary_device *adev)
{
	struct us48_bt *b = auxiliary_get_drvdata(adev);

	b->alive = false;
	cancel_delayed_work_sync(&b->poll);
	debugfs_remove_recursive(b->dir);
}

static const struct auxiliary_device_id bt_ids[] = {
	{ .name = US48_PCI_AUX_ID(US48_PCI_AUX_BUSTEST) },
	{ }
};
MODULE_DEVICE_TABLE(auxiliary, bt_ids);

static struct auxiliary_driver bt_driver = {
	.name		= "bustest",
	.id_table	= bt_ids,
	.probe		= bt_probe,
	.remove		= bt_remove,
};
module_auxiliary_driver(bt_driver);

MODULE_DESCRIPTION("UniFi US-48-500W PCIe bus performance + integrity harness (a1 side)");
MODULE_LICENSE("GPL");
