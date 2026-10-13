#!/usr/bin/env python3
"""Disassemble ARM32 ranges of a 32-bit LE ELF by virtual address (capstone).

Usage:
  python3 scripts/arm_dis.py ELF 0x5e9764 [--len 0x100] [--thumb]
Literal-pool words (ldr rX,[pc,#n]) are annotated with their value / string.
Logs to tmp/logs/arm_dis.log.
"""
import argparse
import logging
import pathlib
import re
import struct
import sys

import capstone

LOG = pathlib.Path("tmp/logs/arm_dis.log")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("addr", type=lambda x: int(x, 0))
    ap.add_argument("--len", type=lambda x: int(x, 0), default=0x100)
    ap.add_argument("--thumb", action="store_true")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    d = pathlib.Path(a.elf).read_bytes()
    phoff, = struct.unpack_from("<I", d, 0x1C)
    phentsize, phnum = struct.unpack_from("<HH", d, 0x2A)
    segs = []
    for i in range(phnum):
        t, off, va, pa, fsz, msz, fl, al = struct.unpack_from("<8I", d, phoff + i * phentsize)
        if t == 1:
            segs.append((va, off, fsz))

    def rd(va, n):
        for sva, off, fsz in segs:
            if sva <= va < sva + fsz:
                o = off + va - sva
                return d[o:o + n]
        return b""

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB if a.thumb else capstone.CS_MODE_ARM)
    md.skipdata = True
    for ins in md.disasm(rd(a.addr, a.len), a.addr):
        note = ""
        m = re.match(r"(r\d+|lr|ip|sp), \[pc, #(-?0x[0-9a-f]+|-?\d+)\]", ins.op_str)
        if m:
            off = int(m.group(2), 0)
            w = rd(ins.address + 8 + off, 4)
            if len(w) == 4:
                v, = struct.unpack("<I", w)
                s = rd(v, 48).split(b"\0")[0]
                note = f"  ; =0x{v:x}" + (f" '{s.decode()}'" if len(s) > 3 and all(32 <= c < 127 for c in s) else "")
        logging.info("%08x: %-8s %s%s", ins.address, ins.mnemonic, ins.op_str, note)


if __name__ == "__main__":
    main()
