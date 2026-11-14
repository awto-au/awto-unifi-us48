#!/usr/bin/env python3
"""Summarise the unit-0 S-channel writes of a bde_trace log and diff them against the replay input.

Usage: python3 scripts/bde_diff.py [tmp/bde_stock_init.log] [--old tmp/schan_stock_init.log]
- New trace line: <ms> <tid> SCH u<unit> <hdr> <addr> <data...>; old (gen_bcm_hr2_init.py input): <seq> <tid> u0 <hdr> <addr> <data...>.
- Header: op = h>>26, blk = h>>20 & 0x3f. Writes: 0x0d reg, 0x09 mem, 0x24 table insert, 0x26 delete.
- Output: per-op/per-block write counts (u0), keys (op, blk, addr) and (op, blk, addr, data) new vs old, memory writes by block/name.
Log: tmp/logs/bde_diff.log
"""
import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import schan_summarize as ss  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
LOG = REPO / "tmp/logs/bde_diff.log"
WRITES = {0x0d: "REGW", 0x09: "MEMW", 0x24: "INS", 0x26: "DEL"}


def load(path, new):
    out = []
    for ln in pathlib.Path(path).read_text().splitlines():
        p = ln.split()
        if new:
            if len(p) < 6 or p[2] != "SCH" or p[3] != "u0":
                continue
            p = [p[0], p[1], "u0"] + p[4:]
        if len(p) < 5 or p[2] != "u0":
            continue
        h, addr = int(p[3], 16), int(p[4], 16)
        op = h >> 26
        if op in WRITES:
            out.append((int(p[0]), op, h >> 20 & 0x3f, addr, tuple(p[5:])))
    return out


def replay_keys():
    """(op, blk, addr) of every entry in the generated replay header (cat 0xff = delay, 1 word)."""
    t = re.sub(r"/\*.*?\*/", "", (REPO / "common/bcm-cmic/bcm_cmic_hr2_init_data.h").read_text(), flags=re.S)
    w = [int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]+", t)]
    keys, i = set(), 0
    while i < len(w):
        if w[i] >> 24 == 0xFF:
            i += 1
            continue
        keys.add((w[i] >> 16 & 0xFF, w[i] >> 8 & 0xFF, w[i + 1]))
        i += 2 + (w[i] & 0xFF)
    return keys


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log", nargs="?", default=str(REPO / "tmp/bde_stock_init.log"))
    ap.add_argument("--old", default=str(REPO / "tmp/schan_stock_init.log"))
    a = ap.parse_args()
    new, old = load(a.log, True), load(a.old, False)
    mems, regs = ss.load_names()
    out = [f"## u0 writes: new={len(new)} old={len(old)}"]
    for tag, rows in (("new", new), ("old", old)):
        c = collections.Counter((WRITES[op], blk) for _s, op, blk, _a, _d in rows)
        out.append(f"## {tag}: (op, blk) -> count")
        out += [f"  {k[0]:5} blk{k[1]:<2} {v}" for k, v in sorted(c.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    ko = {(op, blk, addr) for _s, op, blk, addr, _d in old}
    kn = {(op, blk, addr) for _s, op, blk, addr, _d in new}
    vo = {(op, blk, addr, d) for _s, op, blk, addr, d in old}
    only_new = sorted(kn - ko)
    only_old = sorted(ko - kn)
    out.append(f"## unique (op,blk,addr): new={len(kn)} old={len(ko)} only-new={len(only_new)} only-old={len(only_old)}")
    out.append("## only in new trace (op blk addr name)")
    for op, blk, addr in only_new:
        nm = ss.reg_name(regs, addr, blk) if op == 0x0d else ""
        out.append(f"  {WRITES[op]:5} blk{blk:<2} {addr:#010x} {nm}")
    nv = [(s, op, blk, addr, d) for s, op, blk, addr, d in new
          if (op, blk, addr) in ko and (op, blk, addr, d) not in vo]
    out.append(f"## same (op,blk,addr), data value never seen in old trace: {len(nv)} writes")
    seen = collections.OrderedDict()
    for s, op, blk, addr, d in nv:
        seen.setdefault((op, blk, addr), (s, d))
    for (op, blk, addr), (s, d) in list(seen.items())[:300]:
        out.append(f"  {s:>7} {WRITES[op]:5} blk{blk:<2} {addr:#010x} {' '.join(d)[:60]}")
    rk = replay_keys()
    dropped = collections.defaultdict(list)
    for op, blk, addr in sorted(kn - rk):
        dropped[(op, blk)].append(addr)
    out.append(f"## in the trace but NOT in the replay header: {sum(len(v) for v in dropped.values())} unique (op,blk,addr)")
    for (op, blk), addrs in sorted(dropped.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        out.append(f"  {WRITES[op]:5} blk{blk:<2} {len(addrs):>6} addrs; first {addrs[0]:#010x} last {addrs[-1]:#010x}")
    names = collections.Counter()
    for (op, blk), addrs in dropped.items():
        for addr in addrs:
            nm = ss.reg_name(regs, addr, blk) if op == 0x0d else ""
            if op == 0x09:
                nm = next((m[2] for m in reversed(mems) if m[0] <= addr and blk in m[3]), "")
            names[(WRITES[op], blk, nm.split("[")[0])] += 1
    out.append("## dropped, by (op, blk, name) all")
    out += [f"  {k[0]:5} blk{k[1]:<2} {k[2]:40} {v}" for k, v in sorted(names.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2]))[:4000]]
    text = "\n".join(out)
    print(text)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(text + "\n")


if __name__ == "__main__":
    main()
