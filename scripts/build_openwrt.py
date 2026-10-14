#!/usr/bin/env python3
"""build_openwrt — OpenWrt initramfs for the UniFi US-48-500W (bcm53xx/hr2 subtarget).

- Tree: tmp/openwrt-build, OpenWrt main at OWRT_COMMIT. Reset (checkout + clean target/linux/bcm53xx)
  then `git apply` of openwrt/*.patch in name order, every run.
- .config: bcm53xx/hr2, this device, initramfs only; `make defconfig` fills the rest.
- Downloads + ccache: the machine-shared dirs (#45), via gitignored symlinks tmp/openwrt-dl and
  tmp/ccache-openwrt (or $US48_OPENWRT_DL / $US48_CCACHE_DIR, --dl-dir / --ccache-dir).
  Shared with other buildroots: never delete from them.
- Phases: disk check -> clone/pin -> overlay -> .config -> download -> make.
- Output: tmp/tftp/us48-initramfs.bin (legacy uImage for stock `tftpboot 0x01000000; bootm`).
- nice 19 / ionice idle. Log: tmp/logs/build_openwrt.log; make output: tmp/logs/build_openwrt-make.log.

Usage: build_openwrt.py [--jobs N] [--prepare-only] [--kernel-only] [--save-patch] [--dl-dir D] [--ccache-dir D]
  --save-patch: write the current tree diff back to openwrt/0001-*.patch (after editing the tree).
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("AWTO_UNIFI_US48_REPO")
    if env:
        return Path(env)
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parent)
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip())
    return Path(__file__).resolve().parent.parent


ROOT = repo_root()
TREE = ROOT / "tmp/openwrt-build"
PATCHES = ROOT / "openwrt"
TFTP = ROOT / "tmp/tftp"
LOG = ROOT / "tmp/logs/build_openwrt.log"
MAKE_LOG = ROOT / "tmp/logs/build_openwrt-make.log"
URL = "https://github.com/openwrt/openwrt.git"
OWRT_COMMIT = "0ad1348fb2a7f49c9c5941d4d8c98f88b6974e8d"  # main, 2026-10-13
TARGET_DIR = "target/linux/bcm53xx"
DEVICE = "ubnt_unifi-us-48-500w"
OUT = f"bin/targets/bcm53xx/hr2/openwrt-bcm53xx-hr2-{DEVICE}-initramfs.bin"  # KERNEL_INITRAMFS_SUFFIX .bin
DL_LINK = ROOT / "tmp/openwrt-dl"  # symlinks -> machine-shared dirs; no host path in this repo
CCACHE_LINK = ROOT / "tmp/ccache-openwrt"
MIN_FREE_GB = 30  # full toolchain + kernel build
NICE = ["nice", "-n", "19", "ionice", "-c3"]
CONFIG = f"""CONFIG_TARGET_bcm53xx=y
CONFIG_TARGET_bcm53xx_hr2=y
CONFIG_TARGET_bcm53xx_hr2_DEVICE_{DEVICE}=y
CONFIG_TARGET_ROOTFS_INITRAMFS=y
# CONFIG_TARGET_ROOTFS_SQUASHFS is not set
CONFIG_BUSYBOX_CUSTOM=y
CONFIG_BUSYBOX_CONFIG_I2CDETECT=y
CONFIG_BUSYBOX_CONFIG_I2CGET=y
CONFIG_BUSYBOX_CONFIG_I2CSET=y
CONFIG_BUSYBOX_CONFIG_I2CDUMP=y
CONFIG_BUSYBOX_CONFIG_DEVMEM=y
CONFIG_KERNEL_DEVMEM=y
"""  # busybox i2c/devmem: milestone-2 bring-up without the packages feed


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(cmd, cwd=TREE, out=None, check=True):
    log("$ " + " ".join(map(str, cmd)))
    if out is None:
        r = subprocess.run(cmd, cwd=cwd)
    else:
        with out.open("a") as f:
            r = subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
    if check and r.returncode:
        log(f"FAILED rc={r.returncode}: {' '.join(map(str, cmd))}" + (f" (see {out})" if out else ""))
        sys.exit(r.returncode)
    return r.returncode


def shared_config(dl: Path, cc: Path) -> str:
    """Shared download folder + ccache; both options are hidden unless DEVEL=y."""
    for d in (dl, cc):
        if not d.is_dir():
            log(f"abort: {d} missing: link it to the shared dir (docs/openwrt-port.md, #45)")
            sys.exit(2)
    return f'CONFIG_DEVEL=y\nCONFIG_DOWNLOAD_FOLDER="{dl}"\nCONFIG_CCACHE=y\nCONFIG_CCACHE_DIR="{cc}"\n'


def check_config(want: str) -> None:
    have = set((TREE / ".config").read_text().splitlines())
    missing = [x for x in want.splitlines() if x not in have]
    if missing:
        log(f"abort: lost after defconfig: {missing}")
        sys.exit(3)


def disk_ok() -> None:
    free = shutil.disk_usage(ROOT).free / 2**30
    if free < MIN_FREE_GB:
        log(f"abort: {free:.0f} GiB free < {MIN_FREE_GB} GiB")
        sys.exit(2)


def clone() -> None:
    if not (TREE / ".git").exists():
        run(["git", "clone", "--depth", "1", URL, str(TREE)], cwd=ROOT)
    have = subprocess.run(["git", "rev-parse", "HEAD"], cwd=TREE, capture_output=True, text=True).stdout.strip()
    if have != OWRT_COMMIT:
        run(["git", "fetch", "--depth", "1", "origin", OWRT_COMMIT])
        run(["git", "checkout", "-f", OWRT_COMMIT])


def overlay() -> None:
    run(["git", "reset", "-q"])
    run(["git", "checkout", "-f", "--", TARGET_DIR])
    run(["git", "clean", "-fdq", "--", TARGET_DIR])
    for p in sorted(PATCHES.glob("*.patch")):
        run(["git", "apply", str(p)])
    # openwrt/files/ → OpenWrt's image overlay (bench tools, e.g. us48-fand)
    files = TREE / "files"
    if files.exists():
        shutil.rmtree(files)
    shutil.copytree(PATCHES / "files", files)
    log("overlay: openwrt/files -> files/")


def save_patch() -> None:
    run(["git", "add", "-N", TARGET_DIR])
    p = sorted(PATCHES.glob("0001-*.patch"))[0]
    run(["git", "diff", f"--output={p}", "--", TARGET_DIR])
    log(f"saved {p.relative_to(ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--kernel-only", action="store_true")
    ap.add_argument("--save-patch", action="store_true")
    ap.add_argument("--dl-dir", type=Path, default=Path(os.environ.get("US48_OPENWRT_DL", DL_LINK)),
                    help="shared OpenWrt download folder (default $US48_OPENWRT_DL, else tmp/openwrt-dl)")
    ap.add_argument("--ccache-dir", type=Path, default=Path(os.environ.get("US48_CCACHE_DIR", CCACHE_LINK)),
                    help="shared ccache dir (default $US48_CCACHE_DIR, else tmp/ccache-openwrt)")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if a.save_patch:
        save_patch()
        return 0
    disk_ok()
    clone()
    overlay()
    shared = shared_config(a.dl_dir.absolute(), a.ccache_dir.absolute())
    (TREE / ".config").write_text(CONFIG + shared)
    run(["make", "defconfig"], out=MAKE_LOG)
    check_config(shared)
    if a.prepare_only:
        log("prepared")
        return 0
    disk_ok()
    run(NICE + ["make", f"-j{a.jobs}", "download"], out=MAKE_LOG)
    disk_ok()
    t0 = time.monotonic()
    if a.kernel_only:
        run(NICE + ["make", f"-j{a.jobs}", "target/linux/compile"], out=MAKE_LOG)
    else:
        run(NICE + ["make", f"-j{a.jobs}"], out=MAKE_LOG)
    log(f"make done in {time.monotonic() - t0:.0f} s")
    if not a.kernel_only:
        src = TREE / OUT
        TFTP.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, TFTP / "us48-initramfs.bin")
        log(f"output: tmp/tftp/us48-initramfs.bin ({src.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
