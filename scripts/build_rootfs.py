#!/usr/bin/env python3
"""build_rootfs — minimal initramfs rootfs for the mainline image (busybox + dropbear, static musl, no OpenWrt).

- Toolchain: musl-cross-make (clone in /mnt/1tnvme/git-mirrors/richfelker) builds gcc + musl, soft-float EABI,
  Cortex-A9, once into /mnt/1tnvme/toolchains/ (symlink tmp/musl-arm, or $US48_MUSL_TC). Kernel + modules use the
  Fedora `arm-linux-gnu-gcc` (build_mainline.py). Fedora's cross gcc is hard-float only (VFP libgcc): unusable here.
- Sources: busybox/dropbear/nftables (+libmnl, libnftnl) tarballs in /mnt/1tnvme/toolchains/dl (sha256 pinned below); mcm fetches its own.
- Our files: mainline/rootfs/{busybox.config,authorized_keys,overlay/}; overlay is copied over the stage as is.
- Stage: tmp/mainline/rootfs (build_mainline.py adds modules + firmware, then packs it as the initramfs).
- Log: tmp/logs/build_rootfs.log.

- nft (nftables 1.1.7, static, mini-gmp) is staged in both profiles; cached in tmp/rootfs/tools-bin/nft.

- --tools (#84, debug image only): static test tools into <stage>/usr/bin: memtester, cyclictest (rt-tests, with the
  tools/numa-stub libnuma stand-in), tinymembench, plus tools/us48bench.c and tools/rngtest.c. Built tools are cached in
  tmp/rootfs/tools-bin; delete a file there to rebuild it. Normal profile has none (keeps the 13 MiB slot).

Usage: build_rootfs.py [--toolchain-only] [--stage DIR] [--regen-busybox-config] [--jobs N] [--tools]
"""

import argparse
import hashlib
import os
import select
import shutil
import subprocess
import sys
import time
import urllib.request
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
SRC = ROOT / "mainline/rootfs"
WORK = ROOT / "tmp/rootfs"
LOG = ROOT / "tmp/logs/build_rootfs.log"
TC = Path(os.environ.get("US48_MUSL_TC", ROOT / "tmp/musl-arm"))  # symlink -> /mnt/1tnvme/toolchains/musl-<ver>-arm
TC_STORE = Path("/mnt/1tnvme/toolchains")
MCM_STORE = Path("/mnt/1tnvme/git-mirrors/richfelker/musl-cross-make")
MCM_TARGET = "arm-linux-musleabi"
MCM_GCC = "14.3.0"  # in mcm's hashes/; 9.4.0 default does not build with a current host gcc
CROSS = str(TC / "bin" / (MCM_TARGET + "-"))
ARCH_FLAGS = ["-mcpu=cortex-a9", "-mfloat-abi=soft", "-Os"]  # HR2 A9: no VFP
STALL_S = 60.0     # host-process rule: no output line for 60 s = hung
TOOLCHAIN_STALL_S = 300.0  # gcc/binutils configure + patching are silent for minutes under load
TOOLCHAIN_OVERALL_S = 3600.0  # gcc + binutils + musl build, a guess until measured (no 60 s stall expected)
OVERALL_S = 900.0  # backstop per command; no measured cold worst case yet (musl ~1 min, busybox ~2 min expected)

BUSYBOX = ("busybox-1.37.0", "https://busybox.net/downloads/busybox-1.37.0.tar.bz2",
           "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4")
DROPBEAR = ("dropbear-2026.94", "https://matt.ucc.asn.au/dropbear/releases/dropbear-2026.94.tar.bz2",
            "e098034a843699200c8c977a991fff73159735bf795d5f72ef672c41a6b1ae81")

# nft (route/NAT/firewall tests, docs/route-nat-fw.md): static, mini-gmp, no readline/json/python
LIBMNL = ("libmnl-1.0.5", "https://www.netfilter.org/pub/libmnl/libmnl-1.0.5.tar.bz2",
          "274b9b919ef3152bfb3da3a13c950dd60d6e2bcd54230ffeca298d03b40d0525")
LIBNFTNL = ("libnftnl-1.3.2", "https://www.netfilter.org/pub/libnftnl/libnftnl-1.3.2.tar.xz",
            "c97abc3409f8fa396b4462b2bb7f147a3a47a4ddc97cfa0b2f18890c9cfde8b0")
NFTABLES = ("nftables-1.1.7", "https://www.netfilter.org/pub/nftables/nftables-1.1.7.tar.xz",
            "a6fbf060d8d4fff001517a2b94f356bb4366bfbf0ba366366f9d27cc38caa58f")
MEMTESTER = ("memtester-4.7.1", "https://pyropus.ca/software/memtester/old-versions/memtester-4.7.1.tar.gz",
             "e427de663f7bd22d1ebee8af12506a852c010bd4fcbca1e0e6b02972d298b5bb")
RTTESTS = ("rt-tests-2.11", "https://www.kernel.org/pub/linux/utils/rt-tests/rt-tests-2.11.tar.gz",
           "3d6e6fafaa7e9c72963c634b2999997d2c48c4aa080ca4ea35b52e825a4c64b2")
TINYMEMBENCH = ("tinymembench-a2cf6d7e382e3aea1eb39173174d9fa28cad15f3",  # ssvb/tinymembench master, no releases
                "https://codeload.github.com/ssvb/tinymembench/tar.gz/a2cf6d7e382e3aea1eb39173174d9fa28cad15f3",
                "e9553d6e273c24ff62d9a63f06c4d68ad697f22e35d25102c3740f96e1d25d32")
TOOLS_BIN = WORK / "tools-bin"

# busybox: defconfig + these (symbol -> y/n/value); applied by --regen-busybox-config, result is committed
BUSYBOX_OPTS = {
    "STATIC": "y", "PIE": "n", "BUILD_LIBBUSYBOX": "n", "FEATURE_SHARED_BUSYBOX": "n",
    "EXTRA_COMPAT": "n", "DEBUG": "n", "SELINUX": "n", "FEATURE_PREFER_APPLETS": "n",
    "TC": "n",  # needs linux/pkt_sched.h CBQ, gone from 6.8+ uapi
    "I2CGET": "y", "I2CSET": "y", "I2CDUMP": "y", "I2CDETECT": "y", "I2CTRANSFER": "y",
    "DEVMEM": "y", "SYSLOGD": "y", "LOGREAD": "n", "FEATURE_IPC_SYSLOG": "n",  # no SysV IPC in the kernel: syslogd writes /tmp/messages
     "LOGGER": "y",
    "FEATURE_SYSLOGD_CFG": "n", "KLOGD": "n", "TASKSET": "y", "FEATURE_TASKSET_FANCY": "y",
    "NC": "y", "TIMEOUT": "y", "LSPCI": "y", "MICROCOM": "n", "IFCONFIG": "y", "IP": "y", "IPADDR": "y",
    "IPLINK": "y", "IPROUTE": "y", "FEATURE_IP_ADDRESS": "y", "FEATURE_IP_LINK": "y", "FEATURE_IP_ROUTE": "y",
    "RX": "y", "WATCHDOG": "y", "SHA1_HWACCEL": "n", "SHA256_HWACCEL": "n", "INSMOD": "y", "RMMOD": "y", "LSMOD": "y", "MODPROBE": "y", "DEPMOD": "n", "MODINFO": "n",
    "HWCLOCK": "n", "UDHCPC": "n", "UDHCPD": "n", "DHCPRELAY": "n", "DUMPLEASES": "n",
    "INETD": "n", "TELNETD": "n", "TELNET": "n", "FTPD": "n", "HTTPD": "n", "NTPD": "n",
    "SH_IS_ASH": "y", "ASH": "y", "ASH_JOB_CONTROL": "y", "FEATURE_EDITING": "y",
    "INIT": "y", "FEATURE_USE_INITTAB": "y", "FEATURE_INIT_SYSLOG": "n", "HALT": "y", "POWEROFF": "y", "REBOOT": "y",
    "FEATURE_SYSTEMD": "n", "FEATURE_INSTALLER": "n", "PING": "y", "TRACEROUTE": "n", "WGET": "n",
}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(cmd, cwd=None, env=None, check=True, overall_s=None, stall_s=None) -> int:
    """Run with output to the log; killed after STALL_S without an output line or OVERALL_S in total."""
    log("$ " + " ".join(map(str, cmd)))
    t0 = last = time.monotonic()
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    tail = b""
    with LOG.open("ab") as f:
        while True:
            r, _, _ = select.select([p.stdout], [], [], 1.0)
            now = time.monotonic()
            if r:
                line = p.stdout.readline()
                if not line:
                    break
                last = now
                f.write(line)
                tail = line
                continue
            if p.poll() is not None:
                break
            if now - last > (stall_s or STALL_S) or now - t0 > (overall_s or OVERALL_S):
                p.kill()
                why = f"silent {now - last:.0f} s (limit {stall_s or STALL_S:.0f})" if now - last > (stall_s or STALL_S) else \
                    f"over {now - t0:.0f} s (limit {overall_s or OVERALL_S:.0f})"
                log(f"abort: {cmd[0]} {why}, loadavg {os.getloadavg()}, last line {tail[-200:]!r}")
                sys.exit(5)
    rc = p.wait()
    if check and rc:
        log(f"FAILED rc={rc}: {cmd[0]} (see {LOG.relative_to(ROOT)}, last line {tail[-200:]!r})")
        sys.exit(rc)
    return rc


def fetch(spec) -> Path:
    name, url, sha = spec
    dl = TC_STORE / "dl"
    dl.mkdir(parents=True, exist_ok=True)
    f = dl / url.rsplit("/", 1)[1]
    if not f.exists():
        log(f"download {url}")
        with urllib.request.urlopen(url, timeout=60) as r:  # 60 s: no data = server dead
            f.write_bytes(r.read())
    if hashlib.sha256(f.read_bytes()).hexdigest() != sha:
        log(f"abort: sha256 of {f} != {sha}")
        sys.exit(6)
    return f


def unpack(spec, dst: Path) -> Path:
    """Fresh source dir WORK/<name> from the tarball."""
    f = fetch(spec)
    d = dst / spec[0]
    if d.exists():
        shutil.rmtree(d)
    dst.mkdir(parents=True, exist_ok=True)
    run(["tar", "-xf", str(f), "-C", str(dst)])
    return d


def cc_cmd() -> list:
    return [f"{CROSS}gcc", "-static", *ARCH_FLAGS]


def ensure_toolchain(jobs: int) -> None:
    """gcc + musl cross toolchain via musl-cross-make into TC, once (about 20 min)."""
    if Path(f"{CROSS}gcc").exists():
        return
    real = TC_STORE / f"musl-{MCM_GCC}-arm-cortex-a9-soft"
    if TC.is_symlink() or not TC.exists():
        TC.parent.mkdir(parents=True, exist_ok=True)
        if TC.is_symlink():
            TC.unlink()
        TC.symlink_to(real)
    work = WORK / "mcm"  # build in a copy: the store stays clean
    if work.exists():
        shutil.rmtree(work)
    run(["git", "clone", "-q", str(MCM_STORE), str(work)])
    cache = TC_STORE / "dl/mcm"  # tarballs: seeded into the copy's sources/ (mcm only downloads into its own), kept after
    cache.mkdir(parents=True, exist_ok=True)
    shutil.copytree(cache, work / "sources", dirs_exist_ok=True)
    run(["make", f"-j{jobs}", f"TARGET={MCM_TARGET}", f"GCC_VER={MCM_GCC}", "DL_CMD=curl -fsSL -o",
         f"OUTPUT={real}", "GCC_CONFIG=--enable-languages=c --with-cpu=cortex-a9 --with-float=soft "
         "--disable-libsanitizer --disable-libquadmath --disable-libgomp --disable-libatomic",
         "install"], cwd=work, overall_s=TOOLCHAIN_OVERALL_S, stall_s=TOOLCHAIN_STALL_S,
        env=dict(os.environ, CC="gcc -std=gnu17", CXX="g++ -std=gnu++17"))  # gcc 14 sources vs host gcc 16 defaults (C++20 char8_t, C23)
    shutil.copytree(work / "sources", cache, dirs_exist_ok=True)
    shutil.rmtree(work)
    log(f"toolchain installed in {real}")


def busybox_config(src: Path, regen: bool) -> None:
    cfg = SRC / "busybox.config"
    env = dict(os.environ)
    if regen:
        run(["make", "defconfig"], cwd=src, env=env)
        lines = (src / ".config").read_text().splitlines()
        out = []
        for l in lines:
            key = l.split("=")[0].removeprefix("# ").split(" ")[0]
            if key.startswith("CONFIG_") and key[7:] in BUSYBOX_OPTS:
                continue
            out.append(l)
        for k, v in BUSYBOX_OPTS.items():
            out.append(f"CONFIG_{k}=y" if v == "y" else f"# CONFIG_{k} is not set" if v == "n" else f'CONFIG_{k}={v}')
        (src / ".config").write_text("\n".join(out) + "\n")
        run(["make", "oldconfig"], cwd=src, env=env)  # stdin=DEVNULL: any unanswered new symbol aborts here
        cfg.write_text((src / ".config").read_text())
        log(f"wrote {cfg.relative_to(ROOT)}")
    else:
        shutil.copy2(cfg, src / ".config")
        run(["make", "oldconfig"], cwd=src, env=env)


def build_busybox(jobs: int, stage: Path, regen: bool = False) -> None:
    src = unpack(BUSYBOX, WORK / "src")
    busybox_config(src, regen)
    cc = " ".join(cc_cmd())
    run(["make", f"-j{jobs}", f"CC={cc}", f"HOSTCC=gcc", f"AR={CROSS}ar", f"STRIP={CROSS}strip",
         f"CONFIG_PREFIX={stage}", f"CROSS_COMPILE={CROSS}", "busybox", "install"], cwd=src)
    # applet links: bin/sbin/usr/bin/usr/sbin all point at /bin/busybox (CONFIG_PREFIX install)
    run([f"{CROSS}strip", str(stage / "bin/busybox")])


def build_dropbear(jobs: int, stage: Path) -> None:
    src = unpack(DROPBEAR, WORK / "src")
    cc = " ".join(cc_cmd())
    run(["./configure", "--host=arm-linux-gnueabi", "--disable-zlib", "--disable-pam", "--disable-lastlog",
         "--disable-utmp", "--disable-utmpx", "--disable-wtmp", "--disable-wtmpx", "--disable-loginfunc",
         "--disable-pututline", "--disable-pututxline", "--enable-static", f"CC={cc}"], cwd=src)
    run(["make", f"-j{jobs}", "PROGRAMS=dropbear dropbearkey scp dbclient", "MULTI=1", "STATIC=1"], cwd=src)
    run([f"{CROSS}strip", str(src / "dropbearmulti")])
    dst = stage / "usr/sbin"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "dropbearmulti", dst / "dropbearmulti")
    for name, d in (("dropbear", "usr/sbin"), ("dropbearkey", "usr/bin"), ("dbclient", "usr/bin"), ("scp", "usr/bin")):
        link = stage / d / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to("/usr/sbin/dropbearmulti")  # dropbearmulti dispatches on argv[0]


def build_nft(jobs: int, stage: Path) -> None:
    """nft (nftables) static against musl: libmnl + libnftnl + mini-gmp; built once into TOOLS_BIN (delete to rebuild)."""
    TOOLS_BIN.mkdir(parents=True, exist_ok=True)
    if not (TOOLS_BIN / "nft").exists():
        prefix = WORK / "nft-prefix"
        if prefix.exists():
            shutil.rmtree(prefix)
        cc = " ".join(cc_cmd())
        env = dict(os.environ, PKG_CONFIG_PATH=f"{prefix}/lib/pkgconfig", PKG_CONFIG_LIBDIR=f"{prefix}/lib/pkgconfig")
        common = ["--host=arm-linux-gnueabi", "--enable-static", "--disable-shared", f"--prefix={prefix}", f"CC={cc}"]
        src = unpack(LIBMNL, WORK / "src")
        run(["./configure", *common], cwd=src, env=env)
        run(["make", f"-j{jobs}", "install"], cwd=src, env=env)
        src = unpack(LIBNFTNL, WORK / "src")
        run(["./configure", *common], cwd=src, env=env)
        run(["make", f"-j{jobs}", "install"], cwd=src, env=env)
        src = unpack(NFTABLES, WORK / "src")
        run(["./configure", *common, "--without-cli", "--without-json", "--disable-python", "--disable-man-doc",
             "--disable-debug", "--with-mini-gmp", "LDFLAGS=-static"], cwd=src, env=env)
        # libtool drops a plain -static at the program link; -all-static links libc statically too (no ld-musl on the board)
        run(["make", f"-j{jobs}", "LDFLAGS=-all-static"], cwd=src, env=env)
        run([f"{CROSS}strip", str(src / "src/nft")])
        shutil.copy2(src / "src/nft", TOOLS_BIN / "nft")
    hdr = subprocess.run([f"{CROSS}readelf", "-l", str(TOOLS_BIN / "nft")], capture_output=True, text=True).stdout
    if "INTERP" in hdr:
        raise SystemExit(f"{TOOLS_BIN / 'nft'} is dynamically linked (PT_INTERP): the rootfs has no musl loader; delete it to rebuild")
    dst = stage / "usr/sbin"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TOOLS_BIN / "nft", dst / "nft")
    log(f"nft staged: {(dst / 'nft').stat().st_size} B")


def build_tools(jobs: int, stage: Path) -> None:
    """Static test tools (#84) into stage/usr/bin; each is built once into TOOLS_BIN (delete to rebuild)."""
    TOOLS_BIN.mkdir(parents=True, exist_ok=True)
    flags = " ".join(ARCH_FLAGS)
    cc = " ".join(cc_cmd())
    if not (TOOLS_BIN / "memtester").exists():
        src = unpack(MEMTESTER, WORK / "src")
        (src / "conf-cc").write_text(f"{CROSS}gcc {flags} -O2 -Wno-parentheses -DPOSIX -D_POSIX_C_SOURCE=200809L -D_FILE_OFFSET_BITS=64 -DTEST_NARROW_WRITES -c\n")
        (src / "conf-ld").write_text(f"{CROSS}gcc -static -s\n")
        run(["make", f"-j{jobs}", "memtester"], cwd=src)
        shutil.copy2(src / "memtester", TOOLS_BIN / "memtester")
    if not (TOOLS_BIN / "tinymembench").exists():
        src = unpack(TINYMEMBENCH, WORK / "src")
        run(["make", f"-j{jobs}", f"CC={cc} -O2"], cwd=src)  # NEON paths are #ifdef'd out (no VFP/NEON on this SKU)
        run([f"{CROSS}strip", str(src / "tinymembench")])
        shutil.copy2(src / "tinymembench", TOOLS_BIN / "tinymembench")
    if not (TOOLS_BIN / "cyclictest").exists():
        stub = ROOT / "tools/numa-stub"
        obj = WORK / "numa-stub"
        obj.mkdir(parents=True, exist_ok=True)
        run([f"{CROSS}gcc", *ARCH_FLAGS, "-c", str(stub / "numa.c"), "-o", str(obj / "numa.o")])
        run([f"{CROSS}ar", "rcs", str(obj / "libnuma.a"), str(obj / "numa.o")])
        src = unpack(RTTESTS, WORK / "src")
        run(["patch", "-p1", "-i", str(ROOT / "tools/rt-tests-musl.patch")], cwd=src)  # raw sched_* syscalls (musl ENOSYS)
        cyc = src / "src/cyclictest/cyclictest.c"  # musl already defines sigev_notify_thread_id
        cyc.write_text(cyc.read_text().replace("#define sigev_notify_thread_id _sigev_un._tid",
                                               "#ifndef sigev_notify_thread_id\n#define sigev_notify_thread_id _sigev_un._tid\n#endif"))
        run(["make", f"-j{jobs}", f"CC={CROSS}gcc", f"AR={CROSS}ar", "no_libcpupower=1", "PYLIB=x",
             f"CFLAGS={' '.join(ARCH_FLAGS)} -I{stub} -I{src}/src/include -include musl-sched.h", f"LDFLAGS=-static -L{obj}", "cyclictest"], cwd=src)
        run([f"{CROSS}strip", str(src / "cyclictest")])
        shutil.copy2(src / "cyclictest", TOOLS_BIN / "cyclictest")
    for name in ("us48bench", "rngtest"):  # our own, rebuilt every time (a second)
        run([*cc_cmd(), "-Wall", "-Wextra", "-o", str(TOOLS_BIN / name), str(ROOT / f"tools/{name}.c")])
        run([f"{CROSS}strip", str(TOOLS_BIN / name)])
    dst = stage / "usr/bin"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("memtester", "tinymembench", "cyclictest", "us48bench", "rngtest"):
        shutil.copy2(TOOLS_BIN / name, dst / name)
    log("tools staged: " + ", ".join(f"{n} {(dst / n).stat().st_size} B" for n in ("memtester", "tinymembench", "cyclictest", "us48bench", "rngtest")))


def build(stage: Path, jobs: int = os.cpu_count() or 4, tools: bool = False) -> None:
    stage = stage.resolve()
    """Stage = busybox + dropbear + overlay + authorized_keys (+ test tools). Modules/firmware are added by build_mainline.py."""
    if stage.exists():
        shutil.rmtree(stage)
    for d in ("dev", "proc", "sys", "tmp", "run", "root/.ssh", "etc/dropbear", "lib/modules", "lib/firmware", "mnt"):
        (stage / d).mkdir(parents=True, exist_ok=True)
    ensure_toolchain(jobs)
    build_busybox(jobs, stage)
    build_dropbear(jobs, stage)
    build_nft(jobs, stage)
    if tools:
        build_tools(jobs, stage)
    shutil.copytree(SRC / "overlay", stage, symlinks=True, dirs_exist_ok=True)
    keys = SRC / "authorized_keys"
    shutil.copy2(keys, stage / "root/.ssh/authorized_keys")
    (stage / "root").chmod(0o700)
    (stage / "root/.ssh").chmod(0o700)
    (stage / "root/.ssh/authorized_keys").chmod(0o600)
    size = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file() and not f.is_symlink())
    log(f"rootfs staged in {stage}: {size} B of files (before modules/firmware)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", type=Path, default=ROOT / "tmp/mainline/rootfs")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--tools", action="store_true",
                    help="also stage the static test tools (memtester, cyclictest, tinymembench, us48bench, rngtest)")
    ap.add_argument("--toolchain-only", action="store_true", help="build the musl cross toolchain, then exit")
    ap.add_argument("--regen-busybox-config", action="store_true",
                    help="rewrite mainline/rootfs/busybox.config from busybox defconfig + BUSYBOX_OPTS, then exit")
    a = ap.parse_args()
    if a.regen_busybox_config:
        ensure_toolchain(a.jobs)
        src = unpack(BUSYBOX, WORK / "src")
        busybox_config(src, regen=True)
        return 0
    if a.toolchain_only:
        ensure_toolchain(a.jobs)
        return 0
    build(a.stage, a.jobs, tools=a.tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
