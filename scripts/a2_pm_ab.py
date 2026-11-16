#!/usr/bin/env python3
"""a2_pm_ab — alternate a2_pm.py --count-only configurations in blocks within one boot session (#103 mitigation counts).

- Configs (module params of us48_a2, each a named regdb field): fix (module defaults: EP_TX_WR_CMD_BUF_DEPTH_1), ctl (both depth bits off), txrd (EP_TX_RD_CMD_BUF_DEPTH_1 only), txboth. (AXIIC_PCIE0_S0_FN_MOD_BM_ISS 0x1a004008 through the unit 1 regmap: start fails with -EIO, dropped.)
- Oops = DCC marker "Unable to handle kernel" (dcc_poll on). Debug a1 image + WORK-debug ko staged by a first a2_pm.py run.
Usage: a2_pm_ab.py [--rounds 2] [--block 50] [--configs ctl,fix] [--mhz 1000]    Log: tmp/logs/a2_pm_ab.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/a2_pm_ab.log"
CFG = {"fix": [], "ctl": ["paxb_wr_depth1=0"], "txrd": ["paxb_wr_depth1=0", "paxb_rd_depth1=1"], "txboth": ["paxb_rd_depth1=1"]}
SYM = ["cap_l2x0=0xc06b126c", "cap_memmap=0xc06d4988"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--block", type=int, default=50)
    ap.add_argument("--configs", default="ctl,fix")
    ap.add_argument("--mhz", type=int, default=1000)
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    tot = {c: [0, 0] for c in a.configs.split(",")}
    for rnd in range(a.rounds):
        for c in tot:
            cmd = [sys.executable, "scripts/a2_pm.py", "--count-only", "--no-stage", "--boots", str(a.block), "--block", str(a.block), "--mhz", str(a.mhz),
                   "--label", f"ab-{c}-r{rnd}", *CFG[c], *SYM]
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            m = re.search(r"SUMMARY boots=(\d+) oops=(\d+) ok=(\d+) other=(\d+)", r.stdout)
            if not m:
                msg = f"{c} round {rnd}: no SUMMARY {r.stdout[-300:]}{r.stderr[-300:]}"
            else:
                b, o, ok, ot = map(int, m.groups())
                tot[c][0] += b
                tot[c][1] += o
                msg = f"{c} round {rnd}: boots {b} oops {o} ok {ok} other {ot}"
            print(msg, flush=True)
            with LOG.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")
            if not m:
                return 1
    for c, (b, o) in tot.items():
        print(f"TOTAL {c}: boots {b} oops {o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
