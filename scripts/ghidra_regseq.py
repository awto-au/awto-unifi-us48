#!/usr/bin/env python3
"""Summarise the register sequence of one stock switchdrvr function from the Ghidra C export.

Maps soc_reg_t / soc_field_t constants (soc_reg_addr, soc_reg32_get/set, soc_reg_field_set,
raw writes) to names via regdb/hr2_regs.tsv and the stock field-name table.
Usage: python3 scripts/ghidra_regseq.py 0x100827c [--elf BIN] [--c tmp/ghidra/out/switchdrvr/switchdrvr.c]
Logs to tmp/logs/ghidra_regseq.log.
"""
import argparse
import logging
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from smbus_regdb import FNAMES, Elf  # noqa: E402

LOG = pathlib.Path("tmp/logs/ghidra_regseq.log")
# stock function VA -> (label, register arg index, field arg index or None)
FUNCS = {
    "FUN_0126cfbc": ("addr", 1, None),
    "FUN_01270ac0": ("get32", 1, None),
    "FUN_01271674": ("set32", 1, None),
    "FUN_0126b310": ("field", 1, 3),
    "FUN_0126f548": ("get", 1, None),
    "FUN_01270d60": ("set", 1, None),
    "FUN_012688cc": ("rd32raw", None, None),
}
CALL = re.compile(r"(FUN_[0-9a-f]+)\((.*)\);")
WRITE = re.compile(r"\(\*\w+\)\s*\(?\s*\(?&?DAT_0268143c \+ param_1 \* 0x1e,\s*\w+,\s*(0x[0-9a-f]+|\d+)\)")


def num(s):
    s = s.strip()
    m = re.fullmatch(r"&?DAT_0*([0-9a-f]+)", s)
    if m:
        return int(m.group(1), 16)
    try:
        return int(s, 0)
    except ValueError:
        return None


def split_args(s):
    out, d, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        if ch == "," and d == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("va", type=lambda x: int(x, 0))
    ap.add_argument("--elf", default="private/firmware/rootfs-selected/bin/switchdrvr")
    ap.add_argument("--c", default="tmp/ghidra/out/switchdrvr/switchdrvr.c")
    ap.add_argument("--regs", default="regdb/hr2_regs.tsv")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    regs = {}
    for ln in open(a.regs).read().splitlines()[1:]:
        c = ln.split("\t")
        regs[int(c[0])] = c[1]
    e = Elf(a.elf)

    def fname(i):
        return e.cstr(e.u32(FNAMES + 4 * i)) if i < 0x30000 else None

    hdr = "// ==== FUN_%08x @ %08x ====" % (a.va, a.va)
    body, on = [], False
    for ln in open(a.c, errors="replace"):
        if ln.startswith("// ==== "):
            if on:
                break
            on = ln.startswith(hdr)
            continue
        if on:
            body.append(ln.rstrip("\n"))
    if not body:
        sys.exit("function not found in export")
    last = None
    for ln in body:
        m = WRITE.search(ln)
        if m and last:
            logging.info("  WRITE   %s <- %s", last, m.group(1))
            continue
        m = CALL.search(ln)
        if not m or m.group(1) not in FUNCS:
            continue
        lab, ri, fi = FUNCS[m.group(1)]
        args = split_args(m.group(2))
        if ri is None or ri >= len(args):
            continue
        r = num(args[ri])
        rn = regs.get(r, "reg#%s" % args[ri].strip()) if r is not None else args[ri].strip()
        if lab == "field":
            f = num(args[fi]) if fi < len(args) else None
            fn = fname(f) if f is not None else "?"
            logging.info("  field   %s.%s = %s", rn, fn, args[fi + 1].strip() if fi + 1 < len(args) else "?")
        else:
            logging.info("%-8s %s", lab, rn)
            if lab == "addr":
                last = rn


main()
