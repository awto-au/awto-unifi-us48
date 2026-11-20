#!/usr/bin/env python3
"""cage_watch: log when SFP 1/2 (xe51/xe52) start/stop receiving light, to map which far-end port feeds which cage (#78).

Usage: scripts/cage_watch.py [--secs 60] [--apply]
- Dry run unless --apply. Brings xe51/xe52 up (our lasers on), polls c150 (PMD_LIVE) + c154 (PCS_LIVE) per cage, prints only changes
  with a wall-clock time; correlate with the far end switching its ports one at a time. Ports back to admin down at the end.
- Light seen = c150 == 0x000f (PMD lock) vs 0x0004 (nothing). PCS sync = c154 b0.
- Poll ~1 s per round (one ssh, both cages): the far end must hold each port >= 4 s.
Log: tmp/logs/cage_watch.log. Board address from _ipreg (US48_IP_US48_BOOT override).
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
LOG = ROOT / "tmp/logs/cage_watch.log"
XE = "/sys/module/us48_cmic_xe/parameters"
FRONTS = (51, 52)


def sh(cmd):
    # 30 s: ssh round trip is 1-2 s; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    return r.stdout


def poll():
    cmd = "dmesg -c >/dev/null; " + " ".join(f'echo "{f} c150 c154" > {XE}/tscreg;' for f in FRONTS) + " dmesg | grep range"
    out = sh(cmd)
    res = {}
    for f in FRONTS:
        m = re.search(rf"xe{f} range: c150=([0-9a-f]{{4}}).*c154=([0-9a-f]{{4}})", out)
        res[f] = (int(m.group(1), 16), int(m.group(2), 16)) if m else (-1, -1)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--secs", type=float, default=60)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not a.apply:
        print(f"dry run: would watch xe51/xe52 for {a.secs:.0f} s (use --apply)")
        return 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    flags = {f: int(sh(f"cat /sys/class/net/xe{f}/flags").strip() or "0", 16) & 1 for f in FRONTS}
    with LOG.open("a") as fh:
        def say(s):
            line = f"{time.strftime('%H:%M:%S')} {s}"
            print(line, flush=True)
            fh.write(line + "\n")
            fh.flush()
        try:
            for f in FRONTS:
                sh(f"ip link set xe{f} up")
            say(f"watching xe51/xe52 for {a.secs:.0f} s")
            last = {}
            t0 = time.monotonic()
            while time.monotonic() - t0 < a.secs:
                cur = poll()
                for f, (pmd, pcs) in cur.items():
                    if last.get(f) != (pmd, pcs):
                        say(f"xe{f}: c150={pmd:04x} c154={pcs:04x} light={'YES' if pmd == 0x000f else 'no'} sync={pcs & 1}")
                last = cur
        finally:
            for f in FRONTS:
                if not flags[f]:
                    sh(f"ip link set xe{f} down")
            say("done, admin state restored")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
