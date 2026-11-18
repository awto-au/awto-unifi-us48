// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic switchdev L2 offload, stage 1 (docs/switchdev.md, #57): a VLAN-unaware Linux bridge = one ASIC VLAN + STG per
 * unit. HW learns and ages (L2 poll reports it to the bridge); static/local FDB -> L2X; STP state -> STG_TAB/EGR_VLAN_STG.
 * One hardware domain per unit: cross-unit bridge traffic goes through the CPU (HiGig offload = stage 2). All state: rtnl.
 */
#include <linux/etherdevice.h>
#include <linux/if_bridge.h>
#include <linux/module.h>
#include <linux/netdevice.h>
#include <linux/rtnetlink.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include <net/switchdev.h>

#include "us48_cmic.h"
#include "bcm_cmic.h"

static bool switchdev = true;
module_param(switchdev, bool, 0644);
MODULE_PARM_DESC(switchdev, "1 (default) = offload Linux bridges of us48 ports to the ASIC; 0 = CPU-path bridge (bridges made after the change)");
static bool mgmt_bridge;
module_param(mgmt_bridge, bool, 0644);
MODULE_PARM_DESC(mgmt_bridge, "1 = allow ge1 (management) in an offloaded bridge; default 0 refuses the join");

/* Bridge slot i: ASIC VLAN SW_VID_BASE + i, STG SW_STG_BASE + i; unused by stock and our init (tmp/schan_stock_writes*.txt). */
#define SW_VID_BASE		4000
#define SW_STG_BASE		2

/* Field positions (lsb, width): regdb/hr2_mems.tsv */
struct fld {
	u16 lsb;
	u8 w;
};

#define FLD(l, n)		((struct fld){ .lsb = (l), .w = (n) })
#define VT_PORT_BITMAP		FLD(0, 30)
#define VT_VALID		FLD(38, 1)
#define VT_STG			FLD(39, 9)
#define VT_FID_ID		FLD(48, 12)
#define VT_PARITY		126
#define EV_UT_BITMAP		FLD(0, 30)
#define EV_PORT_BITMAP		FLD(30, 30)
#define EV_STG			FLD(60, 8)
#define EV_PARITY		76
#define STG_PARITY		60
#define PT_PORT_VID		FLD(24, 12)
#define PT_CML_NEW		FLD(121, 4)
#define PT_CML_MOVE		FLD(125, 4)
#define L2_KEY_TYPE		FLD(0, 2)
#define L2_VLAN_ID		FLD(2, 12)
#define L2_MAC			FLD(14, 48)
#define L2_PORT_NUM		FLD(62, 6)
#define L2_MODULE_ID		FLD(68, 7)
#define L2_T			FLD(75, 1)
#define L2_CPU			FLD(91, 1)
#define L2_STATIC		FLD(97, 1)
#define L2_VALID		FLD(99, 1)
#define L2_PARITY		100
#define L2U_VALID		FLD(0, 1)
#define L2U_MAC			FLD(1, 48)
#define L2U_MASK_MAC		FLD(70, 48)	/* MASK 70:69 = KEY 1:69 layout (SDK l2cache.c _bcm_l2_cache_to_l2u) */
#define L2U_MASK_KEY_TYPE	FLD(130, 1)
#define L2U_CPU			FLD(142, 1)
#define L2U_PORT_NUM		FLD(148, 6)
#define L2U_MODULE_ID		FLD(154, 7)
#define L2U_BPDU		FLD(162, 1)
#define L2U_WORDS		6
/* CML_FLAGS 8 = hardware learn + forward (SDK bcm/esw/trx/port.c _bcm_trx_port_cml_flags2hw; #57 learn run) */
#define CML_LEARN		8
#define BPDU_MAC		0x0180c2000000ULL

struct us48_host {
	struct list_head node;
	u8 mac[ETH_ALEN];
};

/*
 * HW-learned entries -> bridge: periodic L2X diff, as the SDK default on HR2 (L2MODE_POLL: soc_feature_l2_modfifo but not
 * l2_modfifo_def, soc/esw/l2xmsg.c soc_l2x_mode_cfg_get). Same pace as its _soc_l2x_thread: 3 s pass in 8 chunks.
 */
#define L2POLL_CHUNKS		8
#define L2POLL_CHUNK		(L2X_ENTRIES / L2POLL_CHUNKS)
#define L2POLL_MS		(3000 / L2POLL_CHUNKS)	/* bcm/esw/l2.c l2xmsg_thread_usec default 3000000 */

struct us48_l2rec {
	u8 mac[ETH_ALEN];
	u16 vid;			/* 0 = nothing reported for this index */
	u8 lport;
};

static struct workqueue_struct *sw_wq;
static struct notifier_block sw_atomic_nb, sw_blocking_nb;

static u64 ent_get(const u32 *e, struct fld f)
{
	u64 v = 0;
	int i;

	for (i = 0; i < f.w; i++)
		if (e[(f.lsb + i) / 32] & BIT((f.lsb + i) % 32))
			v |= BIT_ULL(i);
	return v;
}

static void ent_put(u32 *e, struct fld f, u64 v)
{
	int i;

	for (i = 0; i < f.w; i++) {
		u32 m = BIT((f.lsb + i) % 32);

		if (v & BIT_ULL(i))
			e[(f.lsb + i) / 32] |= m;
		else
			e[(f.lsb + i) / 32] &= ~m;
	}
}

/* EVEN_PARITY over the whole entry (every table here keeps its parity bit as the top used bit). */
static void parity(u32 *e, int words, int par)
{
	int i, ones = 0;

	ent_put(e, FLD(par, 1), 0);
	for (i = 0; i < words; i++)
		ones += hweight32(e[i]);
	ent_put(e, FLD(par, 1), ones & 1);
}

static int mrd(struct us48_cmic *u, u32 blk, u32 a, u32 *e, int w)
{
	return us48_sbus(u, BCM_SCHAN_READ_MEM, blk, a, e, w);
}

static int mwr(struct us48_cmic *u, u32 blk, u32 a, u32 *e, int w)
{
	return us48_sbus(u, BCM_SCHAN_WRITE_MEM, blk, a, e, w);
}

/* BR_STATE_* -> SP_TREE_PORTn: SDK include/soc/ptable.h PVP_STP_DISABLED 0, BLOCKING 1 (also listening), LEARNING 2, FORWARDING 3 */
static u8 stp_hw(u8 st)
{
	switch (st) {
	case BR_STATE_FORWARDING:
		return 3;
	case BR_STATE_LEARNING:
		return 2;
	case BR_STATE_LISTENING:
	case BR_STATE_BLOCKING:
		return 1;
	default:
		return 0;
	}
}

static bool stp_learns(u8 st)
{
	return st == BR_STATE_LEARNING || st == BR_STATE_FORWARDING;
}

/* SP_TREE_PORT<lport> in STG_TAB (ingress) and EGR_VLAN_STG (egress), same layout. */
static int stg_port_set(struct us48_cmic *u, u8 stg, int lport, u8 hw)
{
	const u32 blk[] = { BLK_IPIPE, BLK_EPIPE }, addr[] = { STG_TAB(stg), EGR_VLAN_STG(stg) };
	u32 e[2];
	int i, ret = 0;

	for (i = 0; i < 2 && !ret; i++) {
		ret = mrd(u, blk[i], addr[i], e, 2);
		if (ret)
			break;
		ent_put(e, FLD(2 * lport, 2), hw);
		parity(e, 2, STG_PARITY);
		ret = mwr(u, blk[i], addr[i], e, 2);
	}
	return ret;
}

/* Whole STG entry: CPU (port 0) forwarding as ge_vlan_init, every other port disabled (SDK fb stg.c _bcm_xgs3_stg_stp_init). */
static int stg_init(struct us48_cmic *u, u8 stg, bool clear)
{
	u32 e[2] = { };

	if (!clear)
		ent_put(e, FLD(0, 2), 3);
	parity(e, 2, STG_PARITY);
	return mwr(u, BLK_IPIPE, STG_TAB(stg), e, 2) ?: mwr(u, BLK_EPIPE, EGR_VLAN_STG(stg), e, 2);
}

/* VLAN_TAB/EGR_VLAN[vid] cloned from VLAN 1 (ge_vlan_init pattern), CPU only, own STG; FID_ID = vid keys L2 on vid (SVL or not). */
static int br_vlan_create(struct us48_cmic *u, struct us48_br *b)
{
	u32 e[4], g[3];
	int ret;

	ret = mrd(u, BLK_IPIPE, VLAN_TAB(b->vid), e, 4);
	if (ret || ent_get(e, VT_VALID)) {
		dev_err(u->dev, "switchdev: VLAN %u %s: not touching\n", b->vid, ret ? "unreadable" : "already valid");
		return ret ?: -EBUSY;
	}
	ret = mrd(u, BLK_IPIPE, VLAN_TAB(1), e, 4) ?: mrd(u, BLK_EPIPE, EGR_VLAN(1), g, 3);
	ret = ret ?: stg_init(u, b->stg, false);
	if (ret)
		return ret;
	ent_put(g, EV_UT_BITMAP, BIT(0));
	ent_put(g, EV_PORT_BITMAP, BIT(0));
	ent_put(g, EV_STG, b->stg);
	parity(g, 3, EV_PARITY);
	ent_put(e, VT_PORT_BITMAP, BIT(0));
	ent_put(e, VT_STG, b->stg);
	ent_put(e, VT_FID_ID, b->vid);
	parity(e, 4, VT_PARITY);
	/* egress first: ingress makes the VLAN live */
	ret = mwr(u, BLK_EPIPE, EGR_VLAN(b->vid), g, 3) ?: mwr(u, BLK_IPIPE, VLAN_TAB(b->vid), e, 4);
	dev_info(u->dev, "switchdev: %s -> ASIC VLAN %u STG %u (FID %u)%s\n", netdev_name(b->dev), b->vid, b->stg, b->vid,
		 ret ? ": WRITE FAILED" : "");
	return ret;
}

static void br_vlan_destroy(struct us48_cmic *u, struct us48_br *b)
{
	u32 z[4] = { };
	int ret;

	ret = mwr(u, BLK_IPIPE, VLAN_TAB(b->vid), z, 4) ?: mwr(u, BLK_EPIPE, EGR_VLAN(b->vid), z, 3);
	ret = ret ?: stg_init(u, b->stg, true);
	dev_info(u->dev, "switchdev: ASIC VLAN %u STG %u released%s\n", b->vid, b->stg, ret ? ": WRITE FAILED" : "");
}

/* Port in/out of the bridge VLAN: VLAN_TAB PORT_BITMAP, EGR_VLAN PORT_BITMAP + UT_BITMAP (untagged egress). */
static int br_vlan_member(struct us48_cmic *u, struct us48_br *b, int lport, bool on)
{
	u32 e[4], g[3];
	int ret;

	ret = mrd(u, BLK_IPIPE, VLAN_TAB(b->vid), e, 4) ?: mrd(u, BLK_EPIPE, EGR_VLAN(b->vid), g, 3);
	if (ret)
		return ret;
	ent_put(e, FLD(lport, 1), on);
	parity(e, 4, VT_PARITY);
	ent_put(g, FLD(lport, 1), on);
	ent_put(g, FLD(30 + lport, 1), on);
	parity(g, 3, EV_PARITY);
	if (on)
		return mwr(u, BLK_EPIPE, EGR_VLAN(b->vid), g, 3) ?: mwr(u, BLK_IPIPE, VLAN_TAB(b->vid), e, 4);
	return mwr(u, BLK_IPIPE, VLAN_TAB(b->vid), e, 4) ?: mwr(u, BLK_EPIPE, EGR_VLAN(b->vid), g, 3);
}

/* PORT_TAB: PORT_VID puts untagged ingress in vid; CML_FLAGS_NEW/MOVE = cml (0 = no learning, as the private VLANs). */
static int port_tab(struct us48_cmic *u, int lport, u16 vid, u8 cml)
{
	u32 e[8];
	int ret = mrd(u, BLK_IPIPE, PORT_TAB(lport), e, 8);

	if (ret)
		return ret;
	ent_put(e, PT_PORT_VID, vid);
	ent_put(e, PT_CML_NEW, cml);
	ent_put(e, PT_CML_MOVE, cml);
	return mwr(u, BLK_IPIPE, PORT_TAB(lport), e, 8);
}

/* Static L2X entry {vid, mac} -> port lport (cpu: CPU port 0 + copy to CPU), as l2_op_cpu / bcm_hr2_ge_l2_cpu. */
static int l2x_static(struct us48_cmic *u, u16 vid, const u8 *mac, int lport, bool cpu, bool add)
{
	u32 e[4] = { };

	ent_put(e, L2_VLAN_ID, vid);
	ent_put(e, L2_MAC, ether_addr_to_u64(mac));
	ent_put(e, L2_PORT_NUM, cpu ? 0 : lport);
	ent_put(e, L2_MODULE_ID, u->modid);
	ent_put(e, L2_CPU, cpu);
	ent_put(e, L2_STATIC, 1);
	ent_put(e, L2_VALID, 1);
	parity(e, 4, L2_PARITY);
	return us48_sbus(u, add ? BCM_SCHAN_TABLE_INSERT : BCM_SCHAN_TABLE_DELETE, BLK_IPIPE, L2X, e, 4);
}

/* Walk every valid L2 bridge entry (KEY_TYPE 0) by index: S-channel reads, rtnl held. */
typedef int (*l2x_fn)(struct us48_cmic *u, u32 *e, void *arg);

static int l2x_walk(struct us48_cmic *u, l2x_fn fn, void *arg)
{
	u32 e[4];
	int i, ret;

	for (i = 0; i < L2X_ENTRIES; i++) {
		if ((i & 255) == 255)
			cond_resched();
		ret = mrd(u, BLK_IPIPE, L2X + i, e, 4);
		if (ret)
			return ret;
		if (!ent_get(e, L2_VALID) || ent_get(e, L2_KEY_TYPE))
			continue;
		ret = fn(u, e, arg);
		if (ret)
			return ret;
	}
	return 0;
}

static bool l2x_on_port(struct us48_port *p, struct us48_br *b, const u32 *e)
{
	return ent_get(e, L2_VLAN_ID) == b->vid && !ent_get(e, L2_T) && ent_get(e, L2_MODULE_ID) == p->u->modid &&
	       ent_get(e, L2_PORT_NUM) == p->cfg->lport && !ent_get(e, L2_CPU);
}

struct flush_arg {
	struct us48_port *p;
	struct us48_br *b;
	bool all;			/* static entries too (port leaves) */
	int n;
};

static int flush_one(struct us48_cmic *u, u32 *e, void *arg)
{
	struct flush_arg *f = arg;

	if (!l2x_on_port(f->p, f->b, e) || (!f->all && ent_get(e, L2_STATIC)))
		return 0;
	if (!us48_sbus(u, BCM_SCHAN_TABLE_DELETE, BLK_IPIPE, L2X, e, 4))
		f->n++;
	return 0;
}

static void port_flush(struct us48_port *p, struct us48_br *b, bool all)
{
	struct flush_arg f = { .p = p, .b = b, .all = all };
	int ret = l2x_walk(p->u, flush_one, &f);

	netdev_info(p->ndev, "switchdev: L2 flush (%s) in VLAN %u: %d deleted%s\n", all ? "all" : "dynamic", b->vid,
		    f.n, ret ? ", walk FAILED" : "");
}

/*
 * TCAM X/Y encoding of KEY 1:69 / MASK 70:69 (HR2 xy_tcam + xy_tcam_direct, 40 nm): K0 = KEY & MASK in KEY,
 * K1 = KEY | ~MASK in MASK (SDK soc/common/mem.c _soc_mem_tcam_dm_to_xy). Raw data/mask: unmasked bits must be 0.
 */
static void l2u_dm_to_xy(u32 *e)
{
	int i;

	for (i = 0; i < 69; i++) {
		bool k = ent_get(e, FLD(1 + i, 1)), m = ent_get(e, FLD(70 + i, 1));

		ent_put(e, FLD(1 + i, 1), k && m);
		ent_put(e, FLD(70 + i, 1), k || !m);
	}
}

/* BPDU trap entry, X/Y encoded: SDK l2cache.c bcm_esw_l2_cache_init (01:80:c2:00:00:00/48, CPU | BPDU, CMIC port). */
static void l2u_bpdu_entry(struct us48_cmic *u, u32 *e)
{
	memset(e, 0, L2U_WORDS * sizeof(u32));
	ent_put(e, L2U_VALID, 1);
	ent_put(e, L2U_MAC, BPDU_MAC);
	ent_put(e, L2U_MASK_MAC, GENMASK_ULL(47, 0));
	ent_put(e, L2U_MASK_KEY_TYPE, 1);
	l2u_dm_to_xy(e);
	ent_put(e, L2U_CPU, 1);
	ent_put(e, L2U_PORT_NUM, 0);
	ent_put(e, L2U_MODULE_ID, u->modid);
	ent_put(e, L2U_BPDU, 1);
}

/* BPDU marks the frame as a BPDU, which the blocking STG state still passes to the CPU. Reused if already present. */
static int l2u_bpdu_get(struct us48_cmic *u)
{
	u32 e[L2U_WORDS], want[L2U_WORDS];
	int i, ret, free_idx = -1;

	if (u->l2u_bpdu >= 0)
		return 0;
	l2u_bpdu_entry(u, want);
	for (i = 0; i < L2U_ENTRIES; i++) {
		ret = mrd(u, BLK_IPIPE, L2_USER_ENTRY(i), e, L2U_WORDS);
		if (ret)
			return ret;
		if (!ent_get(e, L2U_VALID)) {
			if (free_idx < 0)
				free_idx = i;
			continue;
		}
		if (!memcmp(e, want, sizeof(e))) {
			u->l2u_bpdu = i;
			u->l2u_ours = false;
			dev_info(u->dev, "switchdev: BPDU trap present at L2_USER_ENTRY[%d]\n", i);
			return 0;
		}
	}
	if (free_idx < 0) {
		dev_err(u->dev, "switchdev: no free L2_USER_ENTRY for the BPDU trap\n");
		return -ENOSPC;
	}
	ret = mwr(u, BLK_IPIPE, L2_USER_ENTRY(free_idx), want, L2U_WORDS);
	dev_info(u->dev, "switchdev: BPDU trap -> L2_USER_ENTRY[%d]%s\n", free_idx, ret ? ": WRITE FAILED" : "");
	if (!ret) {
		u->l2u_bpdu = free_idx;
		u->l2u_ours = true;
	}
	return ret;
}

static void l2u_bpdu_put(struct us48_cmic *u)
{
	u32 z[L2U_WORDS] = { };

	if (u->l2u_bpdu >= 0 && u->l2u_ours) {
		mwr(u, BLK_IPIPE, L2_USER_ENTRY(u->l2u_bpdu), z, L2U_WORDS);
		dev_info(u->dev, "switchdev: BPDU trap L2_USER_ENTRY[%d] removed\n", u->l2u_bpdu);
	}
	u->l2u_bpdu = -1;
	u->l2u_ours = false;
}

/* L2_AGE_TIMER {AGE_ENA 20, AGE_VAL 0:20 s}: SDK soc/esw/firebolt.c soc_firebolt_age_timer_set; stock 0x0010012c = 300 s. */
static void age_set(struct us48_cmic *u, clock_t t)
{
	u32 s = clamp_t(u32, jiffies_to_msecs(clock_t_to_jiffies(t)) / 1000, 1, 0xfffff), v = 0, old = 0;

	ent_put(&v, FLD(20, 1), 1);
	ent_put(&v, FLD(0, 20), s);
	if (bcm_cmic_reg_read(&u->cu, BLK_IPIPE, L2_AGE_TIMER, &old, 1, NULL) || old == v)
		return;
	if (!bcm_cmic_reg_write(&u->cu, BLK_IPIPE, L2_AGE_TIMER, &v, 1))
		dev_info(u->dev, "switchdev: L2 ageing %u s (L2_AGE_TIMER %08x -> %08x)\n", s, old, v);
}

static struct us48_br *br_find(struct us48_cmic *u, const struct net_device *br)
{
	int i;

	for (i = 0; i < US48_BR_MAX; i++)
		if (u->br[i].dev == br)
			return &u->br[i];
	return NULL;
}

static bool br_any(struct us48_cmic *u)
{
	int i;

	for (i = 0; i < US48_BR_MAX; i++)
		if (u->br[i].dev)
			return true;
	return false;
}

static int host_set(struct us48_cmic *u, struct us48_br *b, const u8 *mac, bool add)
{
	struct us48_host *h;
	int ret;

	list_for_each_entry(h, &b->hosts, node)
		if (ether_addr_equal(h->mac, mac))
			goto found;
	if (!add)
		return 0;
	h = kzalloc_obj(*h);
	if (!h)
		return -ENOMEM;
	ret = l2x_static(u, b->vid, mac, 0, true, true);
	if (ret) {
		kfree(h);
		dev_warn(u->dev, "switchdev: host %pM in VLAN %u: L2 insert failed (%d)\n", mac, b->vid, ret);
		return ret;
	}
	ether_addr_copy(h->mac, mac);
	list_add(&h->node, &b->hosts);
	return 0;
found:
	if (add)
		return 0;
	l2x_static(u, b->vid, mac, 0, true, false);
	list_del(&h->node);
	kfree(h);
	return 0;
}

/* Bridge port of this unit with logical port lport in the bridge VLAN vid, else NULL. */
static struct us48_port *l2_port(struct us48_cmic *u, u16 vid, u8 lport)
{
	int i;

	for (i = 0; i < u->nports; i++) {
		struct us48_port *p = u->port[i];

		if (p && p->cfg->lport == lport && p->br && p->br->dev && p->br->vid == vid)
			return p;
	}
	return NULL;
}

static void l2_notify(struct us48_port *p, const u8 *mac, bool add)
{
	struct switchdev_notifier_fdb_info info = { .addr = mac, .vid = 0, .offloaded = true };

	call_switchdev_notifiers(add ? SWITCHDEV_FDB_ADD_TO_BRIDGE : SWITCHDEV_FDB_DEL_TO_BRIDGE, p->ndev, &info.info, NULL);
}

/* What the bridge should see for one L2X entry: dynamic, own module, not trunk/CPU, port in a bridge VLAN (never private). */
static void l2_rec(struct us48_cmic *u, const u32 *e, struct us48_l2rec *r)
{
	memset(r, 0, sizeof(*r));
	if (!ent_get(e, L2_VALID) || ent_get(e, L2_KEY_TYPE) || ent_get(e, L2_STATIC) || ent_get(e, L2_CPU) ||
	    ent_get(e, L2_T) || ent_get(e, L2_MODULE_ID) != u->modid)
		return;
	if (!l2_port(u, ent_get(e, L2_VLAN_ID), ent_get(e, L2_PORT_NUM)))
		return;
	r->vid = ent_get(e, L2_VLAN_ID);
	r->lport = ent_get(e, L2_PORT_NUM);
	u64_to_ether_addr(ent_get(e, L2_MAC), r->mac);
}

/* One chunk: S-channel reads without rtnl, then diff + notify under rtnl (memory only). */
static void l2poll_fn(struct work_struct *w)
{
	struct us48_cmic *u = container_of(to_delayed_work(w), struct us48_cmic, l2poll);
	DECLARE_BITMAP(bad, L2POLL_CHUNK);
	int base = u->l2poll_idx, i, nbad = 0, add = 0, del = 0;

	bitmap_zero(bad, L2POLL_CHUNK);
	for (i = 0; i < L2POLL_CHUNK; i++) {
		if ((i & 255) == 255)
			cond_resched();
		if (mrd(u, BLK_IPIPE, L2X + base + i, &u->l2buf[4 * i], 4)) {
			__set_bit(i, bad);
			nbad++;
		}
	}
	rtnl_lock();
	if (!u->l2poll_on) {
		memset(u->l2shadow, 0, L2X_ENTRIES * sizeof(*u->l2shadow));
		u->l2poll_idx = 0;
		rtnl_unlock();
		return;
	}
	for (i = 0; i < L2POLL_CHUNK; i++) {
		struct us48_l2rec *o = &u->l2shadow[base + i], n;
		struct us48_port *p;

		if (test_bit(i, bad))
			continue;
		l2_rec(u, &u->l2buf[4 * i], &n);
		if (!memcmp(o, &n, sizeof(n)))
			continue;
		p = o->vid ? l2_port(u, o->vid, o->lport) : NULL;
		if (p) {
			l2_notify(p, o->mac, false);
			del++;
		}
		p = n.vid ? l2_port(u, n.vid, n.lport) : NULL;
		if (p) {
			l2_notify(p, n.mac, true);
			add++;
		}
		*o = n;
	}
	rtnl_unlock();
	if (nbad)
		dev_warn_ratelimited(u->dev, "switchdev: L2 poll %d-%d: %d reads failed\n", base, base + L2POLL_CHUNK - 1, nbad);
	if (add || del)
		dev_dbg(u->dev, "switchdev: L2 poll %d-%d: %d learned, %d gone\n", base, base + L2POLL_CHUNK - 1, add, del);
	u->l2poll_idx = (base + L2POLL_CHUNK) % L2X_ENTRIES;
	queue_delayed_work(system_wq, &u->l2poll, msecs_to_jiffies(L2POLL_MS));
}

/* First bridge on the unit: start the poll (buffers kept until unit exit). */
static void l2poll_start(struct us48_cmic *u)
{
	if (u->l2poll_on)
		return;
	if (!u->l2shadow) {
		u->l2shadow = kvcalloc(L2X_ENTRIES, sizeof(*u->l2shadow), GFP_KERNEL);
		u->l2buf = kvcalloc(L2POLL_CHUNK * 4, sizeof(u32), GFP_KERNEL);
		if (!u->l2shadow || !u->l2buf) {
			kvfree(u->l2shadow);
			kvfree(u->l2buf);
			u->l2shadow = NULL;
			u->l2buf = NULL;
			dev_warn(u->dev, "switchdev: no memory for the L2 poll: HW-learned entries not shown in the bridge\n");
			return;
		}
	}
	u->l2poll_on = true;
	queue_delayed_work(system_wq, &u->l2poll, 0);
	dev_info(u->dev, "switchdev: L2 poll on (%d x %d entries, %d ms apart)\n", L2POLL_CHUNKS, L2POLL_CHUNK, L2POLL_MS);
}

/* Port left vid: the bridge drops its FDB entries itself; forget ours so a later join reports afresh. */
static void l2_forget(struct us48_cmic *u, u16 vid, int lport)
{
	int i;

	for (i = 0; u->l2shadow && i < L2X_ENTRIES; i++)
		if (u->l2shadow[i].vid == vid && (lport < 0 || u->l2shadow[i].lport == lport))
			memset(&u->l2shadow[i], 0, sizeof(u->l2shadow[i]));
}

static void br_release(struct us48_cmic *u, struct us48_br *b)
{
	struct us48_host *h, *t;

	list_for_each_entry_safe(h, t, &b->hosts, node) {
		l2x_static(u, b->vid, h->mac, 0, true, false);
		list_del(&h->node);
		kfree(h);
	}
	br_vlan_destroy(u, b);
	l2_forget(u, b->vid, -1);
	b->dev = NULL;
	b->members = 0;
	if (!br_any(u)) {
		l2u_bpdu_put(u);
		u->l2poll_on = false;	/* the work sees it under rtnl and stops */
	}
}

/* Bridge slot for br on this unit, ASIC VLAN created on first use; NULL = no offload (logged). */
static struct us48_br *br_get(struct us48_cmic *u, struct net_device *br)
{
	struct us48_br *b = br_find(u, br);

	if (b)
		return b;
	b = br_find(u, NULL);
	if (!b) {
		dev_warn(u->dev, "switchdev: %s: all %d bridge slots in use: CPU-path bridge\n", netdev_name(br), US48_BR_MAX);
		return NULL;
	}
	if (l2u_bpdu_get(u)) {
		dev_warn(u->dev, "switchdev: %s: no BPDU trap: CPU-path bridge\n", netdev_name(br));
		return NULL;
	}
	b->dev = br;
	if (br_vlan_create(u, b)) {
		b->dev = NULL;
		if (!br_any(u))
			l2u_bpdu_put(u);
		return NULL;
	}
	age_set(u, br_get_ageing_time(br));
	l2poll_start(u);
	return b;
}

static void cross_unit_note(struct us48_port *p, struct net_device *br)
{
	struct net_device *lower;
	struct list_head *iter;

	netdev_for_each_lower_dev(br, lower, iter) {
		struct us48_port *q = us48_is_port(lower) ? netdev_priv(lower) : NULL;

		if (q && q->u != p->u) {
			netdev_info(p->ndev, "switchdev: %s also holds %s (unit %u): cross-unit frames via the CPU (stage 2: HiGig)\n",
				    netdev_name(br), netdev_name(lower), q->u->modid);
			return;
		}
	}
}

/* Join order: STG state disabled, VLAN membership, then PORT_VID (ingress moves last). */
static int hw_join(struct us48_port *p, struct us48_br *b)
{
	struct us48_cmic *u = p->u;
	int lp = p->cfg->lport;

	return stg_port_set(u, b->stg, lp, stp_hw(p->stp)) ?: br_vlan_member(u, b, lp, true) ?:
	       port_tab(u, lp, b->vid, p->learning ? CML_LEARN : 0);
}

/* Leave order: PORT_VID back to the private VLAN (CML 0), out of the VLAN, STG disabled, then its L2 entries. */
static void hw_leave(struct us48_port *p, struct us48_br *b)
{
	struct us48_cmic *u = p->u;
	int lp = p->cfg->lport, ret;

	ret = port_tab(u, lp, p->vid, 0) ?: br_vlan_member(u, b, lp, false) ?: stg_port_set(u, b->stg, lp, 0);
	if (ret)
		netdev_err(p->ndev, "switchdev: leave VLAN %u: write failed (%d)\n", b->vid, ret);
	port_flush(p, b, true);
	l2_forget(u, b->vid, lp);
}

static int sw_can_join(struct us48_port *p, struct net_device *br, struct netlink_ext_ack *extack)
{
	if (!switchdev)
		return 0;
	if (p->cfg->front == 1 && !p->u->modid && !mgmt_bridge) {
		NL_SET_ERR_MSG_MOD(extack, "ge1 is the management port: not in an offloaded bridge (mgmt_bridge=1 allows it)");
		netdev_warn(p->ndev, "switchdev: refused join of %s: management port (us48_cmic mgmt_bridge=1 allows it)\n",
			    netdev_name(br));
		return -EPERM;
	}
	if (br_vlan_enabled(br)) {
		NL_SET_ERR_MSG_MOD(extack, "VLAN-aware bridge offload is stage 2");
		return -EOPNOTSUPP;
	}
	return 0;
}

static int sw_join(struct us48_port *p, struct net_device *br, struct netlink_ext_ack *extack)
{
	struct us48_cmic *u = p->u;
	struct us48_br *b;
	int ret;

	if (!switchdev || p->br)
		return 0;
	b = br_get(u, br);
	if (!b)
		return 0;
	p->stp = BR_STATE_DISABLED;	/* new bridge port: state disabled, BR_LEARNING on (net/bridge/br_if.c new_nbp) */
	p->learning = true;
	ret = hw_join(p, b);
	if (ret)
		goto err_hw;
	b->members |= BIT(p->cfg->lport);
	p->br = b;
	WRITE_ONCE(p->rx_vid, b->vid);
	ret = switchdev_bridge_port_offload(p->ndev, p->ndev, p, &sw_atomic_nb, &sw_blocking_nb, false, extack);
	if (ret) {
		netdev_err(p->ndev, "switchdev: bridge port offload failed (%d)\n", ret);
		goto err_hw;
	}
	netdev_info(p->ndev, "switchdev: offloaded to %s: ASIC VLAN %u STG %u, learning %s\n", netdev_name(br), b->vid,
		    b->stg, p->learning ? "on" : "off");
	cross_unit_note(p, br);
	return 0;
err_hw:
	hw_leave(p, b);
	b->members &= ~BIT(p->cfg->lport);
	p->br = NULL;
	WRITE_ONCE(p->rx_vid, p->vid);
	if (!b->members)
		br_release(u, b);
	return ret;
}

/* PRECHANGEUPPER (unlink): unoffload while the port is still a bridge port (net/dsa/port.c dsa_port_pre_bridge_leave). */
static void sw_pre_leave(struct us48_port *p)
{
	if (p->br)
		switchdev_bridge_port_unoffload(p->ndev, p, &sw_atomic_nb, &sw_blocking_nb);
}

static void sw_leave(struct us48_port *p)
{
	struct us48_br *b = p->br;

	if (!b)
		return;
	hw_leave(p, b);
	b->members &= ~BIT(p->cfg->lport);
	p->br = NULL;
	WRITE_ONCE(p->rx_vid, p->vid);
	netdev_info(p->ndev, "switchdev: left %s: back in private VLAN %u\n", netdev_name(b->dev), p->vid);
	if (!b->members)
		br_release(p->u, b);
}

void us48_sw_port_reapply(struct us48_port *p)
{
	ASSERT_RTNL();
	if (p->br && port_tab(p->u, p->cfg->lport, p->br->vid, p->learning ? CML_LEARN : 0))
		netdev_err(p->ndev, "switchdev: PORT_TAB re-apply after bring-up failed\n");
}

static int sw_stp_set(struct us48_port *p, u8 state)
{
	u8 old = p->stp;
	int ret = stg_port_set(p->u, p->br->stg, p->cfg->lport, stp_hw(state));

	if (ret)
		return ret;
	p->stp = state;
	netdev_info(p->ndev, "switchdev: STP state %u -> STG %u SP_TREE %u\n", state, p->br->stg, stp_hw(state));
	if (stp_learns(old) && !stp_learns(state))
		port_flush(p, p->br, false);
	return 0;
}

/* Only BR_LEARNING is offloaded: a flag change that differs from what the ASIC does (flood on, the rest off) is refused. */
static int sw_pre_flags(const struct switchdev_brport_flags *f, struct netlink_ext_ack *extack)
{
	const unsigned long on = BR_FLOOD | BR_MCAST_FLOOD | BR_BCAST_FLOOD;
	unsigned long m = f->mask & ~BR_LEARNING;

	if ((m & on & ~f->val) || (m & ~on & f->val)) {
		NL_SET_ERR_MSG_MOD(extack, "only the learning flag is offloaded (the ASIC always floods)");
		return -EINVAL;
	}
	return 0;
}

/* Atomic chain: the bridge sends PRE_BRIDGE_FLAGS with call_switchdev_notifiers (net/bridge/br_switchdev.c). */
static int sw_attr_set_atomic(struct net_device *dev, const void *ctx, const struct switchdev_attr *attr,
			      struct netlink_ext_ack *extack)
{
	struct us48_port *p = netdev_priv(dev);

	if ((ctx && ctx != p) || !p->br || attr->id != SWITCHDEV_ATTR_ID_PORT_PRE_BRIDGE_FLAGS)
		return -EOPNOTSUPP;
	return sw_pre_flags(&attr->u.brport_flags, extack);
}

static int sw_attr_set(struct net_device *dev, const void *ctx, const struct switchdev_attr *attr,
		       struct netlink_ext_ack *extack)
{
	struct us48_port *p = netdev_priv(dev);

	if ((ctx && ctx != p) || !p->br)
		return -EOPNOTSUPP;
	switch (attr->id) {
	case SWITCHDEV_ATTR_ID_PORT_STP_STATE:
		return sw_stp_set(p, attr->u.stp_state);
	case SWITCHDEV_ATTR_ID_PORT_PRE_BRIDGE_FLAGS:
		return sw_pre_flags(&attr->u.brport_flags, extack);
	case SWITCHDEV_ATTR_ID_PORT_BRIDGE_FLAGS:
		if (!(attr->u.brport_flags.mask & BR_LEARNING))
			return 0;
		p->learning = !!(attr->u.brport_flags.val & BR_LEARNING);
		if (!p->learning)
			port_flush(p, p->br, false);
		return port_tab(p->u, p->cfg->lport, p->br->vid, p->learning ? CML_LEARN : 0);
	case SWITCHDEV_ATTR_ID_BRIDGE_AGEING_TIME:
		age_set(p->u, attr->u.ageing_time);
		return 0;
	case SWITCHDEV_ATTR_ID_BRIDGE_VLAN_FILTERING:
		if (attr->u.vlan_filtering) {
			NL_SET_ERR_MSG_MOD(extack, "VLAN-aware bridge offload is stage 2");
			return -EINVAL;
		}
		return 0;
	default:
		return -EOPNOTSUPP;
	}
}

/* Deferred FDB event: bridge-local entry (orig = bridge) -> CPU, user static entry on a port -> that port. */
struct sw_fdb_work {
	struct work_struct work;
	struct net_device *dev, *orig;
	unsigned long event;
	u8 mac[ETH_ALEN];
};

static void sw_fdb_work_fn(struct work_struct *w)
{
	struct sw_fdb_work *fw = container_of(w, struct sw_fdb_work, work);
	struct us48_port *p = netdev_priv(fw->dev);
	bool add = fw->event == SWITCHDEV_FDB_ADD_TO_DEVICE;
	struct us48_br *b;
	int ret;

	rtnl_lock();
	if (netif_is_bridge_master(fw->orig)) {
		b = br_find(p->u, fw->orig);
		if (b)
			host_set(p->u, b, fw->mac, add);
	} else if (p->br && p->br->dev == netdev_master_upper_dev_get(fw->dev)) {
		ret = l2x_static(p->u, p->br->vid, fw->mac, p->cfg->lport, false, add);
		if (ret && (add || ret != -ENOENT))
			netdev_warn(fw->dev, "switchdev: static %pM %s failed (%d)\n", fw->mac, add ? "insert" : "delete", ret);
		if (!ret && add) {
			struct switchdev_notifier_fdb_info info = { .addr = fw->mac, .offloaded = true };

			call_switchdev_notifiers(SWITCHDEV_FDB_OFFLOADED, fw->dev, &info.info, NULL);
		}
	}
	rtnl_unlock();
	dev_put(fw->orig);
	dev_put(fw->dev);
	kfree(fw);
}

static bool sw_foreign(const struct net_device *dev, const struct net_device *foreign)
{
	return !us48_is_port(foreign) || ((struct us48_port *)netdev_priv(foreign))->u !=
					 ((struct us48_port *)netdev_priv(dev))->u;
}

static int sw_fdb_mod(struct net_device *dev, struct net_device *orig, unsigned long event, const void *ctx,
		      const struct switchdev_notifier_fdb_info *fi)
{
	struct us48_port *p = netdev_priv(dev);
	struct sw_fdb_work *fw;
	bool host = netif_is_bridge_master(orig);

	if ((ctx && ctx != p) || fi->vid)	/* VLAN-unaware: vid 0 only (vid 1 entries are the same key here) */
		return 0;
	if (!host && (orig != dev || switchdev_fdb_is_dynamically_learned(fi)))
		return 0;			/* foreign port (stage 2), or learned: the ASIC learns it itself */
	fw = kzalloc_obj(*fw, GFP_ATOMIC);
	if (!fw)
		return -ENOMEM;
	INIT_WORK(&fw->work, sw_fdb_work_fn);
	fw->dev = dev;
	fw->orig = orig;
	fw->event = event;
	ether_addr_copy(fw->mac, fi->addr);
	dev_hold(dev);
	dev_hold(orig);
	queue_work(sw_wq, &fw->work);
	return 0;
}

static int sw_atomic_event(struct notifier_block *nb, unsigned long event, void *ptr)
{
	struct net_device *dev = switchdev_notifier_info_to_dev(ptr);

	switch (event) {
	case SWITCHDEV_FDB_ADD_TO_DEVICE:
	case SWITCHDEV_FDB_DEL_TO_DEVICE:
		return notifier_from_errno(switchdev_handle_fdb_event_to_device(dev, event, ptr, us48_is_port,
										 sw_foreign, sw_fdb_mod));
	case SWITCHDEV_PORT_ATTR_SET:
		return notifier_from_errno(switchdev_handle_port_attr_set(dev, ptr, us48_is_port, sw_attr_set_atomic));
	}
	return NOTIFY_DONE;
}

static int sw_blocking_event(struct notifier_block *nb, unsigned long event, void *ptr)
{
	struct net_device *dev = switchdev_notifier_info_to_dev(ptr);

	if (event == SWITCHDEV_PORT_ATTR_SET)
		return notifier_from_errno(switchdev_handle_port_attr_set(dev, ptr, us48_is_port, sw_attr_set));
	return NOTIFY_DONE;		/* objects (VLAN, MDB): not offloaded, the bridge keeps them in software */
}

static int sw_netdev_event(struct notifier_block *nb, unsigned long event, void *ptr)
{
	struct net_device *dev = netdev_notifier_info_to_dev(ptr);
	struct netdev_notifier_changeupper_info *info = ptr;
	struct us48_port *p;

	if ((event != NETDEV_PRECHANGEUPPER && event != NETDEV_CHANGEUPPER) || !us48_is_port(dev) ||
	    !netif_is_bridge_master(info->upper_dev))
		return NOTIFY_DONE;
	p = netdev_priv(dev);
	if (event == NETDEV_PRECHANGEUPPER) {
		if (info->linking)
			return notifier_from_errno(sw_can_join(p, info->upper_dev, info->info.extack));
		sw_pre_leave(p);
		return NOTIFY_DONE;
	}
	if (info->linking)
		return notifier_from_errno(sw_join(p, info->upper_dev, info->info.extack));
	sw_leave(p);
	return NOTIFY_DONE;
}

static struct notifier_block sw_netdev_nb = { .notifier_call = sw_netdev_event };
static struct notifier_block sw_atomic_nb = { .notifier_call = sw_atomic_event };
static struct notifier_block sw_blocking_nb = { .notifier_call = sw_blocking_event };

/* One switch id per unit: each unit is its own bridge hardware domain (offload_fwd_mark scope). */
int us48_sw_parent_id(struct net_device *nd, struct netdev_phys_item_id *ppid)
{
	struct us48_port *p = netdev_priv(nd);

	ppid->id_len = 2;
	ppid->id[0] = 0x48;
	ppid->id[1] = p->u->modid;
	return 0;
}

struct dump_arg {
	struct us48_port *p;
	struct sk_buff *skb;
	struct netlink_callback *cb;
	int idx;
};

/* One RTM_NEWNEIGH per HW entry of the port (net/dsa/user.c dsa_user_port_fdb_do_dump pattern). */
static int dump_one(struct us48_cmic *u, u32 *e, void *arg)
{
	struct dump_arg *d = arg;
	struct ndo_fdb_dump_context *ctx = (void *)d->cb->ctx;
	struct nlmsghdr *nlh;
	struct ndmsg *ndm;
	u8 mac[ETH_ALEN];

	if (!l2x_on_port(d->p, d->p->br, e))
		return 0;
	if (d->idx < ctx->fdb_idx)
		goto skip;
	nlh = nlmsg_put(d->skb, NETLINK_CB(d->cb->skb).portid, d->cb->nlh->nlmsg_seq, RTM_NEWNEIGH, sizeof(*ndm),
			NLM_F_MULTI);
	if (!nlh)
		return -EMSGSIZE;
	ndm = nlmsg_data(nlh);
	memset(ndm, 0, sizeof(*ndm));
	ndm->ndm_family = AF_BRIDGE;
	ndm->ndm_flags = NTF_SELF | NTF_OFFLOADED;
	ndm->ndm_ifindex = d->p->ndev->ifindex;
	ndm->ndm_state = ent_get(e, L2_STATIC) ? NUD_NOARP : NUD_REACHABLE;
	u64_to_ether_addr(ent_get(e, L2_MAC), mac);
	if (nla_put(d->skb, NDA_LLADDR, ETH_ALEN, mac)) {
		nlmsg_cancel(d->skb, nlh);
		return -EMSGSIZE;
	}
	nlmsg_end(d->skb, nlh);
skip:
	d->idx++;
	return 0;
}

/* Default entries (uc/mc lists, as without the op), then the ASIC's entries for an offloaded port: one L2X walk per port. */
int us48_sw_fdb_dump(struct sk_buff *skb, struct netlink_callback *cb, struct net_device *dev,
		     struct net_device *filter_dev, int *idx)
{
	struct us48_port *p = netdev_priv(dev);
	struct dump_arg d = { .p = p, .skb = skb, .cb = cb };
	int ret = ndo_dflt_fdb_dump(skb, cb, dev, filter_dev, idx);

	if (ret || !p->br)
		return ret;
	d.idx = *idx;
	ret = l2x_walk(p->u, dump_one, &d);
	*idx = d.idx;
	return ret;
}

void us48_sw_unit_init(struct us48_cmic *u)
{
	int i;

	u->l2u_bpdu = -1;
	for (i = 0; i < US48_BR_MAX; i++) {
		u->br[i].vid = SW_VID_BASE + i;
		u->br[i].stg = SW_STG_BASE + i;
		INIT_LIST_HEAD(&u->br[i].hosts);
	}
	INIT_DELAYED_WORK(&u->l2poll, l2poll_fn);
}

void us48_sw_unit_exit(struct us48_cmic *u)
{
	rtnl_lock();
	u->l2poll_on = false;
	rtnl_unlock();
	cancel_delayed_work_sync(&u->l2poll);
	kvfree(u->l2shadow);
	kvfree(u->l2buf);
	u->l2shadow = NULL;
	u->l2buf = NULL;
}

int us48_sw_init(void)
{
	int ret;

	sw_wq = alloc_ordered_workqueue("us48_sw", 0);
	if (!sw_wq)
		return -ENOMEM;
	ret = register_switchdev_notifier(&sw_atomic_nb);
	if (ret)
		goto err_wq;
	ret = register_switchdev_blocking_notifier(&sw_blocking_nb);
	if (ret)
		goto err_atomic;
	ret = register_netdevice_notifier(&sw_netdev_nb);
	if (ret)
		goto err_blocking;
	return 0;
err_blocking:
	unregister_switchdev_blocking_notifier(&sw_blocking_nb);
err_atomic:
	unregister_switchdev_notifier(&sw_atomic_nb);
err_wq:
	destroy_workqueue(sw_wq);
	return ret;
}

void us48_sw_exit(void)
{
	unregister_netdevice_notifier(&sw_netdev_nb);
	unregister_switchdev_blocking_notifier(&sw_blocking_nb);
	unregister_switchdev_notifier(&sw_atomic_nb);
	destroy_workqueue(sw_wq);		/* drains queued FDB work */
}
