#!/usr/bin/env python3
"""build_uboot — second-stage mainline U-Boot for the UniFi US-48-500W (BCM53344), issue #46.

- Tree: tmp/u-boot-v2026.07 = detached worktree of the machine U-Boot store (#45) at TAG, reached via the
  gitignored symlink tmp/u-boot-store (or $US48_UBOOT_STORE / --store). Never a clone, never gc in the store.
- Shared core: common/bcm-cmic/ staged read-only into lib/bcm-cmic by scripts/sync_bcm_cmic.py every build.
- Scaffold: uboot-port/ (tracked) is copied into the tree every build; uboot-port/patches/*.patch are
  applied with git apply after the tree is reset to pristine, then PATCH_MARKERS are verified.
- Build: out of tree in tmp/uboot-build, OpenWrt cross toolchain, STAGING_DIR = empty dir.
- Output: tmp/uboot/us48-uboot-chain.bin = legacy uImage (ARM/Linux/kernel, load = entry = TEXT_BASE)
  wrapping u-boot-dtb.bin, for stock `loady 0x01000000; bootm 0x01000000`.
- Logs: tmp/logs/build_uboot.log.

Usage: build_uboot.py [--clean] [--save-patches] [--jobs N]
  --save-patches   write the tree's tracked-file diffs to uboot-port/patches/000N-*.patch (PATCH_SETS), then exit
  --clean          remove the build dir first
"""

import argparse
import collections
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("US48_REPO")
    if env:
        return Path(env)
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parent)
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip())
    return Path(__file__).resolve().parent.parent


ROOT = repo_root()
TAG = "v2026.07"
TREE = ROOT / f"tmp/u-boot-{TAG}"
STORE_LINK = ROOT / "tmp/u-boot-store"  # symlink -> machine U-Boot store; no host path in this repo
SCAFFOLD = ROOT / "uboot-port"
PATCHES = SCAFFOLD / "patches"
BUILD = ROOT / "tmp/uboot-build"
OUTDIR = ROOT / "tmp/uboot"
OUT = OUTDIR / "us48-uboot-chain.bin"
LOG = ROOT / "tmp/logs/build_uboot.log"
OWRT = ROOT / "tmp/openwrt-build"
CROSS = OWRT / "staging_dir/toolchain-arm_cortex-a9_gcc-14.4.0_musl_eabi/bin/arm-openwrt-linux-muslgnueabi-"
STAGING = OUTDIR / "staging"
MKUIMAGE = ROOT / "scripts/mkuimage.py"
LOADADDR = 0x04000000  # = CONFIG_TEXT_BASE in us48_defconfig, checked after the build
DEFCONFIG = "us48_defconfig"
MAX_IMAGE = 1 << 20  # ~1 MiB at 9.9 KiB/s loady = ~100 s; room below the 0x04000000..0x07030000 free area
# worktree add: ~9k files, seconds; store git calls are local
GIT_TIMEOUT = 120
# stall guard (awto-dan#53): no output for STALL_S = hung; backstop = 3x a ~2 min cold build
STALL_S, BUILD_CAP_S = 60, 600
NICE = ["nice", "-n", "19", "ionice", "-c3"]

# scaffold path -> tree path (new files only; hooks into upstream files are in patches/)
FILES = {
    "arch/arm/dts/us48-uboot.dts": "arch/arm/dts/us48-uboot.dts",
    "board/ubiquiti/us48/Kconfig": "board/ubiquiti/us48/Kconfig",
    "board/ubiquiti/us48/Makefile": "board/ubiquiti/us48/Makefile",
    "board/ubiquiti/us48/MAINTAINERS": "board/ubiquiti/us48/MAINTAINERS",
    "board/ubiquiti/us48/board.c": "board/ubiquiti/us48/board.c",
    "board/ubiquiti/us48/sram_clk.c": "board/ubiquiti/us48/sram_clk.c",
    "board/ubiquiti/us48/us48_sram_clk.h": "board/ubiquiti/us48/us48_sram_clk.h",
    "include/configs/us48.h": "include/configs/us48.h",
    "cmd/bcm_cmic.c": "cmd/bcm_cmic.c",
    "drivers/net/us48_cmic_eth.c": "drivers/net/us48_cmic_eth.c",
    "configs/us48_defconfig": "configs/us48_defconfig",
}
# tree path -> (marker, patch that supplies it)
PATCH_MARKERS = {
    "arch/arm/Kconfig": ("config TARGET_US48", "0001-arm-add-TARGET_US48.patch"),
    "lib/Makefile": ("bcm-cmic/", "0002-lib-wire-bcm-cmic.patch"),
    "lib/Kconfig": ("lib/bcm-cmic/Kconfig", "0002-lib-wire-bcm-cmic.patch"),
    "cmd/Makefile": ("CONFIG_CMD_BCM_CMIC", "0003-cmd-wire-bcm-cmic.patch"),
    "cmd/sf.c": ("disabled in this build (read-only)", "0004-cmd-sf-read-only.patch"),
    "drivers/net/Makefile": ("CONFIG_US48_CMIC_ETH", "0005-net-build-us48-cmic-eth.patch"),
    "drivers/spi/iproc_qspi.c": ("a plain READ (0x03) has no dummy phase", "0006-spi-nor-3byte-window.patch"),
    "drivers/mtd/spi/spi-nor-core.c": ("SNOR_F_4B_WINDOW", "0006-spi-nor-3byte-window.patch"),
    "include/linux/mtd/spi-nor.h": ("SNOR_F_4B_WINDOW", "0006-spi-nor-3byte-window.patch"),
}
# patch file -> (subject, tracked files it covers); --save-patches writes one patch per entry
PATCH_SETS = {
    "0001-arm-add-TARGET_US48.patch": ("arm: add TARGET_US48 and source the board Kconfig", ["arch/arm/Kconfig"]),
    "0002-lib-wire-bcm-cmic.patch": ("lib: hook lib/bcm-cmic (staged by sync_bcm_cmic.py)", ["lib/Kconfig", "lib/Makefile"]),
    "0003-cmd-wire-bcm-cmic.patch": ("cmd: build bcm_cmic.c", ["cmd/Makefile"]),
    "0004-cmd-sf-read-only.patch": ("cmd: sf accepts probe and read only (us48 never writes flash)", ["cmd/sf.c"]),
    "0005-net-build-us48-cmic-eth.patch": ("net: build us48_cmic_eth.c", ["drivers/net/Makefile"]),
    "0006-spi-nor-3byte-window.patch": (
        "spi: iproc_qspi no divide by zero on a plain READ; spi-nor keeps EN4B-only 32 MiB parts in 3-byte mode, 4-byte mode only inside one read",
        ["drivers/spi/iproc_qspi.c", "drivers/mtd/spi/spi-nor-core.c", "include/linux/mtd/spi-nor.h"]),
}
# Make a file in the tree dirty that is not meant to be: only these tracked files may differ from TAG
EXPECTED_DIRTY = {f for _, fs in PATCH_SETS.values() for f in fs}
NET_OVERRIDES: list = []
SYNC = ROOT / "scripts/sync_bcm_cmic.py"
CORE_DIR = "lib/bcm-cmic"  # staged shared core, untracked in the tree


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def die(msg: str, rc: int = 1):
    log("ABORT: " + msg)
    sys.exit(rc)


def git(*args, cwd=None, timeout=GIT_TIMEOUT, check=True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", *args], cwd=cwd or TREE, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        die(f"git {args[0]} gave no result within {timeout} s")
    if check and r.returncode:
        die(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def ensure_tree(store: Path) -> None:
    if (TREE / ".git").exists():
        return
    if not store.exists():
        die(f"no U-Boot store: link tmp/u-boot-store to it, or set $US48_UBOOT_STORE / --store (#45)")
    if git("rev-parse", "-q", "--verify", f"refs/tags/{TAG}", cwd=store, check=False).returncode:
        die(f"tag {TAG} not in the store {store}")
    git("worktree", "prune", cwd=store)
    git("worktree", "add", "--detach", str(TREE), TAG, cwd=store, timeout=300)
    log(f"worktree {TREE.relative_to(ROOT)} @ {TAG}")


def reset_tree() -> None:
    """Pristine tree: tracked files back to TAG, staged new files removed. Nothing else edits this tree."""
    git("checkout", "-q", "-f", TAG, "--", ".")
    for dst in FILES.values():
        p = TREE / dst
        if p.exists():
            p.unlink()
    shutil.rmtree(TREE / "board/ubiquiti", ignore_errors=True)
    shutil.rmtree(TREE / CORE_DIR, ignore_errors=True)
    log(f"reset {TREE.relative_to(ROOT)} to {TAG}")


def stage() -> None:
    reset_tree()
    for p in sorted(PATCHES.glob("0*.patch")):
        git("apply", "--whitespace=nowarn", str(p))
        log(f"applied {p.name}")
    for src, dst in FILES.items():
        d = TREE / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SCAFFOLD / src, d)
    log(f"staged {len(FILES)} files from uboot-port/")
    r = subprocess.run([sys.executable, "-I", str(SYNC), "stage", "uboot", "--dest", str(TREE / CORE_DIR)],
                       capture_output=True, text=True, env=dict(os.environ, US48_REPO=str(ROOT)))
    if r.returncode:
        die(f"sync_bcm_cmic: {r.stdout.strip()} {r.stderr.strip()}")
    missing = [f"  {rel}: no {m!r}, supplied by patches/{p}" for rel, (m, p) in PATCH_MARKERS.items()
               if m not in (TREE / rel).read_text()]
    if missing:
        die("tree is not at the expected patch state:\n" + "\n".join(missing))
    dirty = {x[3:] for x in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()}
    if dirty - EXPECTED_DIRTY:
        die(f"unexpected modified tracked files in the tree: {sorted(dirty - EXPECTED_DIRTY)}")
    log(f"verified {len(PATCH_MARKERS)} patch marker(s)")


def save_patches() -> None:
    PATCHES.mkdir(parents=True, exist_ok=True)
    for name, (subject, files) in PATCH_SETS.items():
        diff = git("diff", "--no-ext-diff", "--", *files).stdout
        if not diff:
            die(f"no tracked changes in the tree for {name}")
        head = (f"Subject: {subject}\n\nBase: U-Boot {TAG}. Other files are staged from uboot-port/ by "
                "scripts/build_uboot.py.\n\n")
        (PATCHES / name).write_text(head + diff)
        log(f"wrote patches/{name}")


def make_env() -> dict:
    (STAGING / "usr/include").mkdir(parents=True, exist_ok=True)
    return dict(os.environ, ARCH="arm", CROSS_COMPILE=str(CROSS), STAGING_DIR=str(STAGING))


def run(cmd, cwd=TREE) -> int:
    """Run cmd, tee output to the console and LOG. Stall (no output) and overall caps kill it."""
    log("$ " + " ".join(map(str, cmd)))
    t0 = time.monotonic()
    tail = collections.deque(maxlen=40)
    p = subprocess.Popen(cmd, cwd=cwd, env=make_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace")
    last = [time.monotonic()]
    why = []

    def watch() -> None:
        while p.poll() is None:
            time.sleep(1)
            now = time.monotonic()
            if now - last[0] > STALL_S:
                why.append(f"stall: no output for {STALL_S} s, last line {tail[-1].strip()!r}, "
                           f"loadavg {os.getloadavg()[0]:.1f}")
            elif now - t0 > BUILD_CAP_S:
                why.append(f"overall cap {BUILD_CAP_S} s exceeded")
            if why:
                p.kill()
                return

    threading.Thread(target=watch, daemon=True).start()
    with LOG.open("a") as f:
        for line in p.stdout:
            last[0] = time.monotonic()
            f.write(line)
            tail.append(line)
    rc = p.wait()
    if why:
        log(f"KILLED {' '.join(map(str, cmd))}: {why[0]}")
        return 124
    log(f"exit {rc} ({time.monotonic() - t0:.0f} s)")
    if rc:
        log("last lines:")
        for line in tail:
            log("  | " + line.rstrip())
    return rc


def ident() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT)
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "uboot-port"], capture_output=True, text=True,
                           cwd=ROOT).stdout.strip()
    return f" us48-{r.stdout.strip()}{'-mod' if dirty else ''}"


def build(jobs: int) -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    o = f"O={BUILD}"
    if run(NICE + ["make", o, DEFCONFIG]):
        return 1
    # ident in the banner = repo revision; changes every commit, so not a defconfig line
    ident_str = ident()
    # net defaults: override at build time with $US48_IPADDR / $US48_SERVERIP / $US48_GATEWAYIP
    for var, key in (("US48_IPADDR", "IPADDR"), ("US48_SERVERIP", "SERVERIP"), ("US48_GATEWAYIP", "GATEWAYIP")):
        if os.environ.get(var):
            log(f"{key} = {os.environ[var]} (from ${var})")
            NET_OVERRIDES.append((key, os.environ[var]))
    if run([str(TREE / "scripts/config"), "--file", str(BUILD / ".config"), "--set-str", "IDENT_STRING", ident_str]):
        return 1
    for key, val in NET_OVERRIDES:
        if run([str(TREE / "scripts/config"), "--file", str(BUILD / ".config"), "--set-str", key, val]):
            return 1
    if run(NICE + ["make", o, "olddefconfig"]):
        return 1
    cfg = (BUILD / ".config").read_text().splitlines()
    for want in ("CONFIG_TARGET_US48=y", "CONFIG_SKIP_LOWLEVEL_INIT=y", "CONFIG_WDT_SP805=y", "CONFIG_WATCHDOG=y",
                 "CONFIG_ARM_GLOBAL_TIMER=y", "CONFIG_SYSRESET_SYSCON=y", "CONFIG_CMD_LOADB=y", "CONFIG_CMD_SF=y", "CONFIG_IPROC_QSPI=y",
                 "CONFIG_SPI_FLASH_MACRONIX=y", "CONFIG_CMD_BCM_CMIC=y", "CONFIG_US48_CMIC_ETH=y", "CONFIG_CMD_TFTPBOOT=y", "CONFIG_CMD_PING=y", "# CONFIG_SPI_FLASH_UNLOCK_ALL is not set",
                 f"CONFIG_TEXT_BASE=0x{LOADADDR:08x}"):
        if want not in cfg:
            die(f"{want} missing from the final .config")
    if run(NICE + ["make", o, f"-j{jobs}"]):
        return 1
    return 0


def pack() -> None:
    binp = BUILD / "u-boot-dtb.bin"
    if not binp.exists():
        die(f"{binp} not built")
    OUTDIR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-I", str(MKUIMAGE), str(binp), str(OUT), "--load", hex(LOADADDR),
                        "--name", "us48-uboot"], capture_output=True, text=True)
    if r.returncode:
        die(f"mkuimage: {r.stderr.strip()}")
    log(r.stdout.strip())
    log(f"u-boot-dtb.bin {binp.stat().st_size} B, chain image {OUT.stat().st_size} B -> {OUT.relative_to(ROOT)}")
    if OUT.stat().st_size > MAX_IMAGE:
        die(f"chain image over the {MAX_IMAGE} B budget (loady time + stock bootm window)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--save-patches", action="store_true")
    ap.add_argument("--store", type=Path, default=Path(os.environ.get("US48_UBOOT_STORE", STORE_LINK)),
                    help="machine U-Boot store (default $US48_UBOOT_STORE, else tmp/u-boot-store)")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ensure_tree(a.store)
    if a.save_patches:
        save_patches()
        return 0
    if a.clean:
        shutil.rmtree(BUILD, ignore_errors=True)
    stage()
    if build(a.jobs):
        log("BUILD FAILED")
        return 1
    pack()
    return 0


if __name__ == "__main__":
    sys.exit(main())
