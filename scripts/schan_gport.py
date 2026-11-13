#!/usr/bin/env python3
"""List stock S-channel writes to GPORT blocks (2,3,4) with names, per unit, ordered.
Usage: python3 scripts/schan_gport.py tmp/schan_stock_init.log --unit 1 [--blk 2,3,4] [--ops 0d,09]
Matches per-port regs by stripping low address bits (port index). Logs to tmp/logs/schan_gport.log."""
import argparse, pathlib, sys
R = pathlib.Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser(); ap.add_argument("log"); ap.add_argument("--unit", default="1")
ap.add_argument("--blk", default="2,3,4"); ap.add_argument("--ops", default="0d,09")
a = ap.parse_args()
blks = {int(x) for x in a.blk.split(",")}; ops = {int(x, 16) for x in a.ops.split(",")}
regs = {}
for ln in (R/"tmp/regdb/hr2_regs.tsv").read_text().splitlines()[1:]:
    f = ln.split("\t")
    try:
        for b in f[6].split(","): regs.setdefault((int(b, 0), int(f[7], 16)), (f[1], f[16] if len(f) > 16 else ""))
    except ValueError: pass
mems = []
for ln in (R/"tmp/regdb/hr2_mems.tsv").read_text().splitlines():
    f = ln.split("\t")
    try: mems.append((int(f[10], 16), int(f[7]), f[1]))
    except (ValueError, IndexError): pass
def name(addr, op, blk):
    if op == 9:
        for b, n, nm in mems:
            if b <= addr < b + n: return nm + "[%d]" % (addr - b)
        return "?mem"
    for m in (0, 0xf, 0x3f, 0xff, 0x7ff):
        if (blk, addr & ~m) in regs: return regs[(blk, addr & ~m)][0] + ("" if not m else "|p%d" % (addr & m))
    return "?"
out = []
for n, ln in enumerate(open(R/a.log), 1):
    f = ln.split()
    if len(f) < 5 or f[2] != "u" + a.unit: continue
    h = int(f[3], 16); op = h >> 26; blk = h >> 20 & 0x3f
    if op in ops and blk in blks:
        ad = int(f[4], 16)
        out.append("%d\t%s\tblk%d\top%02x\t%08x\t%s\t%s" % (n, f[0], blk, op, ad, name(ad, op, blk), " ".join(f[5:])))
txt = "\n".join(out)
(R/"tmp/logs").mkdir(exist_ok=True)
(R/"tmp/logs/schan_gport.log").write_text(txt + "\n")
print(txt)
