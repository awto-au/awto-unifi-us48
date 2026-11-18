#!/bin/sh
# Boot sequence: base mounts, syslogd, then /etc/rc.d/S* in order (plain scripts, argument "start").
mount -t proc proc /proc
mount -t sysfs sysfs /sys
grep -q debugfs /proc/filesystems && mount -t debugfs debugfs /sys/kernel/debug
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /run
hostname us48
# SP805 armed by awto-uboot (~46 s): the kernel driver feeds it (WATCHDOG_HANDLE_BOOT_ENABLED) until something opens /dev/watchdog
syslogd -O /tmp/messages -s 256	# no SysV IPC in the kernel, so no logread; before the rc.d scripts so their logger lines land
for s in /etc/rc.d/S*; do
	[ -x "$s" ] && "$s" start
done
