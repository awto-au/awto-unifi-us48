#!/usr/bin/env python3
"""build_mainline — mainline Linux image for the UniFi US-48-500W (BCM53344, Hurricane 2).

- Tree: tmp/linux-mainline = worktree of the machine Linux store (#45), reached via the gitignored
  symlink tmp/linux-store (or $US48_LINUX_STORE / --store). Created on first run; tag BASE fetched from
  the store's `origin` (mainline) if missing. Never --depth, never gc in the store.
- Branch BRANCH (awto/us48/mainline-BASE) = BASE + mainline/*.patch; local to the store, not pushed.
- Config: multi_v7_defconfig + mainline/us48.config; initramfs from the OpenWrt bcm53xx rootfs (+ /init, /dev/console).
- Output: tmp/mainline/us48-mainline.bin = legacy uImage, load/entry 0x00018000, zImage + appended DTB
  (same format as tmp/tftp/us48-initramfs.bin: stock U-Boot `loady 0x01000000; bootm`).
- Logs: tmp/logs/build_mainline.log; make output tmp/logs/build_mainline-make.log.

Usage: build_mainline.py [--keep-branch] [--save-patches] [--checkpatch] [--dtbs-check] [--prepare-only] [--jobs N]
  default          rebuild the branch from mainline/*.patch (git am), configure, build, pack
  --keep-branch    build the branch as it is (after editing the tree); skips git am
  --save-patches   git format-patch BASE..BRANCH -> mainline/ (replaces old patches), then exit
  --checkpatch     scripts/checkpatch.pl --strict on every commit of the branch, then exit
  --dtbs-check     make dtbs_check for the new board DTS (dtschema from tmp/dtschema-venv), then exit
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
TREE = ROOT / "tmp/linux-mainline"
OUTDIR = ROOT / "tmp/mainline"
BUILD = OUTDIR / "build"
PATCHES = ROOT / "mainline"
FRAGMENT = PATCHES / "us48.config"
LOG = ROOT / "tmp/logs/build_mainline.log"
MAKE_LOG = ROOT / "tmp/logs/build_mainline-make.log"
STORE_LINK = ROOT / "tmp/linux-store"  # symlink -> machine Linux store; no host path in this repo
BASE = "v7.3-rc7"
BRANCH = f"awto/us48/mainline-{BASE}"
# tag fetch: objects mostly in the store already, ~300 MB worst case at >=1 MB/s; checkout of ~96k files ~60 s
FETCH_TIMEOUT, WORKTREE_TIMEOUT = 600, 300
DTB = "broadcom/bcm53344-ubnt-unifi-us-48-500w.dtb"
OWRT = ROOT / "tmp/openwrt-build"
CROSS = OWRT / "staging_dir/toolchain-arm_cortex-a9_gcc-14.4.0_musl_eabi/bin/arm-openwrt-linux-muslgnueabi-"
MKIMAGE = OWRT / "staging_dir/host/bin/mkimage"
ROOTFS = OWRT / "build_dir/target-arm_cortex-a9_musl_eabi/root-bcm53xx"
VENV_BIN = ROOT / "tmp/dtschema-venv/bin"
LOADADDR = "0x00018000"  # stock U-Boot writes ATAGs at 0x8000 after copying the image (docs/uboot.md)
NICE = ["nice", "-n", "19", "ionice", "-c3"]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(cmd, cwd=TREE, out=None, env=None, check=True) -> int:
    log("$ " + " ".join(map(str, cmd)))
    if out is None:
        r = subprocess.run(cmd, cwd=cwd, env=env)
    else:
        with out.open("a") as f:
            r = subprocess.run(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
    if check and r.returncode:
        log(f"FAILED rc={r.returncode}" + (f" (see {out.relative_to(ROOT)})" if out else ""))
        sys.exit(r.returncode)
    return r.returncode


def git_out(*args) -> str:
    return subprocess.run(["git", *args], cwd=TREE, capture_output=True, text=True, check=True).stdout.strip()


def store_git(store: Path, *args, timeout: int) -> subprocess.CompletedProcess:
    log(f"$ git -C {store} " + " ".join(args))
    try:
        r = subprocess.run(["git", "-C", str(store), *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"abort: git {args[0]} in the store gave no result within {timeout} s")
        sys.exit(4)
    return r


def ensure_tree(store: Path) -> None:
    """tmp/linux-mainline as a worktree of the store at BRANCH (or BASE); no-op if it exists."""
    if (TREE / ".git").exists():
        return
    shallow = store_git(store, "rev-parse", "--is-shallow-repository", timeout=30).stdout.strip()
    if shallow != "false":
        log(f"abort: {store} is not a full Linux store (link tmp/linux-store to it, or --store; #45)")
        sys.exit(4)
    if store_git(store, "rev-parse", "-q", "--verify", f"refs/tags/{BASE}", timeout=30).returncode:
        r = store_git(store, "fetch", "--no-tags", "origin", f"refs/tags/{BASE}:refs/tags/{BASE}", timeout=FETCH_TIMEOUT)
        if r.returncode:
            log(f"abort: fetch {BASE}: {r.stderr.strip()}")
            sys.exit(4)
    has_branch = not store_git(store, "rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}", timeout=30).returncode
    store_git(store, "worktree", "prune", timeout=30)
    args = [str(TREE), BRANCH] if has_branch else ["--detach", str(TREE), BASE]
    r = store_git(store, "worktree", "add", *args, timeout=WORKTREE_TIMEOUT)
    if r.returncode:
        log(f"abort: worktree add: {r.stderr.strip()}")
        sys.exit(4)
    log(f"worktree {TREE.relative_to(ROOT)} @ {BRANCH if has_branch else BASE}")


def rebuild_branch() -> None:
    if git_out("status", "--porcelain", "--untracked-files=no"):
        log("abort: tree has uncommitted changes (commit, or use --keep-branch)")
        sys.exit(2)
    run(["git", "checkout", "-q", "-B", BRANCH, BASE])
    patches = sorted(PATCHES.glob("0*.patch"))
    run(["git", "am", "--quiet", *map(str, patches)])
    log(f"branch {BRANCH} = {BASE} + {len(patches)} patches")


def save_patches() -> None:
    for p in PATCHES.glob("0*.patch"):
        p.unlink()
    run(["git", "format-patch", "-q", "--no-signature", "-o", str(PATCHES), f"{BASE}..{BRANCH}"])
    log(f"wrote {len(list(PATCHES.glob('0*.patch')))} patches to mainline/")


def checkpatch() -> int:
    bad = 0
    for c in git_out("rev-list", "--reverse", f"{BASE}..{BRANCH}").split():
        subj = git_out("log", "-1", "--format=%s", c)
        mail = subprocess.run(["git", "show", c, "--format=email"], cwd=TREE, capture_output=True, text=True).stdout
        r = subprocess.run(["perl", "scripts/checkpatch.pl", "--strict", "--no-summary", "-"], cwd=TREE,
                           input=mail, capture_output=True, text=True)
        # unsigned on purpose (Signed-off-by: TODO): those two are expected until submission
        lines = [x for x in (r.stdout + r.stderr).splitlines()
                 if x.startswith(("WARNING", "ERROR", "CHECK")) and "Signed-off-by" not in x
                 and "Unrecognized email address: 'TODO'" not in x]
        log(f"checkpatch {c[:9]} {subj}: {len(lines)} finding(s) besides the expected unsigned ones")
        for x in lines:
            log("  " + x)
        bad += len(lines)
    return 1 if bad else 0


def make_env(venv: bool = False) -> dict:
    env = dict(os.environ, ARCH="arm", CROSS_COMPILE=str(CROSS), STAGING_DIR=str(OWRT / "staging_dir"))  # STAGING_DIR: silences wrapper warning
    if venv:
        env["PATH"] = f"{VENV_BIN}:{env['PATH']}"
    return env


def kmake(*args, out=MAKE_LOG, venv=False, jobs=None) -> None:
    run(NICE + ["make", f"-j{jobs or os.cpu_count()}", f"O={BUILD}", *args], out=out, env=make_env(venv))


def dtbs_check(jobs: int) -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    kmake("multi_v7_defconfig", jobs=jobs)
    out = ROOT / "tmp/logs/build_mainline-dtbs_check.log"
    out.write_text("")
    rc = run(NICE + ["make", f"-j{jobs}", f"O={BUILD}", "CHECK_DTBS=y", "W=1", DTB], out=out,
             env=make_env(venv=True), check=False)
    findings = [x for x in out.read_text().splitlines() if "bcm53344-ubnt" in x]
    log(f"dtbs_check rc={rc}: {len(findings)} line(s) mentioning the board DTS (see {out.relative_to(ROOT)})")
    return rc or (1 if findings else 0)


def stage_rootfs() -> Path:
    """OpenWrt rootfs + /init -> sbin/init + a /dev/console node; returns the INITRAMFS_SOURCE value."""
    stage = OUTDIR / "rootfs"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(ROOTFS, stage, symlinks=True)
    if not (stage / "init").exists():
        (stage / "init").symlink_to("sbin/init")
    lst = OUTDIR / "initramfs.list"
    lst.write_text("nod /dev/console 0600 0 0 c 5 1\n")
    return f"{stage} {lst}"


def configure(jobs: int) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    kmake("multi_v7_defconfig", jobs=jobs)
    frag = OUTDIR / "us48-full.config"
    src = stage_rootfs()
    frag.write_text(FRAGMENT.read_text() + f'CONFIG_BLK_DEV_INITRD=y\nCONFIG_INITRAMFS_SOURCE="{src}"\n'
                    "CONFIG_INITRAMFS_ROOT_UID=0\nCONFIG_INITRAMFS_ROOT_GID=0\n")
    run(["scripts/kconfig/merge_config.sh", "-m", "-O", str(BUILD), str(BUILD / ".config"), str(frag)],
        out=MAKE_LOG, env=make_env())
    kmake("olddefconfig", jobs=jobs)
    cfg = (BUILD / ".config").read_text()
    for want in ("CONFIG_ARCH_BCM_HR2=y", "CONFIG_PCIE_IPROC_PLATFORM=y", "CONFIG_ARM_APPENDED_DTB=y",
                 "CONFIG_I2C_MUX_PCA954x=y", "CONFIG_SENSORS_LM75=y", "CONFIG_POWER_RESET_SYSCON=y"):
        if want not in cfg.splitlines():
            log(f"abort: {want} missing from final .config")
            sys.exit(3)


def pack() -> Path:
    boot = BUILD / "arch/arm/boot"
    zimage, dtb = boot / "zImage", boot / "dts" / DTB
    OUTDIR.mkdir(parents=True, exist_ok=True)
    cat = OUTDIR / "zImage-dtb"
    cat.write_bytes(zimage.read_bytes() + dtb.read_bytes())
    out = OUTDIR / "us48-mainline.bin"
    ver = (BUILD / "include/config/kernel.release").read_text().strip()
    run([str(MKIMAGE), "-A", "arm", "-O", "linux", "-T", "kernel", "-C", "none", "-a", LOADADDR, "-e", LOADADDR,
         "-n", f"US-48 mainline {ver}", "-d", str(cat), str(out)], out=MAKE_LOG)
    log(f"output: {out.relative_to(ROOT)} ({out.stat().st_size} bytes), zImage {zimage.stat().st_size}, dtb {dtb.stat().st_size}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--keep-branch", action="store_true")
    ap.add_argument("--save-patches", action="store_true")
    ap.add_argument("--checkpatch", action="store_true")
    ap.add_argument("--dtbs-check", action="store_true")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--store", type=Path, default=Path(os.environ.get("US48_LINUX_STORE", STORE_LINK)),
                    help="machine Linux store (default $US48_LINUX_STORE, else tmp/linux-store)")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    ensure_tree(a.store)
    if a.save_patches:
        save_patches()
        return 0
    if a.checkpatch:
        return checkpatch()
    if not a.keep_branch:
        rebuild_branch()
    if a.dtbs_check:
        return dtbs_check(a.jobs)
    configure(a.jobs)
    if a.prepare_only:
        log("prepared")
        return 0
    t0 = time.monotonic()
    kmake("zImage", DTB, jobs=a.jobs)
    log(f"make done in {time.monotonic() - t0:.0f} s")
    pack()
    return 0


if __name__ == "__main__":
    sys.exit(main())
