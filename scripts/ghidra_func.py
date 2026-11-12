#!/usr/bin/env python3
"""Print decompiled functions (and optional callers/callees) from a Ghidra C export by VA.

Usage: python3 scripts/ghidra_func.py 0x60620c [0x612fd8 ...] [--calls] [--callers] [--bin eswh_switchdrvr]
Reads tmp/ghidra/out/<bin>/<bin>.c (+ .calls.tsv). Logs to tmp/logs/ghidra_func.log.
"""
import argparse
import logging
import pathlib
import re
import sys

LOG = pathlib.Path("tmp/logs/ghidra_func.log")
OUT = pathlib.Path("tmp/ghidra/out")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("addrs", nargs="+", type=lambda x: int(x, 0))
    ap.add_argument("--bin", default="switchdrvr", help="export dir under tmp/ghidra/out")
    ap.add_argument("--name", help="file stem (default: --bin; eswh_switchdrvr uses switchdrvr)")
    ap.add_argument("--calls", action="store_true", help="list callees")
    ap.add_argument("--callers", action="store_true", help="list callers")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    base = OUT / a.bin
    stem = a.name or ("switchdrvr" if a.bin == "eswh_switchdrvr" else a.bin)
    ctsv = base / f"{stem}.calls.tsv"
    want = {f"{x:08x}": x for x in a.addrs}
    text = (base / f"{stem}.c").read_text(errors="replace").split("\n")
    marks = [(i, m.group(1)) for i, l in enumerate(text) if (m := re.match(r"// ==== \S+ @ ([0-9a-f]{8}) ====", l))]
    for k, (i, addr) in enumerate(marks):
        if addr in want:
            end = marks[k + 1][0] if k + 1 < len(marks) else len(text)
            logging.info("// line %d", i + 1)
            logging.info("\n".join(text[i:end]).rstrip())
    if a.calls or a.callers:
        for ln in ctsv.read_text().splitlines():
            p = ln.split("\t")
            if len(p) >= 4:
                if a.calls and p[0] in want:
                    logging.info("callee %s -> %s %s", p[0], p[2], p[3])
                if a.callers and p[2] in want:
                    logging.info("caller %s %s -> %s", p[0], p[1], p[2])


if __name__ == "__main__":
    main()
