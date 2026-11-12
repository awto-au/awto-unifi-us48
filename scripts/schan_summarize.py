#!/usr/bin/env python3
"""Summarise a tools/schan_trace.c log: writes only, first-seen order, last value, count.

Usage: python3 scripts/schan_summarize.py tmp/schan_stock_init.log [--unit 1] [--mem-names]
Header: op = h>>26, blk = h>>20 & 0x3f, bytes = h>>7 & 0x7f. Writes: 0x0d reg, 0x09 mem, 0x24 table insert, 0x26 delete.
Names via tmp/regdb/hr2_{regs,mems}.tsv (addr match on base). Logs to tmp/logs/schan_summarize.log.
"""
import argparse
import collections
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
OPS = {0x0d: "REGW", 0x09: "MEMW", 0x24: "INS", 0x26: "DEL"}


def load_names():
    mems = []
    for ln in (REPO / "tmp/regdb/hr2_mems.tsv").read_text().splitlines():
        f = ln.split("\t")
        try:
            mems.append((int(f[10], 16), int(f[7]), f[1]))
        except (ValueError, IndexError):
            pass
    regs = {}
    for ln in (REPO / "tmp/regdb/hr2_regs.tsv").read_text().splitlines():
        f = ln.split("\t")
        try:
            regs.setdefault(int(f[7], 16), f[1])
        except (ValueError, IndexError):
            pass
    return sorted(mems), regs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log")
    ap.add_argument("--unit", default="")
    a = ap.parse_args()
    mems, regs = load_names()
    seen = collections.OrderedDict()
    for ln in pathlib.Path(a.log).read_text().splitlines():
        p = ln.split()
        if len(p) < 5 or not p[2].startswith("u"):
            continue
        try:
            h = int(p[3], 16)
            addr = int(p[4], 16)
        except ValueError:
            continue
        op = h >> 26
        if op not in OPS or (a.unit and p[2][1:] != a.unit):
            continue
        key = (p[2], OPS[op], h >> 20 & 0x3f, addr)
        c = seen.get(key, [0, None, p[0]])
        c[0] += 1
        c[1] = " ".join(p[5:])
        seen[key] = c
    out = []
    for (u, op, blk, addr), (n, val, t0) in seen.items():
        name = "" if op != "REGW" else (regs.get(addr, "") or regs.get(addr & ~0xff, ""))
        if not name and op in ("MEMW", "INS", "DEL"):
            for base, mx, nm in reversed(mems):
                if base <= addr <= base + mx:
                    name = f"{nm}[{addr - base}]"
                    break
        out.append(f"{t0:>7} {u} {op:4} blk{blk:<2} {addr:#010x} x{n:<5} {name:28} {val}")
    print("\n".join(out))


if __name__ == "__main__":
    main()
