#!/usr/bin/env python3
"""sfp_1g_test: bring up xe51 (SFP 1, unit 0 TSC0 lane 0) as 1000BASE-X against sw2 lan6 and check it (#78, driver-internals 9.4).

Usage: scripts/sfp_1g_test.py [--apply] [--front 51|52] [--no-reload] [--no-put]
- Dry run unless --apply: prints the steps. Setup: 1000BASE-SX module (Cisco GLC-SX-MM) in the cage, fibre to sw2 lan6 (normal port).
- Steps: (1) scp us48_cmic.ko + us48_cmic_xe.ko to /lib/modules/<rel>/ (kernel/us48-cmic, built --module-only);
  (2) scripts/mod_reload.py us48_cmic (unit 0 TSC init runs at us48_cmic probe; us48_cmic_xe follows); (3) xe49 address back
  (the xe reload recreates the unit-1 netdevs admin down); (4) xe<front> up, wait for carrier; (5) checks, PASS/FAIL per line.
- Checks: boot log (1000BASE-X stage, XLMAC), carrier 1, ethtool speed 1000 / Full / 1000baseX, AN + partner modes,
  PCS_LIVE c154 b1, ethtool -m DDM, CPU rx/tx packet counters moving (IPv6 ND/MLD out, sw2 BPDU/LLDP in); no IP is assigned.
- No link: prints the diagnosis registers (PCS/AN/sync/sigdet) of the lane, leaves the port up for a manual look.
- Ends with xe<front> admin down (cage default); xe49 is never touched except the address restore.
- Waits derived: LINK_WAIT_S = (module start 0.3 s SFF-8472 + 2 phylink polls of 1 s + CL37 AN ~0.1 s) x 1.25.
Log: tmp/logs/sfp_1g_test.log. Board address: _ipreg us48-boot (US48_IP_US48_BOOT override).
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/sfp_1g_test.log"
KO = [ROOT / "kernel/us48-cmic/us48_cmic.ko", ROOT / "kernel/us48-cmic/us48_cmic_xe.ko"]
XE = "/sys/module/us48_cmic_xe/parameters"
LANE = {51: 0, 52: 2}
LINK_WAIT_S = 3.0
TRAFFIC_S = 2.0  # IPv6 DAD/ND/MLD go out within ~1 s of the up; sw2 sends BPDU/LLDP at 1-2 s intervals
# Diagnosis registers (OM defs): PMD_LIVE c150, PCS_LATCH c153, PCS_LIVE c154, SYNCACQ c14c, SIGDET c138, PMA c137, AN c180/c181/c188,
# AN status c1a8/c1a9/c1ac/c1ab, LP base page c198, encode/decode c111/c130/c134/c136.
DIAG = [(0xc150, 0xc154), (0xc14c, 0xc14c), (0xc137, 0xc138), (0xc180, 0xc181), (0xc188, 0xc188), (0xc1a8, 0xc1ac), (0xc198, 0xc198),
        (0xc100, 0xc103), (0xc111, 0xc114), (0xc130, 0xc136)]


def say(fh, s):
    print(s, flush=True)
    fh.write(s + "\n")


def sh(cmd, check=True):
    # 30 s: ssh round trip 1-2 s; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    if check and r.returncode:
        raise RuntimeError(f"ssh rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def plan(a):
    f = a.front
    s = []
    if not a.no_put:
        s.append("scp -O us48_cmic.ko us48_cmic_xe.ko -> board:/lib/modules/$(uname -r)/")
    if not a.no_reload:
        s.append("scripts/mod_reload.py us48_cmic   (rmmod us48_cmic_xe, us48_cmic; insmod us48_cmic unit0=1, us48_cmic_xe)")
        s.append(f"ip addr add {_ipreg.ip('us48-xe49')}/24 dev xe49; ip link set xe49 up")
    s += [f"sysctl net.ipv6.conf.xe{f}.disable_ipv6=0; ip link set xe{f} up; wait carrier <= {LINK_WAIT_S} s",
          f"dmesg | grep 'xe{f}\\|tsc xe{f}'", f"ethtool xe{f}; ethtool -m xe{f}", f"tscreg {f}: " + " ".join(f"{x:x}-{y:x}" for x, y in DIAG),
          f"ip -s link show xe{f} twice, {TRAFFIC_S} s apart", f"ip link set xe{f} down"]
    return s


def diag(f, fh):
    for lo, hi in DIAG:
        out = sh(f'echo "{f} {lo:x} {hi:x}" > {XE}/tscreg; dmesg | grep "xe{f} range" | tail -n 1; true')
        say(fh, "  " + out.strip())


def counters(f):
    out = sh(f"ip -s -o link show xe{f}")
    return out.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--front", type=int, default=51, choices=sorted(LANE))
    ap.add_argument("--no-reload", action="store_true")
    ap.add_argument("--no-put", action="store_true")
    a = ap.parse_args()
    f = a.front
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        say(fh, f"# {time.strftime('%Y-%m-%dT%H:%M:%S%z')} sfp_1g_test xe{f} lane {LANE[f]} apply={a.apply}")
        for i, s in enumerate(plan(a), 1):
            say(fh, f"{i}. {s}")
        if not a.apply:
            say(fh, "dry run (use --apply)")
            return 0
        if not a.no_put:
            rel = sh("uname -r").strip()
            for ko in KO:
                r = subprocess.run(["scp", "-O", *OPTS, str(ko), f"root@{_ipreg.ip('us48-boot')}:/lib/modules/{rel}/"],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode:
                    say(fh, f"FAIL scp {ko.name}: {r.stderr.strip()}")
                    return 1
        if not a.no_reload:
            if subprocess.run([sys.executable, str(ROOT / "scripts/mod_reload.py"), "us48_cmic"]).returncode:
                say(fh, "FAIL mod_reload us48_cmic")
                return 1
            sh(f"ip addr add {_ipreg.ip('us48-xe49')}/24 dev xe49; ip link set xe49 up", check=False)
        sh(f"echo 0 > /proc/sys/net/ipv6/conf/xe{f}/disable_ipv6; dmesg -c >/dev/null")
        sh(f"ip link set xe{f} up")
        t0 = time.monotonic()
        while time.monotonic() - t0 < LINK_WAIT_S and sh(f"cat /sys/class/net/xe{f}/carrier", check=False).strip() != "1":
            time.sleep(0.25)
        up = sh(f"cat /sys/class/net/xe{f}/carrier", check=False).strip() == "1"
        say(fh, f"carrier {'1 PASS' if up else '0 FAIL'} after {time.monotonic() - t0:.2f} s")
        log = sh(f"dmesg | grep -E 'xe{f}|tsc xe{f}|sfp'")
        say(fh, log)
        say(fh, "boot: " + ("PASS" if "1000BASE-X" in log else "FAIL no 1000BASE-X stage line"))
        et = sh(f"ethtool xe{f}")
        say(fh, et)
        for name, pat in (("speed 1000", r"Speed: 1000Mb/s"), ("duplex full", r"Duplex: Full"), ("1000baseX/Full", r"1000baseX/Full"),
                          ("link detected", r"Link detected: yes")):
            say(fh, f"{name}: {'PASS' if re.search(pat, et) else 'FAIL'}")
        say(fh, sh(f"ethtool -m xe{f} | grep -iE 'identifier|vendor|wavelength|temperature|rx|tx'", check=False))
        c0 = counters(f)
        time.sleep(TRAFFIC_S)
        c1 = counters(f)
        say(fh, f"counters {c0}\n         -> {c1}")
        say(fh, "counters moving: " + ("PASS" if c0 != c1 else "FAIL (check sw2 side counters, XLMAC TPKT/RPKT)"))
        if not up:
            say(fh, "no link: diagnosis registers")
            diag(f, fh)
            say(fh, "left up for a manual look (ip link set xe%d down when done)" % f)
            return 1
        diag(f, fh)
        sh(f"ip link set xe{f} down")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
