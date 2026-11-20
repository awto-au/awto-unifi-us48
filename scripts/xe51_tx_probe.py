#!/usr/bin/env python3
"""xe51_tx_probe: try single TSC TX register changes on xe51 (unit 0 lane 0) and watch PCS_LIVE for sw1 locking our stream.

Usage: scripts/xe51_tx_probe.py [--apply] [--only 1,3a]
- Dry run unless --apply. Experiments from tmp/xe51-tx-analysis.md (#78): analog TX changes are live (no TX lane reset).
- Per experiment: xe51 down -> write -> xe51 up (laser on; sw1 recalibrates only when light appears) -> RX restart
  (so our RX locks sw1) -> read PCS_LIVE c154 twice. Pass = RF bit 0x80 clear and link bit 0x02 set (sw1 decodes us).
- Always: xe51 admin down and every register reverted at the end (also on error). ge1/xe49 untouched.
- Timing: RX restart 8 s after light (sw1 SerDes calibration finished within ~6 s of light in the 2026-11-20 runs, #78),
  readings 6 s and 12 s after the restart; slowest autoneg-class event 3.5 s, so 6 s = 1.7x.
Log: tmp/logs/xe51_tx_probe.log. Board address from _ipreg (US48_IP_US48_BOOT override).
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
LOG = ROOT / "tmp/logs/xe51_tx_probe.log"
XE = "/sys/module/us48_cmic_xe/parameters"
# id: (name, writes, reverts) as tscwr "<front> <aer> <reg> <mask> <val>"
EXPERIMENTS = {
    "0": ("control, no change", [], []),
    "1": ("TX polarity lane 0 (c011 bit 5)", ["51 0 c011 20 20"], ["51 0 c011 20 0"]),
    "3a": ("IDRIVER/IPREDRIVER/POST2 = 2/2/2 (OM SFI)", ["51 0 c017 7ff0 2220"], ["51 0 c017 7ff0 2020"]),
    "3b": ("IDRIVER 3", ["51 0 c017 0f00 0300"], ["51 0 c017 0f00 0000"]),
    "3c": ("IDRIVER 9 (stock 1G value)", ["51 0 c017 0f00 0900"], ["51 0 c017 0f00 0000"]),
    "4": ("TX FIR 0xcad0 (OM SFI preemph)", ["51 0 c252 ffff cad0"], ["51 0 c252 ffff 5a54"]),
}


def sh(cmd):
    # 30 s: ssh round trip is 1-2 s; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"ssh rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def pcs():
    """PCS_LIVE c154 of xe51 as int (via the read-only tscreg knob)."""
    out = sh(f'dmesg -c >/dev/null; echo "51 c154 c154" > {XE}/tscreg; dmesg | grep "xe51 range"')
    m = re.search(r"c154=([0-9a-f]{4})", out)
    return int(m.group(1), 16) if m else -1


def run_one(key, log):
    name, writes, reverts = EXPERIMENTS[key]
    sh("ip link set xe51 down")
    time.sleep(2)  # sw1 must see the light go away so its next attempt is a fresh calibration
    for w in writes:
        sh(f'echo "{w}" > {XE}/tscwr')
    sh("ip link set xe51 up")
    time.sleep(8)
    sh(f"echo 51 > {XE}/rx_restart")
    time.sleep(6)
    a = pcs()
    time.sleep(6)
    b = pcs()
    car = sh("cat /sys/class/net/xe51/carrier 2>&1").strip()
    sh("ip link set xe51 down")
    for w in reverts:
        sh(f'echo "{w}" > {XE}/tscwr')
    ok = b >= 0 and not (b & 0x80) and bool(b & 0x02)
    line = f"{time.strftime('%H:%M:%S')} exp {key} [{name}]: c154 {a:04x} -> {b:04x} carrier={car} {'PASS: RF clear + link' if ok else 'no lock'}"
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", help="comma list of experiment ids (default: all in order)")
    ap.add_argument("--start", default="", help="HH:MM:SS local: experiment k starts at start + k * --period (peer sampling)")
    ap.add_argument("--period", type=float, default=30.0, help="seconds between experiment starts (one takes ~26 s)")
    a = ap.parse_args()
    keys = [k for k in (a.only.split(",") if a.only else EXPERIMENTS) if k]
    for k in keys:
        print(f"{k}: {EXPERIMENTS[k][0]}  writes={EXPERIMENTS[k][1]} revert={EXPERIMENTS[k][2]}")
    if not a.apply:
        print("dry run (use --apply)")
        return 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    t0 = None
    if a.start:
        h, m, s = (int(x) for x in a.start.split(":"))
        now = time.localtime()
        t0 = time.mktime(now[:3] + (h, m, s) + now[6:])
    with LOG.open("a") as log:
        try:
            for i, k in enumerate(keys):
                if t0:
                    while time.time() < t0 + i * a.period:
                        time.sleep(0.2)
                    log.write(f"# exp {k} laser cycle starts {time.strftime('%H:%M:%S')}; laser on ~{time.strftime('%H:%M:%S', time.localtime(time.time() + 2))}\n")
                run_one(k, log)
        finally:
            sh("ip link set xe51 down")
            for k in keys:
                for w in EXPERIMENTS[k][2]:
                    sh(f'echo "{w}" > {XE}/tscwr')
            log.write("# restored: xe51 down, reverts applied\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
