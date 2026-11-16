#!/usr/bin/env python3
"""hwdb_cold_image — RAM-boot image of the debug profile whose rc scripts leave unit 1 untouched (#86).

Same kernel/modules as tmp/mainline/rootfs-debug; /etc/rc.d keeps only: unit 0 cmic + us48_pci + ge1 + dropbear.
No us48_cmic_xe (unit 1 switch init), no thermal/dma/selftest modules: unit 1 stays as the power-on state until a2 reads it.
Output tmp/mainline/us48-mainline-cold.bin. Only the initramfs is rebuilt (CONFIG_INITRAMFS_SOURCE on the make command line).
Log: tmp/logs/hwdb_cold_image.log (make output tmp/logs/hwdb_cold_make.log).
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_mainline as bm  # noqa: E402
from _cli import help_guard  # noqa: E402

S04 = """#!/bin/sh
# cold-state image (#86): unit 0 cmic + us48_pci + ge1 only; us48_cmic_xe (unit 1 init) is loaded by hand after the cold read
IP=192.168.25.207/24
M=/lib/modules/$(uname -r)
exec 2>/dev/kmsg
echo "us48-net(cold): insmod" > /dev/kmsg
insmod $M/us48_cmic.ko unit0=1 || { echo "us48-net: insmod failed rc=$?" > /dev/kmsg; exit 1; }
insmod $M/us48_pci.ko || echo "us48-net: insmod us48_pci failed rc=$?" > /dev/kmsg
ip link set ge1 up
echo 1 > /proc/sys/net/ipv6/conf/ge1/disable_ipv6
ip addr add $IP dev ge1
echo "us48-net(cold): done" > /dev/kmsg
"""


def main() -> int:
    bm.set_profile("debug")
    bm.LOG = bm.ROOT / "tmp/logs/hwdb_cold_image.log"
    bm.MAKE_LOG = bm.ROOT / "tmp/logs/hwdb_cold_make.log"
    bm.LOG.parent.mkdir(parents=True, exist_ok=True)
    src, dst = bm.OUTDIR / "rootfs-debug", bm.OUTDIR / "rootfs-cold"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)
    rc = dst / "etc/rc.d"
    for n in ("S10us48-dma", "S14us48-thermal", "S90us48-selftest"):
        (rc / n).unlink()
    (rc / "S04us48-net").write_text(S04)
    lst = bm.write_initramfs_list(dst)
    lst2 = bm.OUTDIR / "initramfs-cold.list"
    lst2.write_text(lst.read_text())
    lst.unlink()
    # the debug list is rewritten for the debug build; restore it
    bm.write_initramfs_list(src)
    bm.kmake("zImage", bm.DTB, f"CONFIG_INITRAMFS_SOURCE={lst2}", jobs=os.cpu_count())
    boot = bm.BUILD / "arch/arm/boot"
    cat = (boot / "zImage").read_bytes() + (boot / "dts" / bm.DTB).read_bytes()
    ver = (bm.BUILD / "include/config/kernel.release").read_text().strip()
    out = bm.OUTDIR / "us48-mainline-cold.bin"
    out.write_bytes(bm.uimage(cat, f"US-48 cold {ver}"))
    bm.log(f"output {out} ({out.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
