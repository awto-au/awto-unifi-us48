// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic switchdev L2 offload (docs/switchdev.md, #57): VLAN-unaware bridge = one slot ASIC VLAN + STG per unit;
 * VLAN-aware bridge (one per unit) = ASIC VLAN per bridge VID. HW learns and ages (L2_MOD_FIFO reports it to the bridge);
 * static/local FDB -> L2X; STP -> STG_TAB/EGR_VLAN_STG. One hwdom per unit (HiGig offload = stage 2). All state: rtnl.
 */
#include <linux/delay.h>
#include <linux/etherdevice.h>
#include <linux/if_bridge.h>
#include <linux/if_vlan.h>
#include <linux/module.h>
#include <linux/netdevice.h>
#include <linux/rtnetlink.h>
#include <linux/slab.h>
#include <linux/workqueue.h>
#include <net/switchdev.h>

#include "us48_cmic.h"
#include "us48_hwdb.h"
#include "bcm_cmic.h"

static bool switchdev = true;
module_param(switchdev, bool, 0644);
MODULE_PARM_DESC(switchdev, "1 (default) = offload Linux bridges of us48 ports to the ASIC; 0 = CPU-path bridge (bridges made after the change)");
static bool mgmt_bridge;
module_param(mgmt_bridge, bool, 0644);
MODULE_PARM_DESC(mgmt_bridge, "1 = allow ge1 (management) in an offloaded bridge; default 0 refuses the join");
/* Off: only FIFO overflow left the bridge behind L2X, and that walk runs at once (#135) */
static uint l2_check_s;
module_param(l2_check_s, uint, 0644);
MODULE_PARM_DESC(l2_check_s, "L2X vs bridge consistency walk every N s while a bridge is offloaded; 0 (default) = off");

/* VID plan (hwdb/board/vlan.toml): bridge VIDs below VID_RSVD_MIN; slots, private VLANs, VLAN 4094 above. */
#define SW_VID_BASE		HW_CONST_VID_SLOT_BASE
#define SW_STG_BASE		2	/* slot STG 2 + i: unused by stock and our init (tmp/schan_stock_writes*.txt) */
#define VID_RSVD_MIN		((int)HW_CONST_VID_RSVD_MIN)
#define VID_RSVD_MAX		((int)HW_CONST_VID_RSVD_MAX)
#define RX_VID_NONE		0xffff	/* VLAN-aware port: no tag stripped on RX, the bridge classifies */
static_assert(SW_VID_BASE >= VID_RSVD_MIN && SW_VID_BASE + US48_BR_MAX <= HW_CONST_VID_PRIVATE_BASE + 1);
static_assert(HW_CONST_VID_PRIVATE_BASE + 52 <= VID_RSVD_MAX && VID_RSVD_MAX < VLAN_N_VID - 1);

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
#define PT_EN_IFILTER		FLD(5, 1)
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

/* Static/local FDB entry of a bridge; in L2X only in its mode (vid 0 = VLAN-unaware, else VLAN-aware), fdb_live(). */
struct us48_fdb {
	struct list_head node;
	u8 mac[ETH_ALEN];
	u16 vid;			/* bridge VID, 0 = no VLAN */
	s8 lport;			/* -1 = bridge-local -> CPU */
};

/* Bridge VLANs of a port, kept in either mode (a vlan_filtering toggle is not replayed by the bridge). */
struct us48_pvlan {
	DECLARE_BITMAP(member, VLAN_N_VID);
	DECLARE_BITMAP(untag, VLAN_N_VID);
	u16 pvid;			/* 0 = none */
	u8 stg_old;			/* SP_TREE of the port in the private STG before a VLAN-aware join */
	bool hw;			/* VLAN-aware and the VIDs are in the ASIC */
};

/*
 * HW learn/age -> bridge: L2_MOD_FIFO change queue, popped by S-channel FIFO_POP (SDK soc_mem_pop; HR2 has
 * mem_push_pop via bcm56624_a0). FIFO full pauses HW learning (#134); still one L2X walk diffs table vs shadow then.
 */
#define L2MOD_FIFO		0x1c1c0000	/* regdb hr2_mems.tsv L2_MOD_FIFO: IPIPE, 128 x 162 bits */
#define L2MOD_ENTRIES		128
#define L2MOD_WORDS		6
#define L2MOD_CNT		0x1e011300	/* L2_MOD_FIFO_CNT.NUM_OF_ENTRIES 0:8 (regdb hr2_regs.tsv) */
#define AUX_ARB_CONTROL		0x02000000	/* IPIPE register (regdb hr2_regs.tsv) */
/* L2_MOD_FIFO_ENABLE_ LEARN 1, PPA_REPLACE 2, AGE 4, PPA_DELETE 5: the SDK set (l2mod.c _soc_l2mod_fifo_enable 73-87)
 * minus L2_DELETE 6: CPU deletes are reported where they are made; full-FIFO refusals of our ops avoided. */
#define L2MOD_ENABLE		(BIT(1) | BIT(2) | BIT(4) | BIT(5))
#define LM_OPERATION		FLD(0, 4)
#define LM_ENTRY_INDEX		FLD(5, 3)
#define LM_BUCKET_INDEX		FLD(8, 11)	/* L2X index = bucket * 8 + entry (2048 x 8 = 16384) */
#define LM_L2_ENTRY		20		/* L2_ENTRY_DATA 20:101 = L2X bits 0-100 (KEY_TYPE .. EVEN_PARITY) */
/* OPERATION: SDK l2mod.c _soc_hu2_l2mod_fifo_process (757-799) */
enum { LM_DELETE, LM_PPA_DELETE, LM_AGE, LM_INSERT, LM_PPA_REPLACE, LM_LEARN, LM_MEMWR, LM_OVERFLOW };
/* Idle: one register read per 10 ms (3 jiffies at HZ 250). A drain that finds >= half full re-runs after 1 ms
 * (hrtimer sleep: a jiffy is 4 ms) until one finds less: ceiling 128 / 1 ms (docs/switchdev.md, #135). */
#define L2MOD_POLL_MS		10
#define L2MOD_FAST_US		1000
#define L2MOD_HALF		(L2MOD_ENTRIES / 2)
/* Walk chunk: one chunk's reads stay under the 1 ms fast-drain period on unit 1 (PCIe), FIFO drained between chunks */
#define L2WALK_CHUNK		32
#define L2WALK_CHUNKS		(L2X_ENTRIES / L2WALK_CHUNK)

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

static int stg_port_get(struct us48_cmic *u, u8 stg, int lport, u8 *hw)
{
	u32 e[2];
	int ret = mrd(u, BLK_IPIPE, STG_TAB(stg), e, 2);

	if (!ret)
		*hw = ent_get(e, FLD(2 * lport, 2));
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

static int port_ifilter(struct us48_cmic *u, int lport, bool on, bool *old)
{
	u32 e[8];
	int ret = mrd(u, BLK_IPIPE, PORT_TAB(lport), e, 8);

	if (ret)
		return ret;
	if (old)
		*old = ent_get(e, PT_EN_IFILTER);
	ent_put(e, PT_EN_IFILTER, on);
	return mwr(u, BLK_IPIPE, PORT_TAB(lport), e, 8);
}

/*
 * PORT_TAB.EN_IFILTER = 1 on every port of the unit while it has a bridge: a non-member's tagged frame never enters a bridge
 * VLAN (a loop-cable end would loop it back). Stock value (trace PORT_TAB[2] 0x01430039 b5); SDK fb port.c pc_en_ifilter.
 */
static void ifilter_all(struct us48_cmic *u, bool on)
{
	int i, bad = 0;

	for (i = 0; i < u->nports; i++) {
		int lp = u->cfgs[i].lport;
		bool old = false;

		if (on) {
			bad += !!port_ifilter(u, lp, true, &old);
			u->ifilt_old = old ? u->ifilt_old | BIT(lp) : u->ifilt_old & ~BIT(lp);
		} else {
			bad += !!port_ifilter(u, lp, u->ifilt_old & BIT(lp), NULL);
		}
	}
	dev_info(u->dev, "switchdev: PORT_TAB.EN_IFILTER %s on %d ports (before: %#x)%s\n", on ? "set" : "restored", u->nports,
		 u->ifilt_old, bad ? ": WRITE FAILED" : "");
}

/*
 * VLAN-aware: ASIC VLAN = FID = bridge VID, STG = VLAN 1's (kept: private VLANs clone it). CPU = tagged member of every VID
 * (SDK fb vlan.c _bcm_xgs3_vlan_table_port_add drops CMIC from ubmp): the bridge sees the VID; host/cross-unit need the copy.
 */
static int v1_tmpl(struct us48_cmic *u, u32 *e, u32 *g)
{
	if (u->v1_held) {
		memcpy(e, u->v1_vt, sizeof(u->v1_vt));
		memcpy(g, u->v1_ev, sizeof(u->v1_ev));
		return 0;
	}
	return mrd(u, BLK_IPIPE, VLAN_TAB(1), e, 4) ?: mrd(u, BLK_EPIPE, EGR_VLAN(1), g, 3);
}

/* Ports of the unit (except skip) with vid in the ASIC: VLAN-aware bridge members. */
static int vid_users(struct us48_cmic *u, u16 vid, struct us48_port *skip)
{
	int i, n = 0;

	for (i = 0; i < u->nports; i++) {
		struct us48_port *q = u->port[i];

		n += q && q != skip && q->pv && q->pv->hw && test_bit(vid, q->pv->member);
	}
	return n;
}

/* First member of vid: ASIC VLAN {CPU} from VLAN 1's template. VLAN 1 itself (cold init: HiGig 28/29) is saved, taken over. */
static int avlan_get(struct us48_cmic *u, struct us48_br *b, u16 vid)
{
	u32 e[4], g[3];
	int ret = mrd(u, BLK_IPIPE, VLAN_TAB(vid), e, 4);

	if (ret)
		return ret;
	if (vid != 1 && ent_get(e, VT_VALID)) {
		dev_err(u->dev, "switchdev: VLAN %u already valid (not ours): not touching\n", vid);
		return -EBUSY;
	}
	ret = vid == 1 ? mrd(u, BLK_EPIPE, EGR_VLAN(1), g, 3) : v1_tmpl(u, e, g);
	if (ret)
		return ret;
	if (vid == 1 && !u->v1_held) {
		memcpy(u->v1_vt, e, sizeof(e));
		memcpy(u->v1_ev, g, sizeof(g));
	}
	ent_put(g, EV_UT_BITMAP, 0);
	ent_put(g, EV_PORT_BITMAP, BIT(0));
	ent_put(g, EV_STG, b->astg);
	parity(g, 3, EV_PARITY);
	ent_put(e, VT_PORT_BITMAP, BIT(0));
	ent_put(e, VT_STG, b->astg);
	ent_put(e, VT_FID_ID, vid);
	parity(e, 4, VT_PARITY);
	ret = mwr(u, BLK_EPIPE, EGR_VLAN(vid), g, 3) ?: mwr(u, BLK_IPIPE, VLAN_TAB(vid), e, 4);
	if (!ret && vid == 1)
		u->v1_held = true;
	dev_info(u->dev, "switchdev: %s VID %u -> ASIC VLAN %u STG %u (FID %u)%s%s\n", netdev_name(b->dev), vid, vid, b->astg,
		 vid, vid == 1 ? ", VLAN 1 taken (HiGig 28/29 out until restored)" : "", ret ? ": WRITE FAILED" : "");
	return ret;
}

/* Last member of vid gone: VLAN invalid again, VLAN 1 back to its saved entries (ingress first). */
static void avlan_put(struct us48_cmic *u, u16 vid)
{
	u32 z[4] = { };
	int ret;

	if (vid == 1) {
		if (!u->v1_held)
			return;
		ret = mwr(u, BLK_IPIPE, VLAN_TAB(1), u->v1_vt, 4) ?: mwr(u, BLK_EPIPE, EGR_VLAN(1), u->v1_ev, 3);
		u->v1_held = false;
	} else {
		ret = mwr(u, BLK_IPIPE, VLAN_TAB(vid), z, 4) ?: mwr(u, BLK_EPIPE, EGR_VLAN(vid), z, 3);
	}
	dev_info(u->dev, "switchdev: ASIC VLAN %u %s%s\n", vid, vid == 1 ? "restored" : "released", ret ? ": WRITE FAILED" : "");
}

/* Port in/out of ASIC VLAN vid: VLAN_TAB/EGR_VLAN PORT_BITMAP, EGR_VLAN UT_BITMAP = untagged egress. */
static int vid_set(struct us48_port *p, u16 vid, bool on, bool untag)
{
	struct us48_cmic *u = p->u;
	int lp = p->cfg->lport, others = vid_users(u, vid, p), ret = 0;
	u32 e[4], g[3];

	if (on && !others)
		ret = avlan_get(u, p->br, vid);
	ret = ret ?: mrd(u, BLK_IPIPE, VLAN_TAB(vid), e, 4) ?: mrd(u, BLK_EPIPE, EGR_VLAN(vid), g, 3);
	if (ret || (!on && !ent_get(e, VT_VALID)))
		return ret;
	ent_put(e, FLD(lp, 1), on);
	parity(e, 4, VT_PARITY);
	ent_put(g, FLD(lp, 1), on && untag);
	ent_put(g, FLD(30 + lp, 1), on);
	parity(g, 3, EV_PARITY);
	if (on)
		return mwr(u, BLK_EPIPE, EGR_VLAN(vid), g, 3) ?: mwr(u, BLK_IPIPE, VLAN_TAB(vid), e, 4);
	ret = mwr(u, BLK_IPIPE, VLAN_TAB(vid), e, 4) ?: mwr(u, BLK_EPIPE, EGR_VLAN(vid), g, 3);
	if (!others)
		avlan_put(u, vid);
	return ret;
}

/* PORT_VID while bridged: slot VLAN; VLAN-aware: PVID, else the private VLAN (untagged -> CPU only, the bridge drops them). */
static u16 hw_vid(struct us48_port *p)
{
	return p->br->aware ? (p->pv->pvid ?: p->vid) : p->br->vid;
}

static u8 hw_cml(struct us48_port *p)
{
	return p->learning ? CML_LEARN : 0;
}

static u8 br_stg(struct us48_br *b)
{
	return b->aware ? b->astg : b->stg;
}

static int br_aware_init(struct us48_cmic *u, struct us48_br *b)
{
	u32 e[4], g[3];
	int ret = v1_tmpl(u, e, g);

	if (!ret)
		b->astg = ent_get(e, VT_STG);
	return ret;
}

/* Static L2X entry {vid, mac} -> port lport (cpu: CPU port 0 + copy to CPU), as l2_op_cpu / bcm_hr2_ge_l2_cpu. */
static int l2x_static(struct us48_cmic *u, u16 vid, const u8 *mac, int lport, bool cpu, bool add)
{
	u32 e[4] = { };

	ent_put(e, L2_VLAN_ID, vid);
	ent_put(e, L2_MAC, ether_addr_to_u64(mac));
	ent_put(e, L2_PORT_NUM, cpu ? 0 : lport);
	ent_put(e, L2_MODULE_ID, u->l2_modid);
	ent_put(e, L2_CPU, cpu);
	ent_put(e, L2_STATIC, 1);
	ent_put(e, L2_VALID, 1);
	parity(e, 4, L2_PARITY);
	return us48_sbus(u, add ? BCM_SCHAN_TABLE_INSERT : BCM_SCHAN_TABLE_DELETE, BLK_IPIPE, L2X, e, 4);
}

/* Walk every valid L2 bridge entry (KEY_TYPE 0) by index: S-channel reads, rtnl held. */
typedef int (*l2x_fn)(struct us48_cmic *u, int idx, u32 *e, void *arg);

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
		ret = fn(u, i, e, arg);
		if (ret)
			return ret;
	}
	return 0;
}

static void l2_set(struct us48_cmic *u, int idx, const struct us48_l2rec *n);

/* Entry on p in a VLAN of b: the slot VLAN, or (VLAN-aware) any VID below the reserved range (0 = private-VLAN learning). */
static bool l2x_on_port(struct us48_port *p, struct us48_br *b, const u32 *e)
{
	u16 v = ent_get(e, L2_VLAN_ID);

	return (b->aware ? v < VID_RSVD_MIN : v == b->vid) && !ent_get(e, L2_T) &&
	       ent_get(e, L2_MODULE_ID) == p->u->l2_modid && ent_get(e, L2_PORT_NUM) == p->cfg->lport && !ent_get(e, L2_CPU);
}

struct flush_arg {
	struct us48_port *p;
	struct us48_br *b;
	bool all;			/* static entries too (port leaves) */
	int n;
};

/* CPU deletes are not in the FIFO (L2MOD_ENABLE): a reported entry leaves the bridge here (port leave: l2_forget). */
static int flush_one(struct us48_cmic *u, int idx, u32 *e, void *arg)
{
	struct flush_arg *f = arg;
	struct us48_l2rec z = { };

	if (!l2x_on_port(f->p, f->b, e) || (!f->all && ent_get(e, L2_STATIC)))
		return 0;
	if (us48_sbus(u, BCM_SCHAN_TABLE_DELETE, BLK_IPIPE, L2X, e, 4))
		return 0;
	f->n++;
	if (u->l2shadow && !f->all)
		l2_set(u, idx, &z);
	return 0;
}

static void port_flush(struct us48_port *p, struct us48_br *b, bool all)
{
	struct flush_arg f = { .p = p, .b = b, .all = all };
	int ret = l2x_walk(p->u, flush_one, &f);

	if (b->aware)
		netdev_info(p->ndev, "switchdev: L2 flush (%s) in the bridge VIDs: %d deleted%s\n", all ? "all" : "dynamic",
			    f.n, ret ? ", walk FAILED" : "");
	else
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
	ent_put(e, L2U_MODULE_ID, u->l2_modid);
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

static struct us48_port *sw_port(struct us48_cmic *u, int lport)
{
	int i;

	for (i = 0; i < u->nports; i++)
		if (u->port[i] && u->port[i]->cfg->lport == lport)
			return u->port[i];
	return NULL;
}

static bool fdb_live(const struct us48_br *b, const struct us48_fdb *f)
{
	return b->aware ? f->vid && f->vid < VID_RSVD_MIN : !f->vid;
}

static int fdb_hw(struct us48_cmic *u, struct us48_br *b, struct us48_fdb *f, bool add)
{
	return l2x_static(u, b->aware ? f->vid : b->vid, f->mac, max_t(int, f->lport, 0), f->lport < 0, add);
}

static void fdb_offloaded(struct us48_cmic *u, struct us48_fdb *f, bool on)
{
	struct switchdev_notifier_fdb_info info = { .addr = f->mac, .vid = f->vid, .offloaded = on };
	struct us48_port *p = sw_port(u, f->lport);

	if (p)
		call_switchdev_notifiers(SWITCHDEV_FDB_OFFLOADED, p->ndev, &info.info, NULL);
}

/* Static entry {mac, vid} -> lport (-1 = CPU) added/removed. 1 = in L2X now, 0 = kept for the other mode / gone, < 0 error. */
static int fdb_set(struct us48_cmic *u, struct us48_br *b, const u8 *mac, u16 vid, int lport, bool add)
{
	struct us48_fdb *f;
	int ret = 0;

	list_for_each_entry(f, &b->fdbs, node)
		if (f->vid == vid && ether_addr_equal(f->mac, mac))
			goto found;
	if (!add)
		return 0;
	f = kzalloc_obj(*f);
	if (!f)
		return -ENOMEM;
	ether_addr_copy(f->mac, mac);
	f->vid = vid;
	list_add(&f->node, &b->fdbs);
found:
	if (!add) {
		if (f->lport != lport)
			return 0;
		if (fdb_live(b, f))
			ret = fdb_hw(u, b, f, false);
		list_del(&f->node);
		kfree(f);
		return ret;
	}
	f->lport = lport;
	if (!fdb_live(b, f))
		return 0;
	ret = fdb_hw(u, b, f, true);
	if (ret) {
		dev_warn(u->dev, "switchdev: static %pM vid %u: L2 insert failed (%d)\n", mac, vid, ret);
		list_del(&f->node);
		kfree(f);
		return ret;
	}
	return 1;
}

/* Mode change: the live entries leave L2X (on false) or enter it under the new mode's key (on true). */
static void fdbs_hw(struct us48_cmic *u, struct us48_br *b, bool on)
{
	struct us48_fdb *f;
	int ret;

	list_for_each_entry(f, &b->fdbs, node) {
		if (!fdb_live(b, f))
			continue;
		ret = fdb_hw(u, b, f, on);
		if (ret && on)
			dev_warn(u->dev, "switchdev: static %pM vid %u: L2 insert failed (%d)\n", f->mac, f->vid, ret);
		if (f->lport >= 0)
			fdb_offloaded(u, f, on && !ret);
	}
}

/* Port left: its entries are gone from L2X (port_flush all), the bridge deletes its own. */
static void fdb_drop_port(struct us48_br *b, int lport)
{
	struct us48_fdb *f, *t;

	list_for_each_entry_safe(f, t, &b->fdbs, node)
		if (f->lport == lport) {
			list_del(&f->node);
			kfree(f);
		}
}

/* ASIC VLAN vid is a VLAN of p's bridge: the slot VLAN, or a VID of the port (VLAN-aware, in the ASIC). */
static bool vid_in_br(struct us48_port *p, u16 vid)
{
	if (!p->br->aware)
		return vid == p->br->vid;
	return vid && vid < VID_RSVD_MIN && p->pv->hw && test_bit(vid, p->pv->member);
}

/* Bridge port of this unit with logical port lport in the ASIC VLAN vid of its bridge, else NULL. */
static struct us48_port *l2_port(struct us48_cmic *u, u16 vid, u8 lport)
{
	struct us48_port *p = sw_port(u, lport);

	return p && p->br && p->br->dev && vid_in_br(p, vid) ? p : NULL;
}

/* HW entry -> bridge: vid 0 on a VLAN-unaware bridge, the VID on a VLAN-aware one. */
static void l2_notify(struct us48_port *p, const u8 *mac, u16 vid, bool add)
{
	struct switchdev_notifier_fdb_info info = { .addr = mac, .vid = p->br->aware ? vid : 0, .offloaded = true };

	call_switchdev_notifiers(add ? SWITCHDEV_FDB_ADD_TO_BRIDGE : SWITCHDEV_FDB_DEL_TO_BRIDGE, p->ndev, &info.info, NULL);
}

/* What the bridge should see for one L2X entry: dynamic, own module, not trunk/CPU, port in a bridge VLAN (never private). */
static void l2_rec(struct us48_cmic *u, const u32 *e, struct us48_l2rec *r)
{
	memset(r, 0, sizeof(*r));
	if (!ent_get(e, L2_VALID) || ent_get(e, L2_KEY_TYPE) || ent_get(e, L2_STATIC) || ent_get(e, L2_CPU) ||
	    ent_get(e, L2_T) || ent_get(e, L2_MODULE_ID) != u->l2_modid)
		return;
	if (!l2_port(u, ent_get(e, L2_VLAN_ID), ent_get(e, L2_PORT_NUM)))
		return;
	r->vid = ent_get(e, L2_VLAN_ID);
	r->lport = ent_get(e, L2_PORT_NUM);
	u64_to_ether_addr(ent_get(e, L2_MAC), r->mac);
}

/* Shadow record idx := n, the bridge told the difference (rtnl). Returns 1 = removed, 2 = added (bits). */
static int l2_upd(struct us48_cmic *u, int idx, const struct us48_l2rec *n)
{
	struct us48_l2rec *o = &u->l2shadow[idx];
	struct us48_port *p;
	int r = 0;

	if (!memcmp(o, n, sizeof(*n)))
		return 0;
	p = o->vid ? l2_port(u, o->vid, o->lport) : NULL;
	if (p) {
		l2_notify(p, o->mac, o->vid, false);
		r |= 1;
	}
	p = n->vid ? l2_port(u, n->vid, n->lport) : NULL;
	if (p) {
		l2_notify(p, n->mac, n->vid, true);
		r |= 2;
	}
	*o = *n;
	return r;
}

static void l2_set(struct us48_cmic *u, int idx, const struct us48_l2rec *n)
{
	l2_upd(u, idx, n);
}

/* Shadow index holding {vid, mac}: idx first, else a search (counted: the FIFO index did not match). -1 = none. */
static int l2_find(struct us48_cmic *u, int idx, u16 vid, const u8 *mac)
{
	int i;

	if (u->l2shadow[idx].vid == vid && ether_addr_equal(u->l2shadow[idx].mac, mac))
		return idx;
	for (i = 0; i < L2X_ENTRIES; i++)
		if (u->l2shadow[i].vid == vid && ether_addr_equal(u->l2shadow[i].mac, mac)) {
			u->l2mismatch++;
			return i;
		}
	return -1;
}

/* One L2_MOD_FIFO entry (rtnl). */
static void l2mod_apply(struct us48_cmic *u, const u32 *f)
{
	int idx = ent_get(f, LM_BUCKET_INDEX) * 8 + ent_get(f, LM_ENTRY_INDEX), i;
	struct us48_l2rec n, z = { };
	u8 mac[ETH_ALEN];
	u32 e[4];

	for (i = 0; i < 4; i++)
		e[i] = f[i] >> LM_L2_ENTRY | f[i + 1] << (32 - LM_L2_ENTRY);
	e[3] &= GENMASK(100 - 96, 0);
	switch (ent_get(f, LM_OPERATION)) {
	case LM_LEARN:
	case LM_INSERT:
	case LM_PPA_REPLACE:
		l2_rec(u, e, &n);
		if (n.vid && u->l2shadow[idx].vid && !ether_addr_equal(u->l2shadow[idx].mac, n.mac)) {
			i = l2_find(u, idx, n.vid, n.mac);	/* same MAC reported at another index: drop that record */
			if (i >= 0 && i != idx)
				l2_set(u, i, &z);
		}
		l2_set(u, idx, &n);
		break;
	case LM_DELETE:
	case LM_PPA_DELETE:
	case LM_AGE:
		u64_to_ether_addr(ent_get(e, L2_MAC), mac);
		i = l2_find(u, idx, ent_get(e, L2_VLAN_ID), mac);
		if (i >= 0)
			l2_set(u, i, &z);
		break;
	default:			/* OVERFLOW = hash bucket full, not learned: nothing to report */
		break;
	}
}

/* Pop what L2_MOD_FIFO_CNT shows (no rtnl), report under rtnl. Returns the count read; -ENODEV once the FIFO is off. */
static int l2mod_drain(struct us48_cmic *u)
{
	u32 cnt = 0;
	int i, n = 0, ret;

	ret = bcm_cmic_reg_read(&u->cu, BLK_IPIPE, L2MOD_CNT, &cnt, 1, NULL);
	cnt &= 0xff;
	for (i = 0; !ret && i < min_t(u32, cnt, L2MOD_ENTRIES); i++) {
		ret = us48_sbus(u, BCM_SCHAN_FIFO_POP, BLK_IPIPE, L2MOD_FIFO, &u->l2pop[n * L2MOD_WORDS], L2MOD_WORDS);
		if (!ret)
			n++;
	}
	if (ret == -ENOENT)
		ret = 0;
	rtnl_lock();
	if (!u->l2mod_on) {
		rtnl_unlock();
		return -ENODEV;
	}
	for (i = 0; i < n; i++)
		l2mod_apply(u, &u->l2pop[i * L2MOD_WORDS]);
	u->l2ev += n;
	if (cnt >= L2MOD_ENTRIES)
		u->l2full++;
	if (cnt >= L2MOD_ENTRIES || ret)
		u->l2resync = true;
	rtnl_unlock();
	if (ret)
		dev_warn_ratelimited(u->dev, "switchdev: L2_MOD_FIFO read failed (%d): resync due\n", ret);
	return cnt;
}

/* Whole L2X vs shadow, chunk by chunk: FIFO drained before each chunk read, so later events apply after its diff. */
static int l2_resync(struct us48_cmic *u, const char *why)
{
	DECLARE_BITMAP(bad, L2WALK_CHUNK);
	int c, i, r, nbad = 0, add = 0, del = 0;
	ktime_t t0 = ktime_get();
	s64 dt;

	for (c = 0; c < L2WALK_CHUNKS; c++) {
		int base = c * L2WALK_CHUNK;

		if (l2mod_drain(u) < 0)
			return -ENODEV;
		cond_resched();
		bitmap_zero(bad, L2WALK_CHUNK);
		for (i = 0; i < L2WALK_CHUNK; i++) {
			if (mrd(u, BLK_IPIPE, L2X + base + i, &u->l2buf[4 * i], 4)) {
				__set_bit(i, bad);
				nbad++;
			}
		}
		rtnl_lock();
		if (!u->l2mod_on) {
			rtnl_unlock();
			return -ENODEV;
		}
		for (i = 0; i < L2WALK_CHUNK; i++) {
			struct us48_l2rec n;

			if (test_bit(i, bad))
				continue;
			l2_rec(u, &u->l2buf[4 * i], &n);
			r = l2_upd(u, base + i, &n);
			del += r & 1;
			add += !!(r & 2);
		}
		rtnl_unlock();
	}
	dt = ktime_ms_delta(ktime_get(), t0);
	rtnl_lock();
	u->l2resyncs++;
	u->l2resync_at = jiffies + msecs_to_jiffies(dt);	/* next overflow walk after a gap = this walk's time */
	rtnl_unlock();
	dev_info(u->dev, "switchdev: L2 resync (%s): %d added, %d removed, %d reads failed, %lld ms\n", why, add, del, nbad, dt);
	return 0;
}

static void l2mod_fn(struct work_struct *w)
{
	struct us48_cmic *u = container_of(to_delayed_work(w), struct us48_cmic, l2mod);
	const char *why = NULL;
	int cnt = l2mod_drain(u);

	if (cnt < 0)
		return;
	rtnl_lock();
	if (u->l2resync && time_after_eq(jiffies, u->l2resync_at)) {
		why = "FIFO full";
		u->l2resync = false;
	} else if (l2_check_s && time_after_eq(jiffies, u->l2check_at)) {
		why = "check";
		u->l2check_at = jiffies + secs_to_jiffies(l2_check_s);
	}
	rtnl_unlock();
	if (why && l2_resync(u, why))
		return;
	if (cnt >= L2MOD_HALF) {
		u->l2fast++;
		usleep_range(L2MOD_FAST_US, L2MOD_FAST_US);
		queue_delayed_work(system_wq, &u->l2mod, 0);
		return;
	}
	queue_delayed_work(system_wq, &u->l2mod, msecs_to_jiffies(L2MOD_POLL_MS));
}

/* Pop and drop what is left (FIFO off or not yet on); count of entries dropped. */
static int l2mod_flush(struct us48_cmic *u)
{
	u32 f[L2MOD_WORDS];
	int n = 0;

	while (n < L2MOD_ENTRIES && !us48_sbus(u, BCM_SCHAN_FIFO_POP, BLK_IPIPE, L2MOD_FIFO, f, L2MOD_WORDS))
		n++;
	return n;
}

static int l2mod_enable(struct us48_cmic *u, bool on, u32 *old, u32 *v)
{
	int ret = bcm_cmic_reg_read(&u->cu, BLK_IPIPE, AUX_ARB_CONTROL, old, 1, NULL);

	*v = on ? *old | L2MOD_ENABLE : *old & ~L2MOD_ENABLE;
	return ret ?: bcm_cmic_reg_write(&u->cu, BLK_IPIPE, AUX_ARB_CONTROL, v, 1);
}

/* First bridge on the unit: FIFO recording on + the drain work (buffers kept until unit exit). */
static void l2mod_start(struct us48_cmic *u)
{
	u32 old, v;
	int left;

	if (u->l2mod_on)
		return;
	if (!u->l2shadow) {
		u->l2shadow = kvcalloc(L2X_ENTRIES, sizeof(*u->l2shadow), GFP_KERNEL);
		u->l2buf = kvcalloc(L2WALK_CHUNK * 4, sizeof(u32), GFP_KERNEL);
		u->l2pop = kvcalloc(L2MOD_ENTRIES * L2MOD_WORDS, sizeof(u32), GFP_KERNEL);
		if (!u->l2shadow || !u->l2buf || !u->l2pop) {
			kvfree(u->l2shadow);
			kvfree(u->l2buf);
			kvfree(u->l2pop);
			u->l2shadow = NULL;
			u->l2buf = NULL;
			u->l2pop = NULL;
			dev_err(u->dev, "switchdev: no memory for the L2 change FIFO: HW-learned entries not shown in the bridge\n");
			return;
		}
	}
	left = l2mod_flush(u);
	if (l2mod_enable(u, true, &old, &v)) {
		dev_err(u->dev, "switchdev: AUX_ARB_CONTROL write failed: HW-learned entries not shown in the bridge\n");
		return;
	}
	u->l2ev = u->l2full = u->l2resyncs = u->l2mismatch = u->l2fast = 0;
	u->l2resync = false;
	u->l2resync_at = jiffies;	/* not 0: jiffies starts 5 min before the wrap (INITIAL_JIFFIES) */
	u->l2check_at = jiffies + secs_to_jiffies(l2_check_s);
	u->l2mod_on = true;
	queue_delayed_work(system_wq, &u->l2mod, 0);
	dev_info(u->dev, "switchdev: L2 change FIFO on (AUX_ARB_CONTROL %08x -> %08x), drained every %d ms, %d us from %d%s\n",
		 old, v, L2MOD_POLL_MS, L2MOD_FAST_US, L2MOD_HALF, left ? ", stale entries dropped" : "");
}

/* Last bridge gone: recording off, leftovers dropped; the work sees l2mod_on false under rtnl and stops. */
static void l2mod_stop(struct us48_cmic *u)
{
	u32 old = 0, v = 0;
	int ret, left;

	if (!u->l2mod_on)
		return;
	u->l2mod_on = false;
	ret = l2mod_enable(u, false, &old, &v);
	left = l2mod_flush(u);
	memset(u->l2shadow, 0, L2X_ENTRIES * sizeof(*u->l2shadow));
	dev_info(u->dev, "switchdev: L2 change FIFO off (AUX_ARB_CONTROL %08x -> %08x%s): %u events, %u full, %u fast drains, "
		 "%u resyncs, %u index mismatches, %d dropped\n", old, v, ret ? " WRITE FAILED" : "", u->l2ev, u->l2full,
		 u->l2fast, u->l2resyncs, u->l2mismatch, left);
}

/* Port left vid (0 = every bridge VID): the bridge drops its FDB entries itself; forget ours so a later join reports afresh. */
static void l2_forget(struct us48_cmic *u, u16 vid, int lport)
{
	int i;

	for (i = 0; u->l2shadow && i < L2X_ENTRIES; i++) {
		u16 v = u->l2shadow[i].vid;

		if (v && (vid ? v == vid : v < VID_RSVD_MIN) && (lport < 0 || u->l2shadow[i].lport == lport))
			memset(&u->l2shadow[i], 0, sizeof(u->l2shadow[i]));
	}
}

/* vlan_filtering toggle: the bridge keeps extern_learn entries (vid 0 or the VID): withdraw what b's ports were told. */
static void l2_unreport(struct us48_cmic *u, struct us48_br *b)
{
	int i, n = 0;

	for (i = 0; u->l2shadow && i < L2X_ENTRIES; i++) {
		struct us48_l2rec *r = &u->l2shadow[i];
		struct us48_port *p = r->vid ? l2_port(u, r->vid, r->lport) : NULL;

		if (p && p->br == b) {
			l2_notify(p, r->mac, r->vid, false);
			memset(r, 0, sizeof(*r));
			n++;
		}
	}
	if (n)
		dev_info(u->dev, "switchdev: %s: %d HW-learned entries withdrawn from the bridge\n", netdev_name(b->dev), n);
}

/* {vid, mac} is a user static entry now: the bridge owns it, no extern_learn record (rtnl). */
static void l2_drop(struct us48_cmic *u, u16 vid, const u8 *mac)
{
	int i;

	for (i = 0; u->l2shadow && i < L2X_ENTRIES; i++)
		if (u->l2shadow[i].vid == vid && ether_addr_equal(u->l2shadow[i].mac, mac))
			memset(&u->l2shadow[i], 0, sizeof(u->l2shadow[i]));
}

static void br_release(struct us48_cmic *u, struct us48_br *b)
{
	struct us48_fdb *f, *t;

	list_for_each_entry_safe(f, t, &b->fdbs, node) {
		if (fdb_live(b, f))
			fdb_hw(u, b, f, false);
		list_del(&f->node);
		kfree(f);
	}
	if (!b->aware) {
		br_vlan_destroy(u, b);
		l2_forget(u, b->vid, -1);
	}
	b->dev = NULL;
	b->members = 0;
	b->aware = false;
	if (!br_any(u)) {
		l2u_bpdu_put(u);
		l2mod_stop(u);
		ifilter_all(u, false);
	}
}

/* Unit's VLAN-aware bridge other than br: one per unit, its VIDs are the ASIC VLANs. */
static struct us48_br *br_aware_other(struct us48_cmic *u, const struct net_device *br)
{
	int i;

	for (i = 0; i < US48_BR_MAX; i++)
		if (u->br[i].dev && u->br[i].dev != br && u->br[i].aware)
			return &u->br[i];
	return NULL;
}

/* Bridge slot for br on this unit, ASIC VLAN created on first use (VLAN-unaware); NULL = no offload (logged). */
static struct us48_br *br_get(struct us48_cmic *u, struct net_device *br)
{
	struct us48_br *b = br_find(u, br);
	bool first = !br_any(u);
	int ret;

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
	b->aware = br_vlan_enabled(br);
	ret = b->aware ? br_aware_init(u, b) : br_vlan_create(u, b);
	if (ret) {
		b->dev = NULL;
		b->aware = false;
		if (!br_any(u))
			l2u_bpdu_put(u);
		return NULL;
	}
	if (first)
		ifilter_all(u, true);
	age_set(u, br_get_ageing_time(br));
	l2mod_start(u);
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

/* VLAN-aware join: private-STG state saved, bridge STP state set, every VID of the port, then PORT_VID (ingress last). */
static int aw_join(struct us48_port *p, struct us48_br *b)
{
	struct us48_pvlan *pv = p->pv;
	int lp = p->cfg->lport, ret;
	u16 vid;

	ret = stg_port_get(p->u, b->astg, lp, &pv->stg_old) ?: stg_port_set(p->u, b->astg, lp, stp_hw(p->stp));
	if (ret)
		return ret;
	pv->hw = true;			/* from here aw_leave undoes it */
	for_each_set_bit(vid, pv->member, VLAN_N_VID) {
		ret = vid_set(p, vid, true, test_bit(vid, pv->untag));
		if (ret)
			return ret;
	}
	return port_tab(p->u, lp, hw_vid(p), hw_cml(p));
}

static void aw_leave(struct us48_port *p, struct us48_br *b)
{
	struct us48_pvlan *pv = p->pv;
	int lp = p->cfg->lport, ret, err = 0;
	u16 vid;

	err = port_tab(p->u, lp, p->vid, 0);
	if (pv->hw) {
		for_each_set_bit(vid, pv->member, VLAN_N_VID) {
			ret = vid_set(p, vid, false, false);
			err = err ?: ret;
		}
		ret = stg_port_set(p->u, b->astg, lp, pv->stg_old);
		err = err ?: ret;
	}
	pv->hw = false;
	if (err)
		netdev_err(p->ndev, "switchdev: leave the bridge VIDs: write failed (%d)\n", err);
	port_flush(p, b, true);
	l2_forget(p->u, 0, lp);
}

/* Join order: STG state disabled, VLAN membership, then PORT_VID (ingress moves last). */
static int hw_join(struct us48_port *p, struct us48_br *b)
{
	struct us48_cmic *u = p->u;
	int lp = p->cfg->lport;

	if (b->aware)
		return aw_join(p, b);
	return stg_port_set(u, b->stg, lp, stp_hw(p->stp)) ?: br_vlan_member(u, b, lp, true) ?:
	       port_tab(u, lp, b->vid, hw_cml(p));
}

/* Leave order: PORT_VID back to the private VLAN (CML 0), out of the VLAN, STG disabled, then its L2 entries. */
static void hw_leave(struct us48_port *p, struct us48_br *b)
{
	struct us48_cmic *u = p->u;
	int lp = p->cfg->lport, ret;

	if (b->aware) {
		aw_leave(p, b);
		return;
	}
	ret = port_tab(u, lp, p->vid, 0) ?: br_vlan_member(u, b, lp, false) ?: stg_port_set(u, b->stg, lp, 0);
	if (ret)
		netdev_err(p->ndev, "switchdev: leave VLAN %u: write failed (%d)\n", b->vid, ret);
	port_flush(p, b, true);
	l2_forget(u, b->vid, lp);
}

static u16 rx_vid_br(struct us48_port *p)
{
	return p->br->aware ? RX_VID_NONE : p->br->vid;
}

/*
 * vlan_filtering toggle: HW-learned entries withdrawn from the bridge, statics out of L2X, every member leaves in the old
 * mode and joins in the new one (slot VLAN <-> per-VID ASIC VLANs), statics back under the new mode's key.
 */
static int br_mode_set(struct us48_cmic *u, struct us48_br *b, bool aware)
{
	int i, ret = 0;

	l2_unreport(u, b);
	fdbs_hw(u, b, false);
	for (i = 0; i < u->nports; i++)
		if (u->port[i] && u->port[i]->br == b)
			hw_leave(u->port[i], b);
	if (!b->aware)
		br_vlan_destroy(u, b);
	b->aware = aware;
	ret = aware ? br_aware_init(u, b) : br_vlan_create(u, b);
	for (i = 0; i < u->nports; i++) {
		struct us48_port *q = u->port[i];

		if (!q || q->br != b)
			continue;
		ret = ret ?: hw_join(q, b);
		WRITE_ONCE(q->rx_vid, rx_vid_br(q));
	}
	fdbs_hw(u, b, true);
	dev_info(u->dev, "switchdev: %s: vlan_filtering %d -> %s%s\n", netdev_name(b->dev), aware,
		 aware ? "ASIC VLAN per bridge VID" : "slot VLAN", ret ? ": FAILED" : "");
	return ret;
}

static int sw_vlan_filtering(struct us48_port *p, bool on, struct netlink_ext_ack *extack)
{
	struct us48_cmic *u = p->u;
	struct us48_br *b = p->br;
	int ret;

	if (b->aware == on)
		return 0;
	if (on && br_aware_other(u, b->dev)) {
		NL_SET_ERR_MSG_FMT_MOD(extack, "%s already offloads a VLAN-aware bridge on this unit (one per unit)",
				       netdev_name(br_aware_other(u, b->dev)->dev));
		return -EBUSY;
	}
	ret = br_mode_set(u, b, on);
	if (ret) {
		NL_SET_ERR_MSG_MOD(extack, "ASIC write failed: vlan_filtering not changed");
		br_mode_set(u, b, !on);
	}
	return ret;
}

static int sw_can_join(struct us48_port *p, struct net_device *br, struct netlink_ext_ack *extack)
{
	u16 proto = ETH_P_8021Q;

	if (!switchdev)
		return 0;
	if (p->cfg->front == 1 && !p->u->modid && !mgmt_bridge) {
		NL_SET_ERR_MSG_MOD(extack, "ge1 is the management port: not in an offloaded bridge (mgmt_bridge=1 allows it)");
		netdev_warn(p->ndev, "switchdev: refused join of %s: management port (us48_cmic mgmt_bridge=1 allows it)\n",
			    netdev_name(br));
		return -EPERM;
	}
	if (br_vlan_enabled(br) && br_aware_other(p->u, br)) {
		NL_SET_ERR_MSG_FMT_MOD(extack, "%s already offloads a VLAN-aware bridge on this unit (one per unit)",
				       netdev_name(br_aware_other(p->u, br)->dev));
		netdev_warn(p->ndev, "switchdev: refused join of %s: second VLAN-aware bridge on the unit\n", netdev_name(br));
		return -EBUSY;
	}
	br_vlan_get_proto(br, &proto);
	if (proto != ETH_P_8021Q || br_mst_enabled(br)) {
		NL_SET_ERR_MSG_MOD(extack, "offload needs vlan_protocol 802.1Q and mst_enabled 0");
		return -EOPNOTSUPP;
	}
	return 0;
}

/*
 * L2X/L2_USER_ENTRY MODULE_ID of this unit's ports = what HW learning writes: PORT_TAB.MY_MODID (40:7, regdb), 0 on
 * both units after our cold init (unit 1 learns MODULE_ID 0, #134 burst run), not u->modid. First join sets it.
 */
static int l2_modid_check(struct us48_port *p)
{
	struct us48_cmic *u = p->u;
	u32 e[8];
	u8 m;
	int ret = mrd(u, BLK_IPIPE, PORT_TAB(p->cfg->lport), e, 8);

	if (ret)
		return ret;
	m = ent_get(e, FLD(40, 7));
	if (!br_any(u)) {
		if (m != u->l2_modid || m != u->modid)
			dev_info(u->dev, "switchdev: L2 module id %u (PORT_TAB.MY_MODID), unit modid %u\n", m, u->modid);
		u->l2_modid = m;
	} else if (m != u->l2_modid) {
		netdev_warn(p->ndev, "switchdev: PORT_TAB.MY_MODID %u, unit's bridges use %u: CPU-path bridge\n", m, u->l2_modid);
		return -EXDEV;
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
	ret = l2_modid_check(p);
	if (ret)
		return ret == -EXDEV ? 0 : ret;
	p->pv = kzalloc_obj(*p->pv);
	if (!p->pv)
		return -ENOMEM;
	b = br_get(u, br);
	if (!b) {
		kfree(p->pv);
		p->pv = NULL;
		return 0;
	}
	p->stp = BR_STATE_DISABLED;	/* new bridge port: state disabled, BR_LEARNING on (net/bridge/br_if.c new_nbp) */
	p->learning = true;
	b->members |= BIT(p->cfg->lport);
	p->br = b;
	ret = hw_join(p, b);
	if (ret)
		goto err_hw;
	WRITE_ONCE(p->rx_vid, rx_vid_br(p));
	/* replays the port's VLANs (sw_obj_add) and FDB with ctx = p */
	ret = switchdev_bridge_port_offload(p->ndev, p->ndev, p, &sw_atomic_nb, &sw_blocking_nb, false, extack);
	if (ret) {
		netdev_err(p->ndev, "switchdev: bridge port offload failed (%d)\n", ret);
		goto err_hw;
	}
	if (b->aware)
		netdev_info(p->ndev, "switchdev: offloaded to %s: VLAN-aware, ASIC VLAN = bridge VID, STG %u, learning %s\n",
			    netdev_name(br), b->astg, p->learning ? "on" : "off");
	else
		netdev_info(p->ndev, "switchdev: offloaded to %s: ASIC VLAN %u STG %u, learning %s\n", netdev_name(br), b->vid,
			    b->stg, p->learning ? "on" : "off");
	cross_unit_note(p, br);
	return 0;
err_hw:
	hw_leave(p, b);
	b->members &= ~BIT(p->cfg->lport);
	p->br = NULL;
	WRITE_ONCE(p->rx_vid, p->vid);
	fdb_drop_port(b, p->cfg->lport);
	kfree(p->pv);
	p->pv = NULL;
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
	fdb_drop_port(b, p->cfg->lport);
	kfree(p->pv);
	p->pv = NULL;
	netdev_info(p->ndev, "switchdev: left %s: back in private VLAN %u\n", netdev_name(b->dev), p->vid);
	if (!b->members)
		br_release(p->u, b);
}

/* ge_vlan_init cloned the new private VLAN from VLAN 1: while a bridge holds VID 1 that is FID 1, put VLAN 1's own back. */
static int priv_fid_fix(struct us48_port *p)
{
	struct us48_cmic *u = p->u;
	u32 e[4];
	int ret;

	if (!u->v1_held)
		return 0;
	ret = mrd(u, BLK_IPIPE, VLAN_TAB(p->vid), e, 4);
	if (ret || !ent_get(e, VT_VALID) || ent_get(e, VT_FID_ID) == ent_get(u->v1_vt, VT_FID_ID))
		return ret;
	ent_put(e, VT_FID_ID, ent_get(u->v1_vt, VT_FID_ID));
	parity(e, 4, VT_PARITY);
	return mwr(u, BLK_IPIPE, VLAN_TAB(p->vid), e, 4);
}

/*
 * After the first open's copper bring-up (private VLAN + PORT_TAB written): bridge PORT_TAB back; VLAN-aware, the private
 * STG state ge_vlan_init just set (3) is what the leave restores, the bridge state goes back in.
 */
void us48_sw_port_reapply(struct us48_port *p)
{
	int lp = p->cfg->lport, ret;

	ASSERT_RTNL();
	if (priv_fid_fix(p))
		netdev_err(p->ndev, "switchdev: private VLAN %u FID fix failed\n", p->vid);
	if (!p->br)
		return;
	ret = port_tab(p->u, lp, hw_vid(p), hw_cml(p));
	if (!ret && p->br->aware && p->pv->hw)
		ret = stg_port_get(p->u, p->br->astg, lp, &p->pv->stg_old) ?:
		      stg_port_set(p->u, p->br->astg, lp, stp_hw(p->stp));
	if (ret)
		netdev_err(p->ndev, "switchdev: bridge PORT_TAB/STG re-apply after bring-up failed (%d)\n", ret);
}

static int sw_stp_set(struct us48_port *p, u8 state)
{
	u8 old = p->stp, stg = br_stg(p->br);
	int ret = stg_port_set(p->u, stg, p->cfg->lport, stp_hw(state));

	if (ret)
		return ret;
	p->stp = state;
	netdev_info(p->ndev, "switchdev: STP state %u -> STG %u SP_TREE %u\n", state, stg, stp_hw(state));
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
		return port_tab(p->u, p->cfg->lport, hw_vid(p), hw_cml(p));
	case SWITCHDEV_ATTR_ID_BRIDGE_AGEING_TIME:
		age_set(p->u, attr->u.ageing_time);
		return 0;
	case SWITCHDEV_ATTR_ID_BRIDGE_VLAN_FILTERING:
		return sw_vlan_filtering(p, attr->u.vlan_filtering, extack);
	case SWITCHDEV_ATTR_ID_BRIDGE_VLAN_PROTOCOL:
		/* the bridge treats -EOPNOTSUPP as "fine" here: refuse what the ASIC would tag differently */
		if (attr->u.vlan_protocol != ETH_P_8021Q) {
			NL_SET_ERR_MSG_MOD(extack, "only 802.1Q (TPID 0x8100) is offloaded");
			return -EINVAL;
		}
		return 0;
	case SWITCHDEV_ATTR_ID_BRIDGE_MST:
		if (attr->u.mst) {
			NL_SET_ERR_MSG_MOD(extack, "MST is not offloaded: one STG per bridge");
			return -EINVAL;
		}
		return 0;
	default:
		return -EOPNOTSUPP;
	}
}

/* Deferred FDB event: bridge-local entry (orig = bridge) -> CPU, user static entry on a port -> that port; any VID. */
struct sw_fdb_work {
	struct work_struct work;
	struct net_device *dev, *orig;
	unsigned long event;
	u8 mac[ETH_ALEN];
	u16 vid;
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
			fdb_set(p->u, b, fw->mac, fw->vid, -1, add);
	} else if (p->br && p->br->dev == netdev_master_upper_dev_get(fw->dev)) {
		b = p->br;
		ret = fdb_set(p->u, b, fw->mac, fw->vid, p->cfg->lport, add);
		if (ret < 0 && (add || ret != -ENOENT))
			netdev_warn(fw->dev, "switchdev: static %pM vid %u %s failed (%d)\n", fw->mac, fw->vid,
				    add ? "insert" : "delete", ret);
		if (ret > 0 && add) {
			struct switchdev_notifier_fdb_info info = { .addr = fw->mac, .vid = fw->vid, .offloaded = true };

			l2_drop(p->u, b->aware ? fw->vid : b->vid, fw->mac);
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

	if ((ctx && ctx != p) || fi->vid >= VID_RSVD_MIN)	/* fdb_live() picks the mode's VIDs at apply time */
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
	fw->vid = fi->vid;
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

struct vflush_arg {
	u32 lports;
	int n;
};

/* Dynamic entry of a pending port in a VID it is no longer a member of (the bridge already dropped its copy). */
static int vflush_one(struct us48_cmic *u, int idx, u32 *e, void *arg)
{
	struct vflush_arg *a = arg;
	u16 v = ent_get(e, L2_VLAN_ID);
	int lp = ent_get(e, L2_PORT_NUM);
	struct us48_l2rec z = { };
	struct us48_port *p;

	if (!(a->lports & BIT(lp)) || ent_get(e, L2_STATIC) || ent_get(e, L2_CPU) || ent_get(e, L2_T) ||
	    ent_get(e, L2_MODULE_ID) != u->l2_modid || !v || v >= VID_RSVD_MIN)
		return 0;
	p = sw_port(u, lp);
	if (!p || !p->br || !p->br->aware || test_bit(v, p->pv->member))
		return 0;
	if (!us48_sbus(u, BCM_SCHAN_TABLE_DELETE, BLK_IPIPE, L2X, e, 4)) {
		a->n++;
		if (u->l2shadow)
			l2_set(u, idx, &z);
	}
	return 0;
}

/* One L2X walk for every VID removal queued since the last run (a port leaving with N VIDs = one walk, not N). */
static void vflush_fn(struct work_struct *w)
{
	struct us48_cmic *u = container_of(w, struct us48_cmic, vflush_work);
	struct vflush_arg a = { };
	int ret;

	rtnl_lock();
	a.lports = u->vflush;
	u->vflush = 0;
	if (a.lports) {
		ret = l2x_walk(u, vflush_one, &a);
		dev_info(u->dev, "switchdev: L2 flush of removed bridge VIDs (lports %#x): %d deleted%s\n", a.lports, a.n,
			 ret ? ", walk FAILED" : "");
	}
	rtnl_unlock();
}

/* PORT_VLAN add/change: recorded in either mode, in the ASIC only VLAN-aware. Failure = config refused, state as before. */
static int sw_vlan_add(struct us48_port *p, const struct switchdev_obj_port_vlan *v, struct netlink_ext_ack *extack)
{
	struct us48_pvlan *pv = p->pv;
	bool untag = v->flags & BRIDGE_VLAN_INFO_UNTAGGED, was = test_bit(v->vid, pv->member);
	bool was_ut = test_bit(v->vid, pv->untag);
	u16 old_pvid = pv->pvid;
	int lp = p->cfg->lport, ret;

	__set_bit(v->vid, pv->member);
	__assign_bit(v->vid, pv->untag, untag);
	if (v->flags & BRIDGE_VLAN_INFO_PVID)
		pv->pvid = v->vid;
	else if (pv->pvid == v->vid)
		pv->pvid = 0;
	if (!pv->hw)
		return 0;
	ret = vid_set(p, v->vid, true, untag);
	if (!ret && pv->pvid != old_pvid)
		ret = port_tab(p->u, lp, hw_vid(p), hw_cml(p));
	if (!ret)
		return 0;
	__assign_bit(v->vid, pv->member, was);
	__assign_bit(v->vid, pv->untag, was_ut);
	pv->pvid = old_pvid;
	vid_set(p, v->vid, was, was_ut);
	port_tab(p->u, lp, hw_vid(p), hw_cml(p));
	NL_SET_ERR_MSG_FMT_MOD(extack, "VID %u: ASIC VLAN write failed (%d)", v->vid, ret);
	return ret;
}

static void sw_vlan_del(struct us48_port *p, u16 vid)
{
	struct us48_pvlan *pv = p->pv;
	u16 old_pvid = pv->pvid;
	int ret = 0;

	if (!test_bit(vid, pv->member))
		return;
	__clear_bit(vid, pv->member);
	__clear_bit(vid, pv->untag);
	if (pv->pvid == vid)
		pv->pvid = 0;
	if (!pv->hw)
		return;
	ret = vid_set(p, vid, false, false);
	if (!ret && pv->pvid != old_pvid)
		ret = port_tab(p->u, p->cfg->lport, hw_vid(p), hw_cml(p));
	if (ret)
		netdev_err(p->ndev, "switchdev: VID %u removal: write failed (%d)\n", vid, ret);
	p->u->vflush |= BIT(p->cfg->lport);
	queue_work(sw_wq, &p->u->vflush_work);
}

/* VIDs from VID_RSVD_MIN are the driver's (slot/private VLANs, 4094): refused for ports and for the bridge itself. */
static int sw_vlan_ok(u16 vid, struct netlink_ext_ack *extack)
{
	if (vid < VID_RSVD_MIN)
		return 0;
	NL_SET_ERR_MSG_FMT_MOD(extack, "VID %u: %d-%d are us48_cmic internal VLANs", vid, VID_RSVD_MIN, VID_RSVD_MAX);
	return -EINVAL;
}

static int sw_obj_add(struct net_device *dev, const void *ctx, const struct switchdev_obj *obj, struct netlink_ext_ack *extack)
{
	struct us48_port *p = netdev_priv(dev);
	const struct switchdev_obj_port_vlan *v;
	int ret;

	if (obj->id != SWITCHDEV_OBJ_ID_PORT_VLAN || (ctx && ctx != p) || !p->br)
		return -EOPNOTSUPP;	/* MDB: the bridge keeps it in software */
	v = SWITCHDEV_OBJ_PORT_VLAN(obj);
	ret = sw_vlan_ok(v->vid, extack);
	if (ret || !v->vid || netif_is_bridge_master(obj->orig_dev))
		return ret;		/* host VLAN: the CPU is in every ASIC VLAN of the bridge already */
	return sw_vlan_add(p, v, extack);
}

static int sw_obj_del(struct net_device *dev, const void *ctx, const struct switchdev_obj *obj)
{
	struct us48_port *p = netdev_priv(dev);
	u16 vid;

	if (obj->id != SWITCHDEV_OBJ_ID_PORT_VLAN || (ctx && ctx != p) || !p->br)
		return -EOPNOTSUPP;
	vid = SWITCHDEV_OBJ_PORT_VLAN(obj)->vid;
	if (vid && vid < VID_RSVD_MIN && !netif_is_bridge_master(obj->orig_dev))
		sw_vlan_del(p, vid);
	return 0;
}

static int sw_blocking_event(struct notifier_block *nb, unsigned long event, void *ptr)
{
	struct net_device *dev = switchdev_notifier_info_to_dev(ptr);

	switch (event) {
	case SWITCHDEV_PORT_OBJ_ADD:
		return notifier_from_errno(switchdev_handle_port_obj_add(dev, ptr, us48_is_port, sw_obj_add));
	case SWITCHDEV_PORT_OBJ_DEL:
		return notifier_from_errno(switchdev_handle_port_obj_del(dev, ptr, us48_is_port, sw_obj_del));
	case SWITCHDEV_PORT_ATTR_SET:
		return notifier_from_errno(switchdev_handle_port_attr_set(dev, ptr, us48_is_port, sw_attr_set));
	}
	return NOTIFY_DONE;
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
static int dump_one(struct us48_cmic *u, int idx, u32 *e, void *arg)
{
	struct dump_arg *d = arg;
	struct ndo_fdb_dump_context *ctx = (void *)d->cb->ctx;
	bool aware = d->p->br->aware;
	u16 vid = ent_get(e, L2_VLAN_ID);
	struct nlmsghdr *nlh;
	struct ndmsg *ndm;
	u8 mac[ETH_ALEN];

	if (!l2x_on_port(d->p, d->p->br, e) || (aware && !vid))
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
	if (nla_put(d->skb, NDA_LLADDR, ETH_ALEN, mac) || (aware && nla_put_u16(d->skb, NDA_VLAN, vid))) {
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
		INIT_LIST_HEAD(&u->br[i].fdbs);
	}
	INIT_DELAYED_WORK(&u->l2mod, l2mod_fn);
	INIT_WORK(&u->vflush_work, vflush_fn);
}

void us48_sw_unit_exit(struct us48_cmic *u)
{
	rtnl_lock();
	l2mod_stop(u);			/* normally done: the last bridge left with the netdevs */
	rtnl_unlock();
	cancel_delayed_work_sync(&u->l2mod);
	cancel_work_sync(&u->vflush_work);
	kvfree(u->l2shadow);
	kvfree(u->l2buf);
	kvfree(u->l2pop);
	u->l2shadow = NULL;
	u->l2buf = NULL;
	u->l2pop = NULL;
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
