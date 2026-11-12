// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic: bring-up module for the BCM53346 (unit 1) CMIC on the UniFi US-48-500W.
 * Step 2: abort the RX/TX DMA stock left running, add CPU + xe1 to VLAN/STG 1, receive one
 * frame on CMC0 ch1 into a coherent buffer. docs/driver-internals.md sections 1, 5, 14.
 */
#include <linux/delay.h>
#include <linux/dma-mapping.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/pci.h>
#include <linux/workqueue.h>

#define CMIC_BAR		2
#define SCHAN_CTRL		0x31000
#define SCHAN_MSG(n)		(0x3100c + 4 * (n))
#define  SCHAN_START		BIT(0)
#define  SCHAN_DONE		BIT(1)
#define  SCHAN_ERRS		(BIT(20) | BIT(21) | BIT(22))
#define DMA_CTRL(ch)		(0x31140 + 4 * (ch))
#define  DMA_DIR_TX		BIT(0)
#define  DMA_EN			BIT(1)
#define  DMA_ABORT		BIT(2)
#define  DMA_ENDIAN		(BIT(4) | BIT(5))
#define DMA_STAT		0x31150
#define  DMA_DESC_DONE(ch)	BIT(4 + (ch))
#define  DMA_ACTIVE(ch)		BIT(8 + (ch))
#define DMA_DESC(ch)		(0x31158 + 4 * (ch))
#define COS_CTRL_RX_0(ch)	(0x31168 + 8 * (ch))
#define COS_CTRL_RX_1(ch)	(0x3116c + 8 * (ch))
#define DMA_CURR_DESC(ch)	(0x311a8 + 4 * (ch))
#define DMA_STAT_CLR		0x311a4
#define RX_CH			1
#define RX_BUF			2048
/* EPC_LINK_BMAP_64: IPIPE block 10, 64-bit; bit 0 = CPU port. */
#define BLK_IPIPE		10
#define EPC_LINK_BMAP_64	0x46002700
#define BLK_EPIPE		11
#define VLAN_TAB(i)		(0x14000000 + (i))
#define STG_TAB(i)		(0x14040000 + (i))
#define EGR_VLAN(i)		(0x100c0000 + (i))
#define EGR_VLAN_STG(i)		(0x10100000 + (i))
#define XE1_PORT		27	/* front 50 */
/* One SBUS op answers in a few us; 10 ms = 1000x backstop. Expiry = op abandoned, logged. */
#define SCHAN_TIMEOUT_US	10000
/* Abort completes after one in-flight descriptor (tens of us); 10 ms backstop, logged on expiry. */
#define ABORT_TIMEOUT_US	10000
/* House-LAN broadcasts reach port 50 every few s; poll 100 ms for 30 s. Expiry = no frame, logged. */
#define RX_POLL_MS		100
#define RX_POLLS		300
/* Pre-fill pattern: any word still 0xA5A5A5A5 was never written by the chip (driver-internals 14.5 step 6). */
#define FILL			0xa5a5a5a5

struct us48_cmic {
	struct pci_dev *pdev;
	void __iomem *cmic;
	__le32 *dcb;			/* 16 words */
	dma_addr_t dcb_dma;
	void *buf;
	dma_addr_t buf_dma;
	struct delayed_work rx_work;
	int polls;
};

static u32 rd(struct us48_cmic *u, u32 off) { return ioread32(u->cmic + off); }
static void wr(struct us48_cmic *u, u32 off, u32 v) { iowrite32(v, u->cmic + off); }

/* SBUS op: reg read 0x0b / write 0x0d, mem read 0x07 / write 0x09. Header layout: section 1. */
static int sbus(struct us48_cmic *u, u32 op, u32 blk, u32 addr, u32 *data, int words)
{
	bool write = op == 0x0d || op == 0x09;
	u32 ctrl;
	int i, ret;

	wr(u, SCHAN_CTRL, 0);
	wr(u, SCHAN_MSG(0), op << 26 | blk << 20 | (4 * words) << 7);
	wr(u, SCHAN_MSG(1), addr);
	for (i = 0; write && i < words; i++)
		wr(u, SCHAN_MSG(2 + i), data[i]);
	wr(u, SCHAN_CTRL, SCHAN_START);
	ret = readl_poll_timeout_atomic(u->cmic + SCHAN_CTRL, ctrl, ctrl & (SCHAN_DONE | SCHAN_ERRS),
					1, SCHAN_TIMEOUT_US);
	if (ret || (ctrl & SCHAN_ERRS)) {
		dev_err(&u->pdev->dev, "sbus op %02x blk %u addr %08x: ctrl %08x after %u us\n",
			op, blk, addr, ctrl, SCHAN_TIMEOUT_US);
		wr(u, SCHAN_CTRL, 0);
		return ret ?: -EIO;
	}
	for (i = 0; !write && i < words; i++)
		data[i] = rd(u, SCHAN_MSG(1 + i));
	wr(u, SCHAN_CTRL, 0);
	return 0;
}

/* Set bits (bit numbers into the entry) and keep EVEN_PARITY at bit par consistent. */
static void smem_set_bits(struct us48_cmic *u, u32 blk, u32 addr, int words, int par,
			  const int *bits, int n)
{
	u32 e[4];
	int i, flips = 0;

	if (sbus(u, 0x07, blk, addr, e, words))
		return;
	for (i = 0; i < n; i++) {
		if (!(e[bits[i] / 32] & BIT(bits[i] % 32)))
			flips++;
		e[bits[i] / 32] |= BIT(bits[i] % 32);
	}
	if (flips & 1)
		e[par / 32] ^= BIT(par % 32);
	if (flips)
		sbus(u, 0x09, blk, addr, e, words);
	dev_info(&u->pdev->dev, "mem %u/%08x: %d bit(s) set, w0 %08x w1 %08x\n", blk, addr, flips, e[0], e[1]);
}

/* VLAN 1 + STG 1 lack the CPU and xe1 (27) after stock: add both (driver-internals 14.4). */
static void cpu_path_init(struct us48_cmic *u)
{
	static const int vlan[] = { 0, XE1_PORT };			/* PORT_BITMAP 0:30 */
	static const int egr[] = { 30, 30 + XE1_PORT };			/* PORT_BITMAP 30:30 */
	static const int stg[] = { 0, 1, 2 * XE1_PORT, 2 * XE1_PORT + 1 };	/* SP_TREE 3 = forward */

	smem_set_bits(u, BLK_IPIPE, VLAN_TAB(1), 4, 126, vlan, 2);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN(1), 3, 76, egr, 2);
	smem_set_bits(u, BLK_IPIPE, STG_TAB(1), 2, 60, stg, 4);
	smem_set_bits(u, BLK_EPIPE, EGR_VLAN_STG(1), 2, 60, stg, 4);
}

static void dma_abort(struct us48_cmic *u, int ch)
{
	u32 ctrl = rd(u, DMA_CTRL(ch)), stat;

	if (rd(u, DMA_STAT) & DMA_ACTIVE(ch)) {
		wr(u, DMA_CTRL(ch), ctrl | DMA_EN | DMA_ABORT);
		if (readl_poll_timeout_atomic(u->cmic + DMA_STAT, stat, !(stat & DMA_ACTIVE(ch)),
					      1, ABORT_TIMEOUT_US))
			dev_err(&u->pdev->dev, "ch%d abort: still active after %u us, DMA_STAT %08x\n",
				ch, ABORT_TIMEOUT_US, stat);
	}
	wr(u, DMA_CTRL(ch), rd(u, DMA_CTRL(ch)) & ~(DMA_EN | DMA_ABORT));
	wr(u, DMA_STAT_CLR, BIT(ch));
}

static void rx_stop(struct us48_cmic *u)
{
	dma_abort(u, RX_CH);
}

static void rx_work_fn(struct work_struct *w)
{
	struct us48_cmic *u = container_of(to_delayed_work(w), struct us48_cmic, rx_work);
	u32 w15 = le32_to_cpu(READ_ONCE(u->dcb[15]));

	if (w15 == FILL || !(w15 & BIT(31))) {
		if (++u->polls < RX_POLLS) {
			schedule_delayed_work(&u->rx_work, msecs_to_jiffies(RX_POLL_MS));
			return;
		}
		dev_warn(&u->pdev->dev, "no frame in %u ms; DMA_STAT %08x CTRL %08x CURR %08x\n",
			 RX_POLLS * RX_POLL_MS, rd(u, DMA_STAT), rd(u, DMA_CTRL(RX_CH)),
			 rd(u, DMA_CURR_DESC(RX_CH)));
		print_hex_dump(KERN_INFO, "us48_cmic dcb: ", DUMP_PREFIX_OFFSET, 16, 4, u->dcb, 64, false);
		print_hex_dump(KERN_INFO, "us48_cmic buf: ", DUMP_PREFIX_OFFSET, 16, 4, u->buf, 32, false);
		rx_stop(u);
		return;
	}
	dev_info(&u->pdev->dev, "RX frame: %u bytes (w7 len %u), src port %u, end %u start %u err %u, after %d ms\n",
		 w15 & 0xffff, (le32_to_cpu(u->dcb[7]) >> 16) & 0x3fff, le32_to_cpu(u->dcb[12]) & 0xff,
		 !!(w15 & BIT(16)), !!(w15 & BIT(17)), !!(w15 & BIT(18)), u->polls * RX_POLL_MS);
	print_hex_dump(KERN_INFO, "us48_cmic rx: ", DUMP_PREFIX_OFFSET, 16, 1, u->buf,
		       min_t(u32, w15 & 0xffff, 64), false);
	rx_stop(u);
}

static int us48_cmic_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	struct us48_cmic *u;
	u32 link[2];
	int ch, ret;

	if (PCI_FUNC(pdev->devfn) != 0)
		return -ENODEV;
	u = devm_kzalloc(&pdev->dev, sizeof(*u), GFP_KERNEL);
	if (!u)
		return -ENOMEM;
	u->pdev = pdev;
	ret = pcim_enable_device(pdev);
	if (ret)
		return ret;
	pci_set_master(pdev);
	ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;
	u->cmic = pcim_iomap(pdev, CMIC_BAR, 0);
	if (!u->cmic)
		return -ENOMEM;
	pci_set_drvdata(pdev, u);

	for (ch = 0; ch < 2; ch++)
		dev_info(&pdev->dev, "stock left ch%d CTRL %08x DESC %08x, DMA_STAT %08x\n",
			 ch, rd(u, DMA_CTRL(ch)), rd(u, DMA_DESC(ch)), rd(u, DMA_STAT));
	/* Stock's DCBs live in RAM the kernel has reused: stop both channels before anything else. */
	for (ch = 0; ch < 2; ch++)
		dma_abort(u, ch);
	dev_info(&pdev->dev, "after abort: ch0 %08x ch1 %08x DMA_STAT %08x\n",
		 rd(u, DMA_CTRL(0)), rd(u, DMA_CTRL(1)), rd(u, DMA_STAT));

	/* Init (section 5): keep stock's endian bits, ch0 TX / ch1 RX, all CPU COS to ch1. */
	wr(u, DMA_CTRL(0), (rd(u, DMA_CTRL(0)) & DMA_ENDIAN) | DMA_DIR_TX);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) & DMA_ENDIAN);
	wr(u, COS_CTRL_RX_0(RX_CH), 0xffffffff);
	wr(u, COS_CTRL_RX_1(RX_CH), 0xffff);

	u->dcb = dmam_alloc_coherent(&pdev->dev, 64, &u->dcb_dma, GFP_KERNEL);
	u->buf = dmam_alloc_coherent(&pdev->dev, RX_BUF, &u->buf_dma, GFP_KERNEL);
	if (!u->dcb || !u->buf)
		return -ENOMEM;
	memset(u->dcb, 0xa5, 64);
	memset(u->buf, 0xa5, 64);
	u->dcb[0] = cpu_to_le32(u->buf_dma);
	u->dcb[1] = cpu_to_le32(RX_BUF);
	wmb();
	wr(u, DMA_DESC(RX_CH), u->dcb_dma);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) | DMA_EN);
	dev_info(&pdev->dev, "RX armed: DCB %pad buf %pad, ch1 CTRL %08x DMA_STAT %08x\n",
		 &u->dcb_dma, &u->buf_dma, rd(u, DMA_CTRL(RX_CH)), rd(u, DMA_STAT));

	if (!sbus(u, 0x0b, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2)) {
		dev_info(&pdev->dev, "EPC_LINK_BMAP_64 %08x_%08x\n", link[1], link[0]);
		if (!(link[0] & BIT(0))) {
			link[0] |= BIT(0);
			sbus(u, 0x0d, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2);
		}
	}

	cpu_path_init(u);
	INIT_DELAYED_WORK(&u->rx_work, rx_work_fn);
	schedule_delayed_work(&u->rx_work, msecs_to_jiffies(RX_POLL_MS));
	return 0;
}

static void us48_cmic_remove(struct pci_dev *pdev)
{
	struct us48_cmic *u = pci_get_drvdata(pdev);

	cancel_delayed_work_sync(&u->rx_work);
	rx_stop(u);
}

static const struct pci_device_id us48_cmic_ids[] = {
	/* Function 1 (01:00.1) shares the ID; probe rejects it, only function 0 carries the CMIC. */
	{ PCI_DEVICE(PCI_VENDOR_ID_BROADCOM, 0x8346) },
	{ }
};
MODULE_DEVICE_TABLE(pci, us48_cmic_ids);

static struct pci_driver us48_cmic_driver = {
	.name		= "us48_cmic",
	.id_table	= us48_cmic_ids,
	.probe		= us48_cmic_probe,
	.remove		= us48_cmic_remove,
};
module_pci_driver(us48_cmic_driver);

MODULE_DESCRIPTION("UniFi US-48-500W BCM53346 CMIC bring-up (step 2: abort stock DMA, RX one frame)");
MODULE_LICENSE("GPL");
