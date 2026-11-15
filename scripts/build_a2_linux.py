#!/usr/bin/env python3
"""build_a2_linux — package Linux for a2 (unit 1 Cortex-A9) from the single mainline build (#79).

- One kernel for a1 and a2 (scripts/build_mainline.py, tmp/mainline/build). This script only packages:
  zImage copy (no appended DTB: a2 gets its DTB in r2), us48-a2.dtb from mainline/a2/us48-a2.dts, checks.
- DTS addresses = hwdb (scripts/hwdb.py), passed to cpp as A2_* macros; no second copy in the DTS.
- Initramfs is built into the zImage (same one as a1; /init picks the role from the DT compatible).
- Output: tmp/a2/zImage, tmp/a2/us48-a2.dtb. Log: tmp/logs/build_a2_linux.log.

Usage: build_a2_linux.py [--build]    --build runs scripts/build_mainline.py first
Build guard: no new line in the build log for STALL_S = kill (CLAUDE.md host-process rule); TOTAL_S backstop.
"""

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import hwdb  # noqa: E402

BUILD = ROOT / "tmp/mainline/build"
TREE = ROOT / "tmp/linux-mainline"
OUT = ROOT / "tmp/a2"
DTS = ROOT / "mainline/a2/us48-a2.dts"
LOG = ROOT / "tmp/logs/build_a2_linux.log"
BUILD_LOGS = [ROOT / "tmp/logs/build_mainline.log", ROOT / "tmp/logs/build_mainline-make.log"]
# silent phases (link, initramfs gzip) are ~10 s on this box; 60 s is the shared-box stall rule (awto-dan#53)
STALL_S = 60
TOTAL_S = 3600  # backstop only; not yet measured as 3x cold worst case (first full run logs its time)
# 3 s per tool call: dtc/cpp on a 100-line file take milliseconds
TOOL_TIMEOUT = 3


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def die(msg: str, rc: int = 2) -> None:
    log("abort: " + msg)
    sys.exit(rc)


def run_build() -> None:
    cmd = [sys.executable, str(ROOT / "scripts/build_mainline.py")]
    log("$ " + " ".join(cmd))
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    last, last_change = None, time.monotonic()
    while True:
        try:
            p.wait(timeout=2)  # poll period: well under STALL_S
            break
        except subprocess.TimeoutExpired:
            pass
        sig = tuple((f.stat().st_size if f.exists() else 0) for f in BUILD_LOGS)
        now = time.monotonic()
        if sig != last:
            last, last_change = sig, now
        if now - last_change > STALL_S or now - t0 > TOTAL_S:
            p.kill()
            tail = BUILD_LOGS[1].read_text().splitlines()[-1:] if BUILD_LOGS[1].exists() else []
            die(f"build_mainline stalled/overran: silent {now - last_change:.0f} s (limit {STALL_S}), "
                f"elapsed {now - t0:.0f} s (limit {TOTAL_S}); last make line: {tail}", 4)
    if p.returncode:
        die(f"build_mainline.py rc={p.returncode} (see tmp/logs/build_mainline.log)", p.returncode)
    log(f"build_mainline done in {time.monotonic() - t0:.0f} s")


def cfg() -> dict:
    out = {}
    for line in (BUILD / ".config").read_text().splitlines():
        m = re.match(r"(CONFIG_\w+)=(.*)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def check_config() -> None:
    c = cfg()
    need = ["CONFIG_AUTO_ZRELADDR", "CONFIG_ARM_PATCH_PHYS_VIRT", "CONFIG_HVC_DCC", "CONFIG_SERIAL_EARLYCON",
            "CONFIG_ARM_APPENDED_DTB", "CONFIG_CMDLINE_FROM_BOOTLOADER", "CONFIG_BLK_DEV_INITRD", "CONFIG_MODULES"]
    bad = [k for k in need if c.get(k) != "y"]
    if bad:
        die("kernel .config lacks " + ", ".join(bad))
    if any(c.get(k) == "y" for k in ("CONFIG_CMDLINE_FORCE", "CONFIG_CMDLINE_EXTEND", "CONFIG_SMP")):
        die("CMDLINE_FORCE/EXTEND would override the a2 DT bootargs; SMP must stay off")
    log("config ok: AUTO_ZRELADDR, ARM_PATCH_PHYS_VIRT, HVC_DCC, EARLYCON, CMDLINE_FROM_BOOTLOADER (DT bootargs win)")


def macros(db) -> dict:
    ram, shared = db.region("a2_carveout"), db.region("a2_shared")
    if shared.base + shared.size != ram.base + ram.size:
        die("hwdb a2_shared is not the last bytes of a2_carveout")

    def pair(name, base, size=None):
        d = {f"A2_{name}_BASE": f"0x{base:x}", f"A2_{name}_BASE_H": f"{base:x}"}
        if size is not None:
            d[f"A2_{name}_SIZE"] = f"0x{size:x}"
        return d

    m = {}
    m.update(pair("RAM", ram.base, ram.size - shared.size))  # Linux RAM = carveout minus the shared MiB
    m.update(pair("SHARED", shared.base, shared.size))
    for name, blk in (("GIC_DIST", "gic_dist"), ("GIC_CPU", "gic_cpu"), ("GTIMER", "gtimer"), ("TWD", "twd")):
        m.update(pair(name, db.addr(blk)))
    return m


def build_dtb(db) -> Path:
    dtc = BUILD / "scripts/dtc/dtc"
    if not dtc.exists():
        die(f"{dtc} missing: run build_mainline.py once")
    defs = [f"-D{k}={v}" for k, v in sorted(macros(db).items())]
    pre = OUT / "us48-a2.pre.dts"
    cmd = ["cpp", "-nostdinc", "-undef", "-D__DTS__", "-x", "assembler-with-cpp", "-I", str(TREE / "include"),
           *defs, str(DTS), "-o", str(pre)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TOOL_TIMEOUT)
    if r.returncode:
        die("cpp: " + r.stderr.strip())
    dtb = OUT / "us48-a2.dtb"
    r = subprocess.run([str(dtc), "-I", "dts", "-O", "dtb", "-@", "-Wno-unit_address_vs_reg", "-o", str(dtb), str(pre)],
                       capture_output=True, text=True, timeout=TOOL_TIMEOUT)
    log("dtc: " + (r.stderr.strip() or "clean"))
    if r.returncode:
        die(f"dtc rc={r.returncode}")
    return dtb


def check_layout(db, zimage: Path) -> None:
    """The kernel is decompressed at RAM base + 0x8000; nothing the loader placed may sit in its way."""
    image = BUILD / "arch/arm/boot/Image"
    c = cfg()
    end = db.region("a2_carveout").base + 0x8000 + image.stat().st_size + 0x100000  # + ~bss/margin (vmlinux bss 0.22 MB)
    sizes = {"zImage": zimage.stat().st_size}
    log(f"decompressed kernel + bss reaches a2 AXI ~0x{end:x}; zImage {sizes['zImage']} B")
    # a2_stub is not checked: the stub has jumped to the zImage before the first byte is decompressed over it
    lo = db.region("a2_carveout").base + 0x8000
    for name in ("a2_dtb", "a2_zimage"):
        r = db.region(name)
        if r.base < end and r.base + r.size > lo and r.base != db.region("a2_zimage").base:
            log(f"WARNING: hwdb {name} 0x{r.base:x}+0x{r.size:x} lies inside the decompressed kernel (0x{lo:x}..0x{end:x}): "
                "it is overwritten before the kernel reads it (docs/a2-linux.md risks)")
    zb = db.region("a2_zimage").base
    if end > zb:
        die(f"decompressed kernel reaches 0x{end:x}, past the zImage at 0x{zb:x}: the decompressor would relocate itself")
    # AUTO_ZRELADDR: RAM base = pc & 0xf8000000 while the zImage runs (arch/arm/boot/compressed/head.S)
    ram = db.region("a2_carveout").base
    if zb & 0xf8000000 != ram:
        die(f"AUTO_ZRELADDR would derive RAM base 0x{zb & 0xf8000000:x} from the zImage at 0x{zb:x}, hwdb RAM base is 0x{ram:x}")
    zr = db.region("a2_zimage")
    if sizes["zImage"] > zr.size:
        die(f"zImage {sizes['zImage']} B exceeds hwdb a2_zimage region {zr.size} B")
    if c.get("CONFIG_INITRAMFS_SOURCE"):
        log("initramfs: built into the zImage (shared with a1)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--build", action="store_true", help="run scripts/build_mainline.py first")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.build:
        run_build()
    z = BUILD / "arch/arm/boot/zImage"
    if not z.exists():
        die(f"{z} missing: run with --build")
    check_config()
    db = hwdb.load()
    dtb = build_dtb(db)
    shutil.copy2(z, OUT / "zImage")
    check_layout(db, z)
    log(f"output: tmp/a2/zImage {(OUT / 'zImage').stat().st_size} B, tmp/a2/us48-a2.dtb {dtb.stat().st_size} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
