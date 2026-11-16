#!/usr/bin/env python3
"""a2_oops_ab — alternate a2_boot_cycles.py configurations in blocks within one boot session (#103).

- Pass --mhz/--ko/--diag/--wait-cs and further boot_cycles args after `--`; the clock is always passed (default 1000 here, never the module default).
- Configs: ctl (defaults), late (dcc_late_us=2000), nopoll (dcc_poll=0), load (a1 dd to tmpfs); all use the ping marker.
- Block 15 boots, ROUNDS rounds; per-config oops/nomarker totals printed. Board must be up with the normal image.
Usage: a2_oops_ab.py [--rounds 3] [--configs ctl,late,nopoll,load] [--block 15] [-- extra a2_boot_cycles args]
Log: tmp/logs/a2_oops_ab.log (+ tmp/logs/a2_boot_cycles.log per block)
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/a2_oops_ab.log"
CFG = {
    "ctl": [],
    "late": ["dcc_late_us=2000"],
    "nopoll": ["dcc_poll=0"],
    "load": ["--load"],
    "late10": ["dcc_late_us=10000"],
    "lateload": ["--load", "dcc_late_us=2000"],
    "dscr": ["--dtb", "cap1000-l2.dtb", "dscr_only=1"],  # DSCR-only poll, a2 earlycon off (DTB bootargs "quiet")
    "slow": ["poll_us=100000", "busy_us=100000"],  # poll every 100 ms
    "late20": ["dcc_late_us=20000"],
    "m1000": ["--mhz", "1000"], "m1200": ["--mhz", "1200"], "m1400": ["--mhz", "1400"], "m1600": ["--mhz", "1600"],  # clock sweep (DTB us48-a2-<mhz>-l2.dtb)
    "quiet": ["--dtb", "cap1000-l2.dtb", "dcc_poll=0"],  # no poll, no earlycon
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--block", type=int, default=15)
    ap.add_argument("--configs", default="ctl,late,nopoll,load")
    ap.add_argument("--mhz", type=int, default=1000)
    ap.add_argument("extra", nargs="*")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    tot = {c: [0, 0, 0] for c in a.configs.split(",")}
    for rnd in range(a.rounds):
        for c in tot:
            cmd = [sys.executable, "scripts/a2_boot_cycles.py", f"ab-{c}-r{rnd}", "--cycles", str(a.block), "--ping", "--mhz", str(a.mhz), *CFG[c], *a.extra]
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            m = re.search(r"SUMMARY ok=(\d+) oops=(\d+) nomarker=(\d+)", r.stdout)
            if not m:
                msg = f"{c} round {rnd}: no SUMMARY {r.stdout[-300:]}{r.stderr[-300:]}"
            else:
                ok, oops, nom = map(int, m.groups())
                for i, v in enumerate((ok, oops, nom)):
                    tot[c][i] += v
                msg = f"{c} round {rnd}: ok {ok} oops {oops} nomarker {nom}"
            print(msg, flush=True)
            with LOG.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")
            if not m:
                return 1
    for c, (ok, oops, nom) in tot.items():
        print(f"TOTAL {c}: ok {ok} oops {oops} nomarker {nom}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
