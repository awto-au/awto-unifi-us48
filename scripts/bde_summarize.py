#!/usr/bin/env python3
"""Summarise a tools/bde_trace.c log (BDE reg/iproc/spi + S-channel + DCB), names from tmp/regdb/hr2_regs.tsv.

Usage: python3 scripts/bde_summarize.py tmp/bde_stock_init.log [--pattern REGEX] [--first N]
Line: <ms> <tid> <op> u<unit> <addr> <val>. Prints: op totals, per-block counts, first-touch order of
BDE writes, S-channel register writes matching --pattern (default TSC/XLPORT/XLMAC/SerDes/PLL, chronological,
deduped on name+addr+value), MSPI/QSPI/BSPI + SPI traffic (PoE), DCB descriptor writes.
Logs to tmp/logs/bde_summarize.log.
"""
import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import schan_summarize as ss  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
LOG = REPO / "tmp/logs/bde_summarize.log"
DEFAULT_PAT = r"XLPORT|XLMAC|XMAC|TSC|SERDES|WC_|PLL|PMD|MIIM"


def load_cpu_names():
    """offset -> name for cpureg/iprocreg rows (BDE addresses are CMIC/iproc offsets, no block)."""
    names = {}
    for ln in (REPO / "tmp/regdb/hr2_regs.tsv").read_text().splitlines():
        f = ln.split("\t")
        if len(f) > 7 and f[3] in ("cpureg", "iprocreg"):
            try:
                names.setdefault(int(f[7], 16), f[1])
            except ValueError:
                pass
    return names


def cpu_name(names, addr):
    for key in (addr, addr & ~3):
        if key in names:
            return names[key]
    return ""


def group(name, addr):
    if name:
        return "_".join(name.split("_")[:2])
    return f"{addr >> 12:#x}xxx"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log")
    ap.add_argument("--pattern", default=DEFAULT_PAT)
    ap.add_argument("--first", type=int, default=150, help="rows in the first-touch list")
    a = ap.parse_args()
    pat = re.compile(a.pattern)
    mems, regs = ss.load_names()
    cpu = load_cpu_names()
    out = []
    ops = collections.Counter()
    blocks = collections.defaultdict(lambda: collections.Counter())
    first = collections.OrderedDict()
    sch = collections.OrderedDict()
    spi, mspi, dcb = [], collections.OrderedDict(), []
    for ln in pathlib.Path(a.log).read_text().splitlines():
        p = ln.split()
        if len(p) < 4:
            continue
        ms, op = p[0], p[2]
        ops[op] += 1
        if op in ("R", "W", "IR", "IW") and len(p) >= 6:
            try:
                addr = int(p[4], 16)
            except ValueError:
                continue
            nm = cpu_name(cpu, addr)
            blocks[group(nm, addr)][op] += 1
            if op in ("W", "IW"):
                c = first.get((op, addr), [0, ms, nm, None])
                c[0] += 1
                c[3] = p[5]
                first[(op, addr)] = c
            if "SPI" in nm or "QSPI" in nm:
                mspi[(op, addr)] = (ms, nm, p[5])
        elif op == "SCH" and len(p) >= 5:
            try:
                h, addr = int(p[3], 16), int(p[4], 16)
            except ValueError:
                continue
            o = h >> 26
            if o not in ss.OPS:
                continue
            blk = h >> 20 & 0x3f
            nm = ss.reg_name(regs, addr, blk) if ss.OPS[o] == "REGW" else ""
            if nm and pat.search(nm):
                key = (nm, addr, " ".join(p[5:]))
                sch.setdefault(key, (ms, p[1], p[2], blk))
        elif op in ("SPR", "SPW"):
            spi.append(ln)
        elif op == "DCB":
            dcb.append(ln)
    out.append("## op totals")
    out += [f"  {k:4} {v}" for k, v in sorted(ops.items())]
    out.append("## per block (reads/writes, BDE + iproc)")
    for g, c in sorted(blocks.items(), key=lambda kv: -sum(kv[1].values())):
        out.append(f"  {g:34} R={c['R'] + c['IR']:<7} W={c['W'] + c['IW']}")
    out.append(f"## first-touch order of writes (first {a.first})")
    for (op, addr), (n, ms, nm, val) in list(first.items())[:a.first]:
        out.append(f"  {ms:>7} {op:2} {addr:#010x} x{n:<5} {nm:34} last={val}")
    out.append(f"## S-channel register writes matching /{a.pattern}/ ({len(sch)} unique)")
    for (nm, addr, val), (ms, tid, u, blk) in sch.items():
        out.append(f"  {ms:>7} {tid} {u} blk{blk:<2} {addr:#010x} {nm:30} {val}")
    out.append(f"## MSPI/QSPI BDE accesses ({len(mspi)} unique) and SPI ({len(spi)} lines)")
    for (op, addr), (ms, nm, val) in mspi.items():
        out.append(f"  {ms:>7} {op:2} {addr:#010x} {nm:30} {val}")
    out += ["  " + x for x in spi[:200]]
    out.append(f"## DCB descriptor writes ({len(dcb)})")
    out += ["  " + x for x in dcb[:100]]
    text = "\n".join(out)
    print(text)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(text + "\n")


if __name__ == "__main__":
    main()
