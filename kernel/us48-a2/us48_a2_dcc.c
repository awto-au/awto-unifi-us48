// SPDX-License-Identifier: GPL-2.0
/*
 * a2 DCC console: a2's Linux writes characters to DBGDTRTX (hvc_dcc, earlycon=dcc). The host polls
 * DSCR.DTRTXfull, drains DTRTX into a tty (ttyA2) and a log ring, and feeds tty input into DTRRX.
 */
#include <linux/debugfs.h>
#include <linux/device.h>
#include <linux/hrtimer.h>
#include <linux/kfifo.h>
#include <linux/regmap.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/tty.h>
#include <linux/tty_flip.h>
#include <linux/workqueue.h>
#include "us48_hwdb.h"
#include "us48_a2_dcc.h"

#define DBG(r)		(HW_CS_DEBUG0_BASE + HW_CS_DEBUG0_##r)
/*
 * Poll period 5 ms: the a2 kernel's hvc_dcc_check() gives up after 100 ms and earlycon blocks per character
 * until drained, so <= 10 ms (this is 1/20 of the 100 ms). Expiry has no meaning here: a late poll only
 * stalls a2's console writes. A poll drains up to DCC_BURST words (a2 refills DTRTX within microseconds,
 * each word costs ~2 regmap reads on the PCIe path); the rest waits for the next period.
 */
#define DCC_POLL_NS	(5 * NSEC_PER_MSEC)
#define DCC_BURST	256
#define DCC_LOG_SZ	8192
#define DCC_IN_SZ	256

struct us48_a2_dcc {
	struct device *dev;
	struct regmap *map;
	struct tty_driver *drv;
	struct tty_port port;
	struct hrtimer timer;
	struct work_struct work;
	spinlock_t lock;	/* both fifos */
	bool running;
	u32 polls, words, dscr;	/* debugfs dcc_stats: polls run, DTRTX words drained, last DSCR seen */
	DECLARE_KFIFO(log, u8, DCC_LOG_SZ);
	DECLARE_KFIFO(in, u8, DCC_IN_SZ);
};

static void dcc_deliver(struct us48_a2_dcc *d, const u8 *buf, int n)
{
	struct tty_struct *tty;
	unsigned long fl;

	spin_lock_irqsave(&d->lock, fl);
	while (kfifo_avail(&d->log) < n)
		kfifo_skip(&d->log);
	kfifo_in(&d->log, buf, n);
	spin_unlock_irqrestore(&d->lock, fl);
	tty = tty_port_tty_get(&d->port);
	if (tty) {
		tty_insert_flip_string(&d->port, buf, n);
		tty_flip_buffer_push(&d->port);
		tty_kref_put(tty);
	}
}

static void dcc_work(struct work_struct *w)
{
	struct us48_a2_dcc *d = container_of(w, struct us48_a2_dcc, work);
	u8 buf[DCC_BURST], ch;
	unsigned int dscr, v;
	int i, n = 0, ret = 0;

	for (i = 0; i < DCC_BURST; i++) {
		ret = regmap_read(d->map, DBG(DSCR), &dscr);
		if (ret)
			break;
		WRITE_ONCE(d->dscr, dscr);
		if (dscr & HW_CS_DEBUG0_DSCR_DTRTXFULL_MASK) {
			ret = regmap_read(d->map, DBG(DTRTX), &v);
			if (ret)
				break;
			buf[n++] = v;
		} else if (!(dscr & HW_CS_DEBUG0_DSCR_DTRRXFULL_MASK) && kfifo_get(&d->in, &ch)) {
			ret = regmap_write(d->map, DBG(DTRRX), ch);
			if (ret)
				break;
		} else {
			break;
		}
	}
	WRITE_ONCE(d->polls, d->polls + 1);
	WRITE_ONCE(d->words, d->words + n);
	if (n)
		dcc_deliver(d, buf, n);
	if (ret) {
		dev_err(d->dev, "DCC poll stopped: %d (link down or a2 unreadable)\n", ret);
		WRITE_ONCE(d->running, false);
		return;
	}
	if (READ_ONCE(d->running))
		hrtimer_start(&d->timer, DCC_POLL_NS, HRTIMER_MODE_REL);
}

static enum hrtimer_restart dcc_timer(struct hrtimer *t)
{
	struct us48_a2_dcc *d = container_of(t, struct us48_a2_dcc, timer);

	queue_work(system_highpri_wq, &d->work);	/* regmap sleeps: not in the timer context */
	return HRTIMER_NORESTART;
}

void us48_a2_dcc_start(struct us48_a2_dcc *d)
{
	WRITE_ONCE(d->running, true);
	hrtimer_start(&d->timer, 0, HRTIMER_MODE_REL);
}

void us48_a2_dcc_stop(struct us48_a2_dcc *d)
{
	WRITE_ONCE(d->running, false);
	cancel_work_sync(&d->work);
	hrtimer_cancel(&d->timer);
	cancel_work_sync(&d->work);
}

/* --- tty --- */

static int dcc_tty_install(struct tty_driver *drv, struct tty_struct *tty)
{
	struct us48_a2_dcc *d = drv->driver_state;

	return tty_port_install(&d->port, drv, tty);
}

static ssize_t dcc_tty_write(struct tty_struct *tty, const u8 *buf, size_t count)
{
	struct us48_a2_dcc *d = tty->driver->driver_state;
	unsigned int n = kfifo_in_spinlocked(&d->in, buf, count, &d->lock);

	if (n && READ_ONCE(d->running))
		queue_work(system_highpri_wq, &d->work);
	return n;
}

static unsigned int dcc_tty_write_room(struct tty_struct *tty)
{
	struct us48_a2_dcc *d = tty->driver->driver_state;

	return kfifo_avail(&d->in);
}

static int dcc_tty_open(struct tty_struct *tty, struct file *filp)
{
	return tty_port_open(tty->port, tty, filp);
}

static void dcc_tty_close(struct tty_struct *tty, struct file *filp)
{
	tty_port_close(tty->port, tty, filp);
}

static void dcc_tty_hangup(struct tty_struct *tty)
{
	tty_port_hangup(tty->port);
}

static const struct tty_operations dcc_tty_ops = {
	.install	= dcc_tty_install,
	.open		= dcc_tty_open,
	.close		= dcc_tty_close,
	.hangup		= dcc_tty_hangup,
	.write		= dcc_tty_write,
	.write_room	= dcc_tty_write_room,
};

static const struct tty_port_operations dcc_port_ops;

static void dcc_destroy(void *data)
{
	struct us48_a2_dcc *d = data;

	us48_a2_dcc_stop(d);
	tty_unregister_device(d->drv, 0);
	tty_unregister_driver(d->drv);
	tty_port_destroy(&d->port);
	tty_driver_kref_put(d->drv);
}

struct us48_a2_dcc *us48_a2_dcc_create(struct device *dev, struct regmap *map)
{
	struct us48_a2_dcc *d = devm_kzalloc(dev, sizeof(*d), GFP_KERNEL);
	struct device *tdev;
	int ret;

	if (!d)
		return ERR_PTR(-ENOMEM);
	d->dev = dev;
	d->map = map;
	spin_lock_init(&d->lock);
	INIT_KFIFO(d->log);
	INIT_KFIFO(d->in);
	INIT_WORK(&d->work, dcc_work);
	hrtimer_setup(&d->timer, dcc_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL);
	tty_port_init(&d->port);
	d->port.ops = &dcc_port_ops;
	d->drv = tty_alloc_driver(1, TTY_DRIVER_REAL_RAW | TTY_DRIVER_DYNAMIC_DEV);
	if (IS_ERR(d->drv)) {
		tty_port_destroy(&d->port);
		return ERR_CAST(d->drv);
	}
	d->drv->driver_name = "us48_a2_dcc";
	d->drv->name = "ttyA2";
	d->drv->type = TTY_DRIVER_TYPE_SERIAL;
	d->drv->subtype = SERIAL_TYPE_NORMAL;
	d->drv->init_termios = tty_std_termios;
	d->drv->init_termios.c_cflag = B115200 | CS8 | CREAD | CLOCAL;
	d->drv->driver_state = d;
	tty_set_operations(d->drv, &dcc_tty_ops);
	ret = tty_register_driver(d->drv);
	if (ret)
		goto err_put;
	tdev = tty_port_register_device(&d->port, d->drv, 0, dev);
	if (IS_ERR(tdev)) {
		ret = PTR_ERR(tdev);
		tty_unregister_driver(d->drv);
		goto err_put;
	}
	ret = devm_add_action_or_reset(dev, dcc_destroy, d);
	return ret ? ERR_PTR(ret) : d;
err_put:
	tty_driver_kref_put(d->drv);
	tty_port_destroy(&d->port);
	return ERR_PTR(ret);
}

/* --- debugfs: last DCC_LOG_SZ bytes, readable at any time (boot output is gone from the tty by then) --- */

static int dcc_log_show(struct seq_file *m, void *unused)
{
	struct us48_a2_dcc *d = m->private;
	u8 *buf = kmalloc(DCC_LOG_SZ, GFP_KERNEL);
	unsigned long fl;
	unsigned int n;

	if (!buf)
		return -ENOMEM;
	spin_lock_irqsave(&d->lock, fl);
	n = kfifo_out_peek(&d->log, buf, DCC_LOG_SZ);
	spin_unlock_irqrestore(&d->lock, fl);
	seq_write(m, buf, n);
	kfree(buf);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(dcc_log);

static int dcc_stats_show(struct seq_file *m, void *unused)
{
	struct us48_a2_dcc *d = m->private;

	seq_printf(m, "running %d polls %u words %u last_dscr %#x\n", READ_ONCE(d->running), READ_ONCE(d->polls),
		   READ_ONCE(d->words), READ_ONCE(d->dscr));
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(dcc_stats);

void us48_a2_dcc_debugfs(struct us48_a2_dcc *d, struct dentry *dir)
{
	if (!dir)
		return;
	debugfs_create_file("dcc_log", 0444, dir, d, &dcc_log_fops);
	debugfs_create_file("dcc_stats", 0444, dir, d, &dcc_stats_fops);
}
