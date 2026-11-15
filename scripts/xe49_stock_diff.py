#!/usr/bin/env python3
"""xe49_stock_diff: stock unit-1 S-channel writes for the xe49 path that the minimal cold init skips (#78).

Usage: python3 scripts/xe49_stock_diff.py [--blk 5,6,...] [--lane 0]
- Stock = tmp/bde_stock_init.log (unit 1 S-channel writes, all phases). Ours = tmp/u1_min_init.txt (init_phases.py --emit-min).
- Selects ops of the chosen blocks whose low address byte is the lane (or whose registers are not lane-indexed).
- Prints stock-only ops in stock order (ord, ms, phase, name, addr, data). Log: tmp/logs/xe49_stock_diff.log
- MIIM/TSC writes are not in the trace (CMC0 MIIM page untraced); see docs/init-phases-u1.md.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hwdb  # noqa: E402
import init_phases as ip  # noqa: E402

ROOT = ip.ROOT
LOG = ROOT / "tmp/logs/xe49_stock_diff.log"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--blk", default="5", help="comma list of SBUS block numbers")
    ap.add_argument("--lane", type=int, default=0)
    a = ap.parse_args()
    blks = {int(x) for x in a.blk.split(",")}
    names = ip.Names(hwdb.load())
    ops, _ = ip.load(1, names)
    phases = ip.resolve_phases(ops)

    def phase_of(i):
        return next((p[0] for p in phases if p[2] <= i < p[3]), "?")

    ours = set()
    for ln in (ROOT / "tmp/u1_min_init.txt").read_text().splitlines():
        p = ln.split()
        ours.add((int(p[0])))
    out = []
    for i, o in enumerate(ops):
        if o.blk not in blks:
            continue
        if o.o not in ours and (o.addr & 0xFF) == a.lane:
            out.append(f"{o.o:6d} {o.ms:7d} {phase_of(i):3s} {o.kind} blk{o.blk} {o.addr:#010x} {o.name:40s} " + " ".join(f"{x:08x}" for x in o.data))
    text = "\n".join(out) + "\n"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
