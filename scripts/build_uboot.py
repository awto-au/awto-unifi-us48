#!/usr/bin/env python3
"""build_uboot — second-stage mainline U-Boot for the UniFi US-48-500W (BCM53344), issue #46.

- Tree: tmp/u-boot-v2026.07 = detached worktree of the machine U-Boot store (#45) at TAG, reached via the
  gitignored symlink tmp/u-boot-store (or $US48_UBOOT_STORE / --store). Never a clone, never gc in the store.
- Scaffold: uboot-port/ (tracked) is copied into the tree every build; uboot-port/patches/*.patch are
  applied with git apply after the tree is reset to pristine, then PATCH_MARKERS are verified.
- Build: out of tree in tmp/uboot-build, OpenWrt cross toolchain, STAGING_DIR = empty dir.
- Output: tmp/uboot/us48-uboot-chain.bin = legacy uImage (ARM/Linux/kernel, load = entry = TEXT_BASE)
  wrapping u-boot-dtb.bin, for stock `loady 0x01000000; bootm 0x01000000`.
- Logs: tmp/logs/build_uboot.log.

Usage: build_uboot.py [--clean] [--save-patches] [--jobs N]
  --save-patches   write the tree's tracked-file diff to uboot-port/patches/0001-*.patch, then exit
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
    "include/configs/us48.h": "include/configs/us48.h",
    "configs/us48_defconfig": "configs/us48_defconfig",
}
# tree path -> (marker, patch that supplies it)
PATCH_MARKERS = {
    "arch/arm/Kconfig": ("config TARGET_US48", "0001-arm-add-TARGET_US48.patch"),
}
SAVED_PATCH = PATCHES / "0001-arm-add-TARGET_US48.patch"
# Make a file in the tree dirty that is not meant to be: only these tracked files may differ from TAG
EXPECTED_DIRTY = {"arch/arm/Kconfig"}


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
    missing = [f"  {rel}: no {m!r}, supplied by patches/{p}" for rel, (m, p) in PATCH_MARKERS.items()
               if m not in (TREE / rel).read_text()]
    if missing:
        die("tree is not at the expected patch state:\n" + "\n".join(missing))
    dirty = {x[3:] for x in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()}
    if dirty - EXPECTED_DIRTY:
        die(f"unexpected modified tracked files in the tree: {sorted(dirty - EXPECTED_DIRTY)}")
    log(f"verified {len(PATCH_MARKERS)} patch marker(s)")


def save_patches() -> None:
    diff = git("diff", "--no-ext-diff", "--", *sorted(EXPECTED_DIRTY)).stdout
    if not diff:
        die("no tracked changes in the tree to save")
    PATCHES.mkdir(parents=True, exist_ok=True)
    head = ("Subject: arm: add TARGET_US48 and source the board Kconfig\n\n"
            f"Base: U-Boot {TAG}. The board files are staged from uboot-port/ by scripts/build_uboot.py.\n\n")
    SAVED_PATCH.write_text(head + diff)
    log(f"wrote {SAVED_PATCH.relative_to(ROOT)}")


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
    if run([str(TREE / "scripts/config"), "--file", str(BUILD / ".config"), "--set-str", "IDENT_STRING", ident_str]):
        return 1
    if run(NICE + ["make", o, "olddefconfig"]):
        return 1
    cfg = (BUILD / ".config").read_text().splitlines()
    for want in ("CONFIG_TARGET_US48=y", "CONFIG_SKIP_LOWLEVEL_INIT=y", "CONFIG_WDT_SP805=y", "CONFIG_WATCHDOG=y",
                 "CONFIG_ARM_GLOBAL_TIMER=y", "CONFIG_SYSRESET_SYSCON=y", "CONFIG_CMD_LOADB=y", f"CONFIG_TEXT_BASE=0x{LOADADDR:08x}"):
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
