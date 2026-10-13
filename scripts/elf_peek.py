#!/usr/bin/env python3
"""Read bytes/words/C-strings at virtual addresses of a 32-bit LE ELF (no deps).

Usage:
  python3 scripts/elf_peek.py ELF 0xc0266168 [0xaddr ...] [--words N] [--follow]
  --words N : dump N 32-bit words per address (default 4)
  --follow  : for each word that is a mapped pointer to a printable string, show the string
Logs to tmp/logs/elf_peek.log.
"""
import argparse
import logging
import pathlib
import struct
import sys

LOG = pathlib.Path("tmp/logs/elf_peek.log")


def load(path):
    d = pathlib.Path(path).read_bytes()
    phoff, = struct.unpack_from("<I", d, 0x1C)
    phentsize, phnum = struct.unpack_from("<HH", d, 0x2A)
    segs = []
    for i in range(phnum):
        t, off, va, pa, fsz, msz, fl, al = struct.unpack_from("<8I", d, phoff + i * phentsize)
        if t == 1:
            segs.append((va, off, fsz))
    return d, segs


def read(d, segs, va, n):
    for sva, off, fsz in segs:
        if sva <= va < sva + fsz:
            o = off + va - sva
            return d[o:o + n]
    return None


def cstr(d, segs, va):
    b = read(d, segs, va, 160)
    if not b:
        return None
    s = b.split(b"\0")[0]
    if len(s) >= 3 and all(32 <= c < 127 or c in (9, 10) for c in s):
        return s.decode()
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("addr", nargs="+", type=lambda x: int(x, 0))
    ap.add_argument("--words", type=int, default=4)
    ap.add_argument("--follow", action="store_true")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    d, segs = load(a.elf)
    for va in a.addr:
        b = read(d, segs, va, a.words * 4)
        if b is None:
            logging.info("%08x unmapped", va)
            continue
        for i in range(len(b) // 4):
            w, = struct.unpack_from("<I", b, i * 4)
            s = cstr(d, segs, w) if a.follow else None
            logging.info("%08x: %08x%s", va + i * 4, w, f"  -> {s!r}" if s else "")


main()
