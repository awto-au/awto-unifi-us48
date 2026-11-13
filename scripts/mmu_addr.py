#!/usr/bin/env python3
"""S-channel address for per-port / per-COS HR2 registers (MMU block 0xc, IPIPE 0xa, any block in the RDB).
Usage: python3 scripts/mmu_addr.py REG [PORT] [COS]   |   --validate   |   --cpu-reads
Encoding (derived from tmp/schan_stock_init.log, #56): addr = rdb_offset + (cos << 8) + port; port = MMU/SBUS port number in the low byte,
array index (cos) in bits 15:8. Header: read 0x2c000200|blk<<20, write 0x34000200|blk<<20 (op 0x0b/0x0d).
Logs to tmp/logs/mmu_addr.log."""
import argparse, collections, pathlib
R = pathlib.Path(__file__).resolve().parent.parent
LOG = R / "tmp/logs/mmu_addr.log"
RD, WR = 0x2c000200, 0x34000200
regs = {}
for ln in (R / "tmp/regdb/hr2_regs.tsv").read_text().splitlines()[1:]:
    f = ln.split("\t")
    regs[f[1]] = dict(type=f[3], blk=int(f[6].split(",")[0], 0), off=int(f[7], 16), n=int(f[8]))

def addr(name, port=0, cos=0):
    r = regs[name]
    if cos >= r["n"]: raise SystemExit("%s has %d elements, cos=%d" % (name, r["n"], cos))
    return r["blk"], r["off"] + (cos << 8) + port

def out(msg, lines):
    lines.append(msg); print(msg)

def validate(lines):
    bymm = collections.defaultdict(list)  # (blk, addr & ~0xff) -> [(name, cos)]
    for nm, r in regs.items():
        if r["type"] in ("gen", "port"):
            for c in range(r["n"]):
                bymm[(r["blk"], (r["off"] & ~0xff) + (c << 8))].append((nm, c))
    seen = collections.OrderedDict(); hit = miss = 0
    for ln in (R / "tmp/schan_stock_init.log").read_text().splitlines():
        f = ln.split()
        if len(f) < 6 or f[2] != "u0": continue
        h = int(f[3], 16)
        if h >> 26 != 0x0d: continue
        blk = h >> 20 & 0x3f; a = int(f[4], 16)
        if blk != 0xc: continue
        cand = [(nm, c) for nm, c in bymm.get((blk, a & ~0xff), []) if regs[nm]["off"] & 0xff == 0]
        if cand: hit += 1; seen.setdefault((cand[0][0], cand[0][1], a & 0xff), (ln, cand))
        else: miss += 1
    out("stock u0 MMU writes: %d decoded, %d undecoded" % (hit, miss), lines)
    want = [("HOLCOSPKTSETLIMIT", 0, 0), ("HOLCOSPKTSETLIMIT", 2, 3), ("HOLCOSCELLMAXLIMIT", 0, 1), ("DYNCELLLIMIT", 0, 0), ("DYNCELLLIMIT", 2, 0), ("HOLCOSMINXQCNT", 2, 7)]
    for nm, p, c in want:
        b, a = addr(nm, p, c)
        ln = seen.get((nm, c, p), (None,))[0]
        out("%-20s port=%d cos=%d -> blk 0x%x addr 0x%08x  write hdr 0x%08x  stock: %s" % (nm, p, c, b, a, WR | b << 20, ln or "NOT IN TRACE"), lines)
        if ln and int(ln.split()[4], 16) != a: out("  MISMATCH", lines)

def cpu_reads(lines):
    out("# CPU port = MMU port 0 (stock HOLCOSPKTSETLIMIT[0] cos0-5 final 0x67, cos6-7 0x40; ports 2.. = 0x566). read hdr = 0x2c000200|blk<<20", lines)
    for nm in ("EGRDROPPKTCOUNT_COS", "HOLCOSPKTSETLIMIT", "HOLCOSCELLMAXLIMIT", "HOLCOSMINXQCNT"):
        for c in range(8):
            b, a = addr(nm, 0, c); out("%-22s cos%d blk 0x%x addr 0x%08x hdr 0x%08x" % (nm, c, b, a, RD | b << 20), lines)
    for nm in ("DYNCELLLIMIT", "CFAPCONFIG", "IP_TO_CMICM_CREDIT_TRANSFER"):
        b, a = addr(nm, 0, 0); out("%-22s blk 0x%x addr 0x%08x hdr 0x%08x" % (nm, b, a, RD | b << 20), lines)
    for i in range(9):
        for nm in ("RDBGC%d_SELECT" % i, "RDBGC%d" % i):
            b, a = addr(nm, 0, 0); out("%-22s blk 0x%x addr 0x%08x hdr 0x%08x" % (nm, b, a, RD | b << 20), lines)

ap = argparse.ArgumentParser()
ap.add_argument("reg", nargs="?"); ap.add_argument("port", nargs="?", type=lambda x: int(x, 0), default=0)
ap.add_argument("cos", nargs="?", type=lambda x: int(x, 0), default=0)
ap.add_argument("--validate", action="store_true"); ap.add_argument("--cpu-reads", action="store_true")
a = ap.parse_args()
lines = []
if a.validate: validate(lines)
elif a.cpu_reads: cpu_reads(lines)
elif a.reg:
    b, ad = addr(a.reg.upper(), a.port, a.cos)
    out("%s port=%d cos=%d -> blk 0x%x addr 0x%08x  read hdr 0x%08x  write hdr 0x%08x" % (a.reg.upper(), a.port, a.cos, b, ad, RD | b << 20, WR | b << 20), lines)
else: ap.error("need REG, --validate or --cpu-reads")
LOG.parent.mkdir(parents=True, exist_ok=True)
LOG.write_text("\n".join(lines) + "\n")
