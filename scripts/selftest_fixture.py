#!/usr/bin/env python3
"""selftest_fixture — run the rootfs overlay scripts us48-health and us48-selftest against a fake /proc + /sys tree, host only (#35).

Usage: selftest_fixture.py [--busybox PATH]
- Runs the built static busybox (tmp/mainline/rootfs-normal/bin/busybox from build_rootfs.py) under binfmt/qemu-arm: the real ash + applets.
- Scenarios: healthy tree (selftest 0 FAIL, health fields, uImage CRCs vs zlib), degraded tree (hot LM75, fan error, no PCIe,
  wrong JEDEC), and three consecutive boots through a fake pstore (counter, prev=unclean, prev=clean).
- No board, no flash. Exit 0 = all expectations met. Log: tmp/logs/selftest_fixture.log.
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "mainline/rootfs/overlay"
FIX = ROOT / "tmp/selftest-root"
LOG = ROOT / "tmp/logs/selftest_fixture.log"
RUN_STALL_S = 30  # both scripts run well under 2 s on the host; 30 s = hung
APPLETS = ("cat cut dd grep head od printf sed sort sh tail tee tr uniq uname awk crc32 mountpoint expr "
           "mount true false echo").split()
failures: list[str] = []


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def put(rel: str, text="") -> Path:
    p = FIX / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text)
    return p


def uimage(name: str, data: bytes) -> bytes:
    """Legacy uImage: 64 B big-endian header (magic, hcrc, time, size, load, ep, dcrc, os, arch, type, comp, name[32])."""
    hdr = struct.pack(">IIIIIIIBBBB32s", 0x27051956, 0, 0, len(data), 0x04000000, 0x04000000, zlib.crc32(data), 5, 2, 2, 0,
                      name.encode())
    return hdr + data


def build_tree(bad: bool, bb: Path) -> dict:
    shutil.rmtree(FIX, ignore_errors=True)
    FIX.mkdir(parents=True)
    conf = ROOT / "tmp/mainline/rootfs-normal/etc/us48.conf"
    if not conf.exists():
        sys.exit("run scripts/build_rootfs.py first (etc/us48.conf)")
    (FIX / "etc").mkdir()
    shutil.copy2(conf, FIX / "etc/us48.conf")
    put("proc/cpuinfo", "processor\t: 0\nBogoMIPS\t: 1000.00\nCPU implementer\t: 0x41\nCPU part\t: 0xc09\n")
    put("proc/meminfo", "MemTotal:         190000 kB\n")
    put("proc/uptime", "12.34 56.78\n")
    put("proc/modules", "us48_cmic 100000 1 - Live 0x0\n")
    mtds = [("u-boot", 0xc0000), ("u-boot-env", 0x10000), ("shmoo", 0x10000), ("kernel0", 0xf00000), ("kernel1", 0xf10000),
            ("cfg", 0x100000), ("EEPROM", 0x10000)]
    put("proc/mtd", "dev:    size   erasesize  name\n" + "".join(f'mtd{i}: {s:08x} 00001000 "{n}"\n' for i, (n, s) in enumerate(mtds)))
    ub = uimage("awto-uboot test 2026", os.urandom(40000))
    kh = uimage("us48-mainline-test", os.urandom(4000))
    k0 = bytearray(b"\xff" * (0x200000 + 4096))
    k0[:len(ub)] = ub
    k0[0x200000:0x200000 + len(kh)] = kh
    put("dev/mtd3ro", bytes(k0))
    put("sys/bus/spi/devices/spi1.0/spi-nor/jedec_id", "c22018\n" if bad else "c22019\n")
    for z, (ty, t) in enumerate([("lm75-48", 41500), ("lm75-49", 90000 if bad else 43000), ("lm75-4a", 40000),
                                 ("lm75-4b", 39000), ("die0", 67000), ("die1", 61500), ("cpu-thermal", 1)]):
        put(f"sys/class/thermal/thermal_zone{z}/type", ty + "\n")
        put(f"sys/class/thermal/thermal_zone{z}/temp", f"{t}\n")
    fan = "sys/bus/spi/drivers/us48_fan/spi1.1"
    put(f"{fan}/wire_level", "-1\n" if bad else "0\n")
    put(f"{fan}/frame_errors", "3\n" if bad else "0\n")
    put("sys/module/us48_fan/parameters/quiet", "Y\n")
    for b in range(2, 11):
        put(f"sys/bus/i2c/devices/i2c-{b}/name", "x\n")
    put("sys/bus/i2c/devices/2-0070/name", "pca9548\n")
    for a in ("48", "49", "4a", "4b"):
        put(f"sys/bus/i2c/devices/9-00{a}/name", "lm75\n")
    for i in range(12):
        put(f"sys/bus/i2c/drivers/bcm59111/{7 + i // 6}-00{0x28 + i % 6:02x}/name", "bcm59111\n")
    if not bad:
        cfg = bytearray(256)
        cfg[0x34] = 0x40
        cfg[0x40], cfg[0x41] = 0x10, 0
        cfg[0x52], cfg[0x53] = 0x12, 0x90
        put("sys/bus/pci/devices/0000:00:00.0/class", "0x060400\n")
        put("sys/bus/pci/devices/0000:00:00.0/vendor", "0x14e4\n")
        put("sys/bus/pci/devices/0000:00:00.0/device", "0x8330\n")
        put("sys/bus/pci/devices/0000:00:00.0/config", bytes(cfg))
        put("sys/bus/pci/devices/0000:01:00.0/class", "0x058000\n")
        put("sys/bus/pci/devices/0000:01:00.0/vendor", "0x14e4\n")
        put("sys/bus/pci/devices/0000:01:00.0/device", "0x8346\n")
        put("sys/bus/pci/devices/0000:01:00.0/current_link_speed", "5.0 GT/s PCIe\n")
        put("sys/bus/pci/devices/0000:01:00.0/current_link_width", "2\n")
    for p, cu in (("ge1", True), ("xe49", False), ("xe50", False), ("ge40", True)):
        put(f"sys/class/net/{p}/operstate", "up\n")
        if cu:
            put(f"sys/class/net/{p}/copper_ready", "1\n" if p == "ge1" else "0\n")
            put(f"sys/class/net/{p}/block_ready", "1\n")
    # command dir: real busybox applets + stubs for what needs hardware
    bindir = FIX / "bin"
    bindir.mkdir()
    for a in APPLETS:
        (bindir / a).symlink_to(bb.resolve())
    stubs = {
        "devmem": 'echo 0x00000000' if bad else 'echo 0x00000002',
        "logger": 'echo "$@" >> "$US48_ROOT/tmp/syslog"',
        "i2cdetect": 'echo "50: 50 -- -- -- -- -- -- -- -- -- -- -- -- -- -- --"',
        "i2cget": 'echo 0x15',
    }
    for n, body in stubs.items():
        s = put(f"bin/{n}", f"#!{bb.resolve()} sh\n{body}\n")
        s.chmod(0o755)
    put("tmp/.keep")
    put("run/.keep")
    return {"k0": bytes(k0), "ub": ub, "kh": kh}


def run(script: str, *args: str) -> tuple[int, str]:
    env = {"PATH": str(FIX / "bin"), "US48_ROOT": str(FIX)}
    bb = FIX / "bin/sh"  # symlink kept: busybox picks the applet from argv[0]
    r = subprocess.run([str(bb), str(OVERLAY / "usr/sbin" / script), *args], env=env, capture_output=True, text=True,
                       timeout=RUN_STALL_S)
    return r.returncode, r.stdout + r.stderr


def expect(name: str, ok: bool, detail: str = "") -> None:
    log(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        failures.append(name)


def field(line: str, key: str) -> str:
    for w in line.split():
        if w.startswith(key + "="):
            return w[len(key) + 1:]
    return ""


def main() -> int:
    help_guard(__doc__, strict=True, known=("--busybox",))
    ap = argparse.ArgumentParser()
    ap.add_argument("--busybox", type=Path, default=ROOT / "tmp/mainline/rootfs-normal/bin/busybox")
    a = ap.parse_args()
    if not a.busybox.exists():
        sys.exit(f"{a.busybox} missing: run scripts/build_rootfs.py first")

    d = build_tree(False, a.busybox)
    rc, out = run("us48-selftest")
    log("healthy selftest:\n" + out)
    expect("selftest healthy rc 0", rc == 0 and not any(ln.startswith("FAIL ") for ln in out.splitlines()), f"rc={rc}")
    t0 = time.monotonic()
    rc, out = run("us48-health", "--show")
    expect("health --show runs fast", time.monotonic() - t0 < 5 and rc == 0, f"{time.monotonic() - t0:.2f} s host")
    line = out.strip().splitlines()[-1]
    log(line)
    expect("ubcrc = crc32 of header+data", field(line, "ubcrc") == "%08x" % zlib.crc32(d["ub"]), field(line, "ubcrc"))
    expect("kcrc = crc32 of 64 B header", field(line, "kcrc") == "%08x" % zlib.crc32(d["kh"][:64]), field(line, "kcrc"))
    expect("uboot ident from header", field(line, "uboot") == "awto-uboot_test_2026", field(line, "uboot"))
    expect("pcie LINKSTA", field(line, "pcie") == "rc:0x9012,ep:5.0_GT/s_PCIe_x2", field(line, "pcie"))
    expect("die + lm75", field(line, "die") == "die0:67.0,die1:61.5" and field(line, "lm75").startswith("48:41.5,"),
           f"{field(line, 'die')} {field(line, 'lm75')}")
    expect("fan wire level", field(line, "fan") == "L0/0x00" and field(line, "fanerr") == "0", field(line, "fan"))
    expect("poe 12 chips fw 0x15", field(line, "poe") == "12/12", out.split("poe=")[-1].strip())
    expect("reset reg soft", field(line, "rst").startswith("0x00000002(soft)"), field(line, "rst"))

    # boots through a fake pstore: pmsg file of boot N is the old record of boot N+1
    pm, ps = FIX / "dev/pmsg0", FIX / "sys/fs/pstore/pmsg-ramoops-0"
    ps.parent.mkdir(parents=True, exist_ok=True)
    pm.write_text("")
    (FIX / "sys/fs/pstore").mkdir(exist_ok=True)
    rc, out = run("us48-health")
    expect("boot 1: counter 1, prev none", field(out, "boot") == "1" and field(out, "prev") == "none", out[:120])
    expect("boot 1: cause soft (reg only)", field(out, "cause") == "soft", field(out, "cause"))
    shutil.copy2(pm, ps)  # power fails with no shutdown hook: unclean
    rc, out = run("us48-health")
    expect("boot 2: counter 2, prev unclean", field(out, "boot") == "2" and field(out, "prev") == "unclean", out[:120])
    expect("boot 2: cause soft_unclean", field(out, "cause") == "soft_unclean", field(out, "cause"))
    run("us48-health", "--clean")
    shutil.copy2(pm, ps)
    rc, out = run("us48-health")
    expect("boot 3: counter 3, prev clean, cause reboot", field(out, "boot") == "3" and field(out, "prev") == "clean"
           and field(out, "cause") == "reboot", out[:120])
    syslog = (FIX / "tmp/syslog").read_text() if (FIX / "tmp/syslog").exists() else ""
    expect("syslog line logged", "boot=3" in syslog)

    d = build_tree(True, a.busybox)
    rc, out = run("us48-selftest")
    log("degraded selftest:\n" + out)
    for item in ("FAIL thermal", "FAIL fan", "FAIL pcie", "FAIL flash-id"):
        expect(f"degraded: {item}", item in out)
    expect("degraded rc = FAIL count", rc == sum(1 for ln in out.splitlines() if ln.startswith("FAIL ") and " summary " not in ln),
           f"rc={rc}")
    rc, out = run("us48-health")  # boot mode, no pstore in this tree: rec=no_pstore, register 0 = power on
    line = out.strip().splitlines()[-1]
    expect("degraded health: fan unknown, no pcie, power_on", field(line, "fan") == "unknown" and field(line, "pcie") == "none"
           and field(line, "cause") == "power_on", f"{field(line, 'fan')} {field(line, 'pcie')} {field(line, 'cause')}")

    log("RESULT " + ("PASS" if not failures else "FAIL " + ", ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
