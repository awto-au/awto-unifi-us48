// SPDX-License-Identifier: GPL-2.0
/*
 * us48_cmic: bring-up module for the BCM53346 (unit 1) CMIC on the UniFi US-48-500W.
 * Step 2: abort the RX/TX DMA stock left running (it points into RAM we now own), then
 * receive one frame on CMC0 ch1 into a coherent buffer. docs/driver-internals.md sections 1 and 5.
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
#define DMA_STAT_CLR		0x311a4
#define RX_CH			1
#define RX_BUF			2048
/* EPC_LINK_BMAP_64: IPIPE block 10, 64-bit; bit 0 = CPU port. */
#define BLK_IPIPE		10
#define EPC_LINK_BMAP_64	0x46002700
/* One SBUS op answers in a few us; 10 ms = 1000x backstop. Expiry = op abandoned, logged. */
#define SCHAN_TIMEOUT_US	10000
/* Abort completes after one in-flight descriptor (tens of us); 10 ms backstop, logged on expiry. */
#define ABORT_TIMEOUT_US	10000
/* House-LAN broadcasts reach port 50 every few s; poll 100 ms for 30 s. Expiry = no frame, logged. */
#define RX_POLL_MS		100
#define RX_POLLS		300

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

/* SBUS register op; words = data words (1 or 2). Header layout: driver-internals section 1. */
static int schan(struct us48_cmic *u, bool write, u32 blk, u32 addr, u32 *data, int words)
{
	u32 op = write ? 0x0d : 0x0b, ctrl;
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
		dev_err(&u->pdev->dev, "schan %s blk %u addr %08x: ctrl %08x after %u us\n",
			write ? "write" : "read", blk, addr, ctrl, SCHAN_TIMEOUT_US);
		wr(u, SCHAN_CTRL, 0);
		return ret ?: -EIO;
	}
	for (i = 0; !write && i < words; i++)
		data[i] = rd(u, SCHAN_MSG(1 + i));
	wr(u, SCHAN_CTRL, 0);
	return 0;
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

	if (!(w15 & BIT(31))) {
		if (++u->polls < RX_POLLS) {
			schedule_delayed_work(&u->rx_work, msecs_to_jiffies(RX_POLL_MS));
			return;
		}
		dev_warn(&u->pdev->dev, "no frame in %u ms; DMA_STAT %08x CTRL %08x\n",
			 RX_POLLS * RX_POLL_MS, rd(u, DMA_STAT), rd(u, DMA_CTRL(RX_CH)));
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
	memset(u->dcb, 0, 64);
	u->dcb[0] = cpu_to_le32(u->buf_dma);
	u->dcb[1] = cpu_to_le32(RX_BUF);
	wmb();
	wr(u, DMA_DESC(RX_CH), u->dcb_dma);
	wr(u, DMA_CTRL(RX_CH), rd(u, DMA_CTRL(RX_CH)) | DMA_EN);
	dev_info(&pdev->dev, "RX armed: DCB %pad buf %pad, ch1 CTRL %08x\n",
		 &u->dcb_dma, &u->buf_dma, rd(u, DMA_CTRL(RX_CH)));

	if (!schan(u, false, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2)) {
		dev_info(&pdev->dev, "EPC_LINK_BMAP_64 %08x_%08x\n", link[1], link[0]);
		if (!(link[0] & BIT(0))) {
			link[0] |= BIT(0);
			schan(u, true, BLK_IPIPE, EPC_LINK_BMAP_64, link, 2);
		}
	}

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
