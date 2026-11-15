// SPDX-License-Identifier: GPL-2.0-only
/*
 * us48_vnet: virtual Ethernet between a1 (unit 0 A9) and a2 (unit 1 A9) over the a2 pool in host DDR (#85).
 *
 * One module for both sides. The side is picked at init:
 *  - a2: DT root compatible "ubnt,us48-a2"; platform device `ubnt,us48-vnet` whose memory-region is the
 *    hwdb a2_vnet block. The block is plain linear-mapped RAM (write-back, cached): cache maintenance by the
 *    DMA API (dma_sync_single_for_cpu/device), as for any non-coherent device.
 *  - a1: no DT node. The module is a remoteproc subdevice of `us48_a2`: the pool mapping (dma_alloc_wc, write
 *    combined) exists between prepare and unprepare, rings are initialised before a2 is released.
 *
 * Shared block (docs/a2-design.md, "us48-vnet"): ctl line, one state/head/tail line per side, then
 * two rings of 32 slots (ring k is written by side k: a1 -> a2 = 0, a2 -> a1 = 1).
 *  - Every cache line has exactly one writer. A side writes only its own line + its own tx slots.
 *  - Counters are free running; slot = counter % 32. Publish = data, barrier/clean, then head.
 *  - Doorbell = hrtimer poll on both sides (phase 1). No interrupts, no checksum offload (CHECKSUM_NONE).
 *
 * Ordering (#79): a2 write -> PCIe posted write; a1 sees it only once it landed. Before publishing head, a2 reads
 * back from the slot it just cleaned: with PAXB EP_TX_WR_RD_ORDER (us48_a2 paxb_order=1) the read completes
 * behind the writes. a1 -> a2: wmb() drains the a1 write buffer before head; a2 invalidates before it reads.
 */
#include <linux/dma-mapping.h>
#include <linux/err.h>
#include <linux/etherdevice.h>
#include <linux/ethtool.h>
#include <linux/hrtimer.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/platform_device.h>
#include <linux/remoteproc.h>
#include <linux/skbuff.h>

#include "us48_hwdb.h"
#include "us48_pci.h"

#define VN_MAGIC	0x564e4554	/* "VNET" */
#define VN_VERSION	1
#define VN_SLOTS	32		/* power of two */
#define VN_SLOT_HDR	64		/* len word, padded to a cache line pair */
#define VN_MTU		9000
#define VN_FRAME_MAX	(VN_MTU + ETH_HLEN)
#define VN_SLOT_SIZE	(VN_SLOT_HDR + ALIGN(VN_FRAME_MAX, 64))	/* 9088 = 142 * 64 */
#define VN_RING_OFF	0x1000
#define VN_SHM_SIZE	(VN_RING_OFF + 2 * VN_SLOTS * VN_SLOT_SIZE)
#define VN_A1_OFF	(HW_A2_VNET_BASE - HW_A2_CARVEOUT_BASE)	/* the block inside the a2 pool */

#define VN_DOWN		0
#define VN_UP		1

static_assert(VN_SHM_SIZE <= HW_A2_VNET_SIZE);
static_assert(VN_SLOT_SIZE % 64 == 0);

struct vn_line {		/* written by one side only */
	u32 state;
	u32 head;		/* tx ring producer counter */
	u32 tail;		/* rx ring consumer counter */
	u32 pad[13];
};

struct vn_shm {
	struct {
		u32 magic, version, slots, slot_size;
		u32 pad[12];
	} ctl;
	struct vn_line line[2];	/* [0] a1, [1] a2 */
};

static uint poll_us = 200;
module_param(poll_us, uint, 0644);
MODULE_PARM_DESC(poll_us, "doorbell poll period in us on this side (phase 1: no interrupt; 200 us = 1/5 ms added latency per hop)");

struct vn {
	struct net_device *ndev;
	struct napi_struct napi;
	struct hrtimer timer;
	struct mutex lock;		/* open/started/running transitions */
	bool a1;
	bool open;
	bool avail;			/* a1: pool mapped and subdev started; a2: always */
	bool running;
	bool ctl_ok;			/* ring header seen and valid */
	bool up;			/* peer state UP: carrier */
	struct vn_shm *shm;
	struct device *dmadev;		/* a2: DMA API cache maintenance, NULL on a1 */
	dma_addr_t dma;
	u32 tx_head, tx_tail_seen, rx_tail;
	/* a1 */
	struct rproc *rproc;
	void *(*pool_va)(size_t *size);
	struct rproc_subdev sd;
};

static inline unsigned int vn_me(struct vn *v)
{
	return v->a1 ? 0 : 1;
}

static inline struct vn_line *vn_own(struct vn *v)
{
	return &v->shm->line[vn_me(v)];
}

static inline struct vn_line *vn_peer(struct vn *v)
{
	return &v->shm->line[!vn_me(v)];
}

static inline void *vn_slot(struct vn *v, unsigned int ring, u32 cnt)
{
	return (void *)v->shm + VN_RING_OFF + (ring * VN_SLOTS + (cnt & (VN_SLOTS - 1))) * VN_SLOT_SIZE;
}

/* The writer is done with [p, p+len): a2 cleans + invalidates the lines, a1 drains its write buffer. */
static void vn_to_dev(struct vn *v, void *p, size_t len)
{
	if (v->dmadev)
		dma_sync_single_for_device(v->dmadev, v->dma + (p - (void *)v->shm), ALIGN(len, 64), DMA_BIDIRECTIONAL);
	else
		wmb();
}

/* About to read what the peer wrote: a2 invalidates first (the sync ends with a dsb), a1 orders its loads. */
static void vn_to_cpu(struct vn *v, void *p, size_t len)
{
	if (v->dmadev)
		dma_sync_single_for_cpu(v->dmadev, v->dma + (p - (void *)v->shm), ALIGN(len, 64), DMA_BIDIRECTIONAL);
	else
		dma_rmb();
}

static void vn_publish_own(struct vn *v)
{
	vn_to_dev(v, vn_own(v), sizeof(struct vn_line));
}

static void vn_set_state(struct vn *v, u32 st)
{
	WRITE_ONCE(vn_own(v)->state, st);
	vn_publish_own(v);
}

static bool vn_ctl_valid(struct vn *v)
{
	struct vn_shm *s = v->shm;

	vn_to_cpu(v, &s->ctl, sizeof(s->ctl));
	return READ_ONCE(s->ctl.magic) == VN_MAGIC && READ_ONCE(s->ctl.version) == VN_VERSION &&
	       READ_ONCE(s->ctl.slots) == VN_SLOTS && READ_ONCE(s->ctl.slot_size) == VN_SLOT_SIZE;
}

static void vn_carrier(struct vn *v, bool up)
{
	if (v->up == up)
		return;
	v->up = up;
	if (up) {
		netif_carrier_on(v->ndev);
		netif_wake_queue(v->ndev);
	} else {
		netif_carrier_off(v->ndev);
	}
	netdev_info(v->ndev, "peer %s\n", up ? "up" : "down");
}

static void vn_refresh_tx_tail(struct vn *v)
{
	vn_to_cpu(v, vn_peer(v), sizeof(struct vn_line));
	v->tx_tail_seen = READ_ONCE(vn_peer(v)->tail);
}

static netdev_tx_t vn_xmit(struct sk_buff *skb, struct net_device *ndev)
{
	struct vn *v = netdev_priv(ndev);
	unsigned int len = skb->len;
	u32 *slot;

	if (unlikely(!v->up || len > VN_FRAME_MAX)) {
		ndev->stats.tx_dropped++;
		dev_kfree_skb_any(skb);
		return NETDEV_TX_OK;
	}
	if (v->tx_head - v->tx_tail_seen >= VN_SLOTS) {
		vn_refresh_tx_tail(v);
		if (v->tx_head - v->tx_tail_seen >= VN_SLOTS) {
			netif_stop_queue(ndev);
			return NETDEV_TX_BUSY;
		}
	}
	slot = vn_slot(v, vn_me(v), v->tx_head);
	skb_copy_bits(skb, 0, (void *)slot + VN_SLOT_HDR, len);
	WRITE_ONCE(slot[0], len);
	vn_to_dev(v, slot, VN_SLOT_HDR + len);
	if (v->dmadev) {	/* flush by read-back: the data writes land before head can be seen (#79) */
		vn_to_cpu(v, slot, VN_SLOT_HDR);
		(void)READ_ONCE(slot[0]);
	}
	v->tx_head++;
	WRITE_ONCE(vn_own(v)->head, v->tx_head);
	vn_publish_own(v);
	ndev->stats.tx_packets++;
	ndev->stats.tx_bytes += len;
	dev_consume_skb_any(skb);
	if (v->tx_head - v->tx_tail_seen >= VN_SLOTS) {
		vn_refresh_tx_tail(v);
		if (v->tx_head - v->tx_tail_seen >= VN_SLOTS)
			netif_stop_queue(ndev);
	}
	return NETDEV_TX_OK;
}

static int vn_poll(struct napi_struct *napi, int budget)
{
	struct vn *v = container_of(napi, struct vn, napi);
	struct net_device *ndev = v->ndev;
	struct vn_line *peer = vn_peer(v);
	u32 st, rx_head;
	int done = 0;

	if (!v->ctl_ok) {	/* a2 before a1 has initialised the block (a1 vnet loaded late) */
		if (!vn_ctl_valid(v))
			goto out;
		v->ctl_ok = true;
		vn_to_cpu(v, vn_own(v), sizeof(struct vn_line));
		v->tx_head = READ_ONCE(vn_own(v)->head);
		v->rx_tail = READ_ONCE(vn_own(v)->tail);
		vn_set_state(v, VN_UP);
	}
	vn_to_cpu(v, peer, sizeof(*peer));
	st = READ_ONCE(peer->state);
	rx_head = READ_ONCE(peer->head);
	v->tx_tail_seen = READ_ONCE(peer->tail);
	vn_carrier(v, st == VN_UP);
	if (!v->up)
		goto out;
	if (rx_head - v->rx_tail > VN_SLOTS) {
		netdev_err(ndev, "rx ring: head %u tail %u, resync\n", rx_head, v->rx_tail);
		ndev->stats.rx_errors++;
		v->rx_tail = rx_head;
	}
	while (done < budget && rx_head != v->rx_tail) {
		u32 *slot = vn_slot(v, !vn_me(v), v->rx_tail);
		struct sk_buff *skb;
		u32 len;

		vn_to_cpu(v, slot, VN_SLOT_HDR);
		len = READ_ONCE(slot[0]);
		if (len < ETH_HLEN || len > VN_FRAME_MAX) {
			ndev->stats.rx_errors++;
		} else {
			vn_to_cpu(v, (void *)slot + VN_SLOT_HDR, len);
			skb = napi_alloc_skb(napi, len);
			if (skb) {
				skb_put_data(skb, (void *)slot + VN_SLOT_HDR, len);
				skb->protocol = eth_type_trans(skb, ndev);
				skb->ip_summed = CHECKSUM_NONE;
				ndev->stats.rx_packets++;
				ndev->stats.rx_bytes += len;
				napi_gro_receive(napi, skb);
			} else {
				ndev->stats.rx_dropped++;
			}
		}
		v->rx_tail++;
		done++;
	}
	if (done) {
		WRITE_ONCE(vn_own(v)->tail, v->rx_tail);
		vn_publish_own(v);
	}
	if (netif_queue_stopped(ndev) && v->tx_head - v->tx_tail_seen < VN_SLOTS)
		netif_wake_queue(ndev);
out:
	if (done < budget)
		napi_complete_done(napi, done);
	return done;
}

static enum hrtimer_restart vn_timer(struct hrtimer *t)
{
	struct vn *v = container_of(t, struct vn, timer);

	napi_schedule(&v->napi);
	hrtimer_forward_now(t, ns_to_ktime((u64)max(poll_us, 20U) * NSEC_PER_USEC));
	return HRTIMER_RESTART;
}

/* Caller holds v->lock. Runs while the netdev is open and the block is usable. */
static void vn_update(struct vn *v)
{
	bool want = v->open && v->avail;

	if (want && !v->running) {
		v->running = true;
		v->up = false;
		v->ctl_ok = v->a1 || vn_ctl_valid(v);
		if (v->ctl_ok) {
			vn_to_cpu(v, vn_own(v), sizeof(struct vn_line));
			v->tx_head = READ_ONCE(vn_own(v)->head);
			v->rx_tail = READ_ONCE(vn_own(v)->tail);
			vn_set_state(v, VN_UP);
		}
		napi_enable(&v->napi);
		hrtimer_start(&v->timer, ns_to_ktime((u64)max(poll_us, 20U) * NSEC_PER_USEC), HRTIMER_MODE_REL);
	} else if (!want && v->running) {
		v->running = false;
		vn_carrier(v, false);
		hrtimer_cancel(&v->timer);
		napi_disable(&v->napi);
		netif_tx_disable(v->ndev);
		if (v->shm && v->ctl_ok)
			vn_set_state(v, VN_DOWN);
	}
}

static int vn_open(struct net_device *ndev)
{
	struct vn *v = netdev_priv(ndev);

	mutex_lock(&v->lock);
	v->open = true;
	vn_update(v);
	mutex_unlock(&v->lock);
	return 0;
}

static int vn_stop(struct net_device *ndev)
{
	struct vn *v = netdev_priv(ndev);

	mutex_lock(&v->lock);
	v->open = false;
	vn_update(v);
	mutex_unlock(&v->lock);
	return 0;
}

static const struct net_device_ops vn_ops = {
	.ndo_open		= vn_open,
	.ndo_stop		= vn_stop,
	.ndo_start_xmit		= vn_xmit,
	.ndo_validate_addr	= eth_validate_addr,
	.ndo_set_mac_address	= eth_mac_addr,
};

static void vn_drvinfo(struct net_device *ndev, struct ethtool_drvinfo *info)
{
	struct vn *v = netdev_priv(ndev);

	strscpy(info->driver, "us48_vnet", sizeof(info->driver));
	strscpy(info->bus_info, v->a1 ? "a1:a2-pool" : "a2:a2-vnet", sizeof(info->bus_info));
}

static const struct ethtool_ops vn_ethtool = {
	.get_drvinfo	= vn_drvinfo,
	.get_link	= ethtool_op_get_link,
};

static struct vn *vn_alloc(struct device *parent, bool a1)
{
	/* a1 = .1, a2 = .2: locally administered, fixed (private link, no ARP clash) */
	static const u8 mac[2][ETH_ALEN] = { { 0x02, 0x48, 0, 0, 0, 1 }, { 0x02, 0x48, 0, 0, 0, 2 } };
	struct net_device *ndev = alloc_etherdev(sizeof(struct vn));
	struct vn *v;

	if (!ndev)
		return ERR_PTR(-ENOMEM);
	v = netdev_priv(ndev);
	v->ndev = ndev;
	v->a1 = a1;
	mutex_init(&v->lock);
	hrtimer_setup(&v->timer, vn_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	SET_NETDEV_DEV(ndev, parent);
	strscpy(ndev->name, "vnet%d", sizeof(ndev->name));
	eth_hw_addr_set(ndev, mac[!a1]);
	ndev->netdev_ops = &vn_ops;
	ndev->ethtool_ops = &vn_ethtool;
	ndev->features = 0;		/* no checksum, no TSO/SG offload: every frame is copied and checked by the stack */
	ndev->hw_features = 0;
	ndev->min_mtu = ETH_MIN_MTU;
	ndev->max_mtu = VN_MTU;
	ndev->mtu = VN_MTU;
	ndev->tx_queue_len = 64;
	netif_napi_add(ndev, &v->napi, vn_poll);
	netif_carrier_off(ndev);
	return v;
}

/* ---- a2 side: platform device ---- */

static int vn_a2_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct device_node *rm = of_parse_phandle(dev->of_node, "memory-region", 0);
	struct resource res;
	struct vn *v;
	int ret;

	if (!rm)
		return dev_err_probe(dev, -EINVAL, "no memory-region\n");
	ret = of_address_to_resource(rm, 0, &res);
	of_node_put(rm);
	if (ret)
		return ret;
	if (res.start != HW_A2_VNET_BASE || resource_size(&res) != HW_A2_VNET_SIZE || !pfn_valid(PHYS_PFN(res.start)))
		return dev_err_probe(dev, -EINVAL, "memory-region %pR is not the hwdb a2_vnet block %#x+%#x, or not mapped\n",
				     &res, HW_A2_VNET_BASE, HW_A2_VNET_SIZE);
	ret = dma_set_mask_and_coherent(dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;
	v = vn_alloc(dev, false);
	if (IS_ERR(v))
		return PTR_ERR(v);
	v->shm = phys_to_virt(res.start);
	v->dmadev = dev;
	v->dma = dma_map_single(dev, v->shm, VN_SHM_SIZE, DMA_BIDIRECTIONAL);
	if (dma_mapping_error(dev, v->dma)) {
		ret = -ENOMEM;
		goto free;
	}
	v->avail = true;
	platform_set_drvdata(pdev, v);
	ret = register_netdev(v->ndev);
	if (ret)
		goto unmap;
	dev_info(dev, "a2 side: block %pR, %u slots x %u B, poll %u us\n", &res, VN_SLOTS, VN_SLOT_SIZE, poll_us);
	return 0;
unmap:
	dma_unmap_single(dev, v->dma, VN_SHM_SIZE, DMA_BIDIRECTIONAL);
free:
	netif_napi_del(&v->napi);
	free_netdev(v->ndev);
	return ret;
}

static void vn_a2_remove(struct platform_device *pdev)
{
	struct vn *v = platform_get_drvdata(pdev);

	unregister_netdev(v->ndev);
	dma_unmap_single(&pdev->dev, v->dma, VN_SHM_SIZE, DMA_BIDIRECTIONAL);
	netif_napi_del(&v->napi);
	free_netdev(v->ndev);
}

static const struct of_device_id vn_of_match[] = {
	{ .compatible = "ubnt,us48-vnet" },
	{ }
};
MODULE_DEVICE_TABLE(of, vn_of_match);

static struct platform_driver vn_a2_driver = {
	.probe	= vn_a2_probe,
	.remove	= vn_a2_remove,
	.driver	= { .name = "us48-vnet", .of_match_table = vn_of_match },
};

/* ---- a1 side: remoteproc subdevice of us48_a2 ---- */

static struct vn *vn_a1;

/* Initialise the block. adopt: keep a block that is already valid (module loaded while a2 runs). */
static void vn_a1_init_shm(struct vn *v, bool adopt)
{
	struct vn_shm *s = v->shm;

	if (adopt && vn_ctl_valid(v))
		return;
	memset(s, 0, VN_SHM_SIZE);
	s->ctl.version = VN_VERSION;
	s->ctl.slots = VN_SLOTS;
	s->ctl.slot_size = VN_SLOT_SIZE;
	wmb();
	WRITE_ONCE(s->ctl.magic, VN_MAGIC);
	wmb();
}

static int vn_a1_map(struct vn *v, bool adopt)
{
	size_t sz;
	void *pool = v->pool_va(&sz);

	if (!pool || sz < VN_A1_OFF + VN_SHM_SIZE)
		return -ENODEV;
	v->shm = pool + VN_A1_OFF;
	vn_a1_init_shm(v, adopt);
	return 0;
}

static int vn_sd_prepare(struct rproc_subdev *sd)
{
	struct vn *v = container_of(sd, struct vn, sd);
	int ret;

	mutex_lock(&v->lock);
	ret = vn_a1_map(v, false);
	mutex_unlock(&v->lock);
	return ret;
}

static int vn_sd_start(struct rproc_subdev *sd)
{
	struct vn *v = container_of(sd, struct vn, sd);

	mutex_lock(&v->lock);
	v->avail = true;
	vn_update(v);
	mutex_unlock(&v->lock);
	return 0;
}

static void vn_sd_stop(struct rproc_subdev *sd, bool crashed)
{
	struct vn *v = container_of(sd, struct vn, sd);

	mutex_lock(&v->lock);
	v->avail = false;
	vn_update(v);
	mutex_unlock(&v->lock);
}

static void vn_sd_unprepare(struct rproc_subdev *sd)
{
	struct vn *v = container_of(sd, struct vn, sd);

	mutex_lock(&v->lock);
	v->avail = false;
	vn_update(v);
	v->shm = NULL;
	mutex_unlock(&v->lock);
}

static int vn_a1_probe(void)
{
	struct rproc *(*get)(void) = symbol_get(us48_a2_rproc);
	void *(*pool)(size_t *size) = symbol_get(us48_a2_pool_va);
	struct rproc *rp;
	struct vn *v;
	int ret;

	if (!get || !pool) {	/* symbol_get, not a link dependency: the a2 build of this module has no us48_a2 */
		pr_err("us48_vnet: us48_a2 is not loaded (insmod us48_pci, us48_a2 first)\n");
		ret = -ENODEV;
		goto put;
	}
	rp = get();
	if (!rp) {
		pr_err("us48_vnet: us48_a2 has no remoteproc\n");
		ret = -ENODEV;
		goto put;
	}
	v = vn_alloc(NULL, true);
	if (IS_ERR(v)) {
		ret = PTR_ERR(v);
		goto put;
	}
	v->rproc = rp;
	v->pool_va = pool;
	v->sd.prepare = vn_sd_prepare;
	v->sd.start = vn_sd_start;
	v->sd.stop = vn_sd_stop;
	v->sd.unprepare = vn_sd_unprepare;
	ret = register_netdev(v->ndev);
	if (ret) {
		netif_napi_del(&v->napi);
		free_netdev(v->ndev);
		goto put;
	}
	mutex_lock(&rp->lock);
	rproc_add_subdev(rp, &v->sd);
	if (rp->state == RPROC_RUNNING && !vn_a1_map(v, true)) {	/* a2 is up already: join the live block */
		mutex_lock(&v->lock);
		v->avail = true;
		vn_update(v);
		mutex_unlock(&v->lock);
	}
	mutex_unlock(&rp->lock);
	vn_a1 = v;
	pr_info("us48_vnet: a1 side, block at pool +%#x, %u slots x %u B, poll %u us\n", VN_A1_OFF, VN_SLOTS, VN_SLOT_SIZE, poll_us);
	return 0;
put:
	if (get)
		symbol_put(us48_a2_rproc);
	if (pool)
		symbol_put(us48_a2_pool_va);
	return ret;
}

static void vn_a1_exit(void)
{
	struct vn *v = vn_a1;

	unregister_netdev(v->ndev);	/* ndo_stop: timer + NAPI stopped */
	mutex_lock(&v->rproc->lock);
	mutex_lock(&v->lock);
	v->avail = false;
	vn_update(v);
	v->shm = NULL;
	mutex_unlock(&v->lock);
	rproc_remove_subdev(v->rproc, &v->sd);
	mutex_unlock(&v->rproc->lock);
	netif_napi_del(&v->napi);
	free_netdev(v->ndev);
	symbol_put(us48_a2_rproc);
	symbol_put(us48_a2_pool_va);
}

static bool vn_is_a2;

static int __init vn_init(void)
{
	vn_is_a2 = of_machine_is_compatible("ubnt,us48-a2");
	return vn_is_a2 ? platform_driver_register(&vn_a2_driver) : vn_a1_probe();
}

static void __exit vn_exit(void)
{
	if (vn_is_a2)
		platform_driver_unregister(&vn_a2_driver);
	else
		vn_a1_exit();
}

module_init(vn_init);
module_exit(vn_exit);
MODULE_DESCRIPTION("a1 <-> a2 virtual Ethernet over the a2 pool (UniFi US-48-500W)");
MODULE_LICENSE("GPL");
