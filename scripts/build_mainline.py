#!/usr/bin/env python3
"""build_mainline — mainline Linux image for the UniFi US-48-500W (BCM53344, Hurricane 2).

- Tree: tmp/linux-mainline = worktree of the machine Linux store (#45), reached via the gitignored
  symlink tmp/linux-store (or $US48_LINUX_STORE / --store). Created on first run; tag BASE fetched from
  the store's `origin` (mainline) if missing. Never --depth, never gc in the store.
- Branch BRANCH (awto/us48/mainline-BASE) = BASE + mainline/*.patch; local to the store, not pushed.
- Config: allnoconfig + mainline/us48_defconfig + us48_<profile>.config (explicit, every symbol checked); initramfs = mainline/rootfs (scripts/build_rootfs.py: busybox + dropbear, static musl) + our modules + firmware.
- Toolchain: Fedora cross gcc `arm-linux-gnu-` (kernel + modules); userspace toolchain is build_rootfs.py's.
- Output: tmp/mainline/us48-mainline.bin = legacy uImage (header written here, no mkimage), load/entry 0x00018000,
  zImage + appended DTB.
- Logs: tmp/logs/build_mainline.log; make output tmp/logs/build_mainline-make.log.

Usage: build_mainline.py [--keep-branch] [--save-patches] [--checkpatch] [--dtbs-check] [--prepare-only] [--jobs N]
  default          rebuild the branch from mainline/*.patch (git am), configure, build, pack
  --keep-branch    build the branch as it is (after editing the tree); skips git am
  --save-patches   git format-patch BASE..BRANCH -> mainline/, rewriting only patches whose content changed (names kept), then exit
  --checkpatch     scripts/checkpatch.pl --strict on every commit of the branch, then exit
  --dtbs-check     make dtbs_check for the new board DTS (dtschema from tmp/dtschema-venv), then exit
  --module-only    build the out-of-tree modules only (--tree/--build-dir: private tree + build dir, no staging)
"""

import argparse
import os
import re
import struct
import zlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_rootfs  # noqa: E402


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
PROFILE = "debug"  # --profile; set_profile() derives the per-profile paths
BUILD = OUTDIR / f"build-{PROFILE}"
STAGE = OUTDIR / f"rootfs-{PROFILE}"
PATCHES = ROOT / "mainline"
BASE_FRAGMENT = PATCHES / "us48_defconfig"
LOG = ROOT / "tmp/logs/build_mainline.log"
MAKE_LOG = ROOT / "tmp/logs/build_mainline-make.log"
STORE_LINK = ROOT / "tmp/linux-store"  # symlink -> machine Linux store; no host path in this repo
BASE = "v7.4-rc1"
BRANCH = os.environ.get("US48_KERNEL_BRANCH", f"awto/us48/mainline-{BASE}")  # a second worktree of the store needs its own branch name
# tag fetch: objects mostly in the store already, ~300 MB worst case at >=1 MB/s; checkout of ~96k files ~60 s
FETCH_TIMEOUT, WORKTREE_TIMEOUT = 600, 300
DTB = "broadcom/bcm53344-ubnt-unifi-us-48-500w.dtb"
CROSS = "arm-linux-gnu-"  # dnf: gcc-arm-linux-gnu binutils-arm-linux-gnu
VENV_BIN = ROOT / "tmp/dtschema-venv/bin"
SLOT_BYTES = 0xD00000  # dev.py SLOTS["linux"]: 13 MiB
LOADADDR = "0x00018000"  # stock U-Boot writes ATAGs at 0x8000 after copying the image (docs/uboot.md)
NICE = ["nice", "-n", "19", "ionice", "-c3"]


def set_profile(p: str) -> None:
    global PROFILE, BUILD, STAGE
    PROFILE, BUILD, STAGE = p, OUTDIR / f"build-{p}", OUTDIR / f"rootfs-{p}"


def fragment_text() -> str:
    """Base defconfig + the profile fragment (mainline/us48_<profile>.config)."""
    return BASE_FRAGMENT.read_text() + (PATCHES / f"us48_{PROFILE}.config").read_text()


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(cmd, cwd=None, out=None, env=None, check=True) -> int:
    cwd = cwd or TREE
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


def _norm_patch(text: str) -> str:
    """Patch text without what git regenerates per run: From <hash>, [PATCH n/N], Subject wrapping, index lines, hunk headers."""
    lines = text.splitlines()[1:]
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("Subject: "):
            subj = ln
            while i + 1 < len(lines) and lines[i + 1].startswith(" "):
                i += 1
                subj += " " + lines[i].strip()
            ln = re.sub(r"^Subject: \[PATCH[^\]]*\] ?", "Subject: ", subj)
        elif ln.startswith("index "):    # blob ids move when an earlier patch changes
            i += 1
            continue
        elif ln.startswith("@@ "):       # offsets and function context follow earlier patches and the git version
            ln = "@@"
        out.append(ln)
        i += 1
    return "\n".join(out)


def _subject(norm: str) -> str:
    return next(ln for ln in norm.split("\n") if ln.startswith("Subject: "))


def sync_patches(gen: Path, dest: Path) -> tuple[int, int, int]:
    """Make dest hold the patches in gen: unchanged ones (same normalised content) are left alone, a changed one keeps its
    file name, new/removed ones are added/deleted. Returns (kept, rewritten, added+removed)."""
    old = {}
    for f in sorted(dest.glob("0*.patch")):
        n = _norm_patch(f.read_text())
        old[_subject(n)] = (f, n)
    kept = rewritten = changed = 0
    used = set()
    for g in sorted(gen.glob("0*.patch")):
        text = g.read_text()
        n = _norm_patch(text)
        subj = _subject(n)
        hit = old.get(subj)
        num = g.name[:4]
        if hit:
            f, on = hit
            used.add(f)
            target = f if f.name[:4] == num else f.with_name(num + f.name[4:])
            if target != f:
                f.rename(target)
                changed += 1
            if on == n:
                kept += 1
            else:
                target.write_text(text)
                rewritten += 1
        else:
            (dest / g.name).write_text(text)
            changed += 1
    for subj, (f, _n) in old.items():
        if f not in used:
            f.unlink()
            changed += 1
    return kept, rewritten, changed


def save_patches() -> None:
    """Branch -> mainline/*.patch, touching only patches whose content changed (stable names, no hash/numbering churn)."""
    gen = OUTDIR / "patches-gen"
    shutil.rmtree(gen, ignore_errors=True)
    gen.mkdir(parents=True)
    run(["git", "format-patch", "-q", "--no-signature", "--abbrev=9", "-o", str(gen), f"{BASE}..{BRANCH}"])
    kept, rewritten, changed = sync_patches(gen, PATCHES)
    shutil.rmtree(gen, ignore_errors=True)
    log(f"patches in mainline/: {kept} unchanged, {rewritten} rewritten, {changed} added/removed/renumbered")


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
    env = dict(os.environ, ARCH="arm", CROSS_COMPILE=CROSS)
    if venv:
        env["PATH"] = f"{VENV_BIN}:{env['PATH']}"
    return env


def kmake(*args, out=None, venv=False, jobs=None) -> None:
    out = out or MAKE_LOG
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


def stage_rootfs() -> str:
    """mainline/rootfs (busybox + dropbear + overlay) + firmware + a /dev/console node; returns the INITRAMFS_SOURCE value."""
    stage = STAGE
    build_rootfs.build(stage, os.cpu_count() or 4, tools=PROFILE == "debug")  # test tools (#84): debug image only
    for name in ("us48-a2.elf", "us48-a2-bustest.elf", "us48-a2-bustest-nommu.elf"):	# scripts/build_a2fw.py; remoteproc loads /lib/firmware/<name>
        fw = ROOT / "tmp/a2fw" / name
        if fw.exists():
            shutil.copy2(fw, stage / "lib/firmware" / fw.name)
    # TSC SerDes microcode (OpenMDK ucode, scripts/gen_tsc.py): always staged; us48_cmic_xe request_firmware()s it
    shutil.copy2(ROOT / "hwdb/firmware/us48-tsc.bin", stage / "lib/firmware/us48-tsc.bin")
    # BCM59111 PoE firmware v21 (hwdb/firmware/README.md): request_firmware() of kernel/bcm59111 with fw_load=1
    shutil.copy2(ROOT / "hwdb/firmware/bcm59111-v21.bin", stage / "lib/firmware/bcm59111-v21.bin")
    return str(write_initramfs_list(stage))


def write_initramfs_list(stage: Path) -> Path:
    """gen_init_cpio list of the stage, everything uid/gid 0 (the dir source keeps the host owner) + /dev/console."""
    lst = OUTDIR / f"initramfs-{PROFILE}.list"
    out = []
    for f in sorted(stage.rglob("*")):
        rel = "/" + str(f.relative_to(stage))
        mode = f"{f.lstat().st_mode & 0o7777:04o}"
        if f.is_symlink():
            out.append(f"slink {rel} {os.readlink(f)} {mode} 0 0")
        elif f.is_dir():
            out.append(f"dir {rel} {mode} 0 0")
        else:
            out.append(f"file {rel} {f} {mode} 0 0")
    out.append("nod /dev/console 0600 0 0 c 5 1")  # after its parent dir
    lst.write_text("\n".join(out) + "\n")
    return lst


def configure(jobs: int) -> None:
    """allnoconfig + mainline/us48_defconfig (+ initramfs); aborts if any requested symbol is missing from the result."""
    BUILD.mkdir(parents=True, exist_ok=True)
    frag = OUTDIR / f"us48-full-{PROFILE}.config"
    src = stage_rootfs()
    frag.write_text(fragment_text() + f'CONFIG_INITRAMFS_SOURCE="{src}"\n'
                    "CONFIG_INITRAMFS_ROOT_UID=0\nCONFIG_INITRAMFS_ROOT_GID=0\n")
    env = make_env()
    env["KCONFIG_ALLCONFIG"] = str(frag)
    (BUILD / ".config").unlink(missing_ok=True)
    run(NICE + ["make", f"O={BUILD}", "allnoconfig"], out=MAKE_LOG, env=env)
    cfg = set((BUILD / ".config").read_text().splitlines())
    on = {l.split("=")[0] for l in cfg if "=" in l and not l.startswith("#")}
    missing = []
    for x in frag.read_text().splitlines():
        if x.startswith("# CONFIG_") and x.endswith(" is not set"):
            if x.split()[1] in on:
                missing.append(x)
        elif x.startswith("CONFIG_") and x not in cfg:
            missing.append(x)
    if missing:
        log("abort: requested symbols not in the final .config (unmet dependency?): " + ", ".join(missing))
        sys.exit(3)
    log(f"config: {sum(1 for l in cfg if l.endswith('=y'))} =y, {sum(1 for l in cfg if l.endswith('=m'))} =m")


# Build order = dependency order: us48_pci (aux parent) first, its Module.symvers feeds the others.
MODULES = [("kernel/us48-pci", ["us48_pci.ko"]), ("kernel/us48-a2", ["us48_a2.ko"]),
           ("kernel/us48-vnet", ["us48_vnet.ko"]),
           ("kernel/us48-bustest", ["us48_bustest.ko"]),
           ("kernel/us48-cmic", ["us48_cmic.ko", "us48_cmic_xe.ko"]),
           ("kernel/us48-cmic-gpio", ["us48_cmic_gpio.ko"]), ("kernel/us48-cca-gpio", ["us48_cca_gpio.ko"]),
           ("kernel/us48-cmic-mspi", ["us48_cmic_mspi.ko"]), ("kernel/bcm59111", ["bcm59111.ko"]),
           ("kernel/us48-pvtmon", ["us48_pvtmon.ko"]), ("kernel/us48-fan", ["us48_fan.ko"]),
           ("kernel/us48-idm-reset", ["us48_idm_reset.ko"])]


def intree_modules() -> list:
    """In-tree `=m` modules (modules.order lists them once built; the .config says what is wanted): #84 pl330, dmatest, torture."""
    cfg = BUILD / ".config"
    return [l for l in cfg.read_text().splitlines() if l.endswith("=m")] if cfg.exists() else []


def stage_intree_modules() -> None:
    """Copy the built in-tree .ko files (drivers/dma/pl330.ko ...) next to ours in lib/modules/<release>; the rc scripts/core_test insmod them."""
    order = BUILD / "modules.order"
    names = [l.strip()[:-2] + ".ko" for l in order.read_text().splitlines() if l.strip().endswith(".o")] if order.exists() else []  # lists .o
    dst = STAGE / "lib/modules" / (BUILD / "include/config/kernel.release").read_text().strip()
    dst.mkdir(parents=True, exist_ok=True)
    for rel in names:
        ko = BUILD / rel
        shutil.copy2(ko, dst / ko.name)
        run([f"{CROSS}strip", "--strip-debug", str(dst / ko.name)])
        log(f"{ko.name} ({(dst / ko.name).stat().st_size} B) staged from the kernel tree")


def stage_depmod() -> None:
    """modules.dep (+alias/symbols) for busybox modprobe: host depmod over the flat staged module dir."""
    rel = (BUILD / "include/config/kernel.release").read_text().strip()
    run(["depmod", "-b", str(STAGE), rel], out=MAKE_LOG)
    log(f"modules.dep written: {(STAGE / 'lib/modules' / rel / 'modules.dep').stat().st_size} B")


def build_modules(jobs: int, stage: bool = True) -> None:
    """Out-of-tree us48 modules against this tree, staged into the initramfs rootfs (stage=False: build only).

    Module.symvers of the kernel is needed for a warning-free modpost: built with `vmlinux modules` once per build dir.
    """
    kmake("modules_prepare", jobs=jobs)
    if not (BUILD / "Module.symvers").exists() or intree_modules():
        kmake("vmlinux", "modules", jobs=jobs)
    if stage:
        stage_intree_modules()
    extra = []
    for rel, kos in MODULES:
        mod = ROOT / rel
        env = make_env()
        if extra:
            env["KBUILD_EXTRA_SYMBOLS"] = " ".join(extra)
        run(NICE + ["make", f"-j{jobs}", f"O={BUILD}", f"M={mod}", "clean"], out=MAKE_LOG, env=env, check=False)
        run(NICE + ["make", f"-j{jobs}", f"O={BUILD}", f"M={mod}", "modules"], out=MAKE_LOG, env=env)
        extra.append(str(mod / "Module.symvers"))
        for ko in kos:
            log(f"{ko} ({(mod / ko).stat().st_size} B)")
            if stage:
                dst = STAGE / "lib/modules" / (BUILD / "include/config/kernel.release").read_text().strip()
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(mod / ko, dst / ko)
                run([f"{CROSS}strip", "--strip-debug", str(dst / ko)])  # DWARF would bloat the initramfs
                log(f"{ko} staged into the initramfs rootfs ({dst})")
    if stage:
        stage_depmod()


def configure_modules_only() -> None:
    """Private build dir for module work: allnoconfig + defconfig, no initramfs, no rootfs staging."""
    BUILD.mkdir(parents=True, exist_ok=True)
    frag = BUILD / "us48-modules.config"
    # no initramfs here: its symbols (hidden without INITRAMFS_SOURCE) are left out
    frag.write_text("".join(l for l in fragment_text().splitlines(True) if "INITRAMFS" not in l))
    env = make_env()
    env["KCONFIG_ALLCONFIG"] = str(frag)
    run(NICE + ["make", f"O={BUILD}", "allnoconfig"], out=MAKE_LOG, env=env)
    cfg = set((BUILD / ".config").read_text().splitlines())
    missing = [x for x in frag.read_text().splitlines() if x.startswith("CONFIG_") and x not in cfg]
    if missing:
        log("abort: requested symbols not in the final .config: " + ", ".join(missing))
        sys.exit(3)


def uimage(data: bytes, name: str) -> bytes:
    """Legacy U-Boot uImage (IH_OS_LINUX, IH_ARCH_ARM, IH_TYPE_KERNEL, no compression), load = entry = LOADADDR."""
    addr = int(LOADADDR, 16)
    stamp = int(time.time())

    def header(hcrc: int) -> bytes:
        return struct.pack(">IIIIIIIBBBB32s", 0x27051956, hcrc, stamp, len(data), addr, addr, zlib.crc32(data),
                           5, 2, 2, 0, name.encode()[:31])

    return header(zlib.crc32(header(0))) + data


def pack() -> Path:
    boot = BUILD / "arch/arm/boot"
    zimage, dtb = boot / "zImage", boot / "dts" / DTB
    OUTDIR.mkdir(parents=True, exist_ok=True)
    cat = OUTDIR / f"zImage-dtb-{PROFILE}"
    cat.write_bytes(zimage.read_bytes() + dtb.read_bytes())
    out = OUTDIR / f"us48-mainline-{PROFILE}.bin"
    ver = (BUILD / "include/config/kernel.release").read_text().strip()
    out.write_bytes(uimage(cat.read_bytes(), f"US-48 mainline {ver}"))
    log(f"output: {out.relative_to(ROOT)} ({out.stat().st_size} bytes), zImage {zimage.stat().st_size}, dtb {dtb.stat().st_size}")
    if out.stat().st_size > SLOT_BYTES:
        msg = f"{out.name} {out.stat().st_size} B exceeds the {SLOT_BYTES} B kernel0 linux slot"
        if PROFILE == "normal":
            log("abort: " + msg)
            sys.exit(4)
        log("note: " + msg + " (debug image is RAM-booted via TFTP only)")
    return out


def main() -> int:
    global TREE, BUILD, MAKE_LOG
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--profile", choices=("normal", "debug"), default="debug",
                    help="debug (default): every check; normal: no debug options, must fit the slot")
    ap.add_argument("--keep-branch", action="store_true")
    ap.add_argument("--save-patches", action="store_true")
    ap.add_argument("--checkpatch", action="store_true")
    ap.add_argument("--dtbs-check", action="store_true")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--module-only", action="store_true",
                    help="build the out-of-tree modules (MODULES list); staged into the rootfs unless --build-dir is given")
    ap.add_argument("--tree", type=Path, help="kernel source tree (default tmp/linux-mainline)")
    ap.add_argument("--build-dir", type=Path, help="private kernel build dir (default tmp/mainline/build-<profile>); skips staging")
    ap.add_argument("--store", type=Path, default=Path(os.environ.get("US48_LINUX_STORE", STORE_LINK)),
                    help="machine Linux store (default $US48_LINUX_STORE, else tmp/linux-store)")
    a = ap.parse_args()
    set_profile(a.profile)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    if a.tree:
        TREE = a.tree.resolve()
    if a.build_dir:
        BUILD = a.build_dir.resolve()
        MAKE_LOG = ROOT / "tmp/logs/build_modules-make.log"
    if a.module_only:
        if a.build_dir and not (BUILD / ".config").exists():
            configure_modules_only()
        build_modules(a.jobs, stage=not a.build_dir)
        return 0
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
    build_modules(a.jobs)
    write_initramfs_list(STAGE)  # now includes the modules
    t0 = time.monotonic()
    kmake("zImage", DTB, jobs=a.jobs)
    log(f"make done in {time.monotonic() - t0:.0f} s")
    rel = (BUILD / "include/config/kernel.release").read_text().strip()
    if not (STAGE / "lib/modules" / rel).is_dir():  # rc scripts insmod from /lib/modules/<running release>
        log(f"abort: staged module dir lib/modules/{rel} missing: release changed after staging")
        sys.exit(4)
    pack()
    return 0


if __name__ == "__main__":
    sys.exit(main())
