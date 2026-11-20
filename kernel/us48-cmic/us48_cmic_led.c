// SPDX-License-Identifier: GPL-2.0
/*
 * Front-port LEDs through the CMICd LEDUP0 processor of each unit (docs/leds.md, #14, #139).
 * Our program (us48_ledup.asm) serialises per-slot bytes; policy is here: LED class devices per port bit with
 * netdev-trigger offload, and the st1008f 300-16 interface on /sys/devices/platform/us48-leds.
 */
#include <linux/ctype.h>
#include <linux/jiffies.h>
#include <linux/leds.h>
#include <linux/module.h>
#include <linux/netdevice.h>
#include <linux/panic.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/sort.h>

#include "us48_cmic.h"
#include "us48_hwdb.h"
#include "us48_ledup_prog.h"

/* CMIC_LEDUP0_* offsets: regdb/hr2_regs.tsv via hwdb (same in both units' CMIC window) */
#define LED_CTRL		HW_CMIC0_LEDUP_CTRL
#define LED_STATUS		HW_CMIC0_LEDUP_STATUS
#define LED_CLK			HW_CMIC0_LEDUP_CLK_PARAMS
#define LED_DRAM(i)		(HW_CMIC0_LEDUP_DATA_RAM + 4 * (i))
#define LED_PRAM(i)		(HW_CMIC0_LEDUP_PROGRAM_RAM + 4 * (i))
#define LED_REMAP(i)		(HW_CMIC0_LEDUP_PORT_ORDER_REMAP + 4 * (i))
#define LED_REMAP_REGS		8	/* ports 0-31: SDK _hurricane2_ledup_init writes REMAP_0_3 .. REMAP_28_31 */
#define LED_RAM_WORDS		256	/* SDK _hurricane2_ledup_init zeroes all 256 data words */
#define NSLOT_MAX		32	/* CTL 0xa0-0xbf, SPORT 0xc0-0xdf */
#define XLPORT_LED_CHAIN_CONFIG	0x02022b00	/* regdb, INTRA_DELAY 3:0; SDK + stock trace: 6 on blocks 5 and 6 */
#define XLPORT_LED_INTRA_DELAY	6
#define BLK_XLPORT1		6

static int booting = -1;
module_param(booting, int, 0444);
MODULE_PARM_DESC(booting, "port LEDs at load: -1 (default) booting pattern unless LEDUP already runs our program (module reload: hw), 1 booting, 0 hw");

/* CTL byte, us48_ledup.asm */
#define CTL_VAL(v)		((v) & 3)
#define CTL_MASK(m)		(((m) & 3) << 2)
#define CTL_SPEED(s)		(((s) & 3) << 4)
#define CTL_ACT			BIT(6)
enum { SP_NONE, SP_SLOW, SP_MID, SP_FAST };
/* Half periods = 16/8/4 refreshes (TICK bit 4/3/2) at the reset CLK_PARAMS refresh rate measured on the board (docs/leds.md) */
static const unsigned int sp_ms[] = { 0, 714, 357, 178 };

enum { PAT_AUTO, PAT_HW, PAT_OFF, PAT_ON, PAT_BOOTING, PAT_ERROR, PAT_PANIC, PAT_PANIC2, PAT_FALLBACK,
	PAT_CUSTOM };

#define HW_FLAGS_FAST	(BIT(TRIGGER_NETDEV_LINK_1000) | BIT(TRIGGER_NETDEV_LINK_10000))
#define HW_FLAGS_SLOW	(BIT(TRIGGER_NETDEV_LINK_10) | BIT(TRIGGER_NETDEV_LINK_100))
#define HW_FLAGS_ACT	(BIT(TRIGGER_NETDEV_RX) | BIT(TRIGGER_NETDEV_TX))
#define HW_FLAGS_OK	(BIT(TRIGGER_NETDEV_LINK) | HW_FLAGS_FAST | HW_FLAGS_SLOW | HW_FLAGS_ACT)

struct us48_ledu;

enum { COL_GREEN, COL_AMBER };

struct us48_led {			/* one LED class device = one colour = one output bit of a port's slot */
	struct led_classdev cdev;
	struct us48_ledu *lu;
	struct us48_port *p;
	s8 bit;				/* chain bit of this colour, -1 = no such LED (col_bit) */
	bool hw;			/* netdev trigger offload: value from link, blanking from the scan chain */
	unsigned long flags;		/* TRIGGER_NETDEV_* while hw */
	bool on;
	u8 speed;			/* blink_set offload, SP_* */
	bool registered;
	char name[48];			/* "<port>:<colour>:lan" */
};

struct us48_ledu {
	struct us48_cmic *u;
	int unit;
	int nslot;
	struct us48_port *slot_port[NSLOT_MAX];	/* NULL = padding slot */
	int port_slot[MAXPORTS];
	struct us48_led led[MAXPORTS][2];	/* [port][COL_*] */
	u8 cur[NSLOT_MAX];			/* CTL as last written */
	u8 tick0;
	unsigned long t0;
};

/* Chain format: board-test unknowns, settable at runtime through `chain` (docs/leds.md) */
static struct {
	u8 nbits, pol, extra, slots;
	bool act;
	char order[8];
} chain = { .nbits = 2, .act = true, .order = "front", .slots = 28 };	/* stock: 26 ports + 2 pad, 56 bits */

static DEFINE_SPINLOCK(led_lock);
static struct us48_ledu *units[2];
static int mode = PAT_BOOTING;
static bool manual = true, err_latched;
static char err_why[32];
static u8 custom[2][NSLOT_MAX];
static struct platform_device *led_pdev;
static long (*prev_panic_blink)(int state);

static void dram_wr(struct us48_ledu *lu, int i, u8 v)
{
	iowrite32(v, lu->u->cmic + LED_DRAM(i));
}

static u8 dram_rd(struct us48_ledu *lu, int i)
{
	return ioread32(lu->u->cmic + LED_DRAM(i)) & 0xff;
}

static int port_speed(struct us48_port *p)
{
	if (!netif_carrier_ok(p->ndev))
		return 0;
	if (p->cfg->copper)
		return READ_ONCE(p->speed);
	return us48_cfg_1g(p->cfg) ? 1000 : 10000;
}

static bool flags_lit(unsigned long fl, int speed)
{
	if (!speed)
		return false;
	if (fl & BIT(TRIGGER_NETDEV_LINK))
		return true;
	return (speed == 10 && (fl & BIT(TRIGGER_NETDEV_LINK_10))) ||
	       (speed == 100 && (fl & BIT(TRIGGER_NETDEV_LINK_100))) ||
	       (speed == 1000 && (fl & BIT(TRIGGER_NETDEV_LINK_1000))) ||
	       (speed == 10000 && (fl & BIT(TRIGGER_NETDEV_LINK_10000)));
}

/*
 * Chain bit per colour (board 2026-11-19, docs/leds.md cage map): copper and unit 1 SFP+ green = bit 0, amber = bit 1
 * ("11" is dark on copper); unit 0 SFP 1/2 green = bit 1, bit 0 lights nothing.
 */
static int col_bit(int unit, struct us48_port *p, int col)
{
	if (p->cfg->copper || unit)
		return col;
	return col == COL_GREEN ? 1 : -1;
}

/* Default offload = stock symbols (docs/ledup-stock.md): copper green 1G, SFP+ green 10G, rest the other colour */
static unsigned long default_flags(int unit, struct us48_port *p, int col)
{
	if (p->cfg->copper)
		return (col == COL_GREEN ? HW_FLAGS_FAST : HW_FLAGS_SLOW) | HW_FLAGS_ACT;
	if (!unit)
		return BIT(TRIGGER_NETDEV_LINK) | HW_FLAGS_ACT;
	return (col == COL_GREEN ? BIT(TRIGGER_NETDEV_LINK_10000) : HW_FLAGS_SLOW | BIT(TRIGGER_NETDEV_LINK_1000)) |
	       HW_FLAGS_ACT;
}

static u8 slot_policy(struct us48_ledu *lu, int s, bool all_hw)
{
	struct us48_port *p = lu->slot_port[s];
	u8 val = 0, mask = 0, speed = 0;
	bool act = false;
	int i, c, sp;

	if (!p)
		return 0;
	for (i = 0; i < lu->u->nports && lu->u->port[i] != p; i++)
		;
	sp = port_speed(p);
	for (c = 0; c < 2; c++) {
		struct us48_led *l = &lu->led[i][c];
		unsigned long fl = all_hw ? default_flags(lu->unit, p, c) : l->flags;
		bool lit;

		if (l->bit < 0)
			continue;
		if (all_hw || l->hw) {
			lit = flags_lit(fl, sp);
			val |= lit << l->bit;
			act |= lit && (fl & HW_FLAGS_ACT);
		} else {
			val |= l->on << l->bit;
			if (l->on && l->speed) {
				mask |= BIT(l->bit);
				speed = l->speed;
			}
		}
	}
	return CTL_VAL(val) | CTL_MASK(mask) | CTL_SPEED(speed) | (act && chain.act ? CTL_ACT : 0);
}

static u8 blink_bit(int b, u8 sp)
{
	return CTL_VAL(BIT(b)) | CTL_MASK(BIT(b)) | CTL_SPEED(sp);
}

/* Fixed patterns: one colour per port at a time ("11" is dark on copper); st1008f 300-16 names */
static u8 slot_pattern(struct us48_ledu *lu, int pat, int s)
{
	struct us48_port *p = lu->slot_port[s];
	int g = col_bit(lu->unit, p, COL_GREEN), a = col_bit(lu->unit, p, COL_AMBER);

	if (a < 0)
		a = g;
	switch (pat) {
	case PAT_ON:
		return CTL_VAL(BIT(g));
	case PAT_BOOTING:
		return blink_bit(g, SP_SLOW);
	case PAT_ERROR:
		return blink_bit(a, SP_FAST);
	case PAT_PANIC:
	case PAT_PANIC2:
		return blink_bit((s + (pat == PAT_PANIC2)) & 1 ? a : g, SP_FAST);
	case PAT_FALLBACK:
		return blink_bit(a, SP_MID);
	default:
		return 0;
	}
}

static u8 slot_ctl(struct us48_ledu *lu, int s)
{
	if (s >= lu->nslot || !lu->slot_port[s])
		return 0;
	switch (mode) {
	case PAT_AUTO:
		return slot_policy(lu, s, false);
	case PAT_HW:
		return slot_policy(lu, s, true);
	case PAT_CUSTOM:
		return custom[lu->unit][s];
	default:
		return slot_pattern(lu, mode, s);
	}
}

/* led_lock held, or lock-free from panic_blink: single-word MMIO writes only */
static void apply_unit(struct us48_ledu *lu)
{
	int s;

	for (s = 0; s < lu->nslot; s++) {
		u8 v = slot_ctl(lu, s);

		if (v != lu->cur[s]) {
			dram_wr(lu, US48_LEDUP_CTL + s, v);
			lu->cur[s] = v;
		}
	}
}

static void apply_all(void)
{
	int i;

	for (i = 0; i < 2; i++)
		if (units[i])
			apply_unit(units[i]);
}

static int cmp_front(const void *a, const void *b)
{
	const struct us48_port *x = *(struct us48_port * const *)a, *y = *(struct us48_port * const *)b;

	if (!strcmp(chain.order, "phys") || !strcmp(chain.order, "rphys"))
		return x->cfg->phys - y->cfg->phys;
	return x->cfg->front - y->cfg->front;
}

/* Slot map + chain bytes; LEDUP stopped or running (the program re-reads every refresh). led_lock held. */
static void unit_layout(struct us48_ledu *lu)
{
	struct us48_cmic *u = lu->u;
	int i, n = min(u->nports, NSLOT_MAX);

	for (i = 0; i < n; i++)
		lu->slot_port[i] = u->port[i];
	sort(lu->slot_port, n, sizeof(lu->slot_port[0]), cmp_front, NULL);
	if (chain.order[0] == 'r')
		for (i = 0; i < n / 2; i++)
			swap(lu->slot_port[i], lu->slot_port[n - 1 - i]);
	lu->nslot = min(max(n + chain.extra, (int)chain.slots), NSLOT_MAX);
	for (i = n; i < NSLOT_MAX; i++)
		lu->slot_port[i] = NULL;
	for (i = 0; i < MAXPORTS; i++)
		lu->port_slot[i] = -1;
	for (i = 0; i < lu->nslot; i++) {
		struct us48_port *p = lu->slot_port[i];
		int j;

		dram_wr(lu, US48_LEDUP_SPORT + i, p ? p->cfg->phys : 0);
		for (j = 0; p && j < u->nports; j++)
			if (u->port[j] == p)
				lu->port_slot[j] = i;
		lu->cur[i] = 0xff;	/* force a write */
	}
	dram_wr(lu, US48_LEDUP_NSLOT, lu->nslot);
	dram_wr(lu, US48_LEDUP_NBITS, chain.nbits);
	dram_wr(lu, US48_LEDUP_POL, chain.pol);
	dram_wr(lu, US48_LEDUP_TOTAL, lu->nslot * chain.nbits);
	apply_unit(lu);
}

static struct us48_led *to_ul(struct led_classdev *c)
{
	return container_of(c, struct us48_led, cdev);
}

static void ul_update(struct us48_led *l)
{
	unsigned long fl;

	spin_lock_irqsave(&led_lock, fl);
	if (mode == PAT_AUTO)
		apply_unit(l->lu);
	spin_unlock_irqrestore(&led_lock, fl);
}

static void ul_brightness_set(struct led_classdev *c, enum led_brightness b)
{
	struct us48_led *l = to_ul(c);

	l->hw = false;
	l->on = b != LED_OFF;
	if (!l->on)
		l->speed = SP_NONE;
	ul_update(l);
}

/* Timer trigger offload: nearest LEDUP speed, symmetric; the result is written back as the core expects */
static int ul_blink_set(struct led_classdev *c, unsigned long *on, unsigned long *off)
{
	struct us48_led *l = to_ul(c);
	unsigned long want = (*on || *off) ? (*on + *off) / 2 : 500;
	int s, best = SP_SLOW;

	for (s = SP_SLOW; s <= SP_FAST; s++)
		if (abs((long)sp_ms[s] - (long)want) < abs((long)sp_ms[best] - (long)want))
			best = s;
	*on = *off = sp_ms[best];
	l->hw = false;
	l->on = true;
	l->speed = best;
	ul_update(l);
	return 0;
}

static int ul_hw_supported(struct led_classdev *c, unsigned long flags)
{
	if (flags & ~HW_FLAGS_OK)
		return -EOPNOTSUPP;
	/* the program blanks on RX or TX of the port: only both or neither */
	if ((flags & HW_FLAGS_ACT) && (flags & HW_FLAGS_ACT) != HW_FLAGS_ACT)
		return -EOPNOTSUPP;
	if ((flags & HW_FLAGS_ACT) && !chain.act)
		return -EOPNOTSUPP;
	return 0;
}

static int ul_hw_set(struct led_classdev *c, unsigned long flags)
{
	struct us48_led *l = to_ul(c);
	int ret = ul_hw_supported(c, flags);

	if (ret)
		return ret;
	l->flags = flags;
	l->hw = true;
	ul_update(l);
	return 0;
}

static int ul_hw_get(struct led_classdev *c, unsigned long *flags)
{
	*flags = to_ul(c)->flags;
	return 0;
}

static struct device *ul_hw_dev(struct led_classdev *c)
{
	return &to_ul(c)->p->ndev->dev;
}

static long us48_panic_blink(int state)
{
	int i, m = state ? PAT_PANIC : PAT_PANIC2;

	if (prev_panic_blink)
		prev_panic_blink(state);
	WRITE_ONCE(mode, m);
	for (i = 0; i < 2; i++) {
		struct us48_ledu *lu = READ_ONCE(units[i]);

		if (lu)
			apply_unit(lu);
	}
	return 0;
}

/* XLPORT (cage) status into the scan chain: SDK _hurricane2_ledup_init. Probe before pwrsave; after an XLPORT0 power-up */
void us48_led_chain_cfg(struct us48_cmic *u)
{
	u32 blk, v = XLPORT_LED_INTRA_DELAY;

	for (blk = BLK_XLPORT; blk <= BLK_XLPORT1; blk++)
		if (us48_sbus(u, BCM_SCHAN_WRITE_REG, blk, XLPORT_LED_CHAIN_CONFIG, &v, 1))
			dev_warn(u->dev, "ledup: XLPORT_LED_CHAIN_CONFIG block %u write failed\n", blk);
}

/* SDK _hurricane2_ledup_init (= stock u0 trace, docs/leds.md): scan delays, port remap; EN stays clear */
static void ledup_hw_init(struct us48_cmic *u)
{
	u32 v = ioread32(u->cmic + LED_CTRL);
	int i, k;

	v &= ~(HW_CMIC0_LEDUP_CTRL_EN_MASK | HW_CMIC0_LEDUP_CTRL_SCAN_INTRA_PORT_DELAY_MASK |
	       HW_CMIC0_LEDUP_CTRL_SCAN_START_DELAY_MASK);
	v |= 5 << HW_CMIC0_LEDUP_CTRL_SCAN_INTRA_PORT_DELAY_SHIFT | 6 << HW_CMIC0_LEDUP_CTRL_SCAN_START_DELAY_SHIFT;
	iowrite32(v, u->cmic + LED_CTRL);
	for (i = 0; i < LED_REMAP_REGS; i++) {
		int p0 = 4 * i + 2;
		bool rev = p0 == 26 || p0 == 30;	/* SDK: "Transpose MXQ Block ports" */

		for (v = 0, k = 0; k < 4; k++)
			v |= (rev ? p0 + 3 - k : p0 + k) << (HW_CMIC0_LEDUP_PORT_ORDER_REMAP_PORT1_SHIFT * k);
		iowrite32(v, u->cmic + LED_REMAP(i));
	}
}

/* Our program already running = module reload after boot (awto-uboot does not load LEDUP, docs/leds.md) */
static bool ledup_was_ours[2];

/* Called by us48_chip_cold_init before its CMIC reset (CPS_RESET clears LEDUP) */
void us48_led_pre_reset(void __iomem *cmic, int unit)
{
	int i;

	if (unit < 0 || unit > 1)
		return;
	ledup_was_ours[unit] = false;
	if (!(ioread32(cmic + LED_CTRL) & HW_CMIC0_LEDUP_CTRL_EN_MASK))
		return;
	for (i = 0; i < sizeof(us48_ledup_prog); i++)
		if ((ioread32(cmic + LED_PRAM(i)) & 0xff) != us48_ledup_prog[i])
			return;
	ledup_was_ours[unit] = true;
}

/* Probe: stop, hw init, program, clear data RAM, chain bytes + current pattern, start (stock 0x120ed64 order) */
void us48_led_unit_init(struct us48_cmic *u)
{
	struct us48_ledu *lu;
	unsigned long fl;
	int i, c, unit = u->modid;
	bool reload;
	u32 st;

	if (unit > 1)
		return;
	lu = devm_kzalloc(u->dev, sizeof(*lu), GFP_KERNEL);
	if (!lu)
		return;
	lu->u = u;
	lu->unit = unit;
	reload = ledup_was_ours[unit];
	ledup_hw_init(u);
	for (i = 0; i < LED_RAM_WORDS; i++)
		iowrite32(i < sizeof(us48_ledup_prog) ? us48_ledup_prog[i] : 0, u->cmic + LED_PRAM(i));
	for (i = 0; i < LED_RAM_WORDS; i++)
		dram_wr(lu, i, 0);
	for (i = 0; i < u->nports; i++) {
		for (c = 0; c < 2; c++) {
			struct us48_led *l = &lu->led[i][c];

			l->lu = lu;
			l->p = u->port[i];
			l->bit = col_bit(unit, l->p, c);
			l->hw = true;
			l->flags = default_flags(unit, l->p, c);
		}
	}
	spin_lock_irqsave(&led_lock, fl);
	if (mode == PAT_BOOTING && manual && (booting == 0 || (booting < 0 && reload))) {
		mode = PAT_AUTO;
		manual = false;
	}
	unit_layout(lu);
	spin_unlock_irqrestore(&led_lock, fl);
	iowrite32(ioread32(u->cmic + LED_CTRL) | HW_CMIC0_LEDUP_CTRL_EN_MASK, u->cmic + LED_CTRL);
	st = ioread32(u->cmic + LED_STATUS);
	lu->tick0 = dram_rd(lu, US48_LEDUP_TICK);
	lu->t0 = jiffies;
	dev_info(u->dev, "ledup0: %zu B program, %d slots x %u bits, CTRL %08x STATUS %08x (%s), CLK_PARAMS %08x%s\n",
		 sizeof(us48_ledup_prog), lu->nslot, chain.nbits, ioread32(u->cmic + LED_CTRL), st,
		 st & HW_CMIC0_LEDUP_STATUS_RUNNING_MASK ? "running" : "not running", ioread32(u->cmic + LED_CLK),
		 reload ? ", reload: was running" : "");

	for (i = 0; i < u->nports; i++) {
		for (c = 0; c < 2; c++) {
			struct us48_led *l = &lu->led[i][c];
			struct led_classdev *cd = &l->cdev;

			if (l->bit < 0)
				continue;
			snprintf(l->name, sizeof(l->name), "%s:%s:lan", u->port[i]->ndev->name, c ? "amber" : "green");
			cd->name = l->name;
			cd->max_brightness = 1;
			cd->brightness_set = ul_brightness_set;
			cd->blink_set = ul_blink_set;
			cd->hw_control_trigger = "netdev";
			cd->hw_control_is_supported = ul_hw_supported;
			cd->hw_control_set = ul_hw_set;
			cd->hw_control_get = ul_hw_get;
			cd->hw_control_get_device = ul_hw_dev;
			cd->default_trigger = "netdev";
			cd->flags = LED_RETAIN_AT_SHUTDOWN;
			if (led_classdev_register(u->dev, cd))
				dev_warn(u->dev, "%s: LED class register failed\n", l->name);
			else
				l->registered = true;
		}
	}
	spin_lock_irqsave(&led_lock, fl);
	WRITE_ONCE(units[unit], lu);
	u->leds = lu;
	apply_unit(lu);
	spin_unlock_irqrestore(&led_lock, fl);
}

void us48_led_unit_exit(struct us48_cmic *u)
{
	struct us48_ledu *lu = u->leds;
	unsigned long fl;
	int i, b;

	if (!lu)
		return;
	for (i = 0; i < u->nports; i++)
		for (b = 0; b < 2; b++)
			if (lu->led[i][b].registered)
				led_classdev_unregister(&lu->led[i][b].cdev);
	spin_lock_irqsave(&led_lock, fl);
	WRITE_ONCE(units[lu->unit], NULL);
	for (i = 0; i < lu->nslot; i++)		/* LEDs off; the program keeps running */
		dram_wr(lu, US48_LEDUP_CTL + i, 0);
	u->leds = NULL;
	spin_unlock_irqrestore(&led_lock, fl);
}

/* Carrier/speed changes of our ports: hw-offloaded LEDs follow the link (rtnl, process context) */
static int led_netdev_event(struct notifier_block *nb, unsigned long ev, void *ptr)
{
	struct net_device *nd = netdev_notifier_info_to_dev(ptr);
	unsigned long fl;

	if (!us48_is_port(nd) || (ev != NETDEV_CHANGE && ev != NETDEV_UP && ev != NETDEV_DOWN))
		return NOTIFY_DONE;
	spin_lock_irqsave(&led_lock, fl);
	if (mode == PAT_AUTO || mode == PAT_HW)
		apply_all();
	spin_unlock_irqrestore(&led_lock, fl);
	return NOTIFY_DONE;
}

static struct notifier_block led_netdev_nb = { .notifier_call = led_netdev_event };

/* ---- st1008f 300-16 sysfs interface ---- */

static int col_front(struct us48_ledu **cu, int *cs, int c)
{
	return cu[c]->slot_port[cs[c]]->cfg->front;
}

/* Columns = all port slots of both units in front order: cell digit index = bit * ncols + column */
static int columns(struct us48_ledu **cu, int *cs, int max)
{
	int n = 0, i, s;

	for (i = 0; i < 2; i++) {
		struct us48_ledu *lu = units[i];

		for (s = 0; lu && s < lu->nslot && n < max; s++) {
			if (!lu->slot_port[s])
				continue;
			cu[n] = lu;
			cs[n++] = s;
		}
	}
	for (i = 1; i < n; i++)		/* insertion sort by front */
		for (s = i; s > 0 && col_front(cu, cs, s) < col_front(cu, cs, s - 1); s--) {
			swap(cu[s], cu[s - 1]);
			swap(cs[s], cs[s - 1]);
		}
	return n;
}

static int speed_digit(u8 sp)
{
	return sp == SP_FAST ? 3 : sp == SP_MID ? 4 : 5;
}

static u8 digit_speed(int d)
{
	return d <= 3 ? SP_FAST : d == 4 ? SP_MID : SP_SLOW;
}

static const char *state_name(void)
{
	int i, j, b;

	switch (mode) {
	case PAT_HW:
		return "hw";
	case PAT_BOOTING:
		return "booting";
	case PAT_ERROR:
		return "error";
	case PAT_PANIC:
	case PAT_PANIC2:
		return "panic";
	case PAT_FALLBACK:
		return "fallback";
	case PAT_AUTO:
		for (i = 0; i < 2; i++)
			for (j = 0; units[i] && j < units[i]->u->nports; j++)
				for (b = 0; b < 2; b++)
					if (!units[i]->led[j][b].hw)
						return "custom";
		return "hw";
	default:
		return "custom";
	}
}

static ssize_t state_show(struct device *dev, struct device_attribute *a, char *buf)
{
	unsigned long fl;
	ssize_t n;

	spin_lock_irqsave(&led_lock, fl);
	n = sysfs_emit(buf, "%s%s%s\n", state_name(), manual ? " (manual)" : "", err_latched ? " (error latched)" : "");
	spin_unlock_irqrestore(&led_lock, fl);
	return n;
}
static DEVICE_ATTR_RO(state);

#define NCOL_MAX	(2 * NSLOT_MAX)

static ssize_t pattern_show(struct device *dev, struct device_attribute *a, char *buf)
{
	struct us48_ledu *cu[NCOL_MAX];
	int cs[NCOL_MAX], n, c, b, len = 0;
	unsigned long fl;

	spin_lock_irqsave(&led_lock, fl);
	if (mode == PAT_HW || (mode == PAT_AUTO && !strcmp(state_name(), "hw"))) {
		spin_unlock_irqrestore(&led_lock, fl);
		return sysfs_emit(buf, "hw\n");
	}
	n = columns(cu, cs, NCOL_MAX);
	for (b = 0; b < 2; b++) {
		for (c = 0; c < n; c++) {
			u8 v = cu[c]->cur[cs[c]], sp = v >> 4 & 3;
			int d = !(v & BIT(b)) ? 0 : (v & CTL_MASK(BIT(b))) && sp ? speed_digit(sp) : 7;

			len += sysfs_emit_at(buf, len, "%d", d);
		}
		len += sysfs_emit_at(buf, len, "\n");
	}
	spin_unlock_irqrestore(&led_lock, fl);
	return len;
}

/* auto, hw, off, on, booting, error, panic, fallback, or 2 x ncols digits 0-7 (row bit 0, then row bit 1) */
static ssize_t pattern_store(struct device *dev, struct device_attribute *a, const char *buf, size_t len)
{
	static const char *const names[] = { "auto", "hw", "off", "on", "booting", "error", "panic", NULL, "fallback" };
	struct us48_ledu *cu[NCOL_MAX];
	int cs[NCOL_MAX], n, i, k = 0;
	u8 cells[2 * NCOL_MAX], next[2][NSLOT_MAX] = {};
	unsigned long fl;

	if (err_latched)
		return -EBUSY;
	for (i = 0; i < ARRAY_SIZE(names); i++) {
		if (names[i] && sysfs_streq(buf, names[i])) {
			spin_lock_irqsave(&led_lock, fl);
			mode = i;
			manual = i != PAT_AUTO;
			apply_all();
			spin_unlock_irqrestore(&led_lock, fl);
			return len;
		}
	}
	spin_lock_irqsave(&led_lock, fl);
	n = columns(cu, cs, NCOL_MAX);
	spin_unlock_irqrestore(&led_lock, fl);
	for (i = 0; i < len && k < 2 * n; i++) {
		if (buf[i] >= '0' && buf[i] <= '7')
			cells[k++] = buf[i] - '0';
		else if (!isspace(buf[i]))
			return -EINVAL;
	}
	if (!n || k != 2 * n)
		return -EINVAL;
	for (i = 0; i < n; i++) {
		u8 v = 0, mask = 0, sp = 0;
		int b;

		for (b = 0; b < 2; b++) {
			int d = cells[b * n + i];

			if (!d)
				continue;
			v |= BIT(b);
			if (d == 7)
				continue;
			if (sp && sp != digit_speed(d))
				return -EINVAL;	/* one blink speed per slot (docs/leds.md) */
			sp = digit_speed(d);
			mask |= BIT(b);
		}
		next[cu[i]->unit][cs[i]] = CTL_VAL(v) | CTL_MASK(mask) | CTL_SPEED(sp);
	}
	spin_lock_irqsave(&led_lock, fl);
	memcpy(custom, next, sizeof(custom));
	mode = PAT_CUSTOM;
	manual = true;
	apply_all();
	spin_unlock_irqrestore(&led_lock, fl);
	return len;
}
static DEVICE_ATTR_RW(pattern);

static ssize_t error_show(struct device *dev, struct device_attribute *a, char *buf)
{
	return sysfs_emit(buf, "%d%s%s\n", err_latched, err_latched ? " " : "", err_why);
}

/* any write latches (text = reason); only `clear` releases */
static ssize_t error_store(struct device *dev, struct device_attribute *a, const char *buf, size_t len)
{
	unsigned long fl;

	spin_lock_irqsave(&led_lock, fl);
	if (!err_latched) {
		strscpy(err_why, buf, sizeof(err_why));
		strim(err_why);
		err_latched = true;
		dev_warn(dev, "error latched: %s\n", err_why);
	}
	mode = PAT_ERROR;
	manual = true;
	apply_all();
	spin_unlock_irqrestore(&led_lock, fl);
	return len;
}
static DEVICE_ATTR_RW(error);

/* release the latch and any manual pattern: back to the per-port policy (LED class devices) */
static ssize_t clear_store(struct device *dev, struct device_attribute *a, const char *buf, size_t len)
{
	unsigned long fl;

	spin_lock_irqsave(&led_lock, fl);
	err_latched = false;
	err_why[0] = 0;
	manual = false;
	mode = PAT_AUTO;
	apply_all();
	spin_unlock_irqrestore(&led_lock, fl);
	return len;
}
static DEVICE_ATTR_WO(clear);

/* st1008f `cages` equivalent: one line per column */
static ssize_t ports_show(struct device *dev, struct device_attribute *a, char *buf)
{
	struct us48_ledu *cu[NCOL_MAX];
	int cs[NCOL_MAX], n, c, len;
	unsigned long fl;

	len = sysfs_emit(buf, "column front port unit slot scan_port link_mbps ctl\n");
	spin_lock_irqsave(&led_lock, fl);
	n = columns(cu, cs, NCOL_MAX);
	for (c = 0; c < n; c++) {
		struct us48_port *p = cu[c]->slot_port[cs[c]];

		len += sysfs_emit_at(buf, len, "%d %u %s %d %d %u %d %02x\n", c, p->cfg->front, p->ndev->name,
				     cu[c]->unit, cs[c], p->cfg->phys, port_speed(p), cu[c]->cur[cs[c]]);
	}
	spin_unlock_irqrestore(&led_lock, fl);
	return len;
}
static DEVICE_ATTR_RO(ports);

/* Chain format + live LEDUP state per unit; write "nbits=N pol=X order=front|rfront|phys|rphys extra=N slots=N act=0|1" */
static ssize_t chain_show(struct device *dev, struct device_attribute *a, char *buf)
{
	int i, len = sysfs_emit(buf, "nbits=%u pol=%u order=%s extra=%u slots=%u act=%d\n", chain.nbits,
				chain.pol, chain.order, chain.extra, chain.slots, chain.act);

	for (i = 0; i < 2; i++) {
		struct us48_ledu *lu = READ_ONCE(units[i]);
		unsigned long ms;
		u8 tick;

		if (!lu)
			continue;
		tick = dram_rd(lu, US48_LEDUP_TICK);
		ms = jiffies_to_msecs(jiffies - lu->t0);
		/* TICK wraps every 256 refreshes: the rate is valid only for reads closer than that (docs/leds.md) */
		len += sysfs_emit_at(buf, len,
				     "unit%d STATUS %08x CTRL %08x CLK_PARAMS %08x slots %d tick %u (%u in %lu ms)\n",
				     i, ioread32(lu->u->cmic + LED_STATUS), ioread32(lu->u->cmic + LED_CTRL),
				     ioread32(lu->u->cmic + LED_CLK), lu->nslot, tick, (u8)(tick - lu->tick0), ms);
		lu->tick0 = tick;
		lu->t0 = jiffies;
	}
	return len;
}

static ssize_t chain_store(struct device *dev, struct device_attribute *a, const char *buf, size_t len)
{
	static const char *const orders[] = { "front", "rfront", "phys", "rphys" };
	char *s, *tok, *dup = kstrndup(buf, len, GFP_KERNEL);
	typeof(chain) c = chain;
	unsigned long fl;
	unsigned int v;
	int i, ret = 0;

	if (!dup)
		return -ENOMEM;
	s = strim(dup);
	while (!ret && (tok = strsep(&s, " \t\n"))) {
		if (!*tok)
			continue;
		if (sscanf(tok, "nbits=%u", &v) == 1 && v >= 1 && v <= 4)
			c.nbits = v;
		else if (sscanf(tok, "pol=%x", &v) == 1 && v <= 0xf)
			c.pol = v;
		else if (sscanf(tok, "extra=%u", &v) == 1 && v <= NSLOT_MAX)
			c.extra = v;
		else if (sscanf(tok, "slots=%u", &v) == 1 && v <= NSLOT_MAX)
			c.slots = v;
		else if (sscanf(tok, "act=%u", &v) == 1 && v <= 1)
			c.act = v;
		else if (!strncmp(tok, "order=", 6) && match_string(orders, ARRAY_SIZE(orders), tok + 6) >= 0)
			strscpy(c.order, tok + 6, sizeof(c.order));
		else
			ret = -EINVAL;
	}
	kfree(dup);
	if (ret)
		return ret;
	spin_lock_irqsave(&led_lock, fl);
	chain = c;
	for (i = 0; i < 2; i++)
		if (units[i])
			unit_layout(units[i]);
	spin_unlock_irqrestore(&led_lock, fl);
	return len;
}
static DEVICE_ATTR_RW(chain);

static struct attribute *us48_leds_attrs[] = {
	&dev_attr_state.attr, &dev_attr_pattern.attr, &dev_attr_error.attr, &dev_attr_clear.attr,
	&dev_attr_ports.attr, &dev_attr_chain.attr, NULL,
};
ATTRIBUTE_GROUPS(us48_leds);

int us48_led_init(void)
{
	int ret;

	led_pdev = platform_device_register_data(NULL, "us48-leds", PLATFORM_DEVID_NONE, NULL, 0);
	if (IS_ERR(led_pdev))
		return PTR_ERR(led_pdev);
	ret = device_add_groups(&led_pdev->dev, us48_leds_groups);
	if (ret) {
		platform_device_unregister(led_pdev);
		return ret;
	}
	register_netdevice_notifier(&led_netdev_nb);
	/* ledtrig-panic (system LED) may own panic_blink already: chain it (docs/leds.md) */
	prev_panic_blink = panic_blink;
	panic_blink = us48_panic_blink;
	return 0;
}

void us48_led_exit(void)
{
	if (panic_blink == us48_panic_blink)
		panic_blink = prev_panic_blink;
	unregister_netdevice_notifier(&led_netdev_nb);
	device_remove_groups(&led_pdev->dev, us48_leds_groups);
	platform_device_unregister(led_pdev);
}
